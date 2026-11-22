"""One bounded complete response; internal streams never expose partial tools."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
import json
import math
from email.utils import parsedate_to_datetime
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from openai.types.chat.chat_completion import ChatCompletion, Choice


DEFAULT_MODEL_IDLE_SECONDS = 300.0
MAX_MODEL_DEADLINE_SECONDS = 86_400.0
SERVICE_HTTP_STATUS = {'http_502': 502, 'http_503': 503}


# The published DeepSeek V4/V4.1 text templates insert a fixed reasoning/tools
# preamble, role separators and DSML wrappers per call and named argument.
# Bound those bytes separately from all request-controlled text. Non-text
# content (in particular image URLs) has no bound based on its JSON length.
FLASH_TEXT_PREAMBLE_BYTES = 8192


def flash_text_input_bound(messages, tools):
    """Conservative input ceiling for the plain function-chat format we send."""
    if not isinstance(messages, list) or not messages or not isinstance(tools, list):
        return None
    if any(not isinstance(tool, dict) or set(tool) != {'type', 'function'}
           or tool['type'] != 'function' or not isinstance(tool['function'], dict) for tool in tools):
        return None
    framing = FLASH_TEXT_PREAMBLE_BYTES + 256 * len(messages)
    try:
        for msg in messages:
            if (not isinstance(msg, dict) or msg.get('role') not in ('system', 'user', 'assistant', 'tool')
                    or set(msg) - {'role', 'content', 'tool_calls', 'tool_call_id', 'name', 'reasoning_content'}
                    or any(not isinstance(v, str) for k, v in msg.items() if k != 'tool_calls' and v is not None)):
                return None
            for call in msg.get('tool_calls', []):
                if call['type'] != 'function' or not isinstance(call['function']['name'], str):
                    return None
                args = json.loads(call['function']['arguments'])
                if not isinstance(args, dict):
                    return None
                # The template reserializes structured values and wraps each
                # top-level argument. Count the expanded arguments as well.
                framing += 256 + 128 * len(args) + len(json.dumps(args))
        # ASCII-escaped JSON dominates the UTF-8 bytes of names, text, history
        # (including reasoning) and function schemas in the text template.
        return framing + len(json.dumps([messages, tools], allow_nan=False))
    except (KeyError, TypeError, ValueError, RecursionError, OverflowError):
        return None


def service_retry_after(value, *, now=None):
    """Only bounded HTTP delay-seconds or an aware HTTP date may guide waiting."""
    if value is None:
        return 0.0
    value = value.strip()
    if value.isascii() and value.isdigit():
        seconds = float(value)
    else:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                raise ValueError('Retry-After date has no timezone')
            seconds = max(0.0, date.timestamp() - (time.time() if now is None else now))
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError('invalid Retry-After') from error
    if not math.isfinite(seconds) or not 0 <= seconds <= 30:
        raise ValueError('Retry-After exceeds bounded wait')
    return seconds


class TruncationShape(Enum):
    EMPTY = 'empty'
    TEXT = 'text'
    TOOL_CALLS = 'tool_calls'
    REFUSAL = 'refusal'
    OTHER = 'other'


# Existing transport diagnostics import EMPTY and TOOL_CALLS under this name.
RecoverableTruncation = TruncationShape


def _output_shape(content, tools, refusal, nontext):
    if refusal:
        return TruncationShape.REFUSAL
    if nontext or (content is not None and not isinstance(content, str)):
        return TruncationShape.OTHER
    if tools:
        return TruncationShape.TOOL_CALLS
    return TruncationShape.TEXT if content else TruncationShape.EMPTY


class _ProviderChoice(Choice):
    # Compatible providers may report additional terminal reasons. Preserve
    # them for the controller to reject after accounting for complete usage.
    finish_reason: str
    truncation_shape: TruncationShape | None = None
    nontext_output: bool = False


class _ProviderCompletion(ChatCompletion):
    choices: list[_ProviderChoice]
    stream_issue: str | None = None


def response_issue(response, *, final_plan=False):
    """Classify settled provider errors and enforce the tool-free replacement contract."""
    stream_issue = getattr(response, 'stream_issue', None) if isinstance(response, _ProviderCompletion) else None
    choices = getattr(response, 'choices', None) or ()
    if (stream_issue == 'provider_error' or getattr(response, 'error', None) is not None
            or any(getattr(c, 'error', None) is not None or getattr(getattr(c, 'message', None), 'error', None) is not None
                   for c in choices)):
        return 'model_request_failed', 'provider_error'
    if stream_issue:
        return 'invalid_response', stream_issue
    if not final_plan:
        return None
    if len(choices) != 1:
        return 'invalid_response', 'unexpected_choice_count'
    choice, = choices
    message = getattr(choice, 'message', None)
    if getattr(message, 'role', None) != 'assistant':
        return 'invalid_response', 'unexpected_role'
    if choice.finish_reason == 'length':
        shape = truncated_output(response)
    else:
        shape = _output_shape(message.content, message.tool_calls, getattr(message, 'refusal', None),
            getattr(message, 'function_call', None) or getattr(message, 'audio', None)
            or isinstance(choice, _ProviderChoice) and choice.nontext_output)
    if shape is TruncationShape.REFUSAL:
        return 'model_terminated', 'refusal'
    if shape is TruncationShape.OTHER:
        return 'invalid_response', 'unsupported_output'
    if choice.finish_reason == 'length':
        return None  # The controller seals a second length response without using its fragments.
    if shape is TruncationShape.TOOL_CALLS:
        return 'invalid_tool_batch', 'tools_disabled_for_plan_replacement'
    if choice.finish_reason == 'tool_calls':
        return 'invalid_response', 'tool_finish_without_calls'
    if choice.finish_reason != 'stop':
        return 'model_terminated', 'provider_finish_reason'
    if shape is not TruncationShape.TEXT:
        return 'empty_response', 'missing_plan_text'
    return None


def truncated_output(response: Any) -> TruncationShape | None:
    """Classify a length response without exposing any rejected fragments."""
    try:
        choice, = response.choices
        message = choice.message
        if choice.finish_reason != 'length':
            return None
        if isinstance(choice, _ProviderChoice):
            return choice.truncation_shape or TruncationShape.OTHER
        return _output_shape(message.content, getattr(message, 'tool_calls', None), getattr(message, 'refusal', None),
                             getattr(message, 'function_call', None) or getattr(message, 'audio', None))
    except (AttributeError, TypeError, ValueError):
        return TruncationShape.OTHER


def default_model_idle_timeout(model: str, base_url: str) -> float:
    """Preserve the provider's inference window between network reads."""
    if urlsplit(base_url).hostname == 'api.deepseek.com' and model.casefold() == 'deepseek-v4-pro':
        return 900.0
    return DEFAULT_MODEL_IDLE_SECONDS


class ModelRequestInterrupted(RuntimeError):
    def __init__(self, *, reason: str, deadline_seconds: float,
                 stage: str = "unknown", request: int = 0,
                 cause_types: tuple[str, ...] = (), stream_state: dict[str, Any] | None = None,
                 retry_after_seconds: float = 0.0) -> None:
        self.reason = reason
        self.status_code = SERVICE_HTTP_STATUS.get(reason.removesuffix('_retry_after_invalid'))
        self.deadline_seconds = deadline_seconds
        self.stage = stage
        self.request = request
        self.cause_types = cause_types
        self.retry_after_seconds = retry_after_seconds
        # Only bounded counters/flags may reach logs, never response fragments.
        raw = stream_state if isinstance(stream_state, dict) else {}
        self.stream_state = {key: raw[key] for key in
            ('chunks_received', 'choices_received', 'unfinished_choices')
            if type(raw.get(key)) is int and 0 <= raw[key] <= 2**31 - 1}
        if type(raw.get('usage_received')) is bool:
            self.stream_state['usage_received'] = raw['usage_received']
        super().__init__(
            f"Model request interrupted: stage={stage} request={request} reason={reason} "
            f"deadline_seconds={deadline_seconds:g}; response and usage incomplete; "
            "no partial response exposed; recovery requires controller admission" +
            ("; cause_types=" + ",".join(cause_types) if cause_types else "") +
            ('; stream_state=' + json.dumps(self.stream_state, sort_keys=True) if self.stream_state else '')
        )

    def report(self) -> dict[str, Any]:
        result = dict(stage=self.stage, request=self.request, reason=self.reason,
                      deadline_seconds=self.deadline_seconds, usage_complete=False)
        if self.cause_types:
            result['cause_types'] = list(self.cause_types)
        if self.stream_state:
            result['stream_state'] = dict(self.stream_state)
        if self.reason in SERVICE_HTTP_STATUS:
            result['retry_after_seconds'] = self.retry_after_seconds
        return result


class CompletionClient:
    """Synchronous controller facade over a fresh async client per request.

    Each connection is closed before returning, including cancellation. Keeping
    a client across separate asyncio.run loops would reuse a closed event loop.
    """
    def __init__(self, *, deadline_seconds: float | None = None,
                 idle_seconds: float = DEFAULT_MODEL_IDLE_SECONDS,
                 assemble_stream: bool = False,
                 **options: Any) -> None:
        for name, value in (('deadline', deadline_seconds), ('idle timeout', idle_seconds)):
            if name == 'deadline' and value is None:
                continue
            if not math.isfinite(value) or not 0 < value <= MAX_MODEL_DEADLINE_SECONDS:
                raise ValueError(f"Model request {name} must be positive and at most 86400 seconds")
        self.deadline_seconds = deadline_seconds
        self.idle_seconds = idle_seconds
        self.assemble_stream = assemble_stream
        self._options = dict(options, max_retries=0)
        self.chat = self
        self.completions = self

    def create(self, **kwargs: Any) -> Any:
        from .execution_policy import require_model_execution
        require_model_execution()
        if kwargs.get("stream"):
            raise ValueError("Only complete non-streaming responses may reach the controller")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._complete(**kwargs))
        # Sync Playwright keeps a running loop on the calling thread. The HTTP
        # request needs its own loop. Its deadline cancels the async request;
        # the executor is joined before returning, so no request is abandoned.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="arc-model-request") as worker:
            return worker.submit(lambda: asyncio.run(self._complete(**kwargs))).result()

    async def _complete(self, **kwargs: Any) -> Any:
        from .execution_policy import require_model_execution
        require_model_execution()
        from openai import APIConnectionError, APIStatusError, AsyncOpenAI

        # RunControl clamps this ceiling to the original phase/task deadline.
        # Rebuild the HTTP timeout for every request: the phase can change,
        # and the read-idle window must not become an overall response limit.
        # Without a controller, retain a finite whole-request idle-sized cap.
        deadline = self.deadline_seconds if self.deadline_seconds is not None else self.idle_seconds
        timeout = httpx.Timeout(min(self.idle_seconds, deadline),
            connect=min(10.0, deadline), write=min(30.0, deadline), pool=min(10.0, deadline))
        async with AsyncOpenAI(**dict(self._options, timeout=timeout)) as client:
            try:
                return await asyncio.wait_for(
                    self._request(client, kwargs), timeout=deadline)
            except TimeoutError as exc:
                raise ModelRequestInterrupted(
                    reason="deadline", deadline_seconds=deadline) from exc
            except APIStatusError as exc:
                reason = f'http_{exc.status_code}'
                if reason not in SERVICE_HTTP_STATUS:
                    raise
                retry_after = 0.0
                try:
                    retry_after = service_retry_after(exc.response.headers.get('retry-after'))
                except ValueError:
                    reason += '_retry_after_invalid'
                raise ModelRequestInterrupted(reason=reason, deadline_seconds=deadline,
                    cause_types=(type(exc).__name__,), retry_after_seconds=retry_after) from exc
            except (APIConnectionError, httpx.TransportError) as exc:
                causes = []
                seen = set()
                cause = exc
                while cause is not None and id(cause) not in seen:
                    seen.add(id(cause))
                    causes.append(type(cause).__name__)
                    cause = cause.__cause__
                raise ModelRequestInterrupted(
                    reason="transport_error", deadline_seconds=deadline,
                    cause_types=tuple(causes)) from exc
            except (ValueError, TypeError, AttributeError) as exc:
                raise ModelRequestInterrupted(reason='invalid_response',
                    deadline_seconds=deadline,
                    cause_types=(type(exc).__name__,)) from exc

    async def _request(self, client, kwargs):
        deadline = self.deadline_seconds if self.deadline_seconds is not None else self.idle_seconds
        if not self.assemble_stream:
            return await client.chat.completions.create(**kwargs)
        options = dict(kwargs, stream=True, stream_options={
            **(kwargs.get('stream_options') or {}), 'include_usage': True})
        stream = await client.chat.completions.create(**options)
        metadata, choices, usage = {}, {}, None
        stream_issue = None
        chunks_received = 0
        async with stream:
            async for chunk in stream:
                chunks_received += 1
                if metadata and chunk.id != metadata['id']:
                    raise ModelRequestInterrupted(reason='inconsistent_stream',
                                                  deadline_seconds=deadline)
                metadata = dict(id=chunk.id, model=chunk.model, created=chunk.created,
                                object='chat.completion')
                if chunk.usage is not None:
                    usage = chunk.usage.model_dump()
                if getattr(chunk, 'error', None) is not None:
                    stream_issue = 'provider_error'
                for part in chunk.choices:
                    state = choices.setdefault(part.index, dict(index=part.index,
                        message=dict(role='assistant', content=None), finish_reason=None,
                        logprobs=None, tools={}, nontext_output=False))
                    delta = part.delta
                    if getattr(part, 'error', None) is not None or getattr(delta, 'error', None) is not None:
                        stream_issue = 'provider_error'
                    if state['finish_reason'] is not None:
                        # A usage-only tail may follow a terminal choice. Never merge
                        # new content or a second finish into that choice.
                        if (part.finish_reason is not None or part.logprobs is not None
                                or any(delta.model_dump(exclude_none=True).values())):
                            stream_issue = stream_issue or 'choice_after_finish'
                        continue
                    if delta.role not in (None, 'assistant'):
                        stream_issue = stream_issue or 'unexpected_role'
                    if getattr(delta, 'function_call', None) is not None or getattr(delta, 'audio', None) is not None:
                        state['nontext_output'] = True
                    for field in ('content', 'reasoning_content', 'refusal'):
                        value = getattr(delta, field, None)
                        if value is not None:
                            if not isinstance(value, str):
                                raise ModelRequestInterrupted(reason='invalid_stream_delta',
                                                              deadline_seconds=deadline)
                            state['message'][field] = (state['message'].get(field) or '') + value
                    for tool in delta.tool_calls or []:
                        entry = state['tools'].setdefault(tool.index, dict(id='', type='function',
                            function=dict(name='', arguments='')))
                        if tool.id and tool.id != entry['id']:
                            entry['id'] += tool.id
                        if tool.function is not None:
                            for field in ('name', 'arguments'):
                                value = getattr(tool.function, field, None)
                                if value is not None:
                                    if not isinstance(value, str):
                                        raise ModelRequestInterrupted(reason='invalid_stream_delta',
                                                                      deadline_seconds=deadline)
                                    entry['function'][field] += value
                    if part.finish_reason is not None:
                        state['finish_reason'] = part.finish_reason
                    if part.logprobs is not None:
                        state['logprobs'] = part.logprobs.model_dump()
        # HTTP 200, partial arguments, a finish marker alone or EOF alone do
        # not establish a complete response. Require the final provider usage.
        if not choices or usage is None or any(part['finish_reason'] is None for part in choices.values()):
            raise ModelRequestInterrupted(reason='incomplete_stream', deadline_seconds=deadline,
                stream_state=dict(chunks_received=chunks_received, choices_received=len(choices),
                    unfinished_choices=sum(part['finish_reason'] is None for part in choices.values()),
                    usage_received=usage is not None))
        complete = []
        for index in sorted(choices):
            part = choices[index]
            tools = part.pop('tools')
            nontext_output = part['nontext_output']
            if stream_issue:
                # Preserve the terminal marker and complete usage without exposing
                # content or parsing tool arguments from a rejected envelope.
                part['message'] = dict(role='assistant', content=None)
                part['logprobs'] = None
            elif part['finish_reason'] not in ('stop', 'tool_calls'):
                # Retain the fact that output fragments existed, never the fragments.
                message = part['message']
                if part['finish_reason'] == 'length':
                    part['truncation_shape'] = _output_shape(message.get('content'), tools,
                                                             message.get('refusal'), nontext_output)
                part['message'] = dict(role='assistant', content=None)
                part['logprobs'] = None
            elif tools:
                if any(not tool['id'] or not tool['function']['name'] for tool in tools.values()):
                    raise ModelRequestInterrupted(reason='incomplete_stream_tool',
                                                  deadline_seconds=deadline)
                part['message']['tool_calls'] = [tools[key] for key in sorted(tools)]
            complete.append(part)
        try:
            return _ProviderCompletion.model_validate(dict(metadata, choices=complete, usage=usage,
                                                          stream_issue=stream_issue), strict=True)
        except ValueError as exc:
            raise ModelRequestInterrupted(reason='invalid_stream_response',
                                          deadline_seconds=deadline,
                                          cause_types=(type(exc).__name__,)) from exc

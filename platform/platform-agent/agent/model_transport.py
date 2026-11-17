"""One bounded complete response; internal streams never expose partial tools."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
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


class _ProviderChoice(Choice):
    # Compatible providers may report additional terminal reasons. Preserve
    # them for the controller to reject after accounting for complete usage.
    finish_reason: str


class _ProviderCompletion(ChatCompletion):
    choices: list[_ProviderChoice]


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
        if self.reason == 'http_503':
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
                if exc.status_code != 503:
                    raise
                reason, retry_after = 'http_503', 0.0
                try:
                    retry_after = service_retry_after(exc.response.headers.get('retry-after'))
                except ValueError:
                    reason = 'http_503_retry_after_invalid'
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
                for part in chunk.choices:
                    state = choices.setdefault(part.index, dict(index=part.index,
                        message=dict(role='assistant', content=None), finish_reason=None,
                        logprobs=None, tools={}))
                    delta = part.delta
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
            if part['finish_reason'] not in ('stop', 'tool_calls'):
                # Failed/truncated generations can end in a partial tool. Only
                # the terminal status and validated usage may reach control.
                part['message'] = dict(role='assistant', content=None)
                part['logprobs'] = None
            elif tools:
                if any(not tool['id'] or not tool['function']['name'] for tool in tools.values()):
                    raise ModelRequestInterrupted(reason='incomplete_stream_tool',
                                                  deadline_seconds=deadline)
                part['message']['tool_calls'] = [tools[key] for key in sorted(tools)]
            complete.append(part)
        try:
            return _ProviderCompletion.model_validate(dict(metadata, choices=complete, usage=usage), strict=True)
        except ValueError as exc:
            raise ModelRequestInterrupted(reason='invalid_stream_response',
                                          deadline_seconds=deadline,
                                          cause_types=(type(exc).__name__,)) from exc

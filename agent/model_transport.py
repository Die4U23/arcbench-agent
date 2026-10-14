"""One bounded complete response; internal streams never expose partial tools."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import math
from typing import Any
from urllib.parse import urlsplit

import httpx


DEFAULT_MODEL_DEADLINE_SECONDS = 300.0
MAX_MODEL_DEADLINE_SECONDS = 900.0


def default_model_deadline(model: str, base_url: str) -> float:
    """Pro inference gets a finite longer window; other transports stay unchanged."""
    if urlsplit(base_url).hostname == 'api.deepseek.com' and model.casefold() == 'deepseek-v4-pro':
        return MAX_MODEL_DEADLINE_SECONDS
    return DEFAULT_MODEL_DEADLINE_SECONDS


class ModelRequestInterrupted(RuntimeError):
    def __init__(self, *, reason: str, deadline_seconds: float,
                 stage: str = "unknown", request: int = 0,
                 cause_types: tuple[str, ...] = ()) -> None:
        self.reason = reason
        self.deadline_seconds = deadline_seconds
        self.stage = stage
        self.request = request
        self.cause_types = cause_types
        super().__init__(
            f"Model request interrupted: stage={stage} request={request} reason={reason} "
            f"deadline_seconds={deadline_seconds:g}; response and usage incomplete; "
            "generation stopped, no automatic resend" +
            ("; cause_types=" + ",".join(cause_types) if cause_types else "")
        )

    def report(self) -> dict[str, Any]:
        result = dict(stage=self.stage, request=self.request, reason=self.reason,
                      deadline_seconds=self.deadline_seconds, usage_complete=False)
        if self.cause_types:
            result['cause_types'] = list(self.cause_types)
        return result


class CompletionClient:
    """Synchronous controller facade over a fresh async client per request.

    Each connection is closed before returning, including cancellation. Keeping
    a client across separate asyncio.run loops would reuse a closed event loop.
    """
    def __init__(self, *, deadline_seconds: float = DEFAULT_MODEL_DEADLINE_SECONDS,
                 assemble_stream: bool = False,
                 **options: Any) -> None:
        if not math.isfinite(deadline_seconds) or not 0 < deadline_seconds <= MAX_MODEL_DEADLINE_SECONDS:
            raise ValueError("Model request deadline must be positive and at most 900 seconds")
        self.deadline_seconds = deadline_seconds
        self.assemble_stream = assemble_stream
        # A successful header can precede a long inference pause. The outer
        # asyncio deadline bounds the entire response, including keepalives;
        # an unrelated 60-second read cap must not shorten that advertised limit.
        self._options = dict(options, max_retries=0, timeout=httpx.Timeout(
            deadline_seconds, connect=min(10.0, deadline_seconds),
            write=min(30.0, deadline_seconds), pool=min(10.0, deadline_seconds)))
        self.chat = self
        self.completions = self

    def create(self, **kwargs: Any) -> Any:
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
        from openai import APIConnectionError, AsyncOpenAI

        async with AsyncOpenAI(**self._options) as client:
            try:
                return await asyncio.wait_for(
                    self._request(client, kwargs), timeout=self.deadline_seconds)
            except TimeoutError as exc:
                raise ModelRequestInterrupted(
                    reason="deadline", deadline_seconds=self.deadline_seconds) from exc
            except (APIConnectionError, httpx.TransportError) as exc:
                causes = []
                seen = set()
                cause = exc
                while cause is not None and id(cause) not in seen:
                    seen.add(id(cause))
                    causes.append(type(cause).__name__)
                    cause = cause.__cause__
                raise ModelRequestInterrupted(
                    reason="transport_error", deadline_seconds=self.deadline_seconds,
                    cause_types=tuple(causes)) from exc
            except (ValueError, TypeError, AttributeError) as exc:
                raise ModelRequestInterrupted(reason='invalid_response',
                    deadline_seconds=self.deadline_seconds,
                    cause_types=(type(exc).__name__,)) from exc

    async def _request(self, client, kwargs):
        if not self.assemble_stream:
            return await client.chat.completions.create(**kwargs)
        from openai.types.chat import ChatCompletion

        options = dict(kwargs, stream=True, stream_options={
            **(kwargs.get('stream_options') or {}), 'include_usage': True})
        stream = await client.chat.completions.create(**options)
        metadata, choices, usage = {}, {}, None
        async with stream:
            async for chunk in stream:
                if metadata and chunk.id != metadata['id']:
                    raise ModelRequestInterrupted(reason='inconsistent_stream',
                                                  deadline_seconds=self.deadline_seconds)
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
                                                              deadline_seconds=self.deadline_seconds)
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
                                                                      deadline_seconds=self.deadline_seconds)
                                    entry['function'][field] += value
                    if part.finish_reason is not None:
                        state['finish_reason'] = part.finish_reason
                    if part.logprobs is not None:
                        state['logprobs'] = part.logprobs.model_dump()
        # HTTP 200, partial arguments, a finish marker alone or EOF alone do
        # not establish a complete response. Require the final provider usage.
        if not choices or usage is None or any(part['finish_reason'] is None for part in choices.values()):
            raise ModelRequestInterrupted(reason='incomplete_stream', deadline_seconds=self.deadline_seconds)
        complete = []
        for index in sorted(choices):
            part = choices[index]
            tools = part.pop('tools')
            if tools:
                if any(not tool['id'] or not tool['function']['name'] for tool in tools.values()):
                    raise ModelRequestInterrupted(reason='incomplete_stream_tool',
                                                  deadline_seconds=self.deadline_seconds)
                part['message']['tool_calls'] = [tools[key] for key in sorted(tools)]
            complete.append(part)
        try:
            return ChatCompletion.model_validate(dict(metadata, choices=complete, usage=usage))
        except ValueError as exc:
            raise ModelRequestInterrupted(reason='invalid_stream_response',
                                          deadline_seconds=self.deadline_seconds,
                                          cause_types=(type(exc).__name__,)) from exc

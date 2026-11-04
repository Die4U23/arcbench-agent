"""Immutable platform generation limits. Defaults favour completeness over cost for the Flash campaign.

Environment knobs (all optional, validated, never read from .env files):
  ARC_ALLOWED_MODELS       comma list or '*'
  ARC_TOKEN_LIMIT          this RUN's total token allocation (overrides the task-derived limit)
  ARC_MAX_INPUT_TOKENS     per-request input bound reserved before each call (also the context window)
  ARC_REQUEST_LIMIT, ARC_TOOL_LIMIT, ARC_MAX_OUTPUT_TOKENS
  ARC_IMPLEMENT_REQUESTS, ARC_REPAIR_REQUESTS, ARC_REVIEW_REQUESTS, ARC_PACKET_CHARS, ARC_CONTEXT_CHARS
  ARC_RUNTIME_CHECK (0/1), ARC_RUNTIME_ROUNDS, ARC_RUNTIME_SECONDS, ARC_RUNTIME_TEST_TIMEOUT_MS
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os


_FLASH_ALIASES = frozenset({"deepseek-flash", "deepseek-v4-flash", "deepseek-v4.1-flash-1017"})
_FLASH_ALLOWED = ("deepseek-flash", "deepseek-v4-flash")

_INT_BOUNDS = {
    'ARC_EXECUTION_SECONDS': (1, 86400),
    'ARC_TOOL_BATCH_LIMIT': (1, 128),
    'ARC_REPEAT_LIMIT': (1, 20),
    'ARC_IDLE_TURNS': (1, 100),
    'ARC_PLAN_STEPS': (1, 128),
    'ARC_PLAN_REQUESTS': (1, 100),
    'ARC_STEP_REQUESTS': (1, 100),
    'ARC_REPLAN_LIMIT': (0, 10),
    "ARC_TOKEN_LIMIT": (1, 100_000_000),
    "ARC_MAX_INPUT_TOKENS": (1, 100_000_000),
    "ARC_REQUEST_LIMIT": (1, 1_000_000),
    "ARC_TOOL_LIMIT": (1, 1_000_000),
    "ARC_MAX_OUTPUT_TOKENS": (1, 1_000_000),
    "ARC_IMPLEMENT_REQUESTS": (1, 1_000_000),
    "ARC_REPAIR_REQUESTS": (1, 1_000_000),
    "ARC_REVIEW_REQUESTS": (1, 1_000_000),
    "ARC_PACKET_CHARS": (1, 100_000_000),
    "ARC_CONTEXT_CHARS": (1, 100_000_000),
    "ARC_RUNTIME_ROUNDS": (1, 1_000),
    "ARC_RUNTIME_SECONDS": (1, 86_400),
    "ARC_RUNTIME_TEST_TIMEOUT_MS": (1, 3_600_000),
}


@dataclass(frozen=True)
class PlatformSettings:
    allowed_models: tuple[str, ...] | None = None
    model_aliases: tuple[frozenset[str], ...] = (_FLASH_ALIASES,)
    max_input_tokens: int = 1_048_576
    max_output_tokens: int = 16_384
    tool_limit: int = 2_000
    max_tool_calls: int = 2_000
    context_window_tokens: int = 1_048_576
    request_allowance: int = 28
    implement_request_allowance: int = 64
    repair_request_allowance: int = 16
    review_request_allowance: int = 4
    packet_chars: int = 750_000
    context_chars: int = 48_000
    request_limit: int = 400
    runtime_check: bool = True
    runtime_rounds: int = 4
    runtime_seconds: int = 3_600
    runtime_test_timeout_ms: int = 10_000
    token_limit: int | None = None
    execution_mode: str = 'plan_execute'
    execution_seconds: int = 3600
    tool_batch_limit: int = 16
    repeated_action_limit: int = 3
    idle_turn_limit: int = 6
    plan_step_limit: int = 24
    plan_request_allowance: int = 4
    step_request_allowance: int = 12
    replan_limit: int = 1

    def execution_limits(self):
        from .platform_execution import ExecutionLimits
        return ExecutionLimits(requests=self.request_limit, tools=self.tool_limit,
            seconds=self.execution_seconds, batch_tools=self.tool_batch_limit,
            repeated_actions=self.repeated_action_limit, idle_turns=self.idle_turn_limit,
            plan_steps=self.plan_step_limit, plan_requests=self.plan_request_allowance,
            step_requests=self.step_request_allowance, replans=self.replan_limit)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> PlatformSettings:
        values: dict[str, object] = {}
        mode = environ.get('ARC_EXECUTION_MODE', 'plan_execute')
        if mode not in {'react', 'plan_execute'}:
            raise ValueError('ARC_EXECUTION_MODE')
        values['execution_mode'] = mode
        for name, field in {
            'ARC_EXECUTION_SECONDS': 'execution_seconds',
            'ARC_TOOL_BATCH_LIMIT': 'tool_batch_limit',
            'ARC_REPEAT_LIMIT': 'repeated_action_limit',
            'ARC_IDLE_TURNS': 'idle_turn_limit',
            'ARC_PLAN_STEPS': 'plan_step_limit',
            'ARC_PLAN_REQUESTS': 'plan_request_allowance',
            'ARC_STEP_REQUESTS': 'step_request_allowance',
            'ARC_REPLAN_LIMIT': 'replan_limit',
        }.items():
            value = _optional_int(environ, name)
            if value is not None:
                values[field] = value
        if "ARC_ALLOWED_MODELS" in environ:
            values["allowed_models"] = _parse_allowed_models(environ["ARC_ALLOWED_MODELS"])
        token_limit = _optional_int(environ, "ARC_TOKEN_LIMIT")
        if token_limit is not None:
            values["token_limit"] = token_limit
        max_input = _optional_int(environ, "ARC_MAX_INPUT_TOKENS")
        if max_input is not None:
            values["max_input_tokens"] = max_input
            values["context_window_tokens"] = max_input
        request_limit = _optional_int(environ, "ARC_REQUEST_LIMIT")
        if request_limit is not None:
            values["request_limit"] = request_limit
        tool_limit = _optional_int(environ, "ARC_TOOL_LIMIT")
        if tool_limit is not None:
            values["tool_limit"] = tool_limit
            values["max_tool_calls"] = tool_limit
        output_tokens = _optional_int(environ, "ARC_MAX_OUTPUT_TOKENS")
        if output_tokens is not None:
            values["max_output_tokens"] = output_tokens
        implement_requests = _optional_int(environ, "ARC_IMPLEMENT_REQUESTS")
        if implement_requests is not None:
            values["implement_request_allowance"] = implement_requests
        repair_requests = _optional_int(environ, "ARC_REPAIR_REQUESTS")
        if repair_requests is not None:
            values["repair_request_allowance"] = repair_requests
        review_requests = _optional_int(environ, "ARC_REVIEW_REQUESTS")
        if review_requests is not None:
            values["review_request_allowance"] = review_requests
        packet_chars = _optional_int(environ, "ARC_PACKET_CHARS")
        if packet_chars is not None:
            values["packet_chars"] = packet_chars
        context_chars = _optional_int(environ, "ARC_CONTEXT_CHARS")
        if context_chars is not None:
            values["context_chars"] = context_chars
        if "ARC_RUNTIME_CHECK" in environ:
            values["runtime_check"] = _parse_flag("ARC_RUNTIME_CHECK", environ["ARC_RUNTIME_CHECK"])
        runtime_rounds = _optional_int(environ, "ARC_RUNTIME_ROUNDS")
        if runtime_rounds is not None:
            values["runtime_rounds"] = runtime_rounds
        runtime_seconds = _optional_int(environ, "ARC_RUNTIME_SECONDS")
        if runtime_seconds is not None:
            values["runtime_seconds"] = runtime_seconds
        test_timeout = _optional_int(environ, "ARC_RUNTIME_TEST_TIMEOUT_MS")
        if test_timeout is not None:
            values["runtime_test_timeout_ms"] = test_timeout
        return cls(**values)


def models_equivalent(settings: PlatformSettings, requested, returned) -> bool:
    if requested == returned:
        return True
    if not isinstance(requested, str) or not isinstance(returned, str):
        return False
    left, right = requested.casefold(), returned.casefold()
    for family in settings.model_aliases:
        names = {name.casefold() for name in family}
        if left in names and right in names:
            return True
    return False


def model_allowed(settings: PlatformSettings, name: str) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    if settings.allowed_models is None:
        return True
    return name.casefold() in {item.casefold() for item in settings.allowed_models}


def _parse_allowed_models(raw: str) -> tuple[str, ...] | None:
    if not isinstance(raw, str):
        raise ValueError("ARC_ALLOWED_MODELS")
    text = raw.strip()
    if text == "*":
        return None
    names = tuple(part.strip() for part in raw.split(","))
    if not names or any(not name for name in names):
        raise ValueError("ARC_ALLOWED_MODELS")
    return names


def _optional_int(environ: Mapping[str, str], name: str) -> int | None:
    if name not in environ:
        return None
    return _parse_int(name, environ[name])


def _parse_flag(name: str, raw: str) -> bool:
    if not isinstance(raw, str) or raw.strip() not in {"0", "1"}:
        raise ValueError(name)
    return raw.strip() == "1"


def _parse_int(name: str, raw: str) -> int:
    minimum, maximum = _INT_BOUNDS[name]
    if not isinstance(raw, str):
        raise ValueError(name)
    text = raw.strip()
    if text.startswith("+"):
        text = text[1:]
    if not text.isdigit():
        raise ValueError(name)
    value = int(text)
    if value < minimum or value > maximum:
        raise ValueError(name)
    return value

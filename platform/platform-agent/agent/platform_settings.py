"""Immutable platform generation limits. Defaults favour completeness over cost for the Flash campaign.

Environment knobs (all optional, validated, never read from .env files):
  ARC_ALLOWED_MODELS       comma list or '*'
  ARC_TOKEN_LIMIT          this RUN's total token allocation (overrides the task-derived limit)
  ARC_MAX_INPUT_TOKENS     maximum per-request input reservation (also the context window)
  ARC_REQUEST_LIMIT, ARC_TOOL_LIMIT, ARC_MAX_OUTPUT_TOKENS
  ARC_IMPLEMENT_REQUESTS, ARC_REPAIR_REQUESTS, ARC_REVIEW_REQUESTS, ARC_PACKET_CHARS, ARC_CONTEXT_CHARS
  ARC_PLAN_WINDOW_REQUIREMENTS  maximum requirements per adaptive planning window (default 8)
  ARC_RUNTIME_CHECK (0/1), ARC_BEHAVIOR_CHECK (0/1), ARC_RUNTIME_ROUNDS, ARC_RUNTIME_SECONDS, ARC_RUNTIME_TEST_TIMEOUT_MS
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from urllib.parse import urlsplit


DEFAULT_REASONING_EFFORT = 'max'
MAX_RUN_SECONDS = 86_400
_FLASH_ALIASES = frozenset({"deepseek-flash", "deepseek-v4-flash", "deepseek-v4.1-flash-1017"})
_FLASH_ALLOWED = ("deepseek-flash", "deepseek-v4-flash")


def uses_flash_text_template(model: str, endpoint: str) -> bool:
    """Only the known Flash routes use the budgeted text-chat serialization."""
    url = urlsplit(endpoint)
    return (model.casefold() in _FLASH_ALIASES and url.scheme == 'https'
            and url.netloc.lower() in {'api.arc-bench.com', 'api.deepseek.com'}
            and url.path.rstrip('/') in {'', '/v1'} and not url.query and not url.fragment)

_INT_BOUNDS = {
    'ARC_API_RETRIES_PER_CALL': (0, 4),
    'ARC_API_RETRIES_TOTAL': (0, 20),
    'ARC_API_WAIT_SECONDS': (0, 300),
    'ARC_RECOVERY_ATTEMPTS': (0, 10),
    'ARC_EXECUTION_SECONDS': (1, MAX_RUN_SECONDS),
    'ARC_TOOL_BATCH_LIMIT': (1, 128),
    'ARC_REPEAT_LIMIT': (1, 20),
    'ARC_IDLE_TURNS': (1, 100),
    'ARC_PLAN_STEPS': (1, 128),
    'ARC_PLAN_WINDOW_REQUIREMENTS': (1, 128),
    'ARC_PLAN_REQUESTS': (1, 100),
    'ARC_STEP_REQUESTS': (1, 100),
    'ARC_REPLAN_LIMIT': (0, 10),
    'ARC_PROTOCOL_REPAIRS': (0, 10),
    'ARC_INSPECTION_TURNS': (1, 100),
    'ARC_WORKSPACE_CONTEXT_BYTES': (0, 1000000),
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
    "ARC_RUNTIME_SECONDS": (1, MAX_RUN_SECONDS),
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
    request_allowance: int | None = None
    implement_request_allowance: int | None = None
    repair_request_allowance: int | None = None
    review_request_allowance: int | None = None
    packet_chars: int = 750_000
    context_chars: int = 48_000
    request_limit: int | None = None
    runtime_check: bool = True
    behavior_check: bool = True
    runtime_rounds: int = 4
    runtime_seconds: int = MAX_RUN_SECONDS
    runtime_test_timeout_ms: int = 10_000
    token_limit: int | None = None
    execution_mode: str = 'plan_execute'
    input_policy: str = 'requirements_only'
    delivery_policy: str = 'runnable_candidate'
    reasoning_effort: str | None = DEFAULT_REASONING_EFFORT
    execution_seconds: int = MAX_RUN_SECONDS
    tool_batch_limit: int = 16
    repeated_action_limit: int = 3
    idle_turn_limit: int = 6
    plan_step_limit: int = 24
    plan_window_requirements: int = 8
    plan_request_allowance: int | None = None
    step_request_allowance: int | None = None
    replan_limit: int = 1
    protocol_repair_limit: int = 2
    workspace_context_bytes: int = 200000
    inspection_turn_limit: int = 3
    control_policy: str = 'adaptive'
    budget_policy: str = 'adaptive'
    recovery_attempt_limit: int | None = None
    api_retries_per_call: int | None = None
    api_retry_total: int | None = None
    api_wait_seconds: int = 60

    def execution_limits(self):
        from .platform_execution import ExecutionLimits
        return ExecutionLimits(requests=self.request_limit, tools=self.tool_limit,
            seconds=self.execution_seconds, batch_tools=self.tool_batch_limit,
            repeated_actions=self.repeated_action_limit, idle_turns=self.idle_turn_limit,
            plan_steps=self.plan_step_limit, plan_requests=self.plan_request_allowance,
            step_requests=self.step_request_allowance, replans=self.replan_limit,
            protocol_repairs=self.protocol_repair_limit, inspection_turns=self.inspection_turn_limit)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> PlatformSettings:
        values: dict[str, object] = {}
        for name, field in [('ARC_CONTROL_POLICY', 'control_policy'), ('ARC_BUDGET_POLICY', 'budget_policy')]:
            value = environ.get(name, 'adaptive')
            if value not in {'adaptive', 'legacy'}:
                raise ValueError(name)
            values[field] = value
        policy = environ.get('ARC_INPUT_POLICY', 'requirements_only')
        if policy not in {'requirements_only', 'provided_tests'}:
            raise ValueError('ARC_INPUT_POLICY')
        values['input_policy'] = policy
        delivery = environ.get('ARC_DELIVERY_POLICY', cls.delivery_policy)
        if delivery not in {'verified', 'runnable_candidate'}:
            raise ValueError('ARC_DELIVERY_POLICY')
        values['delivery_policy'] = delivery
        if 'ARC_REASONING_EFFORT' in environ:
            effort = environ['ARC_REASONING_EFFORT']
            if effort not in {'none', 'low', 'medium', 'high', 'max'}:
                raise ValueError('ARC_REASONING_EFFORT')
            values['reasoning_effort'] = effort
        mode = environ.get('ARC_EXECUTION_MODE', 'plan_execute')
        if mode not in {'react', 'plan_execute'}:
            raise ValueError('ARC_EXECUTION_MODE')
        values['execution_mode'] = mode
        for name, field in {
            'ARC_API_RETRIES_PER_CALL': 'api_retries_per_call',
            'ARC_API_RETRIES_TOTAL': 'api_retry_total',
            'ARC_API_WAIT_SECONDS': 'api_wait_seconds',
            'ARC_RECOVERY_ATTEMPTS': 'recovery_attempt_limit',
            'ARC_EXECUTION_SECONDS': 'execution_seconds',
            'ARC_TOOL_BATCH_LIMIT': 'tool_batch_limit',
            'ARC_REPEAT_LIMIT': 'repeated_action_limit',
            'ARC_IDLE_TURNS': 'idle_turn_limit',
            'ARC_PLAN_STEPS': 'plan_step_limit',
            'ARC_PLAN_WINDOW_REQUIREMENTS': 'plan_window_requirements',
            'ARC_PLAN_REQUESTS': 'plan_request_allowance',
            'ARC_STEP_REQUESTS': 'step_request_allowance',
            'ARC_REPLAN_LIMIT': 'replan_limit',
            'ARC_PROTOCOL_REPAIRS': 'protocol_repair_limit',
            'ARC_INSPECTION_TURNS': 'inspection_turn_limit',
            'ARC_WORKSPACE_CONTEXT_BYTES': 'workspace_context_bytes',
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
        if "ARC_BEHAVIOR_CHECK" in environ:
            values["behavior_check"] = _parse_flag("ARC_BEHAVIOR_CHECK", environ["ARC_BEHAVIOR_CHECK"])
        runtime_rounds = _optional_int(environ, "ARC_RUNTIME_ROUNDS")
        if runtime_rounds is not None:
            values["runtime_rounds"] = runtime_rounds
        runtime_seconds = _optional_int(environ, "ARC_RUNTIME_SECONDS")
        if runtime_seconds is not None:
            values["runtime_seconds"] = runtime_seconds
        test_timeout = _optional_int(environ, "ARC_RUNTIME_TEST_TIMEOUT_MS")
        if test_timeout is not None:
            values["runtime_test_timeout_ms"] = test_timeout
        if 'ARC_DELIVERY_POLICY' not in environ and (
                mode != 'plan_execute' or values['control_policy'] != 'adaptive'
                or not values.get('runtime_check', cls.runtime_check)
                or not values.get('behavior_check', cls.behavior_check)):
            values['delivery_policy'] = 'verified'
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

"""Content-free, versioned request observations; no billing estimates."""
from __future__ import annotations

import json
import logging
import re
from functools import wraps
from datetime import datetime, timezone
from time import monotonic_ns
from typing import Any
import uuid

LOGGER = logging.getLogger(__name__)
MARKER = "ARCBENCH_METRIC "


def identifier(value: Any) -> str | None:
    """Never log arbitrary response text, URLs, or exception messages."""
    return value if isinstance(value, str) and re.fullmatch(r"[\w./:-]{1,160}", value) else None


def count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def usage_fields(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    details = getattr(usage, "prompt_tokens_details", None)
    hit = count(getattr(usage, "prompt_cache_hit_tokens", None))
    hit_source = "prompt_cache_hit_tokens" if hit is not None else None
    if hit is None:
        hit = count(getattr(details, "cached_tokens", None))
        hit_source = "prompt_tokens_details.cached_tokens" if hit is not None else None
    # Keep the raw miss field. Deriving it from total input would mix provider
    # reporting conventions and disguise absence as observed usage.
    return {
        "prompt_tokens": count(getattr(usage, "prompt_tokens", None)),
        "completion_tokens": count(getattr(usage, "completion_tokens", None)),
        "cache_hit_tokens": hit,
        "cache_hit_source": hit_source,
        "cache_miss_tokens": count(getattr(usage, "prompt_cache_miss_tokens", None)),
        "reasoning_tokens": count(getattr(
            getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)),
    }


def emit(event: str, **fields: Any) -> None:
    LOGGER.info("%s%s", MARKER, json.dumps(
        {"schema_version": 1, "event": event,
         "recorded_at": datetime.now(timezone.utc).isoformat(), **fields},
        sort_keys=True, allow_nan=False))


def observe_operation(name):
    """Wall time, including failed operations; nested spans are not additive."""
    def decorate(function):
        @wraps(function)
        def observed(*args, **kwargs):
            span = uuid.uuid4().hex
            started = monotonic_ns()
            emit("operation_started", operation=name, span_id=span)
            error = None
            result = None
            try:
                result = function(*args, **kwargs)
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                passed = getattr(result, "passed", None)
                emit("operation_finished", operation=name, span_id=span,
                     seconds=(monotonic_ns() - started) / 1_000_000_000,
                     status="returned" if error is None else "error",
                     error_type=type(error).__name__ if error else None,
                     local_passed=passed if type(passed) is bool else None)
        return observed
    return decorate


class RequestObservation:
    """One ID per actual transport attempt, including retries and interruptions."""

    def __init__(self, *, stage: str, requested_model: str, provider: str | None,
                 request: int) -> None:
        self.fields = {
            "attempt_id": uuid.uuid4().hex,
            "request": request,
            "stage": identifier(stage),
            "requested_model": identifier(requested_model),
            "provider": identifier(provider),
        }
        emit("model_request_started", **self.fields)

    def finish(self, *, response: Any, elapsed: float, error: BaseException | None) -> None:
        emit("model_request_finished", **self.fields,
             seconds=elapsed, status="returned" if error is None else "error",
             error_type=type(error).__name__ if error is not None else None,
             actual_model=identifier(getattr(response, "model", None)),
             **usage_fields(response))

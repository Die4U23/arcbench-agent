"""Explicit zero-model-call scope for offline competition tooling."""
from contextlib import contextmanager
from contextvars import ContextVar

_offline = ContextVar("arcbench_offline_validation", default=False)


class ModelExecutionDisabled(RuntimeError):
    pass


def require_model_execution() -> None:
    if _offline.get():
        raise ModelExecutionDisabled("Model calls are disabled during offline validation")


@contextmanager
def offline_validation():
    token = _offline.set(True)
    try:
        yield
    finally:
        _offline.reset(token)

"""Controller/worker distinction is internal, never an environment opt-out."""

from pathlib import Path
import os

_WORKER = False


def in_worker() -> bool:
    return _WORKER


def enter_worker() -> None:
    global _WORKER
    if os.name != "posix" or not Path("/.dockerenv").is_file() or os.getuid() != 1000:
        raise RuntimeError(
            "sandbox worker requires the configured non-root Linux container"
        )
    _WORKER = True

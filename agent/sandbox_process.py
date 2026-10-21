"""Bound subprocess output before allocation; callers own descendant cleanup."""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path


class SandboxProcessError(RuntimeError):
    pass


class SandboxInfrastructureError(SandboxProcessError):
    """An execution prerequisite failed; model edits must not retry this run."""

    def __init__(self, message: str, *, reason: str = "sandbox_unavailable"):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class BoundedResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def run_bounded(
    command: Sequence[str],
    *,
    timeout: float,
    output_limit: int,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> BoundedResult:
    if timeout <= 0 or output_limit <= 0:
        raise ValueError("positive timeout and output limit required")
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
    )
    overflow = threading.Event()
    outputs = [bytearray(), bytearray()]
    errors = []

    def read_pipe(pipe, output):
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    return
                remaining = output_limit - len(output)
                output.extend(chunk[: max(0, remaining)])
                if len(chunk) > remaining:
                    overflow.set()
                    # Keep draining while the owner terminates the process.
        except OSError as exc:
            errors.append(str(exc))
        finally:
            pipe.close()

    threads = [
        threading.Thread(target=read_pipe, args=(pipe, output), daemon=True)
        for pipe, output in zip((process.stdout, process.stderr), outputs)
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if overflow.is_set():
                raise SandboxProcessError("sandbox output limit exceeded")
            if time.monotonic() >= deadline:
                raise SandboxProcessError("sandbox command deadline exceeded")
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        for thread in threads:
            thread.join(timeout=max(0.0, min(1.0, deadline - time.monotonic())))
        if any(thread.is_alive() for thread in threads):
            raise SandboxProcessError("descendants kept subprocess output open")
        if overflow.is_set():
            raise SandboxProcessError("sandbox output limit exceeded")
        if errors:
            raise SandboxProcessError("sandbox output could not be collected")
        return BoundedResult(process.returncode, bytes(outputs[0]), bytes(outputs[1]))
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        # Docker callers must additionally remove their exact owned container;
        # killing this CLI process alone never proves container cleanup.
        for thread in threads:
            thread.join(timeout=0.1)

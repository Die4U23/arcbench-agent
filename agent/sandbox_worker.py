"""Trusted image entrypoint. It never receives a model client or API credentials."""

from __future__ import annotations

import base64
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sys
import time

from .docker_sandbox import _decode, _encode
from .sandbox_context import enter_worker
from .sandbox_files import apply_snapshot, snapshot_project


def _stop_other_processes() -> None:
    """Quiesce the owned PID namespace before exporting regular files."""
    deadline = time.monotonic() + 3
    while True:
        try:
            while os.waitpid(-1, os.WNOHANG)[0] > 0:
                pass
        except ChildProcessError:
            pass
        active = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal() or int(entry.name) in (1, os.getpid()):
                continue
            try:
                state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
                if state == "Z":
                    continue
                if entry.stat().st_uid != os.getuid():
                    raise RuntimeError(
                        "unexpected privileged process in worker namespace"
                    )
                active.append(int(entry.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
        if not active:
            return
        for pid in active:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if time.monotonic() >= deadline:
            raise RuntimeError("worker namespace could not be quiesced; export refused")
        time.sleep(0.02)


class CaptureOnlyReview:
    def __init__(self) -> None:
        self.requests = []

    def review_visual_case(self, *, reference, reference_dir, case, screenshot):
        if len(screenshot) > 8 * 1024 * 1024 or not screenshot.startswith(
            b"\x89PNG\r\n\x1a\n"
        ):
            raise ValueError("visual capture must be a bounded PNG")
        self.requests.append(
            {
                "name": case["name"],
                "reference": reference,
                "png": base64.b64encode(screenshot).decode("ascii"),
            }
        )
        return {"verdict": "capture_pending", "issues": [], "matched": []}


def dispatch(request: dict, project: Path, task: Path) -> dict:
    from .tools import ProjectTools

    options = request.get("options", {})
    if not isinstance(options, dict):
        raise ValueError("worker options must be an object")
    operation = request["operation"]
    tools = ProjectTools(project, timeout_seconds=int(request["deadline_seconds"]))
    # Every short-lived session has its own node_modules; installation is offline.
    tools.ensure_dependencies()
    if operation == "dependencies":
        return {"installed": True, "mode": "offline-cache-only"}
    if operation == "project_script":
        return json.loads(
            tools.run_project_script(options["directory"], options["script"])
        )
    if operation == "verify_project":
        from .verify import verify_project

        checked = verify_project(
            project, include_root=options.get("include_root", True)
        )
        return {"checks": [asdict(check) for check in checked.checks]}
    if operation in ("public", "native"):
        if operation == "native":
            from .native_verification import verify_public_acceptance
        else:
            from .contract_checks import verify_public_acceptance
        checked = verify_public_acceptance(
            project,
            options.get("tree", {}),
            startup_only=options.get("startup_only", False),
        )
        return {"checks": [asdict(check) for check in checked]}
    if operation == "static_names":
        from .static_names import verify_frontend_names

        checked = verify_frontend_names(project)
        return {"checks": [] if checked is None else [asdict(checked)]}
    if operation == "visual_capture":
        from .visual_acceptance import verify_visual_acceptance, REPORT_RELATIVE_PATH

        review = CaptureOnlyReview()
        checked = verify_visual_acceptance(project, task, options["references"], review)
        return {
            "checks": [asdict(check) for check in checked.checks],
            "captures": review.requests,
            "report": json.loads((project / REPORT_RELATIVE_PATH).read_text()),
        }
    raise ValueError("unsupported worker operation")


def main() -> int:
    enter_worker()
    request_path = Path(sys.argv[1])
    if request_path.stat().st_size > 96 * 1024 * 1024:
        raise ValueError("worker request limit exceeded")
    request = json.loads(request_path.read_bytes())
    if not isinstance(request, dict) or request.get("version") != 1:
        raise ValueError("invalid worker request")
    deadline = request.get("deadline_seconds")
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (float, int))
        or not 0 < deadline <= 1800
    ):
        raise ValueError("invalid worker deadline")

    def expired(_signum, _frame):
        raise TimeoutError("isolated worker deadline exceeded")

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(math.ceil(deadline))
    project, task = Path("/work"), Path("/tmp/task")
    task.mkdir(parents=True, exist_ok=True)
    apply_snapshot(project, _decode(request.get("files")), before={})
    apply_snapshot(task, _decode(request.get("task")), before={})
    Path("/tmp/home").mkdir(exist_ok=True)
    if Path("/opt/npm-cache").is_dir():
        shutil.copytree("/opt/npm-cache", "/tmp/npm-cache", dirs_exist_ok=True)
    try:
        result = dispatch(request, project, task)
    finally:
        _stop_other_processes()
    exported = snapshot_project(project)
    response = json.dumps(
        {"version": 1, "result": result, "files": _encode(exported)},
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    if len(response) > 48 * 1024 * 1024:
        raise ValueError("worker response limit exceeded")
    signal.alarm(0)
    sys.stdout.buffer.write(response)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(
            f"Isolated execution failed (no host fallback): {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        catalog = Path("/opt/toolchain/package.json")
        if catalog.is_file():
            try:
                available = json.loads(catalog.read_text()).get("dependencies", {})
                print(
                    "Offline cache: pin these exact dependency versions: "
                    + json.dumps(available, sort_keys=True),
                    file=sys.stderr,
                )
            except (OSError, ValueError):
                pass
        raise SystemExit(1)

"""Fail-closed Docker boundary. No generated process receives host credentials."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid
from typing import Any

from .sandbox_files import (
    MAX_FILES,
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    apply_snapshot,
    snapshot_project,
    validate_relative_path,
    validate_snapshot,
)
from .sandbox_process import SandboxProcessError, run_bounded
from .request_metrics import observe_operation

IMAGE = "arcbench-sandbox:1"
_OPERATIONS = {
    "dependencies",
    "project_script",
    "verify_project",
    "public",
    "native",
    "visual_capture",
    "static_names",
}
_CLI_ENV = {
    "PATH",
    "SystemRoot",
    "SYSTEMROOT",
    "WINDIR",
    "HOME",
    "USERPROFILE",
    "TEMP",
    "TMP",
    "APPDATA",
    "LOCALAPPDATA",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
}
_MAX_RESPONSE = 48 * 1024 * 1024
_PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "NO_PROXY")


def _encode(files: dict[str, bytes]) -> dict[str, str]:
    validate_snapshot(files)
    return {
        name: base64.b64encode(data).decode("ascii") for name, data in files.items()
    }


def _decode(value: Any) -> dict[str, bytes]:
    if not isinstance(value, dict) or len(value) > MAX_FILES:
        raise SandboxProcessError("invalid sandbox file response")
    files: dict[str, bytes] = {}
    total = 0
    for name, data in value.items():
        validate_relative_path(name)
        if not isinstance(data, str) or len(data) > 4 * ((MAX_FILE_BYTES + 2) // 3):
            raise SandboxProcessError("invalid sandbox file size/encoding")
        try:
            decoded = base64.b64decode(data, validate=True)
        except (ValueError, TypeError) as exc:
            raise SandboxProcessError("invalid sandbox file encoding") from exc
        total += len(decoded)
        if len(decoded) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise SandboxProcessError("sandbox decoded byte limit exceeded")
        files[name] = decoded
    validate_snapshot(files)
    return files


class DockerSandbox:
    def __init__(self) -> None:
        self.docker = shutil.which("docker")
        if not self.docker:
            raise SandboxProcessError(
                "Docker is required; host execution fallback is disabled"
            )
        self.env = {key: value for key, value in os.environ.items() if key in _CLI_ENV}

    def _command(self, args: list[str], *, timeout: float = 15, limit: int = 65536):
        return run_bounded(
            [self.docker, *args], env=self.env, timeout=timeout, output_limit=limit
        )

    def _preflight(self) -> None:
        endpoint = self._command(
            ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
        )
        if endpoint.returncode:
            raise SandboxProcessError("cannot inspect the Docker context")
        hosts = [endpoint.stdout.decode("utf-8").strip()]
        if self.env.get("DOCKER_HOST"):
            hosts.append(self.env["DOCKER_HOST"])
        if any(not host.startswith(("unix://", "npipe://")) for host in hosts):
            raise SandboxProcessError(
                "sandbox requires a local Docker socket or named pipe"
            )
        info = self._command(["info", "--format", "{{.OSType}}"])
        if info.returncode or info.stdout.strip() != b"linux":
            raise SandboxProcessError(
                "a running Linux-container Docker engine is required"
            )

    def _remove_owned(self, owner: str) -> None:
        query = [
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            f"label=arcbench.sandbox.owner={owner}",
            "--format",
            "{{.ID}}",
        ]
        found = self._command(query)
        if found.returncode:
            raise SandboxProcessError("cannot verify owned-container cleanup")
        identifiers = found.stdout.decode("ascii").split()
        if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in identifiers):
            raise SandboxProcessError(
                "Docker returned an invalid owned-container identity"
            )
        for identifier in identifiers:
            removed = self._command(["rm", "--force", identifier])
            if removed.returncode:
                # --rm may win the race after our list. Only a successful fresh
                # query proving this exact ID absent turns that failure benign.
                remaining = self._command(query)
                if (
                    remaining.returncode
                    or identifier in remaining.stdout.decode("ascii").split()
                ):
                    raise SandboxProcessError("owned container could not be removed")
        verify = self._command(query)
        if verify.returncode or verify.stdout.strip():
            raise SandboxProcessError(
                "owned container remains or removal is unverified"
            )

    @observe_operation("docker_invocation")
    def invoke(
        self,
        operation: str,
        project: Path,
        *,
        options: dict[str, Any] | None = None,
        task: Path | None = None,
        timeout: float = 180,
    ) -> dict[str, Any]:
        if operation not in _OPERATIONS or not 0 < timeout <= 1800:
            raise ValueError("invalid sandbox operation or deadline")
        self._preflight()
        before = snapshot_project(project)
        payload = {
            "version": 1,
            "operation": operation,
            "options": options or {},
            "files": _encode(before),
            "task": _encode(snapshot_project(task)) if task else {},
            "deadline_seconds": timeout,
        }
        encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode(
            "utf-8"
        )
        if len(encoded) > 2 * _MAX_RESPONSE:
            raise SandboxProcessError("sandbox request limit exceeded")
        owner = uuid.uuid4().hex
        name = "arcbench-" + owner
        with tempfile.TemporaryDirectory(prefix="arcbench-input-") as temporary:
            request = Path(temporary) / "request.json"
            request.write_bytes(encoded)
            request.chmod(0o444)
            args = [
                "run",
                "--pull=never",
                "--name",
                name,
                "--label",
                f"arcbench.sandbox.owner={owner}",
                "--rm",
                "--init",
                "--network",
                "none",
                "--read-only",
                "--user",
                "1000:1000",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--pids-limit",
                "256",
                "--memory",
                "2048m",
                "--cpus",
                "2",
                "--log-driver",
                "none",
                "--shm-size",
                "128m",
                "--tmpfs",
                "/work:rw,nosuid,nodev,size=512m,mode=0700,uid=1000,gid=1000",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=1024m,mode=1777",
                "--mount",
                f"type=bind,src={request},dst=/input/request.json,readonly",
                "--env",
                "HOME=/tmp/home",
                "--env",
                "PYTHONPATH=/opt/arcbench",
                "--env",
                "NPM_CONFIG_OFFLINE=true",
                "--env",
                "NPM_CONFIG_CACHE=/tmp/npm-cache",
                "--entrypoint",
                "python",
                IMAGE,
                "-m",
                "agent.sandbox_worker",
                "/input/request.json",
            ]
            # Docker config.json can inject credential-bearing proxy variables
            # independently of the CLI process environment. Explicit empty values
            # override both upper/lowercase client defaults.
            image_position = args.index("--entrypoint")
            proxy_flags = [
                value
                for name in _PROXY_NAMES
                for key in (name, name.lower())
                for value in ("--env", key + "=")
            ]
            args[image_position:image_position] = proxy_flags
            try:
                completed = self._command(
                    args, timeout=timeout + 15, limit=_MAX_RESPONSE
                )
                if completed.returncode:
                    raise SandboxProcessError(
                        "sandbox worker failed; no files imported: "
                        + completed.stderr[-4000:].decode("utf-8", errors="replace")
                    )
                response = json.loads(completed.stdout)
                if (
                    not isinstance(response, dict)
                    or response.get("version") != 1
                    or not isinstance(response.get("result"), dict)
                ):
                    raise SandboxProcessError("invalid sandbox worker response")
                exported = _decode(response.get("files"))
            finally:
                # Even a killed or timed-out Docker CLI can leave its container running.
                # Only this invocation's unguessable ownership label is selected.
                self._remove_owned(owner)
            apply_snapshot(project, exported, before=before)
            return response["result"]

"""Explicit, authenticated transport for the same offline Docker worker."""
from __future__ import annotations

import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .sandbox_files import apply_snapshot, snapshot_project
from .sandbox_process import SandboxInfrastructureError, SandboxProcessError

MAX_MESSAGE = 96 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SandboxInfrastructureError("sandbox redirects are forbidden")


class RemoteSandbox:
    def __init__(self, config: dict):
        self.endpoint = config["endpoint"].rstrip("/")
        self.token = config["token"]
        self.image = config["image"]
        self.expires = config["expires"]
        url = urllib.parse.urlsplit(self.endpoint)
        local = url.scheme == "http" and url.hostname == "127.0.0.1"
        if (not (url.scheme == "https" or local) or not url.hostname or url.username
                or url.password or url.query or url.fragment or url.path not in ("", "/")
                or not isinstance(self.token, str) or len(self.token) < 40
                or not isinstance(self.image, str) or not self.image.startswith("sha256:")
                or type(self.expires) not in (int, float) or self.expires <= time.time()):
            raise SandboxInfrastructureError("invalid or expired remote sandbox configuration")
        # Do not inherit proxy credentials; HTTPS certificates remain verified.
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _request(self, method, path, payload=None):
        if time.time() >= self.expires:
            raise SandboxInfrastructureError("remote sandbox authorization expired")
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        if body is not None and len(body) > MAX_MESSAGE:
            raise SandboxInfrastructureError("remote sandbox request limit exceeded")
        request = urllib.request.Request(self.endpoint + path, data=body, method=method,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with self.http.open(request, timeout=60) as response:
                raw = response.read(MAX_MESSAGE + 1)
            if len(raw) > MAX_MESSAGE:
                raise SandboxInfrastructureError("remote sandbox response limit exceeded")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise SandboxInfrastructureError("invalid remote sandbox response")
            return result
        except (OSError, ValueError) as exc:
            # URLs, response bodies and bearer credentials must not enter model logs.
            raise SandboxInfrastructureError("remote sandbox transport failed: " + type(exc).__name__, reason="sandbox_transport_failed") from None

    def check_available(self):
        result = self._request("GET", "/health")
        if result.get("image") != self.image or result.get("mode") != "offline-docker":
            raise SandboxInfrastructureError("remote sandbox image or boundary mismatch")

    def invoke(self, operation, project, *, options=None, task=None, timeout=180):
        from .docker_sandbox import _OPERATIONS, _decode, _encode
        if operation not in _OPERATIONS or not 0 < timeout <= 1800:
            raise ValueError("invalid sandbox operation or deadline")
        before = snapshot_project(project)
        job = uuid.uuid4().hex
        payload = {"operation": operation, "options": options or {}, "timeout": timeout,
                   "files": _encode(before), "task": _encode(snapshot_project(task)) if task else {}}
        accepted = self._request("POST", "/jobs/" + job, payload)
        if accepted.get("job") != job:
            raise SandboxInfrastructureError("remote sandbox job identity mismatch")
        deadline = time.monotonic() + timeout + 90
        while time.monotonic() < deadline:
            result = self._request("GET", "/jobs/" + job)
            if result.get("state") == "done":
                if result.get("image") != self.image or not isinstance(result.get("result"), dict):
                    raise SandboxInfrastructureError("invalid remote sandbox result")
                exported = _decode(result.get("files"))
                apply_snapshot(project, exported, before=before)
                return result["result"]
            if result.get("state") == "failed":
                message = "remote Docker worker failed: " + str(result.get("error", ""))[:4500]
                if result.get("failure_kind") == "infrastructure":
                    reason = result.get("reason")
                    if reason not in {"dependency_cache_missing", "dependency_execution_denied", "dependency_prerequisite_failed", "owned_server_cleanup_failed"}:
                        reason = "sandbox_unavailable"
                    raise SandboxInfrastructureError(message, reason=reason)
                raise SandboxProcessError(message)
            if result.get("state") != "running":
                raise SandboxInfrastructureError("invalid remote sandbox job state")
            time.sleep(1)
        raise SandboxInfrastructureError("remote sandbox deadline exceeded; no files imported")


def configured_remote():
    # Only controller-owned upload config, never a generated-project file or env opt-out.
    path = Path(__file__).resolve().parent.parent / "sandbox-remote.json"
    if not path.exists():
        return None
    if path.stat().st_size > 4096:
        raise SandboxInfrastructureError("remote sandbox configuration limit exceeded")
    return RemoteSandbox(json.loads(path.read_text(encoding="utf-8")))

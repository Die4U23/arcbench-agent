"""Loopback-only, short-lived gateway; execution always uses the reviewed Docker image.

No model proxy, shell endpoint, host paths, Docker API or credentials are exposed.
An external authenticated tunnel is a separate, explicitly authorized operator action.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.docker_sandbox import DockerSandbox, IMAGE, _OPERATIONS, _decode, _encode
from agent.remote_sandbox import MAX_MESSAGE
from agent.sandbox_files import apply_snapshot, snapshot_project


class Gateway:
    def __init__(self, token, expires, image, *, executor=None, max_jobs=1000):
        if len(token) < 40 or not time.time() < expires <= time.time() + 14400:
            raise ValueError("require a strong temporary token and at most four-hour expiry")
        self.token, self.expires, self.image = token, expires, image
        self.executor = executor or self.execute
        self.max_jobs = max_jobs
        self.jobs = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=1)

    def execute(self, payload):
        sandbox = DockerSandbox()
        if sandbox.remote is not None:
            raise RuntimeError("gateway must execute local Docker, never another gateway")
        sandbox.check_available()
        actual = sandbox._command(["image", "inspect", "--format", "{{.Id}}", IMAGE])
        if actual.stdout.decode().strip() != self.image:
            from agent.sandbox_process import SandboxInfrastructureError
            raise SandboxInfrastructureError("trusted image changed")
        with tempfile.TemporaryDirectory(prefix="arcbench-gateway-") as folder:
            project, task = Path(folder) / "project", Path(folder) / "task"
            project.mkdir(); task.mkdir()
            apply_snapshot(project, _decode(payload["files"]), before={})
            apply_snapshot(task, _decode(payload["task"]), before={})
            result = sandbox.invoke(payload["operation"], project, task=task,
                                    options=payload["options"], timeout=payload["timeout"])
            return {"state": "done", "image": self.image, "result": result,
                    "files": _encode(snapshot_project(project))}

    def submit(self, job, raw):
        digest = hashlib.sha256(raw).hexdigest()
        with self.lock:
            existing = self.jobs.get(job)
            if existing is not None:
                if existing[0] != digest:
                    raise ValueError("job identity cannot be reused with different input")
                return
            if len(self.jobs) >= self.max_jobs or any(item[1] is not None and not item[1].done() for item in self.jobs.values()):
                raise ValueError("job limit or active execution")
            payload = json.loads(raw)
            if (not isinstance(payload, dict) or set(payload) != {"operation", "options", "timeout", "files", "task"}
                    or payload["operation"] not in _OPERATIONS or not isinstance(payload["options"], dict)
                    or type(payload["timeout"]) not in (int, float) or not 0 < payload["timeout"] <= 1800):
                raise ValueError("invalid operation")
            _decode(payload["files"]); _decode(payload["task"])
            # Keep only the most recent large result; retain small tombstones so
            # a duplicate old job can never trigger another execution.
            for key, (old_digest, future) in list(self.jobs.items()):
                if future is not None and future.done():
                    self.jobs[key] = (old_digest, None)
            self.jobs[job] = (digest, self.pool.submit(self.executor, payload))

    def result(self, job):
        with self.lock:
            entry = self.jobs.get(job)
        if entry is None:
            return None
        future = entry[1]
        if future is None:
            return {"state": "expired"}
        if not future.done():
            return {"state": "running"}
        try:
            return future.result()
        except Exception as exc:
            # Worker failures include bounded diagnostic output, never HTTP headers/config.
            from agent.sandbox_process import SandboxInfrastructureError
            return {"state": "failed", "error": str(exc)[-4500:],
                    "failure_kind": "infrastructure" if isinstance(exc, SandboxInfrastructureError) else "project",
                    "reason": exc.reason if isinstance(exc, SandboxInfrastructureError) else None}


def handler(gateway):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *_args):
            pass

        def reply(self, code, body):
            encoded = json.dumps(body, allow_nan=False).encode()
            if len(encoded) > MAX_MESSAGE:
                code, encoded = 500, b'{"error":"response limit exceeded"}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)
            self.close_connection = True

        def authorized(self):
            supplied = self.headers.get("Authorization", "")
            return (time.time() < gateway.expires and not self.headers.get("Origin")
                    and hmac.compare_digest(supplied.encode(), ("Bearer " + gateway.token).encode()))

        def do_GET(self):
            if not self.authorized():
                return self.reply(403, {"error": "authorization unavailable"})
            if self.path == "/health":
                return self.reply(200, {"mode": "offline-docker", "image": gateway.image})
            if not re.fullmatch(r"/jobs/[a-f0-9]{32}", self.path):
                return self.reply(404, {})
            result = gateway.result(self.path[6:])
            self.reply(404 if result is None else 200, result or {})

        def do_POST(self):
            if not self.authorized():
                return self.reply(403, {"error": "authorization unavailable"})
            if not re.fullmatch(r"/jobs/[a-f0-9]{32}", self.path):
                return self.reply(404, {})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < size <= MAX_MESSAGE:
                    return self.reply(413, {})
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError("incomplete request")
                gateway.submit(self.path[6:], raw)
            except (ValueError, TypeError, KeyError, OSError):
                return self.reply(400, {"error": "invalid or unavailable job"})
            self.reply(202, {"job": self.path[6:]})
    return Handler


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18871)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    gateway = Gateway(config["token"], config["expires"], config["image"])
    sandbox = DockerSandbox()
    if sandbox.remote is not None:
        raise RuntimeError("gateway requires local Docker")
    sandbox.check_available()
    actual = sandbox._command(["image", "inspect", "--format", "{{.Id}}", IMAGE])
    if actual.stdout.decode().strip() != config["image"]:
        raise RuntimeError("trusted image mismatch")
    server = BoundedServer(("127.0.0.1", args.port), handler(gateway))
    # Authorization expires even if the operator forgets to stop the tunnel.
    timer = threading.Timer(max(0, gateway.expires - time.time()), server.shutdown)
    timer.daemon = True
    timer.start()
    print("Loopback sandbox gateway ready; no public tunnel started", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        gateway.pool.shutdown(wait=True)
        timer.cancel()


if __name__ == "__main__":
    main()

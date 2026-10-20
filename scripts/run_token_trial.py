"""Explicit, serial Flash trials bounded by tokens; never reads a money ledger."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time


TOKEN_LIMIT = 2_000_000
INPUT_BOUND = 1_100_000  # Above the documented 1M standard context window.
OUTPUT_BOUND = 4096
REQUEST_LIMIT = 48
ROUND_REQUEST_LIMIT = 8
ROUND_SECONDS = 600
MODEL = "deepseek-flash"
POLICY = {"model": MODEL, "total_tokens": TOKEN_LIMIT, "requests": REQUEST_LIMIT,
          "round_requests": ROUND_REQUEST_LIMIT, "round_seconds": ROUND_SECONDS,
          "input_bound": INPUT_BOUND, "max_output": OUTPUT_BOUND}


class TokenStop(RuntimeError):
    pass


class TokenBudget:
    def __init__(self, path):
        self.path = Path(path)

    def save(self):
        temporary = self.path.with_suffix(".next")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(self.data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    @contextmanager
    def trial(self, label, metadata):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(".lock")
        with lock.open("x", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
                "policy": POLICY, "requests": [], "trials": [], "stopped": None}
            if self.data["policy"] != POLICY or self.data["stopped"]:
                raise TokenStop("Policy mismatch or stopped token study")
            if any(r["status"] != "settled" for r in self.data["requests"]):
                raise TokenStop("Unknown usage remains reserved; no automatic retry")
            for record in self.data["requests"]:
                self.validate_usage(record["usage"])
            if any(t["label"] == label or t["outcome"] == "running" for t in self.data["trials"]):
                raise TokenStop("Trial already exists or prior trial did not finish")
            if len(self.data["trials"]) >= 6:
                raise TokenStop("Six planned trials exhausted")
            self.label, self.started = label, time.monotonic()
            self.current = dict(label=label, outcome="running", metadata=metadata)
            self.data["trials"].append(self.current)
            self.save()
            try:
                yield self
            finally:
                if self.current["outcome"] == "running":
                    self.current["outcome"] = "interrupted"
                self.current["seconds"] = time.monotonic() - self.started
                self.save()
        finally:
            lock.unlink()

    def remaining_seconds(self):
        return ROUND_SECONDS - (time.monotonic() - self.started)

    def stop(self, reason):
        self.data["stopped"] = reason
        self.save()
        raise TokenStop(reason)

    def reserve(self):
        requests = self.data["requests"]
        if self.data["stopped"] or any(r["status"] != "settled" for r in requests):
            raise TokenStop("Study stopped or unknown usage remains reserved")
        spent = sum(r["usage"]["prompt_tokens"] + r["usage"]["completion_tokens"] for r in requests)
        if spent + INPUT_BOUND + OUTPUT_BOUND > TOKEN_LIMIT:
            raise TokenStop("Insufficient token headroom for maximum possible request")
        if len(requests) >= REQUEST_LIMIT or sum(r["label"] == self.label for r in requests) >= ROUND_REQUEST_LIMIT:
            raise TokenStop("Request limit reached")
        if self.remaining_seconds() < 240:
            raise TokenStop("Insufficient time for request and cleanup")
        record = dict(label=self.label, status="reserved", reserved_tokens=INPUT_BOUND + OUTPUT_BOUND)
        requests.append(record)
        self.save()
        return record

    def settle(self, record, usage, model):
        if record["status"] != "reserved":
            raise TokenStop("Cannot settle twice")
        if model not in {MODEL, "DeepSeek-V4.1-Flash-1017"}:
            self.stop("Unexpected actual model")
        try:
            self.validate_usage(usage)
        except TokenStop:
            self.stop("Missing, invalid, or out-of-bound token usage")
        record.update(status="settled", usage=usage, actual_model=model)
        self.save()

    @staticmethod
    def validate_usage(usage):
        if (not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0
                for k in ("prompt_tokens", "completion_tokens"))
                or usage["prompt_tokens"] > INPUT_BOUND or usage["completion_tokens"] > OUTPUT_BOUND):
            raise TokenStop("Missing, invalid, or out-of-bound token usage")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"status": "UNRUN", "policy": POLICY}))
        return
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise TokenStop("Provide the currently authorized key through the environment")
    if not args.image.startswith("sha256:") or len(args.image) != 71:
        raise TokenStop("An immutable Docker image ID is required")
    sys.path.insert(0, str(args.source.resolve()))
    import httpx
    from agent.llm import ModelClient
    from agent.model_transport import CompletionClient
    from agent.tools import ProjectTools
    import agent.docker_sandbox as docker_module
    docker_module.IMAGE = args.image
    spec_bytes = args.spec.read_bytes()
    spec = json.loads(spec_bytes)
    with TokenBudget(args.study / "token-usage.json").trial(args.label, {
            "source_commit": args.source_commit, "image": args.image,
            "spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
            "source_sha256": {str(p.relative_to(args.source)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted((args.source / "agent").glob("*.py"))}}) as budget:
        directory = args.study / args.label
        directory.mkdir()
        project = directory / "project"
        project.mkdir()
        for name, content in spec["files"].items():
            path = project / name
            if not path.resolve().is_relative_to(project.resolve()):
                budget.stop("Unsafe fixture path")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        protected = {name: (project / name).read_bytes() for name in spec["protected"]}
        logging.basicConfig(filename=directory / "controller.log", level=logging.INFO)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

        class Tools(ProjectTools):
            def protection(self):
                if any((project / name).is_symlink() or not (project / name).is_file()
                       or (project / name).read_bytes() != value for name, value in protected.items()):
                    budget.stop("Protected fixture changed")

            def call(self, name, arguments):
                if budget.remaining_seconds() < 180:
                    raise TokenStop("Insufficient cleanup headroom")
                self.protection()
                try:
                    return super().call(name, arguments)
                finally:
                    self.protection()

        tools = Tools(project)
        class Transport:
            def __init__(self):
                self.chat = self.completions = self

            def create(self, **kwargs):
                tools.protection()
                if kwargs.get("model") != MODEL:
                    budget.stop("Only Flash is allowed")
                kwargs.update(model=MODEL, temperature=0, max_tokens=OUTPUT_BOUND,
                              extra_body={"thinking": {"type": "disabled"}})
                if (set(kwargs) - {"model", "messages", "tools", "tool_choice", "max_tokens", "temperature", "extra_body"}
                        or any(not isinstance(m.get("content", ""), (str, type(None))) for m in kwargs["messages"])
                        or any(t.get("type") != "function" for t in kwargs.get("tools", []))):
                    budget.stop("Only standard text and local function tools are allowed")
                record = budget.reserve()
                transport = CompletionClient(deadline_seconds=90, assemble_stream=True,
                    api_key=key, base_url="https://api.deepseek.com",
                    http_client=httpx.AsyncClient(trust_env=False, follow_redirects=False))
                response = transport.create(**kwargs)
                raw = response.model_dump()
                (directory / f"response-{len(budget.data['requests']):03}.json").write_text(
                    json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
                budget.settle(record, raw.get("usage"), response.model)
                return response

        os.environ.update(MODEL=MODEL, OPENAI_BASE_URL="https://api.deepseek.com",
                          ARCBENCH_IMPLEMENTATION_THINKING="disabled",
                          ARCBENCH_VISUAL_IMPLEMENTATION="disabled",
                          ARCBENCH_MAX_MODEL_REQUESTS=str(ROUND_REQUEST_LIMIT + 3),
                          ARCBENCH_MAX_TOTAL_TOKENS=str(TOKEN_LIMIT))
        outcome, error_type, checks = "failed", None, []
        try:
            initial = [json.loads(tools.run_project_script(".", name)) for name in ("build", "test")]
            if initial[0]["exit_code"] or initial[1]["exit_code"] == 0:
                budget.stop("Seed must build and fail the fixed acceptance")
            model = ModelClient(max_turns=ROUND_REQUEST_LIMIT, max_tool_calls=30)
            # Reserve no hidden model calls: final acceptance is local Docker.
            model.client = Transport()
            subtree = dict(spec["requirements"]["children"][0], project_requirements=spec["requirements"])
            model.implement(task_type="library", subtree=subtree, plan=spec["plan"], project_tools=tools)
            model.raise_if_request_interrupted()
            tools.protection()
            checks = [json.loads(tools.run_project_script(".", name)) for name in ("build", "test")]
            outcome = "passed" if all(c["exit_code"] == 0 for c in checks) else "failed"
        except Exception as exc:
            error_type = type(exc).__name__
            outcome = "interrupted"
        finally:
            budget.current["outcome"] = outcome
            requests = [r for r in budget.data["requests"] if r["label"] == args.label]
            result = {"label": args.label, "outcome": outcome, "error_type": error_type,
                      "requests": len(requests), "seconds": time.monotonic() - budget.started,
                      "usage": {field: sum(r["usage"][field] for r in requests)
                                if requests and all(r["status"] == "settled" and
                                    type(r["usage"].get(field)) is int for r in requests) else None
                                for field in ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")},
                      "usage_complete": all(r["status"] == "settled" for r in requests),
                      "checks": [{"exit_code": c["exit_code"]} for c in checks],
                      "scope": "Synthetic shared-module repair, not platform 30/30"}
            (directory / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "stopped", "error_type": type(error).__name__}))
        raise SystemExit(1)

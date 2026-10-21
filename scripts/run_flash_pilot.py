"""One authorized fixed-repair pilot. Default is offline preflight, not execution."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime/src"))

import httpx
from agent.experiment_budget import (ExperimentBudget, ExperimentStopped, MODEL,
                                     OUTPUT_BOUND, RESERVATION, usage_cost)
from agent.model_transport import CompletionClient
from agent.llm import ModelClient
from agent.tools import ProjectTools
import agent.docker_sandbox as docker_module

STUDY = ROOT / "local-runs/issue21-flash-80cny-20261020"
EXPECTED_IMAGE = "sha256:3c9ee4121afa393a13ffa0d6d278b30545735781610e430aa2c4a08e7948296c"


def configuration():
    keys = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "MODEL")
    values = {key: os.environ.get(key, "") for key in keys}
    for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key.strip() in values and not values[key.strip()]:
            values[key.strip()] = value.strip().strip("\"'")
    if values["OPENAI_BASE_URL"].rstrip("/") not in {
            "https://api.deepseek.com", "https://api.deepseek.com/v1"}:
        raise ExperimentStopped("Existing endpoint is not official DeepSeek")
    if values["MODEL"] != MODEL or not values["OPENAI_API_KEY"]:
        raise ExperimentStopped("Existing Flash configuration is unavailable")
    return values


def balance(key):
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as client:
        response = client.get("https://api.deepseek.com/user/balance",
                              headers={"Authorization": "Bearer " + key})
    if response.status_code != 200:
        raise ExperimentStopped("Official read-only balance query failed")
    payload = response.json()
    entries = payload.get("balance_infos", [])
    if payload.get("is_available") is not True or len(entries) != 1 or entries[0]["currency"] != "CNY":
        raise ExperimentStopped("Available CNY account is required")
    return Decimal(entries[0]["total_balance"])


class GuardedTransport:
    """A fresh direct-only client per attempt. No transport retry or redirects."""
    def __init__(self, budget, values, artifacts, protection_check):
        self.budget, self.values = budget, values
        self.artifacts, self.protection_check = artifacts, protection_check
        self.chat = self.completions = self

    def create(self, **kwargs):
        self.protection_check()
        if kwargs.get("model") != MODEL:
            raise ExperimentStopped("Non-Flash model request rejected before transport")
        kwargs = dict(kwargs, model=MODEL, temperature=0, max_tokens=OUTPUT_BOUND,
                      extra_body={"thinking": {"type": "disabled"}})
        before = balance(self.values["OPENAI_API_KEY"])
        if before < RESERVATION:
            raise ExperimentStopped("Account cannot cover the full worst-case reservation")
        request_id = self.budget.reserve(kwargs)
        try:
            # The account read is not model usage. Only this call can spend.
            transport = CompletionClient(deadline_seconds=300, assemble_stream=True,
                api_key=self.values["OPENAI_API_KEY"], base_url="https://api.deepseek.com",
                http_client=httpx.AsyncClient(trust_env=False, follow_redirects=False))
            response = transport.create(**kwargs)
            raw = response.model_dump()
            with (self.artifacts / (request_id + ".response.json")).open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
            usage = raw.get("usage")
            self.budget.settle(request_id, usage=usage, actual_model=response.model,
                               provider_request_id=response.id)
            # Keep pricing calculations separate from account reconciliation.
            # The expected charge must lie between off-peak and peak prices.
            after = balance(self.values["OPENAI_API_KEY"])
            delta, upper = before - after, usage_cost(usage)
            reconciled = upper / 2 - Decimal("0.000001") <= delta <= upper + Decimal("0.000001")
            record = self.budget.data["requests"][-1]
            # Preserve mismatch evidence too, without recording either balance.
            # A delta alone is never a verified per-request charge.
            record.update(account_delta_cny=str(delta),
                          expected_charge_lower_cny=str(upper / 2),
                          expected_charge_upper_cny=str(upper),
                          reconciliation="within_offpeak_peak_usage_bounds" if reconciled else "mismatch")
            self.budget.save()
            if not reconciled:
                self.budget.stop("Account delta does not reconcile with provider usage; stop all paid work")
                raise ExperimentStopped("Account reconciliation failed")
            return response
        except BaseException as exc:
            if not self.budget.data["stopped"]:
                self.budget.stop("Request/settlement interrupted: " + type(exc).__name__)
            raise


class ProtectedTools(ProjectTools):
    def __init__(self, project, budget, protected):
        super().__init__(project)
        self.budget, self.protected = budget, protected
        self.script_events = []
        self.tool_requests = 0

    def protection_check(self):
        for name, expected in self.protected.items():
            path = self.project_dir / name
            if not path.is_file() or path.is_symlink() or path.read_bytes() != expected:
                self.budget.stop("Protected input changed")
                raise ExperimentStopped("Protected input changed")

    def call(self, name, arguments):
        if self.budget.remaining_seconds() < 180:
            raise ExperimentStopped("Insufficient time for bounded tool execution")
        self.protection_check()
        self.tool_requests += 1
        try:
            return super().call(name, arguments)
        finally:
            self.protection_check()

    def run_project_script(self, directory, script):
        if self.budget.remaining_seconds() < 180:
            raise ExperimentStopped("Insufficient time for bounded Docker execution")
        self.protection_check()
        started = time.monotonic()
        try:
            result = super().run_project_script(directory, script)
            value = json.loads(result)
            self.script_events.append(dict(script=script, directory=directory,
                exit_code=value.get("exit_code"), seconds=time.monotonic() - started))
            return result
        except BaseException:
            self.script_events.append(dict(script=script, directory=directory,
                exit_code=None, seconds=time.monotonic() - started))
            raise
        finally:
            self.protection_check()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-commit")
    args = parser.parse_args()
    spec_bytes = (ROOT / "benchmarks/issue21/pilot-spec.json").read_bytes()
    case = next(c for c in json.loads(spec_bytes)["cases"] if c["id"] == "repair")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if not args.execute:
        print(json.dumps({"mode": "offline_preflight", "model_requests": 0, "commit": commit,
                          "case": "repair", "reservation_cny": str(RESERVATION),
                          "policy": ExperimentBudget.policy()}, indent=2))
        return
    if datetime.now(timezone.utc).date().isoformat() != ExperimentBudget.policy()["price_checked_date"]:
        raise ExperimentStopped("Official price/context bounds must be rechecked for the execution date")
    if commit != args.expected_commit:
        raise ExperimentStopped("Candidate commit differs from pinned expected commit")
    if subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "agent", "scripts", "benchmarks"],
                      cwd=ROOT).returncode:
        raise ExperimentStopped("Executable candidate has uncommitted changes")
    values = configuration()
    sandbox = docker_module.DockerSandbox()
    sandbox._preflight()
    inspected = sandbox._command(["image", "inspect", docker_module.IMAGE, "--format", "{{.Id}}"])
    if inspected.returncode or inspected.stdout.decode().strip() != EXPECTED_IMAGE:
        raise ExperimentStopped("Previously verified Docker image is unavailable or changed")
    # Pin the verified content ID for this controller process, not a mutable tag.
    docker_module.IMAGE = EXPECTED_IMAGE
    with ExperimentBudget(STUDY / "ledger.json") as budget:
        if budget.data["rounds"]:
            raise ExperimentStopped("Pilot already registered; review before authorizing follow-up execution")
        directory = STUDY / "pilot-001"
        directory.mkdir()  # Refuse to overwrite a prior output, even after failure.
        project = directory / "project"
        project.mkdir()
        artifacts = directory / "responses"
        artifacts.mkdir()
        files = {**case["initial_files"], **case["protected_acceptance"]}
        for name, content in files.items():
            (project / name).write_text(content, encoding="utf-8", newline="\n")
        protected = {name: content.encode() for name, content in files.items()
                     if name != "index.cjs"}
        budget.start_round({"case": "repair", "evidence_kind": "real_model",
            "source_commit": commit, "baseline_commit": "b9be372ff9319dbe6e6887852921a1c64bab3c27",
            "image_id": EXPECTED_IMAGE, "spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
            "input_sha256": {k: hashlib.sha256(v.encode()).hexdigest() for k, v in files.items()},
            "model": MODEL, "temperature": 0, "thinking": "disabled", "max_tokens": OUTPUT_BOUND})
        logging.basicConfig(filename=directory / "controller.log", level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        tools = ProtectedTools(project, budget, protected)
        outcome, error_type = "failed", None
        try:
            seed_build = json.loads(tools.run_project_script(".", "build"))
            seed_test = json.loads(tools.run_project_script(".", "test"))
            if seed_build["exit_code"] != 0 or seed_test["exit_code"] == 0:
                raise ExperimentStopped("Fixed seed must build and fail its protected acceptance")
            tools.script_events.clear()
            os.environ.update(values)
            os.environ.update(MODEL=MODEL, VISUAL_MODEL=MODEL, VISUAL_REVIEW_MODEL=MODEL,
                ARCBENCH_IMPLEMENTATION_THINKING="disabled", ARCBENCH_VISUAL_IMPLEMENTATION="disabled",
                ARCBENCH_MAX_MODEL_REQUESTS="150", ARCBENCH_MAX_TOTAL_TOKENS="2000000")
            model = ModelClient(max_turns=12, max_tool_calls=30)
            model.client = GuardedTransport(budget, values, artifacts, tools.protection_check)
            exhausted = model.implement(task_type="library",
                subtree={"id": "REPAIR-1", "name": "Fixed library repair",
                         "description": case["turns"][0]}, plan=(
                    "Inspect the provided index.cjs, correct the quantity defect, run the supplied "
                    "build and protected acceptance, then finish. package.json and acceptance.cjs "
                    "are immutable. Do not scaffold a website, install dependencies, or add features."),
                project_tools=tools)
            model.raise_if_request_interrupted()
            tools.protection_check()
            checks = [json.loads(tools.run_project_script(".", name)) for name in ("build", "test")]
            if not exhausted and all(c["exit_code"] == 0 for c in checks):
                outcome = "passed"
        except BaseException as exc:
            error_type = type(exc).__name__
            timed_out = (getattr(exc, "reason", None) == "deadline" or
                         isinstance(exc, ExperimentStopped) and "time" in str(exc).lower())
            outcome = "timeout" if timed_out else "interrupted" if budget.data["stopped"] else "failed"
        finally:
            budget.finish_round(outcome)
            requests = budget.data["requests"]
            result = {"evidence_kind": "real_model", "scope": "synthetic fixed-repair pilot only",
                "outcome": outcome, "error_type": error_type, "source_commit": commit,
                "model": MODEL, "provider": "api.deepseek.com", "image_id": EXPECTED_IMAGE,
                "model_requests": len(requests), "tool_requests": tools.tool_requests,
                "script_events": tools.script_events, "round": budget.data["rounds"][-1],
                "known_upper_cost_cny": str(sum((Decimal(r["upper_cost_cny"]) for r in requests), Decimal(0))),
                "account_delta_cny": str(sum((Decimal(r.get("account_delta_cny", "0")) for r in requests), Decimal(0)))
                    if all("account_delta_cny" in r for r in requests) else None,
                "account_reconciliation_complete": bool(requests) and all(
                    r.get("reconciliation") == "within_offpeak_peak_usage_bounds" for r in requests),
                "verified_invoice_cost": None,
                "note": "Balance deltas corroborate usage; not an itemized invoice. No A/B conclusion."}
            (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                                   encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if outcome != "passed":
            raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not expose SDK errors, response bodies, configuration, keys or balances.
        print(json.dumps({"status": "stopped_before_or_during_execution",
                          "error_type": type(exc).__name__}))
        raise SystemExit(1)

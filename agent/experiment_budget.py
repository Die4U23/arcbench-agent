"""Fail-closed, single-writer CNY ledger for explicitly authorized Flash trials.

This is separate from normal Agent defaults. It never sends a network request.
The full advertised standard context window is reserved, not a character/token
approximation. Unknown usage retains the entire reservation and stops the study.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import time
import uuid

MODEL = "deepseek-flash"
PRICE_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
INPUT_BOUND = 1_100_000  # Above either decimal or binary interpretation of 1M.
OUTPUT_BOUND = 4096
SERIALIZED_LIMIT = 65_536
TOKEN_CAP = 2_000_000
REQUEST_CAP = 150
SECONDS_CAP = 2700
TOTAL_CAP = Decimal("80")
STUDY_SPEND_CAP = Decimal("70")  # Ten yuan stays unavailable to automatic runs.
ROUND_CAP = Decimal("10")
HIT_PRICE, MISS_PRICE, OUTPUT_PRICE = map(Decimal, ("0.04", "2", "8"))
MILLION = Decimal(1_000_000)
RESERVATION = (INPUT_BOUND * MISS_PRICE + OUTPUT_BOUND * OUTPUT_PRICE) / MILLION


class ExperimentStopped(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def usage_cost(usage):
    fields = ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens",
              "prompt_cache_miss_tokens")
    if not isinstance(usage, dict) or any(
            type(usage.get(key)) is not int or usage[key] < 0 for key in fields):
        raise ExperimentStopped("Incomplete provider usage; reservation retained")
    if usage["prompt_tokens"] != usage["prompt_cache_hit_tokens"] + usage["prompt_cache_miss_tokens"]:
        raise ExperimentStopped("Inconsistent provider usage; reservation retained")
    if usage["prompt_tokens"] > INPUT_BOUND or usage["completion_tokens"] > OUTPUT_BOUND:
        raise ExperimentStopped("Provider usage exceeded proven bounds; stop study")
    return (usage["prompt_cache_hit_tokens"] * HIT_PRICE +
            usage["prompt_cache_miss_tokens"] * MISS_PRICE +
            usage["completion_tokens"] * OUTPUT_PRICE) / MILLION


class ExperimentBudget:
    """A lock persists after a process crash. No automatic stale-lock recovery."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = self.path.with_suffix(".lock")
        self.acquired = False
        self.round_id = None
        self.started = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.lock.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps({"pid": os.getpid(), "created_at": now()}))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise ExperimentStopped("Study lock exists; inspect uncertain prior run, never auto-retry") from None
        self.acquired = True
        try:
            if self.path.exists():
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                if self.data["policy"] != self.policy():
                    raise ExperimentStopped("Stored budget policy differs; do not reset ledger")
                for record in self.data["requests"]:
                    if record["status"] == "settled" and (
                            Decimal(record["upper_cost_cny"]) != usage_cost(record["usage"])):
                        raise ExperimentStopped("Stored usage/cost mismatch; do not reset ledger")
                if self.data["stopped"] or any(r["status"] != "settled" for r in self.data["requests"]):
                    raise ExperimentStopped("Study contains stopped/unknown usage; do not release or retry")
                if any(r["outcome"] == "running" for r in self.data["rounds"]):
                    raise ExperimentStopped("Prior round has no terminal state; inspection required")
            else:
                self.data = {"schema_version": 1, "policy": self.policy(),
                             "created_at": now(), "stopped": None, "requests": [], "rounds": []}
                self.save()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    @staticmethod
    def policy():
        return dict(model=MODEL, total_cny=str(TOTAL_CAP), spend_cap_cny=str(STUDY_SPEND_CAP),
                    round_cny=str(ROUND_CAP), input_bound=INPUT_BOUND, output_bound=OUTPUT_BOUND,
                    token_cap=TOKEN_CAP, request_cap=REQUEST_CAP, seconds_cap=SECONDS_CAP,
                    hit_per_million=str(HIT_PRICE), miss_per_million=str(MISS_PRICE),
                    output_per_million=str(OUTPUT_PRICE), price_source=PRICE_SOURCE,
                    price_checked_date="2026-10-20",
                    price_checked_at_utc="2026-10-20T11:14:56Z")

    def save(self):
        temporary = self.path.with_suffix(".next")
        # A leftover temporary file is uncertainty, not permission to overwrite.
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(self.data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def __exit__(self, *args):
        if self.acquired:
            self.lock.unlink()
            self.acquired = False

    def start_round(self, metadata):
        if self.round_id is not None or self.data["stopped"]:
            raise ExperimentStopped("Cannot start another round in this session")
        if len(self.data["rounds"]) >= 7:
            raise ExperimentStopped("Seven preallocated rounds exhausted")
        self.round_id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.data["rounds"].append(dict(id=self.round_id, started_at=now(),
                                       outcome="running", metadata=metadata))
        self.save()
        return self.round_id

    def remaining_seconds(self):
        if self.started is None:
            raise ExperimentStopped("Round has not started")
        return SECONDS_CAP - (time.monotonic() - self.started)

    def stop(self, reason):
        self.data["stopped"] = reason
        self.save()

    def reserve(self, kwargs):
        if self.round_id is None or self.data["stopped"]:
            raise ExperimentStopped("Study is not running")
        if kwargs.get("model") != MODEL or kwargs.get("max_tokens") != OUTPUT_BOUND:
            raise ExperimentStopped("Only the fixed Flash model/output limit is authorized")
        allowed = {"model", "messages", "tools", "tool_choice", "max_tokens",
                   "temperature", "extra_body"}
        if set(kwargs) - allowed or kwargs.get("temperature") != 0 or \
                kwargs.get("extra_body") != {"thinking": {"type": "disabled"}}:
            raise ExperimentStopped("Unreviewed request configuration")
        # Standard text/function calls only. No server tools, images, uploads,
        # extended context flags, arbitrary endpoints or request headers.
        if any(not isinstance(t, dict) or t.get("type") != "function"
               for t in kwargs.get("tools", [])):
            raise ExperimentStopped("Only local function tools are permitted")
        if any(not isinstance(m.get("content", ""), (str, type(None))) for m in kwargs["messages"]):
            raise ExperimentStopped("Only text messages are permitted")
        wire_bytes = len(json.dumps(kwargs, ensure_ascii=False, allow_nan=False).encode())
        if wire_bytes > SERIALIZED_LIMIT:
            raise ExperimentStopped("Serialized request exceeds fixed 64 KiB limit")
        if self.remaining_seconds() < 480:  # 300s model + 180s verification/cleanup margin.
            raise ExperimentStopped("Insufficient wall-time headroom")
        previous = self.data["requests"]
        if any(r["status"] != "settled" for r in previous):
            raise ExperimentStopped("An unresolved reservation exists")
        current = [r for r in previous if r["round_id"] == self.round_id]
        total_cost = sum((Decimal(r["upper_cost_cny"]) for r in previous), Decimal(0))
        round_cost = sum((Decimal(r["upper_cost_cny"]) for r in current), Decimal(0))
        tokens = sum(r["usage"]["prompt_tokens"] + r["usage"]["completion_tokens"] for r in current)
        if total_cost + RESERVATION > STUDY_SPEND_CAP or round_cost + RESERVATION > ROUND_CAP:
            raise ExperimentStopped("Insufficient money for worst-case request reservation")
        if tokens + INPUT_BOUND + OUTPUT_BOUND > TOKEN_CAP or len(current) >= REQUEST_CAP:
            raise ExperimentStopped("Insufficient token/request headroom")
        request_id = uuid.uuid4().hex
        previous.append(dict(id=request_id, round_id=self.round_id, status="reserved",
                             started_at=now(), serialized_bytes=wire_bytes,
                             reserved_cny=str(RESERVATION), upper_cost_cny=str(RESERVATION)))
        self.save()  # The caller must not send until durable reservation succeeds.
        return request_id

    def settle(self, request_id, *, usage, actual_model, provider_request_id):
        record = next(r for r in self.data["requests"] if r["id"] == request_id)
        if record["status"] != "reserved":
            raise ExperimentStopped("Request already settled; no double release")
        if actual_model not in {MODEL, "DeepSeek-V4.1-Flash-1017"}:
            self.stop("Unexpected response model; reservation retained")
            raise ExperimentStopped(self.data["stopped"])
        try:
            cost = usage_cost(usage)
        except ExperimentStopped as exc:
            self.stop(str(exc))
            raise
        record.update(status="settled", usage=usage, actual_model=actual_model,
                      provider_request_id=provider_request_id, finished_at=now(),
                      upper_cost_cny=str(cost))
        self.save()

    def finish_round(self, outcome):
        record = next(r for r in self.data["rounds"] if r["id"] == self.round_id)
        record.update(outcome=outcome, finished_at=now(),
                      seconds=SECONDS_CAP - self.remaining_seconds())
        self.save()

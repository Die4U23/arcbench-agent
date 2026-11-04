"""Durable, exclusive pre-request budget for one competition campaign.

The caller supplies a verified provider context bound and applicable rates.
An unresolved request deliberately keeps its full reservation after a crash.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import os
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable
from uuid import uuid4
from urllib.parse import urlsplit


class CampaignBudgetStopped(RuntimeError):
    """The campaign has stopped before another chargeable action."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _money(value: str | Decimal) -> Decimal:
    if not isinstance(value, (str, Decimal)) or isinstance(value, bool):
        raise ValueError("money and rates must be exact decimal strings")
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("money and rates must be exact decimal strings") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("money and rates must be finite and nonnegative")
    return number


@dataclass(frozen=True)
class BudgetLimits:
    requests: int
    tokens: int
    tools: int
    cash_cny: str
    seconds: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 0 for value in
               (self.requests, self.tokens, self.tools, self.seconds)):
            raise ValueError("budget counts must be nonnegative integers")
        _money(self.cash_cny)


@dataclass(frozen=True)
class CampaignBudgetConfig:
    campaign_id: str
    model: str
    endpoint: str
    source_fingerprint: str
    rate_source: str
    input_miss_cny_per_million: str
    input_hit_cny_per_million: str
    output_cny_per_million: str
    max_input_tokens: int
    campaign: BudgetLimits
    tasks: dict[str, BudgetLimits]
    stages: dict[str, dict[str, BudgetLimits]]

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in
                   (self.campaign_id, self.model, self.endpoint, self.source_fingerprint, self.rate_source)):
            raise ValueError("campaign, model, endpoint, source and rate identities are required")
        if self.rate_source not in {"verified", "offline_fixture"}:
            raise ValueError("rate_source must be verified or offline_fixture")
        parsed_endpoint = urlsplit(self.endpoint)
        if parsed_endpoint.username or parsed_endpoint.password or parsed_endpoint.query or parsed_endpoint.fragment:
            raise ValueError("endpoint identity must not contain credentials or query parameters")
        if type(self.max_input_tokens) is not int or self.max_input_tokens < 1:
            raise ValueError("max_input_tokens requires a verified positive provider context bound")
        for rate in (self.input_miss_cny_per_million, self.input_hit_cny_per_million,
                     self.output_cny_per_million):
            _money(rate)
        if (_money(self.input_hit_cny_per_million) >
                _money(self.input_miss_cny_per_million)):
            raise ValueError("cache hit rate cannot exceed uncached reservation rate")
        if not self.tasks or set(self.tasks) != set(self.stages):
            raise ValueError("every task needs a stage budget")
        for task, stages in self.stages.items():
            if not task or not stages or not all(isinstance(stage, str) and stage for stage in stages):
                raise ValueError("every stage needs a name and budget")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CampaignBudgetConfig":
        values = dict(data)
        values["campaign"] = BudgetLimits(**values["campaign"])
        values["tasks"] = {key: BudgetLimits(**limit) for key, limit in values["tasks"].items()}
        values["stages"] = {task: {stage: BudgetLimits(**limit) for stage, limit in stages.items()}
                            for task, stages in values["stages"].items()}
        return cls(**values)


class CampaignBudget:
    def __init__(self, path: Path | str, config: CampaignBudgetConfig,
                 task_id: str, *, live_allowed: bool = False, create: bool = False,
                 request_validator: Callable[[int], None] | None = None) -> None:
        if task_id not in config.tasks:
            raise ValueError("task is not in immutable campaign configuration")
        if live_allowed and config.rate_source != "verified":
            raise ValueError("offline fixture rates cannot authorize a live request")
        if live_allowed and not callable(request_validator):
            raise ValueError("live campaign requires a source-bound request validator")
        self.path = Path(path)
        self.config = config
        self._config_json = json.dumps(asdict(config), sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False)
        self.task_id = task_id
        self.live_allowed = live_allowed
        self._request_validator = request_validator
        # Do not create a new campaign implicitly in a missing directory.
        if not self.path.parent.is_dir():
            raise FileNotFoundError(self.path.parent)
        if self.path.is_symlink():
            raise CampaignBudgetStopped("campaign_ledger_symlink")
        if not create and not self.path.is_file():
            raise CampaignBudgetStopped("campaign_ledger_missing")
        if create:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError as exc:
                raise CampaignBudgetStopped("campaign_ledger_already_exists") from exc
            else:
                os.close(descriptor)
        self._initialize(create=create)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self, *, create: bool) -> None:
        canonical = self._config_json
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not create and not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                             "AND name='campaign'").fetchone():
                raise CampaignBudgetStopped("campaign_ledger_uninitialized")
            db.execute("CREATE TABLE IF NOT EXISTS campaign (id INTEGER PRIMARY KEY CHECK (id=1), "
                       "config_json TEXT NOT NULL, config_sha256 TEXT NOT NULL, started REAL NOT NULL, stopped TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, task TEXT NOT NULL, "
                       "stage TEXT NOT NULL, made REAL NOT NULL, input_max INTEGER NOT NULL, output_max INTEGER NOT NULL, "
                       "held_cash TEXT NOT NULL, status TEXT NOT NULL, prompt INTEGER, completion INTEGER, "
                       "hit INTEGER, miss INTEGER, settled_cash TEXT, observed_usage_json TEXT, failure_reason TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS tool_events (id TEXT PRIMARY KEY, task TEXT NOT NULL, "
                       "stage TEXT NOT NULL, count INTEGER NOT NULL, made REAL NOT NULL)")
            row = db.execute("SELECT * FROM campaign WHERE id=1").fetchone()
            if row is None:
                if not create:
                    raise CampaignBudgetStopped("campaign_ledger_uninitialized")
                db.execute("INSERT INTO campaign VALUES (1,?,?,?,NULL)", (canonical, digest, time.time()))
            elif row["config_sha256"] != digest or row["config_json"] != canonical:
                raise CampaignBudgetStopped("campaign_config_changed")
            # A pending reservation from any prior process is an unknown charge.
            if db.execute("SELECT 1 FROM reservations WHERE status='pending' LIMIT 1").fetchone():
                self._stop(db, "request_usage_unknown")

    @staticmethod
    def _stop(db: sqlite3.Connection, reason: str) -> None:
        db.execute("UPDATE campaign SET stopped=COALESCE(stopped, ?) WHERE id=1", (reason,))
        # A stop must survive the exception raised to its caller.
        db.commit()

    def stop(self, reason: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._stop(db, reason)

    def freeze(self, reservation_id: str, reason: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE reservations SET failure_reason=? WHERE id=? AND task=? AND status='pending'",
                       (reason, reservation_id, self.task_id))
            self._stop(db, reason)

    def _check_open(self, db: sqlite3.Connection, stage: str) -> None:
        self._check_config(db)
        if stage not in self.config.stages[self.task_id]:
            raise ValueError("stage is not in immutable task budget")
        row = db.execute("SELECT started, stopped FROM campaign WHERE id=1").fetchone()
        if row["stopped"]:
            raise CampaignBudgetStopped(row["stopped"])
        if db.execute("SELECT 1 FROM reservations WHERE status='pending' LIMIT 1").fetchone():
            self._stop(db, "request_usage_unknown")
            raise CampaignBudgetStopped("request_usage_unknown")
        now = time.time()
        task_start = db.execute("SELECT MIN(made) FROM (SELECT made FROM reservations WHERE task=? "
                                "UNION ALL SELECT made FROM tool_events WHERE task=?)",
                                (self.task_id, self.task_id)).fetchone()[0]
        stage_start = db.execute("SELECT MIN(made) FROM (SELECT made FROM reservations WHERE task=? AND stage=? "
                                 "UNION ALL SELECT made FROM tool_events WHERE task=? AND stage=?)",
                                 (self.task_id, stage, self.task_id, stage)).fetchone()[0]
        for label, start, limit in (("campaign", row["started"], self.config.campaign.seconds),
                                    ("task", task_start, self.config.tasks[self.task_id].seconds),
                                    ("stage", stage_start, self.config.stages[self.task_id][stage].seconds)):
            if start is not None and now - start >= limit:
                self._stop(db, f"{label}_time_budget")
                raise CampaignBudgetStopped(f"{label}_time_budget")

    def _check_config(self, db: sqlite3.Connection) -> None:
        current = json.dumps(asdict(self.config), sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False)
        if current != self._config_json:
            self._stop(db, "campaign_config_changed")
            raise CampaignBudgetStopped("campaign_config_changed")

    def check_time(self, stage: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_open(db, stage)

    @staticmethod
    def _totals(db: sqlite3.Connection, task: str | None = None,
                stage: str | None = None) -> tuple[int, int, int, Decimal]:
        rows = db.execute("SELECT * FROM reservations").fetchall()
        matched = [row for row in rows if (task is None or row["task"] == task)
                   and (stage is None or row["stage"] == stage)]
        requests = len(matched)
        tokens = sum((row["prompt"] + row["completion"] if row["status"] == "settled"
                      else row["input_max"] + row["output_max"]) for row in matched)
        cash = sum((Decimal(row["settled_cash"] if row["status"] == "settled" else row["held_cash"])
                    for row in matched), Decimal(0))
        tool_rows = db.execute("SELECT task, stage, count FROM tool_events").fetchall()
        tools = sum(row["count"] for row in tool_rows if (task is None or row["task"] == task)
                    and (stage is None or row["stage"] == stage))
        return requests, tokens, tools, cash

    def _enforce(self, db: sqlite3.Connection, stage: str, *, requests: int = 0,
                 tokens: int = 0, tools: int = 0, cash: Decimal = Decimal(0)) -> None:
        for label, task, part, limit in (
            ("campaign", None, None, self.config.campaign),
            ("task", self.task_id, None, self.config.tasks[self.task_id]),
            ("stage", self.task_id, stage, self.config.stages[self.task_id][stage]),
        ):
            used = self._totals(db, task, part)
            for name, current, increment, ceiling in zip(
                ("request", "token", "tool", "cash"), used,
                (requests, tokens, tools, cash),
                (limit.requests, limit.tokens, limit.tools, _money(limit.cash_cny))):
                if current + increment > ceiling:
                    reason = f"{label}_{name}_budget"
                    self._stop(db, reason)
                    raise CampaignBudgetStopped(reason)

    def reserve(self, stage: str, input_upper_bound: int, max_output: int) -> str:
        if type(input_upper_bound) is not int or input_upper_bound != self.config.max_input_tokens:
            raise ValueError("input bound must equal the pinned verified provider context bound")
        if type(max_output) is not int or max_output < 1 or max_output > input_upper_bound:
            raise ValueError("invalid output bound")
        rate = _money(self.config.input_miss_cny_per_million)
        output_rate = _money(self.config.output_cny_per_million)
        held_cash = (Decimal(input_upper_bound) * rate + Decimal(max_output) * output_rate) / 1_000_000
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_open(db, stage)
            if self.live_allowed:
                try:
                    self._request_validator(max_output)
                except Exception as error:
                    self._stop(db, "funding_contract_invalid")
                    raise CampaignBudgetStopped("funding_contract_invalid") from error
            self._enforce(db, stage, requests=1, tokens=input_upper_bound + max_output, cash=held_cash)
            rid = uuid4().hex
            db.execute("INSERT INTO reservations (id,task,stage,made,input_max,output_max,held_cash,status) "
                       "VALUES (?,?,?,?,?,?,?,'pending')",
                       (rid, self.task_id, stage, time.time(), input_upper_bound, max_output, str(held_cash)))
            return rid

    @staticmethod
    def _field(value: Any, name: str) -> Any:
        return value.get(name) if isinstance(value, dict) else getattr(value, name, None)

    def _usage(self, usage: Any) -> tuple[int, int, int, int]:
        prompt = self._field(usage, "prompt_tokens")
        completion = self._field(usage, "completion_tokens")
        hit = self._field(usage, "prompt_cache_hit_tokens")
        miss = self._field(usage, "prompt_cache_miss_tokens")
        details = self._field(usage, "prompt_tokens_details")
        detail_hit = self._field(details, "cached_tokens")
        if hit is None:
            hit = detail_hit
        elif detail_hit is not None and hit != detail_hit:
            raise ValueError("cache hit fields disagree")
        if hit is not None and miss is None and type(prompt) is int and type(hit) is int:
            miss = prompt - hit
        if any(type(value) is not int or value < 0 for value in (prompt, completion, hit, miss)):
            raise ValueError("usage/cache breakdown missing or invalid")
        if hit + miss != prompt:
            raise ValueError("cache breakdown disagrees with prompt total")
        total = self._field(usage, "total_tokens")
        if total is not None and (type(total) is not int or total != prompt + completion):
            raise ValueError("reported total tokens disagree")
        return prompt, completion, hit, miss

    def settle(self, reservation_id: str, usage: Any) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_config(db)
            row = db.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
            if row is None or row["task"] != self.task_id or row["status"] != "pending":
                raise CampaignBudgetStopped("reservation_not_pending")
            try:
                prompt, completion, hit, miss = self._usage(usage)
            except ValueError:
                self._record_failed_usage(db, reservation_id, usage, "usage_unknown")
                self._stop(db, "usage_unknown")
                raise CampaignBudgetStopped("usage_unknown")
            if prompt > row["input_max"] or completion > row["output_max"]:
                self._record_failed_usage(db, reservation_id, usage, "usage_exceeds_reservation")
                self._stop(db, "usage_exceeds_reservation")
                raise CampaignBudgetStopped("usage_exceeds_reservation")
            cash = (Decimal(hit) * _money(self.config.input_hit_cny_per_million)
                    + Decimal(miss) * _money(self.config.input_miss_cny_per_million)
                    + Decimal(completion) * _money(self.config.output_cny_per_million)) / 1_000_000
            if cash > Decimal(row["held_cash"]):
                self._record_failed_usage(db, reservation_id, usage, "usage_exceeds_reservation")
                self._stop(db, "usage_exceeds_reservation")
                raise CampaignBudgetStopped("usage_exceeds_reservation")
            db.execute("UPDATE reservations SET status='settled', prompt=?, completion=?, hit=?, miss=?, "
                       "settled_cash=? WHERE id=?", (prompt, completion, hit, miss, str(cash), reservation_id))

    def _record_failed_usage(self, db: sqlite3.Connection, reservation_id: str,
                             usage: Any, reason: str) -> None:
        # Preserve only token counters; never persist a response body or secret.
        fields = ("prompt_tokens", "completion_tokens", "total_tokens",
                  "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
        observed = {field: self._field(usage, field) for field in fields}
        details = self._field(usage, "prompt_tokens_details")
        observed["cached_tokens"] = self._field(details, "cached_tokens")
        db.execute("UPDATE reservations SET observed_usage_json=?, failure_reason=? WHERE id=?",
                   (json.dumps(observed, sort_keys=True, default=str), reason, reservation_id))

    def record_tools(self, stage: str, count: int) -> None:
        if type(count) is not int or count < 0:
            raise ValueError("tool count must be nonnegative")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_open(db, stage)
            self._enforce(db, stage, tools=count)
            if count:
                db.execute("INSERT INTO tool_events VALUES (?,?,?,?,?)",
                           (uuid4().hex, self.task_id, stage, count, time.time()))

    def snapshot(self) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT config_sha256, started, stopped FROM campaign WHERE id=1").fetchone()
            totals = self._totals(db)
            pending = [item[0] for item in db.execute("SELECT id FROM reservations WHERE status='pending'")]
            return {"config_sha256": row["config_sha256"], "started": row["started"],
                    "stopped": row["stopped"], "requests": totals[0], "tokens": totals[1],
                    "tools": totals[2], "cash_cny": str(totals[3]), "pending_reservations": pending}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Initialize a competition budget ledger without model access")
    subcommands = parser.add_subparsers(dest="command", required=True)
    initialize = subcommands.add_parser("init", help="create one fresh ledger exclusively")
    initialize.add_argument("--config", type=Path, required=True)
    initialize.add_argument("--ledger", type=Path, required=True)
    initialize.add_argument("--task-id", required=True)
    args = parser.parse_args()
    if args.command == "init":
        configuration = CampaignBudgetConfig.from_dict(json.loads(args.config.read_text(encoding="utf-8")))
        ledger = CampaignBudget(args.ledger, configuration, args.task_id, create=True)
        print(json.dumps(ledger.snapshot(), sort_keys=True))

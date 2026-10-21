"""Offline-only trial registration and reporting. Never executes a workload/model."""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.request_metrics import MARKER


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_new(path, value):
    # Refuse to overwrite evidence, including an existing failed trial.
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def number(value, *, integer=False):
    return (type(value) is int if integer else type(value) in (int, float)) and \
        math.isfinite(value) and value >= 0


def request_report(lines):
    """Recover every started attempt, including a log ending before completion."""
    attempts = {}
    operations = {}
    tools = []
    for line in lines:
        if MARKER not in line:
            continue
        event = json.loads(line.split(MARKER, 1)[1])
        if event.get("schema_version") != 1:
            raise ValueError("Unknown metric schema")
        if event["event"] == "tool_returned":
            tools.append(event)
            continue
        if event["event"] in {"operation_started", "operation_finished"}:
            entry = operations.setdefault(event["span_id"], {})
            field = "start" if event["event"].endswith("started") else "finish"
            if field in entry:
                raise ValueError("Duplicate operation event")
            entry[field] = event
            continue
        if event["event"] not in {"model_request_started", "model_request_finished"}:
            continue
        key = event["attempt_id"]
        entry = attempts.setdefault(key, {})
        field = "start" if event["event"].endswith("started") else "finish"
        if field in entry:
            raise ValueError(f"Duplicate {field} for attempt {key}; do not concatenate duplicate logs")
        entry[field] = event
    groups = defaultdict(list)
    for entry in attempts.values():
        event = entry.get("finish", entry.get("start"))
        if "start" not in entry:
            raise ValueError("Finished attempt lacks start: incomplete log")
        if "finish" in entry:
            for key in ("provider", "requested_model", "request", "stage"):
                if entry["start"].get(key) != event.get(key):
                    raise ValueError(f"Mismatched attempt {key}")
        key = (event.get("provider"), event.get("actual_model"),
               event.get("requested_model"), event.get("stage"))
        groups[key].append(entry)
    summaries = []
    for (provider, actual, requested, stage), entries in sorted(groups.items(), key=lambda x: str(x[0])):
        finished = [e["finish"] for e in entries if "finish" in e]
        row = dict(provider=provider, actual_model=actual, requested_model=requested,
                   stage=stage, attempts=len(entries), returned=sum(
                       e["status"] == "returned" for e in finished),
                   errors=sum(e["status"] == "error" for e in finished),
                   incomplete=len(entries) - len(finished))
        for field in ("seconds", "prompt_tokens", "completion_tokens",
                      "cache_hit_tokens", "cache_miss_tokens"):
            observed = [e[field] for e in finished if number(e.get(field), integer=field != "seconds")]
            row[field] = sum(observed) if len(observed) == len(entries) else None
            row[field + "_known_sum"] = sum(observed)
            row[field + "_observed_attempts"] = len(observed)
        hit, miss = row["cache_hit_tokens"], row["cache_miss_tokens"]
        row["provider_cache_hit_ratio"] = hit / (hit + miss) if \
            hit is not None and miss is not None and hit + miss > 0 else None
        row["verified_cost"] = None
        summaries.append(row)
    spans = []
    for span_id, entry in operations.items():
        if "start" not in entry:
            raise ValueError("Finished operation lacks start")
        if "finish" in entry and entry["start"]["operation"] != entry["finish"]["operation"]:
            raise ValueError("Mismatched operation")
        spans.append({"span_id": span_id, "operation": entry["start"]["operation"],
                      "status": entry.get("finish", {}).get("status", "incomplete"),
                      "seconds": entry.get("finish", {}).get("seconds"),
                      "local_passed": entry.get("finish", {}).get("local_passed")})
    usage_observed = sum(all(number(entry.get("finish", {}).get(field), integer=True)
                             for field in ("prompt_tokens", "completion_tokens"))
                         for entry in attempts.values())
    return {"schema_version": 1, "attempts": len(attempts), "groups": summaries,
            "usage_observed_attempts": usage_observed,
            "unknown_usage_attempts": len(attempts) - usage_observed,
            "operations": spans, "tool_returns": tools,
            "note": "Missing usage is unknown, never zero; provider/model/stage groups are separate. "
                    "No prices inferred; reconcile billing separately. Returned response is not task success. "
                    "Operation spans overlap and must not be summed. Tool returns exclude interrupted calls."}


COUNT_METRICS = ("build_attempts", "build_repairs", "model_requests", "tool_requests")
TIME_METRICS = ("total_seconds", "model_seconds", "tool_seconds", "build_seconds",
                "acceptance_seconds")
BOOL_METRICS = ("first_build_passed", "first_acceptance_passed")


def prepare(spec):
    spec = json.loads(json.dumps(spec))
    if spec.get("schema_version") != 1 or len(spec.get("cases", [])) < 3:
        raise ValueError("A version-1 spec with all three workload classes is required")
    kinds = {case["kind"] for case in spec["cases"]}
    if kinds != {"from_scratch", "targeted_repair", "multi_turn_change"}:
        raise ValueError("Missing workload class")
    ids = [case["id"] for case in spec["cases"]]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate case id")
    for case in spec["cases"]:
        for key in ("initial_files", "turns", "protected_acceptance"):
            if key not in case:
                raise ValueError(f"Case {case['id']} lacks {key}")
    arms = spec["arms"]
    if len(arms) != 3 or len({a["id"] for a in arms}) != 3:
        raise ValueError("Expected distinct baseline Flash, candidate Flash, Claude Code arms")
    for name in ids + [arm["id"] for arm in arms]:
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,60}", name):
            raise ValueError("Case/arm IDs must be safe filename components")
    repeats = spec["repeats"]
    if type(repeats) is not int or repeats < 3:
        raise ValueError("At least three pre-registered repetitions required")
    slots = []
    # Rotate the execution order to avoid always warming caches for one arm.
    for repetition in range(repeats):
        for case in spec["cases"]:
            for arm in arms[repetition % 3:] + arms[:repetition % 3]:
                slots.append({"id": f"{case['id']}.{arm['id']}.{repetition + 1}",
                              "case": case["id"], "arm": arm["id"],
                              "repetition": repetition + 1, "state": "planned"})
    return {"schema_version": 1, "spec": spec, "spec_sha256": digest(spec),
            "paid_execution_authorized": False, "slots": slots}


def validate_manifest(manifest):
    if digest(manifest["spec"]) != manifest["spec_sha256"]:
        raise ValueError("Spec changed after registration")
    if manifest["slots"] != prepare(manifest["spec"])["slots"]:
        raise ValueError("Registered trial matrix changed")


def validate_result(result):
    if result.get("outcome") not in {"passed", "failed", "timeout", "interrupted", "blocked"}:
        raise ValueError("Explicit outcome required, including failures")
    if result.get("evidence_kind") not in {"mock", "real_docker", "real_model"}:
        raise ValueError("Explicit evidence kind required")
    for key in COUNT_METRICS + TIME_METRICS:
        value = result.get(key)
        if value is not None and not number(value, integer=key in COUNT_METRICS):
            raise ValueError(f"Invalid metric {key}")
    for key in BOOL_METRICS:
        if result.get(key) is not None and type(result[key]) is not bool:
            raise ValueError(f"Invalid metric {key}")
    if result.get("build_repairs") is not None and result.get("build_attempts") is not None:
        if result["build_repairs"] > max(0, result["build_attempts"] - 1):
            raise ValueError("Repairs cannot exceed builds after the first")
    if not isinstance(result.get("artifacts"), dict) or not result["artifacts"]:
        raise ValueError("Evidence artifact SHA256 map required")
    for value in result["artifacts"].values():
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("Invalid artifact SHA256")
    if result["evidence_kind"] == "real_model":
        for key in ("actual_model", "provider", "tool_version", "source_commit", "image_id"):
            if not isinstance(result.get(key), str) or not result[key]:
                raise ValueError(f"Real-model result lacks {key}")
    cost = result.get("verified_cost")
    if cost is not None:
        if not isinstance(cost, dict) or not number(cost.get("amount")) or \
                not cost.get("currency") or not cost.get("billing_reference"):
            raise ValueError("Cost requires amount, currency and reconcilable billing reference")


def verify_artifacts(result, root):
    """Hash named evidence without printing contents or accepting outside paths."""
    root = root.resolve()
    for name, expected in result["artifacts"].items():
        if not isinstance(name, str) or "\\" in name or ":" in name:
            raise ValueError("Artifact paths must be relative POSIX paths")
        parts = name.split("/")
        if any(not part or part.startswith(".") for part in parts):
            raise ValueError("Hidden/traversing artifact path")
        path = root
        for part in parts:
            path = path / part
            if path.is_symlink() or path.is_junction():
                raise ValueError("Linked artifact path")
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError("Artifact must be a regular file inside evidence directory")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise ValueError(f"Artifact hash mismatch: {name}")


def trial_report(manifest, evidence):
    validate_manifest(manifest)
    groups = defaultdict(list)
    planned = 0
    valid_ids = {slot["id"] for slot in manifest["slots"]}
    if set(evidence) - valid_ids:
        raise ValueError("Evidence for unregistered trial")
    for slot in manifest["slots"]:
        entry = evidence.get(slot["id"])
        if entry is None:
            planned += 1
            continue
        if entry.get("spec_sha256") != manifest["spec_sha256"]:
            raise ValueError("Trial belongs to a different spec")
        result = entry.get("result")
        if result is not None:
            validate_result(result)
        else:
            result = {"outcome": "interrupted", "evidence_kind": entry["evidence_kind"]}
        if result["evidence_kind"] != entry["evidence_kind"]:
            raise ValueError("Trial evidence kind changed")
        # Do not split failed/unfinished trials with unknown model metadata out
        # of the successful arm's denominator.
        group = (slot["case"], slot["arm"], result["evidence_kind"])
        groups[group].append(result)
    rows = []
    for (case, arm, kind), results in sorted(groups.items()):
        identities = {(r.get("provider"), r.get("actual_model"), r.get("image_id"),
                       r.get("source_commit"), r.get("tool_version")) for r in results}
        comparable = len(identities) == 1 and (kind != "real_model" or
                                              all(x is not None for x in next(iter(identities))))
        row = dict(case=case, arm=arm, evidence_kind=kind,
                   observed_configurations=[list(x) for x in sorted(identities, key=str)],
                   configuration_consistent=comparable, started=len(results),
                   passed=sum(r["outcome"] == "passed" for r in results))
        row["completion_rate"] = row["passed"] / row["started"]
        row["outcomes"] = {outcome: sum(r["outcome"] == outcome for r in results)
                           for outcome in ("passed", "failed", "timeout", "interrupted", "blocked")}
        # Every started trial is in the denominator. Unknown first-check results
        # are reported separately and never presented as a known failure.
        for metric in BOOL_METRICS:
            row[metric] = {"passed_over_started": sum(r.get(metric) is True for r in results) / len(results),
                           "unknown": sum(r.get(metric) is None for r in results)}
        row["metrics"] = {}
        for metric in COUNT_METRICS + TIME_METRICS:
            values = [r[metric] for r in results if r.get(metric) is not None]
            row["metrics"][metric] = {"observed": len(values), "missing": len(results) - len(values),
                                     "samples": values}
            if comparable and len(results) >= 3 and len(values) == len(results):
                row["metrics"][metric].update(min=min(values), median=statistics.median(values),
                                               max=max(values))
        currencies = {r["verified_cost"]["currency"] for r in results if r.get("verified_cost")}
        row["verified_cost"] = (
            {"currency": next(iter(currencies)), "amount": sum(r["verified_cost"]["amount"] for r in results)}
            if comparable and len(currencies) == 1 and all(r.get("verified_cost") for r in results) else None)
        row["preliminary_distribution"] = comparable and len(results) >= 3
        rows.append(row)
    return {"planned": len(manifest["slots"]), "not_started": planned,
            "started": sum(r["started"] for r in rows), "groups": rows,
            "note": "Mock/Docker/model evidence stays separate. Missing completion after start counts "
                    "as interrupted. No performance improvement inferred."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--spec", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    req = sub.add_parser("requests")
    req.add_argument("--log", type=Path, required=True)
    req.add_argument("--output", type=Path, required=True)
    for action in ("start", "finish", "report"):
        command = sub.add_parser(action)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--evidence", type=Path, required=True)
        if action != "report":
            command.add_argument("--slot", required=True)
        if action == "start":
            command.add_argument("--kind", choices=("mock", "real_docker", "real_model"), required=True)
        if action == "finish":
            command.add_argument("--result", type=Path, required=True)
        if action == "report":
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        write_new(args.output, prepare(read(args.spec)))
    elif args.action == "requests":
        with args.log.open(encoding="utf-8-sig") as stream:
            write_new(args.output, request_report(stream))
    else:
        manifest = read(args.manifest)
        validate_manifest(manifest)
        ids = {slot["id"] for slot in manifest["slots"]}
        if args.action != "report" and args.slot not in ids:
            raise ValueError("Unregistered slot")
        if args.action == "start":
            args.evidence.mkdir(parents=True, exist_ok=True)
            write_new(args.evidence / (args.slot + ".start.json"), {
                "spec_sha256": manifest["spec_sha256"], "evidence_kind": args.kind})
        elif args.action == "finish":
            started = read(args.evidence / (args.slot + ".start.json"))
            result = read(args.result)
            validate_result(result)
            verify_artifacts(result, args.evidence)
            if started["evidence_kind"] != result["evidence_kind"]:
                raise ValueError("Evidence kind differs from registration")
            if started["spec_sha256"] != manifest["spec_sha256"]:
                raise ValueError("Started under different spec")
            write_new(args.evidence / (args.slot + ".result.json"), result)
        else:
            evidence = {}
            for path in args.evidence.glob("*.start.json"):
                key = path.name.removesuffix(".start.json")
                evidence[key] = read(path)
                result_path = args.evidence / (key + ".result.json")
                if result_path.exists():
                    evidence[key]["result"] = read(result_path)
                    validate_result(evidence[key]["result"])
                    verify_artifacts(evidence[key]["result"], args.evidence)
            for path in args.evidence.glob("*.result.json"):
                if path.name.removesuffix(".result.json") not in evidence:
                    raise ValueError("Result without pre-registered start")
            write_new(args.output, trial_report(manifest, evidence))


if __name__ == "__main__":
    main()

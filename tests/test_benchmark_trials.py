"""Offline ledger rejects lost failures, mixed evidence, and overwritten records."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.benchmark_trials import prepare, trial_report, validate_result, verify_artifacts, write_new


SPEC = {
    "schema_version": 1, "repeats": 3,
    "cases": [{"id": name, "kind": kind, "initial_files": {}, "turns": ["task"],
               "protected_acceptance": {"check.cjs": "test"}}
              for name, kind in (("fresh", "from_scratch"), ("repair", "targeted_repair"),
                                 ("changes", "multi_turn_change"))],
    "arms": [{"id": name} for name in ("flash_before", "flash_after", "claude_code")],
}


def result(**changes):
    return dict({"outcome": "failed", "evidence_kind": "mock", "total_seconds": 10,
                 "artifacts": {"raw.json": "a" * 64}}, **changes)


class BenchmarkTrialsTests(unittest.TestCase):
    def test_all_slots_registered_and_rotated(self):
        manifest = prepare(SPEC)
        self.assertEqual(len(manifest["slots"]), 27)
        self.assertEqual(len({s["id"] for s in manifest["slots"]}), 27)
        self.assertEqual(manifest["slots"][0]["arm"], "flash_before")
        self.assertEqual(manifest["slots"][9]["arm"], "flash_after")
        self.assertFalse(manifest["paid_execution_authorized"])

    def test_timeout_and_incomplete_stay_in_same_denominator(self):
        manifest = prepare(SPEC)
        evidence = {}
        for i, outcome in enumerate(("passed", "timeout", None), 1):
            value = {"spec_sha256": manifest["spec_sha256"], "evidence_kind": "mock"}
            if outcome:
                value["result"] = result(outcome=outcome)
            evidence[f"fresh.flash_before.{i}"] = value
        report = trial_report(manifest, evidence)
        self.assertEqual(report["not_started"], 24)
        row = report["groups"][0]
        self.assertEqual(row["started"], 3)
        self.assertEqual(row["completion_rate"], 1 / 3)
        self.assertEqual(row["outcomes"]["interrupted"], 1)
        self.assertEqual(row["metrics"]["total_seconds"]["missing"], 1)
        self.assertNotIn("median", row["metrics"]["total_seconds"])
        self.assertIsNone(row["verified_cost"])

    def test_minimum_three_and_configuration_match_before_distribution(self):
        manifest = prepare(SPEC)
        entries = {f"fresh.flash_before.{i}": {
            "spec_sha256": manifest["spec_sha256"], "evidence_kind": "mock",
            "result": result(total_seconds=i)} for i in (1, 2)}
        self.assertFalse(trial_report(manifest, entries)["groups"][0]["preliminary_distribution"])
        entries["fresh.flash_before.3"] = {"spec_sha256": manifest["spec_sha256"],
            "evidence_kind": "mock", "result": result(total_seconds=3)}
        row = trial_report(manifest, entries)["groups"][0]
        self.assertEqual(row["metrics"]["total_seconds"]["median"], 2)
        entries["fresh.flash_before.3"]["result"]["actual_model"] = "different"
        row = trial_report(manifest, entries)["groups"][0]
        self.assertFalse(row["preliminary_distribution"])
        self.assertNotIn("median", row["metrics"]["total_seconds"])

    def test_real_failure_missing_metadata_not_split_out_of_denominator(self):
        manifest = prepare(SPEC)
        evidence = {
            "fresh.flash_before.1": {"spec_sha256": manifest["spec_sha256"],
                "evidence_kind": "real_model", "result": result(outcome="passed",
                    evidence_kind="real_model", actual_model="flash", provider="provider",
                    tool_version="1", source_commit="abc", image_id="sha256:image")},
            "fresh.flash_before.2": {"spec_sha256": manifest["spec_sha256"],
                                   "evidence_kind": "real_model"}}
        row = trial_report(manifest, evidence)["groups"][0]
        self.assertEqual(row["completion_rate"], 0.5)
        self.assertFalse(row["configuration_consistent"])

    def test_mock_and_real_docker_are_not_combined(self):
        manifest = prepare(SPEC)
        evidence = {f"fresh.flash_before.{i}": {
            "spec_sha256": manifest["spec_sha256"], "evidence_kind": kind,
            "result": result(evidence_kind=kind)} for i, kind in
                ((1, "mock"), (2, "real_docker"))}
        self.assertEqual(len(trial_report(manifest, evidence)["groups"]), 2)

    def test_mutated_spec_unregistered_trial_and_wrong_fingerprint_fail(self):
        manifest = prepare(SPEC)
        with self.assertRaisesRegex(ValueError, "unregistered"):
            trial_report(manifest, {"unregistered": {}})
        with self.assertRaisesRegex(ValueError, "different spec"):
            trial_report(manifest, {"fresh.flash_before.1": {"spec_sha256": "changed"}})
        manifest["spec"]["repeats"] = 4
        with self.assertRaisesRegex(ValueError, "changed"):
            trial_report(manifest, {})

    def test_invalid_cost_numeric_and_missing_evidence_are_rejected(self):
        for values in ({"total_seconds": -1}, {"model_requests": 1.5},
                       {"build_attempts": 2, "build_repairs": 2},
                       {"verified_cost": {"amount": 1, "currency": "USD"}},
                       {"first_build_passed": "true"}, {"artifacts": {}},
                       {"total_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                validate_result(result(**values))
        validate_result(result(verified_cost={"amount": 0, "currency": "USD",
                                             "billing_reference": "fixture-only"}))

    def test_evidence_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "result.json"
            write_new(path, result())
            with self.assertRaises(FileExistsError):
                write_new(path, result(outcome="passed"))
            self.assertEqual(json.loads(path.read_text())["outcome"], "failed")

    def test_ids_cannot_escape_evidence_directory(self):
        spec = deepcopy(SPEC)
        spec["arms"][0]["id"] = "../../escape"
        with self.assertRaisesRegex(ValueError, "filename"):
            prepare(spec)

    def test_artifact_changes_and_outside_paths_are_rejected(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "raw.txt").write_bytes(b"raw")
            value = result(artifacts={"raw.txt": hashlib.sha256(b"raw").hexdigest()})
            verify_artifacts(value, root)
            (root / "raw.txt").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                verify_artifacts(value, root)
            with self.assertRaisesRegex(ValueError, "artifact path"):
                verify_artifacts(result(artifacts={"../outside": "a" * 64}), root)


if __name__ == "__main__":
    unittest.main()

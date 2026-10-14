"""Exercise atomic repair and real source rollback through ModelClient's tool loop."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent.candidate_checkpoint import CandidateCheckpoint, PUBLIC_CHECK
from agent.tools import ProjectTools
from agent.verify import CheckResult
from tests.test_llm_budget import _FakeMessage, _FakeToolCall, _make_client, _read_files_call


def write_files(**files):
    return _FakeToolCall("edit", "write_files", json.dumps({
        "files": [{"path": path, "content": content} for path, content in files.items()]}))


def checks(*, preserved: bool, fixed: bool = False):
    statuses = (("preserved", "passed" if preserved else "failed"),
                ("repair", "passed" if fixed else "failed"))
    return (CheckResult("frontend: npm run build", True, 0, "built"),
            CheckResult("web template structure", True, 0, "layout"),
            CheckResult(PUBLIC_CHECK, preserved and fixed, 0, "fixed assertions",
                        case_statuses=statuses))


class CheckpointContextTests(unittest.TestCase):
    def project(self, root):
        tools = ProjectTools(root)
        tools.write_files([{"path": "frontend/src/provider.js", "content": "old"},
                           {"path": "backend/src/consumer.js", "content": "old"}])
        guard = CandidateCheckpoint(root)
        guard.consider(checks(preserved=True))
        return tools, guard

    def test_related_file_migration_is_checked_only_after_complete_repair(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools, guard = self.project(root)
            observed = []
            def checkpoint():
                provider = (root / "frontend/src/provider.js").read_text()
                consumer = (root / "backend/src/consumer.js").read_text()
                observed.append((provider, consumer))
                report = checks(preserved=provider == consumer, fixed=provider == consumer == "new")
                note = guard.consider(report, final=True)
                if note:
                    tools.source_revision += 1
                return ("Current build and test scripts pass." if all(c.passed for c in report)
                        else "Related file contract: FAILED")
            model = _make_client(None, None, [
                _FakeMessage([write_files(**{"frontend/src/provider.js": "new"})]),
                *[_FakeMessage([_read_files_call([path])]) for path in (
                    "frontend/src/provider.js", "backend/src/consumer.js",
                    "frontend/src/provider.js", "backend/src/consumer.js")],
                _FakeMessage([write_files(**{"backend/src/consumer.js": "new"})]),
                _FakeMessage(None, "related changes complete")])
            exhausted = model.implement(task_type="web", subtree={"id": "ROOT"},
                plan="Migrate both sides", project_tools=tools, repair_feedback="Repair related contract",
                checkpoint_feedback=checkpoint, request_allowance=12)
            self.assertFalse(exhausted)
            self.assertEqual(observed, [("new", "new")])
            self.assertEqual(tools.source_revision, 0)
            self.assertEqual(guard.passing, {"preserved", "repair"})
            self.assertTrue(model.last_implementation_handoff)

    def test_actual_regression_at_repair_finish_still_restores_source(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools, guard = self.project(root)
            def checkpoint():
                preserved = (root / "frontend/src/provider.js").read_text() == "old"
                note = guard.consider(checks(preserved=preserved), final=True)
                if note:
                    tools.source_revision += 1
                return "Protected repair assertion: FAILED; " + note
            model = _make_client(None, None, [
                _FakeMessage([write_files(**{"frontend/src/provider.js": "broken"})]),
                _FakeMessage(None, "done")])
            self.assertFalse(model.implement(task_type="web", subtree={"id": "ROOT"}, plan="Repair",
                project_tools=tools, repair_feedback="Repair behavior", checkpoint_feedback=checkpoint))
            self.assertEqual((root / "frontend/src/provider.js").read_text(), "old")
            self.assertEqual((root / "backend/src/consumer.js").read_text(), "old")
            self.assertEqual(tools.source_revision, 1)
            self.assertTrue(model.last_implementation_deferred)
            self.assertFalse(model.last_implementation_handoff)
            self.assertEqual(model.last_implementation_stop_reason, "checkpoint_failed")
            self.assertIsNone(model.last_budget_report)

    def test_repair_slice_limit_checks_and_restores_unfinished_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools, guard = self.project(root)
            observed = []
            def checkpoint():
                observed.append(model.model_requests_used)
                note = guard.consider(checks(preserved=False), final=True)
                if note:
                    tools.source_revision += 1
                return "Protected regression: FAILED; " + note
            model = _make_client(None, None, [_FakeMessage([write_files(**{
                "frontend/src/provider.js": f"unfinished-{index}"})]) for index in range(20)])
            self.assertFalse(model.implement(task_type="web", subtree={"id": "ROOT"}, plan="Repair",
                project_tools=tools, repair_feedback="Repair behavior", checkpoint_feedback=checkpoint,
                request_allowance=30))
            self.assertEqual(model.model_requests_used, 12)
            self.assertEqual(observed, [12])
            self.assertEqual((root / "frontend/src/provider.js").read_text(), "old")
            self.assertEqual(model.last_implementation_stop_reason, "allocation")
            self.assertIn("request_allowance=12", model.last_implementation_stop_detail)
            self.assertTrue(model.last_implementation_deferred)
            self.assertIsNone(model.last_budget_report)

    def test_truncation_retry_does_not_exceed_last_repair_request(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools, _ = self.project(root)
            observed = []
            def checkpoint():
                observed.append(model.model_requests_used)
                return "Repair behavior: FAILED"
            model = _make_client(None, None, [
                *[_FakeMessage([write_files(**{"frontend/src/provider.js": f"edit-{i}"})]) for i in range(11)],
                _FakeMessage([write_files(**{"frontend/src/partial.js": "must not write"})], finish_reason="length"),
                _FakeMessage(None, "unused retry")],
                {"MODEL": "deepseek-flash", "OPENAI_BASE_URL": "https://api.deepseek.com"})
            self.assertFalse(model.implement(task_type="web", subtree={"id": "ROOT"}, plan="Repair",
                project_tools=tools, repair_feedback="Repair behavior", checkpoint_feedback=checkpoint))
            self.assertEqual(model.model_requests_used, 12)
            self.assertEqual(observed, [12])
            self.assertEqual(model.last_implementation_stop_reason, "allocation")
            self.assertFalse((root / "frontend/src/partial.js").exists())
            self.assertIsNone(model.last_budget_report)

    def test_delivery_reserve_boundary_verifies_once_and_cannot_spend_reserved_requests(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools, _ = self.project(root)
            observed = []
            model = _make_client(None, None, [])
            model.model_requests_used = model.max_model_requests - 3
            def checkpoint():
                observed.append(model.model_requests_used)
                # A callback that includes an audit must obey the same global
                # model guard, even at the local verification boundary.
                model._record_model_request()
                self.fail("A reserved model request was consumed")
            self.assertTrue(model.implement(task_type="web", subtree={"id": "ROOT"}, plan="Repair",
                project_tools=tools, repair_feedback="Repair behavior", checkpoint_feedback=checkpoint))
            self.assertEqual(observed, [247])
            self.assertEqual(model.model_requests_used, 247)
            self.assertEqual(model.client.completions.calls, 0)
            self.assertEqual(model.last_budget_report.budget, "delivery_request_reserve")
            self.assertEqual(model.last_implementation_stop_reason, "delivery_request_reserve")
            self.assertFalse(model.last_implementation_deferred)
            self.assertFalse(model.last_implementation_handoff)

    def test_restore_drops_old_tool_protocol_and_compacted_success_ledger(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools, guard = self.project(root)
            def checkpoint():
                note = guard.consider(checks(preserved=False))
                if note:
                    tools.source_revision += 1
                return "Remaining protected repair: FAILED; " + note
            model = _make_client(None, None, [
                _FakeMessage([_read_files_call(["frontend/src/provider.js"])]),
                _FakeMessage([write_files(**{"frontend/src/provider.js": "ROLLBACK_ONLY_SOURCE",
                    "frontend/src/rollback-new.js": "REMOVED_SOURCE"})]),
                _FakeMessage([_read_files_call(["frontend/src/provider.js", "frontend/src/rollback-new.js"])]),
                _FakeMessage([_read_files_call(["backend/src/consumer.js"])]),
                _FakeMessage([_read_files_call(["frontend/src/provider.js"])]),
                _FakeMessage(None, "return for repair")])
            with patch("agent.llm.MAX_CONVERSATION_CHARS", 1):
                self.assertFalse(model.implement(task_type="web", subtree={"id": "ROOT"}, plan="Implement",
                    project_tools=tools, checkpoint_feedback=checkpoint))
            after_restore = model.client.completions.requests[4]["messages"]
            context = json.dumps(after_restore)
            self.assertIn("controller restored an earlier candidate", context)
            self.assertNotIn("ROLLBACK_ONLY_SOURCE", context)
            self.assertNotIn("REMOVED_SOURCE", context)
            self.assertNotIn("rollback-new.js", context)
            self.assertFalse(any(message["role"] in {"assistant", "tool"} for message in after_restore))
            final_context = json.dumps(model.client.completions.requests[-1]["messages"])
            self.assertIn('old', final_context)
            self.assertNotIn("rollback-new.js", final_context)
            self.assertNotIn("ROLLBACK_ONLY_SOURCE", final_context)
            self.assertFalse((root / "frontend/src/rollback-new.js").exists())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agent.llm import BudgetExhaustion
from agent.orchestrator import _finalize_delivery, run_agent, run_model_agent
from agent.requirements import RequirementModule
from agent.verify import CheckResult, VerificationResult


def reserve():
    return BudgetExhaustion("delivery_token_reserve", 8_000_000, 7_600_000, 0, (), 200, 400, 200)


def candidate(*, build=True):
    return VerificationResult(False, (
        CheckResult("frontend: npm run build", build, 0 if build else 1, "build"),
        CheckResult("web template structure", True, 0, "layout"),
        CheckResult("frontend: npm run test", False, 1, "Registration feedback assertion failed"),
        CheckResult("agent generation budget", False, 1, "Delivery reserve reached"),
    ))


class DeliveryTests(unittest.TestCase):
    def test_runnable_candidate_preserves_failed_validation_and_report(self):
        with tempfile.TemporaryDirectory() as directory, patch(
                "agent.orchestrator.verify_public_acceptance",
                return_value=(CheckResult("web runtime startup", True, 0, "HTTP 200"),)) as probe:
            root = Path(directory)
            result = _finalize_delivery(root, candidate(), reserve())
            self.assertFalse(result.passed)
            self.assertTrue(result.ready_for_evaluation)
            probe.assert_called_once_with(root, {}, startup_only=True)
            report = json.loads((root / ".arc/delivery-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["formal_acceptance"], "pending")
            self.assertFalse(report["local_verification_passed"])
            self.assertIn("Registration feedback assertion failed", result.summary())

    def test_build_dependencies_layout_and_runtime_failures_block_delivery(self):
        cases = [candidate(build=False), VerificationResult(False, candidate().checks[2:]),
                 VerificationResult(False, (*candidate().checks,
                     CheckResult("project dependencies", False, 1, "install failed")))]
        for case in cases:
            with self.subTest(checks=case.checks), tempfile.TemporaryDirectory() as directory, patch(
                    "agent.orchestrator.verify_public_acceptance") as probe:
                result = _finalize_delivery(Path(directory), case, reserve())
                self.assertFalse(result.ready_for_evaluation)
                probe.assert_not_called()
        with tempfile.TemporaryDirectory() as directory, patch(
                "agent.orchestrator.verify_public_acceptance",
                return_value=(CheckResult("web runtime startup", False, 1, "Cannot start"),)):
            result = _finalize_delivery(Path(directory), candidate(), reserve())
            self.assertFalse(result.ready_for_evaluation)
            self.assertIn("Cannot start", result.summary())

    def test_normal_failure_and_early_tool_limit_do_not_enable_handoff(self):
        for budget in (None, BudgetExhaustion("tool_call_budget", 4, 4, 0, (), 1, 4, 1)):
            with tempfile.TemporaryDirectory() as directory, patch(
                    "agent.orchestrator.verify_public_acceptance") as probe:
                self.assertFalse(_finalize_delivery(Path(directory), candidate(), budget).ready_for_evaluation)
                probe.assert_not_called()

    def test_run_exit_allows_evaluation_without_reporting_tests_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
            config = SimpleNamespace(output_dir=Path(directory), requirement_dir=Path(directory), demo_mode=False)
            result = VerificationResult(False, candidate().checks, ready_for_evaluation=True)
            with patch("agent.orchestrator.load_requirement_tree", return_value={"id": "ROOT", "children": []}), \
                 patch("agent.orchestrator.run_model_agent", return_value=result):
                self.assertEqual(run_agent(runtime, config), 0)
            runtime.events.mark_run_failed.assert_not_called()
            runtime.events.mark_test_passed.assert_not_called()
            self.assertIn("did not fully pass", runtime.events.mark_run_completed.call_args.args[0])

    def test_budget_stop_runs_local_checks_and_handoff_without_model_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(task_type="web", max_model_turns=None, max_tool_calls=None,
                output_dir=root, requirement_dir=root / "requirements")
            tree = {"id": "ROOT", "children": [{"id": "REQ-1"}]}
            modules = [RequirementModule("REQ-1", "Example", tree["children"][0])]
            model = MagicMock()
            model.plan.return_value = "plan"
            model.last_budget_report = reserve()
            tools = SimpleNamespace(written_paths=[], ensure_dependencies=lambda: None)
            def implement(**kwargs):
                tools.written_paths.append("frontend/src/App.jsx")
                return True
            model.implement.side_effect = implement
            runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
            with patch("agent.orchestrator.ModelClient", return_value=model), \
                 patch("agent.orchestrator.ProjectTools", return_value=tools), \
                 patch("agent.orchestrator.verify_project", return_value=candidate()) as verify, \
                 patch("agent.orchestrator.verify_web_template_structure", return_value=VerificationResult(True, ())), \
                 patch("agent.orchestrator.verify_public_acceptance", return_value=(CheckResult("web runtime startup", True, 0, "OK"),)):
                result = run_model_agent(runtime, config, tree, modules)
            self.assertTrue(result.ready_for_evaluation)
            self.assertFalse(result.passed)
            verify.assert_called_once_with(root)
            model.review_requirements.assert_not_called()
            self.assertEqual(model.implement.call_count, 1)
            runtime.events.mark_test_passed.assert_not_called()
            runtime.traceability.set_test_pass_status.assert_called_once_with("TEST-REQ-1", False)


if __name__ == "__main__":
    unittest.main()

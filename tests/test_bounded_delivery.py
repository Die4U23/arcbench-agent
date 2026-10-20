
# Legacy fixtures exercise worker internals; host isolation is tested separately.
import unittest.mock as _sandbox_mock
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agent.candidate_checkpoint import CandidateCheckpoint, PUBLIC_CHECK
from agent.orchestrator import _finalize_delivery, run_agent, run_model_agent
from agent.requirements import RequirementModule
from agent.verify import CheckResult, VerificationResult


def candidate():
    return VerificationResult(False, (
        CheckResult("frontend: npm run build", True, 0, "built"),
        CheckResult("web template structure", True, 0, "layout"),
        CheckResult(PUBLIC_CHECK, False, 1, "required behavior remains failed"),
        CheckResult("agent generation stop", False, 1, "bounded slices ended"),
    ))


@_sandbox_mock.patch("agent.sandbox_context._WORKER", True)
class BoundedDeliveryTests(unittest.TestCase):
    def test_explicit_stop_keeps_failures_and_usage_without_claiming_acceptance(self):
        with tempfile.TemporaryDirectory() as directory, patch(
                "agent.orchestrator.verify_public_acceptance",
                return_value=(CheckResult("web runtime startup", True, 0, "HTTP 200"),)) as startup:
            root = Path(directory)
            result = _finalize_delivery(root, candidate(), None,
                generation_stop_reason="bounded_repair_exhausted")
            self.assertTrue(result.ready_for_evaluation)
            self.assertFalse(result.passed)
            self.assertIn("required behavior remains failed", result.summary())
            startup.assert_called_once_with(root, {}, startup_only=True)
            report = json.loads((root / ".arc/delivery-report.json").read_text())
            self.assertIsNone(report["budget"])
            self.assertFalse(report["local_verification_passed"])
            self.assertEqual(report["formal_acceptance"], "pending")
            self.assertEqual(report["handoff_reason"], "bounded repair slices exhausted")

    def test_arbitrary_or_single_slice_stop_cannot_enable_delivery(self):
        for reason in (None, "allocation", "no_progress", "repeated_tool_failure", "unknown"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory, patch(
                    "agent.orchestrator.verify_public_acceptance") as startup:
                result = _finalize_delivery(Path(directory), candidate(), None,
                    generation_stop_reason=reason)
                self.assertFalse(result.ready_for_evaluation)
                startup.assert_not_called()

    def test_missing_or_failed_infrastructure_blocks_bounded_stop_delivery(self):
        base = candidate().checks
        cases = [VerificationResult(False, base[1:]),
                 VerificationResult(False, (base[0], *base[2:]))]
        for name in ("frontend: npm run build", "web template structure", "project dependencies"):
            cases.append(VerificationResult(False, (
                *(check for check in base if check.name != name),
                CheckResult(name, False, 1, "infrastructure failed"))))
        for case in cases:
            with self.subTest(checks=case.checks), tempfile.TemporaryDirectory() as directory, patch(
                    "agent.orchestrator.verify_public_acceptance") as startup:
                result = _finalize_delivery(Path(directory), case, None,
                    generation_stop_reason="bounded_repair_exhausted")
                self.assertFalse(result.ready_for_evaluation)
                startup.assert_not_called()
        for startup_checks in ((), (CheckResult("web runtime startup", False, 1, "cannot start"),)):
            with self.subTest(startup=startup_checks), tempfile.TemporaryDirectory() as directory, patch(
                    "agent.orchestrator.verify_public_acceptance", return_value=startup_checks):
                result = _finalize_delivery(Path(directory), candidate(), None,
                    generation_stop_reason="bounded_repair_exhausted")
                self.assertFalse(result.ready_for_evaluation)

    def test_controller_exhaustion_finalizes_retained_source_without_paid_review(self):
        for complete_report in (True, False):
            with self.subTest(complete=complete_report), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "frontend/src/App.jsx"
                source.parent.mkdir(parents=True)
                source.write_text("baseline", encoding="utf-8")
                node = {"id": "REQ-3.2"}
                tree = {"id": "ROOT", "children": [node]}
                config = SimpleNamespace(task_type="web", max_model_turns=None, max_tool_calls=None,
                    output_dir=root, requirement_dir=root / "requirements", demo_mode=False)
                runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
                tools = SimpleNamespace(written_paths=[], source_revision=0, ensure_dependencies=lambda: None)
                model = MagicMock(plan=MagicMock(return_value="plan"))
                model.last_budget_report = None
                model.last_request_interruption = None
                model.last_implementation_deferred = False
                guard = CandidateCheckpoint(root)
                consider = MagicMock(wraps=guard.consider)
                guard.consider = consider
                repairs = []
                final_sources = []

                def implement(**kwargs):
                    tools.written_paths.append("frontend/src/App.jsx")
                    model.last_implementation_stop_reason = "no_progress"
                    if model.implement.call_count > 1:
                        repairs.append(kwargs["subtree"]["id"])
                        # A partial repair loses earlier protected behavior.
                        # Delivery must use the recovered source after recheck.
                        source.write_text("unfinished", encoding="utf-8")
                    return False

                def public(_project, _tree, **kwargs):
                    if kwargs.get("startup_only"):
                        final_sources.append(source.read_text())
                        return (CheckResult("web runtime startup", True, 0, "HTTP 200"),)
                    statuses = (("REQ-3.2: successful booking", "failed"),
                        ("REQ-1.1: existing account", "failed" if source.read_text() == "unfinished" else "passed"))
                    if not complete_report:
                        statuses = ()
                    return (CheckResult(PUBLIC_CHECK, False, 1, "booking still fails",
                        ("REQ-3.2",) if complete_report else (), statuses),)

                model.implement.side_effect = implement
                with ExitStack() as stack:
                    stack.enter_context(patch("agent.orchestrator.ModelClient", return_value=model))
                    stack.enter_context(patch("agent.orchestrator.ProjectTools", return_value=tools))
                    stack.enter_context(patch("agent.orchestrator.CandidateCheckpoint", return_value=guard))
                    stack.enter_context(patch("agent.orchestrator.verify_project", return_value=VerificationResult(
                        True, (CheckResult("frontend: npm run build", True, 0, "built"),))))
                    stack.enter_context(patch("agent.orchestrator.verify_web_template_structure",
                        return_value=VerificationResult(True, (CheckResult("web template structure", True, 0, "layout"),))))
                    stack.enter_context(patch("agent.orchestrator.verify_public_acceptance", side_effect=public))
                    visual = stack.enter_context(patch("agent.orchestrator.verify_visual_acceptance"))
                    result = run_model_agent(runtime, config, tree, [RequirementModule("ROOT", "all", tree)])
                self.assertEqual(repairs, ["REQ-3.2" if complete_report else "ROOT"])
                self.assertTrue(result.ready_for_evaluation)
                self.assertFalse(result.passed)
                self.assertIn("booking still fails", result.summary())
                self.assertTrue(any(call.kwargs.get("final") is True for call in consider.call_args_list))
                self.assertEqual(final_sources, ["baseline" if complete_report else "unfinished"])
                if complete_report:
                    self.assertEqual(tools.source_revision, 1)
                model.review_requirements.assert_not_called()
                visual.assert_not_called()
                self.assertIsNone(model.last_budget_report)
                report = json.loads((root / ".arc/delivery-report.json").read_text())
                self.assertEqual(report["generation_stop_reason"], "bounded_repair_exhausted")
                self.assertIsNone(report["budget"])
                with patch("agent.orchestrator.load_requirement_tree", return_value=tree), patch(
                        "agent.orchestrator.run_model_agent", return_value=result):
                    self.assertEqual(run_agent(runtime, config), 0)
                runtime.events.mark_run_failed.assert_not_called()
                runtime.events.mark_test_passed.assert_not_called()


if __name__ == "__main__":
    unittest.main()

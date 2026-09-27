from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.orchestrator import run_model_agent
from agent.requirements import RequirementModule
from agent.verify import CheckResult, VerificationResult


class BudgetExhaustionOrchestratorTests(unittest.TestCase):
    def test_model_run_prepares_web_directories_before_model_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            config = SimpleNamespace(
                task_type="web",
                max_model_turns=3,
                max_tool_calls=4,
                output_dir=output_dir,
            )

            with patch("agent.orchestrator.ModelClient", side_effect=RuntimeError("stop before API")):
                with self.assertRaisesRegex(RuntimeError, "stop before API"):
                    run_model_agent(SimpleNamespace(), config, {}, [])

            self.assertTrue((output_dir / "frontend").is_dir())
            self.assertTrue((output_dir / "backend").is_dir())

    def test_budget_exhaustion_still_verifies_and_records_test_status(self) -> None:
        passed = VerificationResult(True, (CheckResult("tests", True, 0, "ok"),))
        failed = VerificationResult(False, (CheckResult("tests", False, 1, "failure"),))
        scenarios = (
            ("passes without repair", [passed], True, 1),
            ("fails after repair", [failed, failed], False, 2),
        )

        for name, verification_results, expected_passed, expected_implement_calls in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                output_dir = Path(temp_dir) / "output"
                config = SimpleNamespace(
                    task_type="web",
                    max_model_turns=3,
                    max_tool_calls=4,
                    output_dir=output_dir,
                    requirement_dir=Path(temp_dir) / "requirements",
                )
                tree = {"id": "ROOT", "children": [{"id": "REQ-1", "name": "Example"}]}
                modules = [
                    RequirementModule(
                        node_id="REQ-1",
                        name="Example",
                        subtree=tree["children"][0],
                    )
                ]
                runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
                model = MagicMock()
                model.plan.return_value = "implementation plan"
                project_tools = SimpleNamespace(written_paths=[])

                def implement(*args, **kwargs):
                    project_tools.written_paths.append("index.html")
                    return True

                model.implement.side_effect = implement

                with (
                    patch("agent.orchestrator.ModelClient", return_value=model),
                    patch("agent.orchestrator.ProjectTools", return_value=project_tools),
                    patch("agent.orchestrator.verify_project", side_effect=verification_results) as verify,
                    patch(
                        "agent.orchestrator.verify_web_template_structure",
                        return_value=VerificationResult(
                            True,
                            (CheckResult("web template structure", True, 0, "ok"),),
                        ),
                    ),
                    self.assertLogs("arcbench_agent", level="WARNING") as logs,
                ):
                    result = run_model_agent(runtime, config, tree, modules)

                self.assertEqual(result.passed, expected_passed)
                self.assertEqual(verify.call_count, len(verification_results))
                self.assertEqual(model.implement.call_count, expected_implement_calls)
                self.assertTrue(any("reached the turn/tool-call budget" in line for line in logs.output))
                self.assertEqual(runtime.events.mark_test_passed.call_count, int(expected_passed))
                self.assertEqual(runtime.events.mark_test_failed.call_count, int(not expected_passed))
                runtime.traceability.upsert_test.assert_called_once_with(
                    test_id="TEST-REQ-1",
                    req_id="REQ-1",
                    type="INTEGRATION",
                    passed=expected_passed,
                )
                runtime.traceability.set_test_pass_status.assert_called_once_with(
                    "TEST-REQ-1",
                    expected_passed,
                )

    def test_missing_web_directories_trigger_repair_and_block_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                task_type="web",
                max_model_turns=3,
                max_tool_calls=4,
                output_dir=Path(temp_dir) / "output",
                requirement_dir=Path(temp_dir) / "requirements",
            )
            tree = {"id": "ROOT", "children": [{"id": "REQ-1", "name": "Example"}]}
            modules = [
                RequirementModule(
                    node_id="REQ-1",
                    name="Example",
                    subtree=tree["children"][0],
                )
            ]
            runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
            model = MagicMock()
            model.plan.return_value = "implementation plan"
            project_tools = SimpleNamespace(written_paths=[])

            def implement(*args, **kwargs):
                project_tools.written_paths.append("package.json")
                return False

            model.implement.side_effect = implement
            project_passed = VerificationResult(True, (CheckResult("tests", True, 0, "ok"),))
            structure_missing = VerificationResult(
                False,
                (CheckResult("web template structure", False, 1, "Missing frontend/ and backend/"),),
            )

            with (
                patch("agent.orchestrator.ModelClient", return_value=model),
                patch("agent.orchestrator.ProjectTools", return_value=project_tools),
                patch("agent.orchestrator.verify_project", return_value=project_passed),
                patch("agent.orchestrator.verify_web_template_structure", return_value=structure_missing),
                self.assertLogs("arcbench_agent", level="WARNING"),
            ):
                result = run_model_agent(runtime, config, tree, modules)

            self.assertFalse(result.passed)
            self.assertEqual(model.implement.call_count, 2)
            repair_feedback = model.implement.call_args.kwargs["repair_feedback"]
            self.assertIn("web template structure: FAILED", repair_feedback)
            self.assertIn("frontend/ and backend/", repair_feedback)
            runtime.events.mark_test_failed.assert_called_once()


if __name__ == "__main__":
    unittest.main()

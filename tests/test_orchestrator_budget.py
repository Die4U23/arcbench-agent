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


if __name__ == "__main__":
    unittest.main()

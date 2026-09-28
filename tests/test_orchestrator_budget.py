from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.llm import BudgetExhaustion
from agent.orchestrator import run_model_agent
from agent.requirements import RequirementModule
from agent.verify import CheckResult, VerificationResult


class BudgetExhaustionOrchestratorTests(unittest.TestCase):
    def test_repair_targets_requirement_named_by_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            nodes = [{"id": "REQ-1", "name": "First"}, {"id": "REQ-2", "name": "Second"}]
            tree = {"id": "ROOT", "children": nodes}
            modules = [RequirementModule(node_id=node["id"], name=node["name"], subtree=node) for node in nodes]
            config = SimpleNamespace(
                task_type="web", max_model_turns=None, max_tool_calls=None,
                output_dir=output_dir, requirement_dir=Path(temp_dir) / "requirements",
            )
            runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
            model = MagicMock()
            model.plan.return_value = "plan"
            tools = SimpleNamespace(written_paths=[])

            def implement(*args, **kwargs):
                tools.written_paths.append(f"frontend/{kwargs['subtree']['id']}.js")
                return False

            model.implement.side_effect = implement
            failed = VerificationResult(False, (
                CheckResult("frontend tests", False, 1, "REQ-2 booking form failed"),
                CheckResult("frontend build", True, 0, "large successful output"),
            ))
            passed = VerificationResult(True, (CheckResult("frontend tests", True, 0, "ok"),))
            structure = VerificationResult(True, (CheckResult("structure", True, 0, "ok"),))
            with (
                patch("agent.orchestrator.ModelClient", return_value=model),
                patch("agent.orchestrator.ProjectTools", return_value=tools),
                patch("agent.orchestrator.verify_project", side_effect=[failed, passed]),
                patch("agent.orchestrator.verify_web_template_structure", return_value=structure),
            ):
                result = run_model_agent(runtime, config, tree, modules)
            self.assertTrue(result.passed)
            self.assertEqual([call.kwargs["subtree"]["id"] for call in model.implement.call_args_list],
                             ["REQ-1", "REQ-2", "REQ-2"])
            repair_feedback = model.implement.call_args_list[-1].kwargs["repair_feedback"]
            self.assertIn("REQ-2 booking form failed", repair_feedback)
            self.assertNotIn("large successful output", repair_feedback)

    def test_visual_review_waits_for_build_and_structure_to_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            node = {"id": "REQ-1", "name": "Example"}
            tree = {"id": "ROOT", "visual_reference": ["reference.png"], "children": [node]}
            modules = [RequirementModule(node_id="REQ-1", name="Example", subtree=node)]
            config = SimpleNamespace(
                task_type="web", max_model_turns=None, max_tool_calls=None,
                output_dir=output_dir, requirement_dir=Path(temp_dir) / "requirements",
            )
            runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
            model = MagicMock()
            model.plan.return_value = "plan"
            tools = SimpleNamespace(written_paths=[])

            def implement(*args, **kwargs):
                tools.written_paths.append("frontend/index.html")
                return False

            model.implement.side_effect = implement
            failed = VerificationResult(False, (CheckResult("build", False, 1, "compile error"),))
            passed = VerificationResult(True, (CheckResult("build", True, 0, "ok"),))
            structure = VerificationResult(True, (CheckResult("structure", True, 0, "ok"),))
            with (
                patch("agent.orchestrator.ModelClient", return_value=model),
                patch("agent.orchestrator.ProjectTools", return_value=tools),
                patch("agent.orchestrator.verify_project", side_effect=[failed, passed]),
                patch("agent.orchestrator.verify_web_template_structure", return_value=structure),
                patch("agent.orchestrator.verify_visual_acceptance", return_value=passed) as visual,
            ):
                result = run_model_agent(runtime, config, tree, modules)
            self.assertTrue(result.passed)
            visual.assert_called_once()

    def test_repair_continues_while_verification_failures_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            config = SimpleNamespace(
                task_type="web",
                max_model_turns=None,
                max_tool_calls=None,
                output_dir=output_dir,
                requirement_dir=Path(temp_dir) / "requirements",
            )
            node = {"id": "REQ-1", "name": "Example"}
            tree = {"id": "ROOT", "children": [node]}
            modules = [RequirementModule(node_id="REQ-1", name="Example", subtree=node)]
            runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
            model = MagicMock()
            model.plan.return_value = "implementation plan"
            project_tools = SimpleNamespace(written_paths=[])

            def implement(*args, **kwargs):
                project_tools.written_paths.append("frontend/index.html")
                return False

            model.implement.side_effect = implement
            first = VerificationResult(False, (CheckResult("tests", False, 1, "error: missing form"),))
            second = VerificationResult(False, (CheckResult("tests", False, 1, "error: wrong response"),))
            passed = VerificationResult(True, (CheckResult("tests", True, 0, "ok"),))
            structure = VerificationResult(True, (CheckResult("web template structure", True, 0, "ok"),))

            with (
                patch("agent.orchestrator.ModelClient", return_value=model) as model_constructor,
                patch("agent.orchestrator.ProjectTools", return_value=project_tools),
                patch("agent.orchestrator.verify_project", side_effect=[first, second, passed]) as verify,
                patch("agent.orchestrator.verify_web_template_structure", return_value=structure),
                self.assertLogs("arcbench_agent", level="WARNING"),
            ):
                result = run_model_agent(runtime, config, tree, modules)

            self.assertTrue(result.passed)
            model_constructor.assert_called_once_with(max_turns=None, max_tool_calls=None)
            self.assertEqual(model.implement.call_count, 3)
            self.assertEqual(verify.call_count, 3)
            runtime.events.mark_test_passed.assert_called_once()

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

    def test_budget_exhaustion_stops_generation_and_never_reports_success(self) -> None:
        passed = VerificationResult(True, (CheckResult("tests", True, 0, "ok"),))
        failed = VerificationResult(False, (CheckResult("tests", False, 1, "failure"),))
        scenarios = (
            ("partial output passes checks", [passed], False, 1),
            ("partial output fails checks", [failed], False, 1),
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
                self.assertTrue(any("stopping further requirement generation" in line for line in logs.output))
                self.assertEqual(runtime.events.mark_test_passed.call_count, 0)
                self.assertEqual(runtime.events.mark_test_failed.call_count, 1)
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
            self.assertTrue(callable(model.implement.call_args_list[0].kwargs["checkpoint_feedback"]))
            repair_feedback = model.implement.call_args.kwargs["repair_feedback"]
            self.assertIn("web template structure: FAILED", repair_feedback)
            self.assertIn("frontend/ and backend/", repair_feedback)
            runtime.events.mark_test_failed.assert_called_once()

    def test_budget_exhaustion_stops_before_planning_the_next_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            requirement_dir = Path(temp_dir) / "requirements"
            first = {"id": "REQ-1", "name": "First"}
            second = {"id": "REQ-2", "name": "Second"}
            tree = {"id": "ROOT", "children": [first, second]}
            modules = [
                RequirementModule(node_id=node["id"], name=node["name"], subtree=node)
                for node in (first, second)
            ]
            config = SimpleNamespace(
                task_type="web",
                max_model_turns=3,
                max_tool_calls=4,
                output_dir=output_dir,
                requirement_dir=requirement_dir,
            )
            runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
            model = MagicMock()
            model.plan.return_value = "plan"
            model.last_budget_report = BudgetExhaustion(
                budget="tool_call_budget",
                limit=4,
                used=4,
                requested=2,
                tool_names=("read_files", "write_files"),
                turns_used=3,
                tool_calls_used=4,
                model_requests_used=6,
            )
            project_tools = SimpleNamespace(written_paths=[])

            def exhaust_after_partial_write(*args, **kwargs):
                project_tools.written_paths.append("frontend/index.html")
                return True

            model.implement.side_effect = exhaust_after_partial_write
            passed = VerificationResult(True, (CheckResult("build", True, 0, "ok"),))

            with (
                patch("agent.orchestrator.ModelClient", return_value=model),
                patch("agent.orchestrator.ProjectTools", return_value=project_tools),
                patch("agent.orchestrator.verify_project", return_value=passed) as verify,
                patch(
                    "agent.orchestrator.verify_web_template_structure",
                    return_value=VerificationResult(
                        True,
                        (CheckResult("web template structure", True, 0, "ok"),),
                    ),
                ),
                self.assertLogs("arcbench_agent", level="WARNING"),
            ):
                result = run_model_agent(runtime, config, tree, modules)

            self.assertFalse(result.passed)
            self.assertEqual(model.plan.call_count, 1)
            self.assertEqual(model.implement.call_count, 1)
            verify.assert_called_once_with(output_dir)
            self.assertEqual(project_tools.written_paths, ["frontend/index.html"])
            self.assertIn("requested_tool_calls=2", result.summary())
            self.assertIn("total_model_requests=6", result.summary())


if __name__ == "__main__":
    unittest.main()

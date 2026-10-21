"""No provider requests: regressions for the platform repair storm."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agent.container_checks import run_checks
from agent.orchestrator import _prepare_local_candidate, run_model_agent
from agent.sandbox_process import SandboxInfrastructureError
from agent.tools import ProjectTools
from tests.test_llm_budget import _FakeMessage, _FakeToolCall, _make_client


class InfrastructureStopTests(unittest.TestCase):
    def test_environment_failure_escapes_batched_scripts_and_check_wrappers(self):
        with tempfile.TemporaryDirectory() as folder:
            tools = ProjectTools(Path(folder))
            error = SandboxInfrastructureError("offline", reason="dependency_cache_missing")
            with patch.object(tools, "run_project_script", side_effect=error) as script:
                with self.assertRaises(SandboxInfrastructureError):
                    tools.run_project_scripts([{"directory": ".", "script": "test"}] * 2)
                self.assertEqual(script.call_count, 1)
            with patch("agent.container_checks.DockerSandbox", side_effect=error):
                with self.assertRaises(SandboxInfrastructureError):
                    run_checks("verify_project", Path(folder))
            candidate = MagicMock()
            with patch.object(tools, "ensure_dependencies", side_effect=error) as install:
                with self.assertRaises(SandboxInfrastructureError):
                    _prepare_local_candidate(SimpleNamespace(output_dir=Path(folder)), {}, candidate, tools)
                self.assertEqual(install.call_count, 1)
                candidate.consider.assert_not_called()

    def test_model_tool_failure_stops_before_another_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            tools = ProjectTools(Path(folder))
            tools.write_file("app.js", "existing source")
            call = _FakeToolCall("scripts", "run_project_scripts", json.dumps({"scripts": [{"directory": ".", "script": "test"}]}))
            model = _make_client(None, None, [_FakeMessage([call]), _FakeMessage(None)])
            with patch.object(tools, "run_project_script", side_effect=SandboxInfrastructureError("disconnected")):
                with self.assertRaises(SandboxInfrastructureError):
                    model.implement(task_type="web", subtree={"id": "ROOT"}, plan="repair",
                        project_tools=tools, repair_feedback="existing failure", request_allowance=12)
            self.assertEqual(model.model_requests_used, 1)

    def test_initial_failure_stops_all_modules_and_records_circuit_without_model_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "frontend/src").mkdir(parents=True)
            (root / "frontend/src/app.js").write_text("existing source")
            config = SimpleNamespace(output_dir=root, requirement_dir=root / "requirements",
                task_type="web", max_model_turns=None, max_tool_calls=None)
            model = MagicMock(model_requests_used=0, prompt_tokens_used=0, completion_tokens_used=0)
            runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
            with patch("agent.orchestrator.ModelClient", return_value=model), \
                 patch.object(ProjectTools, "ensure_dependencies", side_effect=SandboxInfrastructureError("closed", reason="sandbox_transport_failed")) as install, \
                 patch("agent.orchestrator._record_verification"), \
                 self.assertLogs("agent.request_metrics", level="INFO") as logs:
                result = run_model_agent(runtime, config, {"id": "ROOT"}, [])
            self.assertFalse(result.passed)
            self.assertEqual(install.call_count, 1)
            model.plan.assert_not_called()
            model.implement.assert_not_called()
            model.review_requirements.assert_not_called()
            model.log_usage.assert_called_once()
            record = json.loads((root / ".arc/infrastructure-failure.json").read_text())
            self.assertEqual(record["action"], "stop_run")
            self.assertEqual(record["retry_count"], 0)
            self.assertEqual(record["reason"], "sandbox_transport_failed")
            self.assertTrue(record["first_location"]["file"])
            self.assertEqual(record["scope"], "run")
            self.assertIn("repair_circuit_opened", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()

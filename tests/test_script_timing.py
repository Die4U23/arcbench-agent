"""Script timing separates preparation from execution without leaking payloads."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent.tools import ProjectTools


class ScriptTimingTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.frontend = self.root / "frontend"
        self.frontend.mkdir()
        self.tools = ProjectTools(self.root)
        (self.frontend / "package.json").write_text(json.dumps({
            "dependencies": {"example": "1.0.0"},
            "scripts": {"build": "node build.js --token MANIFEST_SECRET_SENTINEL"}}), encoding="utf-8")

    def test_real_dependency_path_and_command_have_independent_timings(self):
        completed = subprocess.CompletedProcess([], 0, "OUTPUT_SECRET_SENTINEL", "")
        with patch("agent.tools.shutil.which", return_value="npm"), \
             patch("agent.tools.subprocess.run", return_value=completed) as run, \
             patch("agent.tools.time.perf_counter", side_effect=[10, 100, 101, 103]), \
             self.assertLogs("agent.tools", level="INFO") as logs:
            result = json.loads(self.tools.run_project_script("./frontend", "build"))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][1], "install")
        self.assertEqual(run.call_args_list[1].args[0], ["npm", "run", "build"])
        self.assertEqual(result, {"command": "npm run build", "directory": "./frontend",
                                  "exit_code": 0, "output": "OUTPUT_SECRET_SENTINEL"})
        message = "\n".join(logs.output)
        self.assertIn('directory="frontend" script=build exit_code=0', message)
        self.assertIn("dependency_seconds=90.000 command_seconds=2.000 status=command_completed", message)
        for private in ("MANIFEST_SECRET_SENTINEL", "OUTPUT_SECRET_SENTINEL", str(self.root), "--token"):
            self.assertNotIn(private, message)

    def test_nonzero_exit_keeps_original_result_and_logs_real_code(self):
        completed = subprocess.CompletedProcess([], 7, "", "build failure")
        with patch.object(self.tools, "ensure_dependencies"), \
             patch("agent.tools.shutil.which", return_value="npm"), \
             patch("agent.tools.subprocess.run", return_value=completed), \
             patch("agent.tools.time.perf_counter", side_effect=[0, 0.5, 1, 4]), \
             self.assertLogs("agent.tools", level="INFO") as logs:
            result = json.loads(self.tools.run_project_script("frontend", "build"))
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["output"], "build failure")
        self.assertIn("exit_code=7 dependency_seconds=0.500 command_seconds=3.000", logs.output[0])

    def test_dependency_failure_records_no_command_and_preserves_exception(self):
        failure = RuntimeError("DEPENDENCY_SECRET_SENTINEL")
        with patch.object(self.tools, "ensure_dependencies", side_effect=failure), \
             patch("agent.tools.subprocess.run") as run, \
             patch("agent.tools.time.perf_counter", side_effect=[1, 6]), \
             self.assertLogs("agent.tools", level="INFO") as logs:
            with self.assertRaises(RuntimeError) as caught:
                self.tools.run_project_script("frontend", "build")
        self.assertIs(caught.exception, failure)
        run.assert_not_called()
        self.assertIn("exit_code=None dependency_seconds=5.000 command_seconds=not_started status=dependency_error", logs.output[0])
        self.assertNotIn("DEPENDENCY_SECRET_SENTINEL", logs.output[0])

    def test_timeout_records_elapsed_command_without_leaking_exception_payload(self):
        failure = subprocess.TimeoutExpired("COMMAND_SECRET_SENTINEL", 120, output="TIMEOUT_OUTPUT_SECRET")
        with patch.object(self.tools, "ensure_dependencies"), \
             patch("agent.tools.shutil.which", return_value="npm"), \
             patch("agent.tools.subprocess.run", side_effect=failure), \
             patch("agent.tools.time.perf_counter", side_effect=[0, 2, 3, 123]), \
             self.assertLogs("agent.tools", level="INFO") as logs:
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                self.tools.run_project_script("frontend", "build")
        self.assertIs(caught.exception, failure)
        self.assertIn("exit_code=None dependency_seconds=2.000 command_seconds=120.000 status=command_error", logs.output[0])
        self.assertNotIn("SECRET", logs.output[0])

    def test_missing_npm_reports_command_not_started(self):
        with patch.object(self.tools, "ensure_dependencies"), \
             patch("agent.tools.shutil.which", return_value=None), \
             patch("agent.tools.subprocess.run") as run, \
             patch("agent.tools.time.perf_counter", side_effect=[0, 0.25]), \
             self.assertLogs("agent.tools", level="INFO") as logs:
            with self.assertRaisesRegex(RuntimeError, "npm was not found on PATH"):
                self.tools.run_project_script("frontend", "build")
        run.assert_not_called()
        self.assertIn("command_seconds=not_started status=command_not_started", logs.output[0])

    def test_unapproved_script_is_rejected_before_timing_or_execution(self):
        with patch.object(self.tools, "ensure_dependencies") as prepare, \
             patch("agent.tools.subprocess.run") as run, self.assertNoLogs("agent.tools", level="INFO"):
            with self.assertRaisesRegex(ValueError, "Script is not allowed"):
                self.tools.run_project_script("frontend", "build --token SECRET")
        prepare.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

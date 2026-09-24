from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.tools import ProjectTools
from agent.verify import _run_npm_script


class SubprocessOutputEncodingTests(unittest.TestCase):
    def test_project_tool_decodes_utf8_output_without_locale_failures(self) -> None:
        project = Path(__file__).parent / "fixtures" / "subprocess-project"
        completed = subprocess.CompletedProcess("npm", 0, stdout="✓ build passed", stderr="")
        with patch("agent.tools.shutil.which", return_value="npm.cmd"), patch(
            "agent.tools.subprocess.run", return_value=completed
        ) as run:
            result = json.loads(ProjectTools(project).run_project_script(".", "build"))

        self.assertEqual(result["exit_code"], 0)
        self.assertIn("build passed", result["output"])
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_project_verifier_decodes_utf8_output_without_locale_failures(self) -> None:
        package_dir = Path(__file__).parent / "fixtures" / "subprocess-project"
        completed = subprocess.CompletedProcess("npm", 0, stdout="✓ build passed", stderr="")
        with patch("agent.verify.shutil.which", return_value="npm.cmd"), patch(
            "agent.verify.subprocess.run", return_value=completed
        ) as run:
            result = _run_npm_script(package_dir, "build")

        self.assertTrue(result.passed)
        self.assertIn("build passed", result.output)
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")


if __name__ == "__main__":
    unittest.main()

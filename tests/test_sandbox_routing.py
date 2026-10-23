import base64
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.container_visual import verify_isolated_visual
from agent.tools import ProjectTools
from agent.verify import verify_project
from agent.native_verification import verify_public_acceptance
from agent.browser_contract import verify_browser_contract
from agent.station_units import verify_station_units


def check(name, passed):
    return {
        "name": name,
        "passed": passed,
        "exit_code": 0 if passed else 1,
        "output": "fixture",
        "requirement_ids": [],
        "case_statuses": [],
    }


class SandboxRoutingTests(unittest.TestCase):
    def test_project_tool_never_falls_back_when_docker_fails(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("agent.sandbox_context._WORKER", False),
            patch(
                "agent.docker_sandbox.DockerSandbox.invoke",
                side_effect=RuntimeError("unavailable"),
            ),
            patch("agent.docker_sandbox.shutil.which", return_value="/docker"),
            patch("agent.tools.subprocess.run") as host_run,
        ):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                ProjectTools(Path(temporary)).run_project_script(".", "test")
            host_run.assert_not_called()

    def test_script_and_verification_dispatch_preserve_parameters(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("agent.sandbox_context._WORKER", False),
            patch("agent.docker_sandbox.shutil.which", return_value="/docker"),
            patch("agent.docker_sandbox.DockerSandbox.invoke") as invoke,
        ):
            root = Path(temporary)
            invoke.return_value = {"exit_code": 0, "output": "fixture"}
            self.assertEqual(
                json.loads(ProjectTools(root).run_project_script(".", "test"))[
                    "exit_code"
                ],
                0,
            )
            self.assertEqual(invoke.call_args.args[0], "project_script")
            self.assertEqual(
                invoke.call_args.kwargs["options"], {"directory": ".", "script": "test"}
            )
            invoke.return_value = {"checks": [check("build", True)]}
            self.assertTrue(verify_project(root, include_root=False).passed)
            self.assertEqual(invoke.call_args.args[0], "verify_project")
            self.assertEqual(
                invoke.call_args.kwargs["options"], {"include_root": False}
            )

    def test_public_native_is_one_isolated_session(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("agent.sandbox_context._WORKER", False),
            patch("agent.docker_sandbox.shutil.which", return_value="/docker"),
            patch(
                "agent.docker_sandbox.DockerSandbox.invoke",
                return_value={"checks": [check("native", True)]},
            ) as invoke,
            patch(
                "agent.native_verification.original_public_acceptance"
            ) as host_public,
        ):
            result = verify_public_acceptance(Path(temporary), {}, startup_only=True)
            self.assertTrue(result[0].passed)
            self.assertEqual(invoke.call_args.args[0], "native")
            host_public.assert_not_called()

    def test_browser_probes_refuse_standalone_host_execution(self):
        with patch("agent.sandbox_context._WORKER", False):
            self.assertFalse(verify_browser_contract(Path("/unused")).passed)
            self.assertFalse(verify_station_units(Path("/unused")).passed)


class IsolatedVisualReviewTests(unittest.TestCase):
    def fixture(self, root):
        manifest = {
            "version": 1,
            "cases": [
                {
                    "name": "page",
                    "reference": "reference.png",
                    "route": "/",
                    "viewport": {"width": 800, "height": 600},
                    "visual_expectations": ["Visible form"],
                }
            ],
            "flows": [
                {
                    "name": "fill",
                    "route": "/",
                    "viewport": {"width": 800, "height": 600},
                    "covers": ["#x"],
                    "steps": [
                        {"action": "fill", "selector": "#x", "value": "a"},
                        {"action": "expect_value", "selector": "#x", "value": "a"},
                    ],
                }
            ],
        }
        (root / "arcbench-visual-acceptance.json").write_text(json.dumps(manifest))
        return {
            "checks": [
                check("visual reference: page", False),
                check("browser interaction: fill", True),
            ],
            "captures": [
                {
                    "name": "page",
                    "reference": "reference.png",
                    "png": base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode(),
                }
            ],
            "report": {
                "version": 1,
                "cases": [{"name": "page", "verdict": "capture_pending"}],
                "flows": [{"name": "fill", "passed": True}],
            },
        }

    def test_only_host_model_can_approve_captured_reference(self):
        calls = []

        def review(**kwargs):
            calls.append(kwargs)
            return {"verdict": "pass", "issues": [], "matched": ["form"]}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.fixture(root)
            model = SimpleNamespace(review_visual_case=review, api_key="FAKE_SECRET")
            with patch("agent.container_visual.DockerSandbox") as docker:
                docker.return_value.invoke.return_value = result
                checked = verify_isolated_visual(root, root, ["reference.png"], model)
                self.assertNotIn(
                    "FAKE_SECRET", repr(docker.return_value.invoke.call_args)
                )
            self.assertTrue(checked.passed)
            self.assertEqual(calls[0]["reference_dir"], root)
            self.assertEqual(calls[0]["case"]["name"], "page")

    @unittest.skipUnless(os.name == "posix", "symlink fixture requires POSIX")
    def test_failed_capture_does_not_write_report_through_host_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            outside = Path(temporary) / "outside"
            outside.mkdir()
            self.fixture(root)
            (root / "artifacts").symlink_to(outside, target_is_directory=True)
            with patch("agent.container_visual.DockerSandbox") as docker:
                docker.return_value.invoke.side_effect = ValueError("unsafe workspace")
                checked = verify_isolated_visual(root, root, ["reference.png"], None)
            self.assertFalse(checked.passed)
            self.assertEqual(list(outside.iterdir()), [])

    def test_worker_pass_verdict_is_not_accepted_as_model_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.fixture(root)
            result["report"]["cases"][0]["verdict"] = "pass"
            with patch("agent.container_visual.DockerSandbox") as docker:
                docker.return_value.invoke.return_value = result
                checked = verify_isolated_visual(root, root, ["reference.png"], None)
            self.assertFalse(checked.passed)
            self.assertIn("cannot approve", checked.checks[-1].output)


if __name__ == "__main__":
    unittest.main()

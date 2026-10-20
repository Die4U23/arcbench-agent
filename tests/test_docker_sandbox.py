import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.docker_sandbox import DockerSandbox
from agent.sandbox_process import BoundedResult, SandboxProcessError


class DockerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "app.js").write_bytes(b"old")
        (self.root / ".env").write_text("FAKE_API_KEY=do-not-forward")
        self.calls = []

    def fake_command(self, command, **kwargs):
        self.calls.append((command, kwargs))
        args = command[1:]
        if args[0] == "context":
            return BoundedResult(0, b"unix:///var/run/docker.sock", b"")
        if args[0] == "info":
            return BoundedResult(0, b"linux", b"")
        if args[0] == "ps":
            return BoundedResult(0, b"", b"")
        if args[0] == "run":
            mount = args[args.index("--mount") + 1]
            request = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
            payload = json.loads(request.read_text())
            self.assertNotIn(".env", payload["files"])
            response = {
                "version": 1,
                "result": {"ok": True},
                "files": {"app.js": base64.b64encode(b"new").decode()},
            }
            return BoundedResult(0, json.dumps(response).encode(), b"")
        raise AssertionError(args)

    def test_locked_runtime_flags_minimal_env_and_safe_export(self):
        with (
            patch.dict(os.environ, {"FAKE_API_KEY": "secret"}),
            patch("agent.docker_sandbox.shutil.which", return_value="/docker"),
            patch("agent.docker_sandbox.run_bounded", side_effect=self.fake_command),
        ):
            result = DockerSandbox().invoke(
                "project_script", self.root, options={"script": "test"}
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual((self.root / "app.js").read_bytes(), b"new")
        run, kwargs = next(call for call in self.calls if call[0][1] == "run")
        self.assertEqual(run[run.index("--network") + 1], "none")
        self.assertEqual(run[run.index("--user") + 1], "1000:1000")
        self.assertEqual(run[run.index("--cap-drop") + 1], "ALL")
        self.assertIn("--read-only", run)
        self.assertIn("--pull=never", run)
        self.assertNotIn("FAKE_API_KEY", kwargs["env"])
        self.assertNotIn(str(self.root), " ".join(run))
        self.assertFalse(any("/var/run/docker.sock" in arg for arg in run))

    def test_missing_docker_never_falls_back_to_host(self):
        with patch("agent.docker_sandbox.shutil.which", return_value=None):
            with self.assertRaisesRegex(SandboxProcessError, "fallback is disabled"):
                DockerSandbox()

    def test_remote_engine_is_rejected_before_project_execution(self):
        with (
            patch("agent.docker_sandbox.shutil.which", return_value="/docker"),
            patch(
                "agent.docker_sandbox.run_bounded",
                return_value=BoundedResult(0, b"ssh://remote", b""),
            ) as run,
        ):
            with self.assertRaisesRegex(SandboxProcessError, "local Docker"):
                DockerSandbox().invoke("project_script", self.root)
        self.assertEqual(run.call_count, 1)

    def test_timeout_removes_only_owned_container_and_imports_nothing(self):
        identifier = "a" * 64
        exists = [True]

        def runner(command, **kwargs):
            if command[1] == "run":
                raise SandboxProcessError("injected deadline")
            if command[1] == "ps":
                self.assertIn("label=arcbench.sandbox.owner=", " ".join(command))
                return BoundedResult(0, identifier.encode() if exists[0] else b"", b"")
            if command[1] == "rm":
                self.assertEqual(command, ["/docker", "rm", "--force", identifier])
                exists[0] = False
                return BoundedResult(0, b"", b"")
            return self.fake_command(command, **kwargs)

        with (
            patch("agent.docker_sandbox.shutil.which", return_value="/docker"),
            patch("agent.docker_sandbox.run_bounded", side_effect=runner),
        ):
            with self.assertRaisesRegex(SandboxProcessError, "injected deadline"):
                DockerSandbox().invoke("project_script", self.root)
        self.assertFalse(exists[0])
        self.assertEqual((self.root / "app.js").read_bytes(), b"old")

    def test_auto_remove_race_is_accepted_only_after_absence_is_verified(self):
        lists = [0]

        def runner(command, **kwargs):
            if command[1] == "ps":
                lists[0] += 1
                return BoundedResult(0, b"a" * 64 if lists[0] == 1 else b"", b"")
            if command[1] == "rm":
                return BoundedResult(1, b"", b"No such container")
            return self.fake_command(command, **kwargs)

        with (
            patch("agent.docker_sandbox.shutil.which", return_value="/docker"),
            patch("agent.docker_sandbox.run_bounded", side_effect=runner),
        ):
            DockerSandbox().invoke("project_script", self.root)
        self.assertGreaterEqual(lists[0], 3)
        self.assertEqual((self.root / "app.js").read_bytes(), b"new")

    def test_unverified_cleanup_prevents_output_import(self):
        def runner(command, **kwargs):
            if command[1] == "ps":
                return BoundedResult(1, b"", b"daemon unreachable")
            return self.fake_command(command, **kwargs)

        with (
            patch("agent.docker_sandbox.shutil.which", return_value="/docker"),
            patch("agent.docker_sandbox.run_bounded", side_effect=runner),
        ):
            with self.assertRaisesRegex(SandboxProcessError, "cleanup"):
                DockerSandbox().invoke("project_script", self.root)
        self.assertEqual((self.root / "app.js").read_bytes(), b"old")

    def test_malformed_worker_result_does_not_apply_files(self):
        def runner(command, **kwargs):
            if command[1] == "run":
                return BoundedResult(
                    0, b'{"version":1,"result":{},"files":{"../escape":"eA=="}}', b""
                )
            return self.fake_command(command, **kwargs)

        with (
            patch("agent.docker_sandbox.shutil.which", return_value="/docker"),
            patch("agent.docker_sandbox.run_bounded", side_effect=runner),
        ):
            with self.assertRaises(ValueError):
                DockerSandbox().invoke("project_script", self.root)
        self.assertEqual((self.root / "app.js").read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()

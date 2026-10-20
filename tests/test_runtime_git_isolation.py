import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from arcbench_agent_runtime.gitops import GitClient
from agent.tools import ProjectTools


class RuntimeGitIsolationTests(unittest.TestCase):
    def test_git_receives_identity_but_not_model_tokens_or_config_injections(self):
        client = GitClient(
            SimpleNamespace(project_dir=Path("/unused")), SimpleNamespace()
        )
        fake = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "FAKE_SECRET",
                    "AWS_SECRET_ACCESS_KEY": "FAKE_AWS",
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "core.hooksPath",
                    "GIT_CONFIG_VALUE_0": "/untrusted",
                    "ARC_GIT_USER_NAME": "Test User",
                },
            ),
            patch(
                "arcbench_agent_runtime.gitops.subprocess.run", return_value=fake
            ) as run,
        ):
            client.run(["status", "--short"])
        command = run.call_args.args[0]
        env = run.call_args.kwargs["env"]
        self.assertIn("core.hooksPath=" + os.devnull, command)
        self.assertIn("core.fsmonitor=false", command)
        self.assertEqual(env["GIT_AUTHOR_NAME"], "Test User")
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        for name in (
            "OPENAI_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
        ):
            self.assertNotIn(name, env)

    def test_model_file_tools_cannot_write_git_or_npm_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            tools = ProjectTools(Path(temporary))
            for name in (
                ".git/config",
                ".GIT/hooks/pre-commit",
                ".gitattributes",
                ".gitmodules",
                ".npmrc",
                ".docker/config.json",
                ".envrc",
                "id_ecdsa",
            ):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    tools.write_file(name, "untrusted")


if __name__ == "__main__":
    unittest.main()

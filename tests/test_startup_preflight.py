import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agent.docker_sandbox import DockerSandbox
from agent.sandbox_process import SandboxProcessError


class StartupPreflightTests(unittest.TestCase):
    def entrypoint(self):
        spec = importlib.util.spec_from_file_location('entrypoint_preflight_test', Path(__file__).parents[1] / 'main.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_missing_sandbox_stops_before_any_model_or_generation_work(self):
        with patch.dict(os.environ, {'MODEL':'deepseek-flash','VISUAL_MODEL':'deepseek-flash','VISUAL_REVIEW_MODEL':'deepseek-flash'}):
            entry = self.entrypoint()
            runtime = MagicMock()
            config = SimpleNamespace(output_dir=Path('unused'), demo_mode=False)
            with patch.object(entry, 'parse_config', return_value=config), \
                 patch.object(entry.logging, 'basicConfig'), \
                 patch.object(entry.AgentRuntime, 'from_env', return_value=runtime), \
                 patch.object(entry, 'run_agent') as run, \
                 patch.object(entry, 'DockerSandbox') as sandbox:
                sandbox.return_value.check_available.side_effect = SandboxProcessError('missing trusted image')
                self.assertEqual(entry.main(), 1)
                run.assert_not_called()
                runtime.events.mark_run_failed.assert_called_once()

    def test_demo_does_not_require_a_model_execution_sandbox(self):
        with patch.dict(os.environ, {'MODEL':'deepseek-flash','VISUAL_MODEL':'deepseek-flash','VISUAL_REVIEW_MODEL':'deepseek-flash'}):
            entry = self.entrypoint()
            config = SimpleNamespace(output_dir=Path('unused'), demo_mode=True)
            with patch.object(entry, 'parse_config', return_value=config), \
                 patch.object(entry.logging, 'basicConfig'), \
                 patch.object(entry.AgentRuntime, 'from_env'), \
                 patch.object(entry, 'run_agent', return_value=0), \
                 patch.object(entry, 'DockerSandbox') as sandbox:
                self.assertEqual(entry.main(), 0)
                sandbox.assert_not_called()

    def test_preflight_checks_the_image_without_starting_a_container(self):
        with patch('agent.docker_sandbox.shutil.which', return_value='docker'):
            sandbox = DockerSandbox()
        with patch.object(sandbox, '_preflight') as engine, patch.object(sandbox, '_command') as command:
            command.return_value = SimpleNamespace(returncode=1, stdout=b'')
            with self.assertRaisesRegex(SandboxProcessError, 'image is unavailable'):
                sandbox.check_available()
            engine.assert_called_once()
            self.assertEqual(command.call_args.args[0][:2], ['image','inspect'])
            command.return_value = SimpleNamespace(returncode=0, stdout=b'sha256:abc')
            sandbox.check_available()


if __name__ == '__main__':
    unittest.main()

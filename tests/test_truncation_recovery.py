"""Reproduce the terminal length/length failure through the real orchestrator."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agent.orchestrator import run_model_agent
from agent.requirements import RequirementModule
from agent.verify import CheckResult, VerificationResult
from tests.test_llm_budget import _FakeMessage, _make_client, _write_call


class TruncationRecoveryTests(unittest.TestCase):
    def test_truncated_module_without_edits_continues_but_does_not_pass_checks(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tree = {'id': 'ROOT', 'children': [{'id': 'REQ-1'}]}
            model = _make_client(None, None, [
                _FakeMessage([_write_call('partial.js')], finish_reason='length'),
                _FakeMessage([_write_call('retry-partial.js')], finish_reason='length'),
                _FakeMessage(None, 'integration incomplete'),
                _FakeMessage(None, 'repair incomplete'),
            ], {'MODEL': 'deepseek-flash', 'OPENAI_BASE_URL': 'https://api.deepseek.com'})
            create = model.client.completions.create
            def completion(**kwargs):
                response = create(**kwargs)
                # Include rejected outputs in actual model accounting.
                response.usage = SimpleNamespace(prompt_tokens=100, completion_tokens=200)
                return response
            model.client.completions.create = completion
            model.plan = MagicMock(return_value='plan')
            model.log_usage = MagicMock()
            failure = VerificationResult(False, (
                CheckResult('frontend: npm run build', False, 1, 'App is incomplete'),))
            layout = VerificationResult(True, (
                CheckResult('web template structure', True, 0, 'layout'),))
            runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
            config = SimpleNamespace(task_type='web', max_model_turns=None, max_tool_calls=None,
                output_dir=root, requirement_dir=root/'requirements')
            with patch('agent.orchestrator.ModelClient', return_value=model), \
                 patch('agent.tools.ProjectTools.ensure_dependencies'), \
                 patch('agent.orchestrator.verify_project', return_value=failure) as verify, \
                 patch('agent.orchestrator.verify_web_template_structure', return_value=layout), \
                 patch('agent.orchestrator.verify_public_acceptance', return_value=()):
                result = run_model_agent(runtime, config, tree,
                    [RequirementModule('REQ-1', 'Incomplete', tree['children'][0])])
            self.assertFalse(result.passed)
            self.assertFalse(result.ready_for_evaluation)
            self.assertIn('App is incomplete', result.summary())
            self.assertGreaterEqual(verify.call_count, 2)
            self.assertEqual(model.model_requests_used, 4)
            self.assertEqual(model.prompt_tokens_used, 400)
            self.assertEqual(model.completion_tokens_used, 800)
            self.assertFalse((root/'partial.js').exists())
            self.assertFalse((root/'retry-partial.js').exists())
            runtime.events.mark_implementation_done.assert_not_called()
            model.log_usage.assert_called_once()


if __name__ == '__main__':
    unittest.main()

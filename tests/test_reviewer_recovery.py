from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.llm import ModelBudgetExceeded, ModelClient, RequirementReviewError
from agent.orchestrator import _finalize_delivery, run_model_agent
from agent.requirements import RequirementModule
from agent.tools import ProjectTools
from agent.verify import CheckResult, VerificationResult
from test_llm_budget import _FakeMessage, _make_client, _repair_review


class RequirementReviewerRecoveryTests(unittest.TestCase):
    def test_fabricated_source_is_reaudited_once_against_same_source_then_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = ProjectTools(Path(directory))
            tools.write_file('frontend/src/App.jsx', 'export default function App() { return null; }')
            model = _make_client(None, None, [
                _FakeMessage(None, _repair_review('invented', evidence='invented_source()')),
                _FakeMessage(None, _repair_review('required rendering missing')),
            ])
            feedback = model.review_requirements({'id': 'ROOT', 'description': 'test task'}, tools)
            self.assertEqual(model.model_requests_used, 2)
            self.assertIn('required rendering missing', feedback)
            self.assertNotIn('invented', feedback)
            requests = model.client.completions.requests
            correction = requests[1]['messages'][-1]['content']
            self.assertIn('outside the supplied production source', correction)
            self.assertIn('same supplied requirements', correction)
            self.assertEqual(model.review_requirements({'id': 'ROOT', 'description': 'test task'}, tools), feedback)
            self.assertEqual(model.model_requests_used, 2)

    def test_repeated_invalid_json_and_evidence_never_become_pass_or_cached(self):
        for payload in ('{bad json}', _repair_review('invented', evidence='not supplied')):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                tools = ProjectTools(Path(directory))
                tools.write_file('frontend/src/App.jsx', 'return null;')
                model = _make_client(None, None, [_FakeMessage(None, payload)] * 3)
                with self.assertRaises(RequirementReviewError):
                    model.review_requirements({'id': 'ROOT', 'description': 'test task'}, tools)
                self.assertEqual(model.model_requests_used, 2)
                self.assertEqual(model.client.completions.calls, 2)
                self.assertIsNone(model._requirement_review_cache)

    def test_retry_stops_at_request_budget_before_second_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = ProjectTools(Path(directory))
            model = _make_client(None, None, [_FakeMessage(None, '{bad json}')],
                                 env_overrides={'ARCBENCH_MAX_MODEL_REQUESTS': '1'})
            with self.assertRaises(ModelBudgetExceeded):
                model.review_requirements({'id': 'ROOT'}, tools)
            self.assertEqual(model.client.completions.calls, 1)

    def test_source_paths_normalize_harmless_syntax_but_cannot_escape_supplied_files(self):
        tree = {'id': 'ROOT', 'description': 'test task'}
        source = {'frontend/src/App.jsx': 'return null;'}
        for path in ('./frontend/src/App.jsx', 'frontend\\src\\App.jsx'):
            self.assertIn('frontend/src/App.jsx', ModelClient._parse_requirement_review(
                _repair_review('missing render', path=path), tree, source))
        for path in ('../frontend/src/App.jsx', '/frontend/src/App.jsx', 'frontend/src/other.jsx'):
            with self.subTest(path=path), self.assertRaises(RequirementReviewError):
                ModelClient._parse_requirement_review(_repair_review('missing render', path=path), tree, source)


def local_checks():
    return (
        CheckResult('frontend: npm run build', True, 0, 'built'),
        CheckResult('web template structure', True, 0, 'layout'),
        CheckResult('public interaction regression (reconstructed jsdom, not Stage 3)', True, 0, '32 passed'),
    )


class ReviewerFailureDeliveryTests(unittest.TestCase):
    def test_invalid_reviewer_does_not_repair_production_or_claim_local_success(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            config = SimpleNamespace(task_type='web', max_model_turns=None, max_tool_calls=None,
                                     output_dir=output, requirement_dir=output / 'requirements')
            node = {'id': 'REQ-1'}
            tree = {'id': 'ROOT', 'children': [node]}
            model = MagicMock()
            model.plan.return_value = 'plan'
            model.last_budget_report = None
            model.last_implementation_handoff = False
            model.last_implementation_deferred = False
            model.review_requirements.side_effect = RequirementReviewError('invalid source after retry')
            tools = SimpleNamespace(written_paths=[], ensure_dependencies=lambda: None)
            def implement(**kwargs):
                tools.written_paths.append('frontend/src/App.jsx')
                return False
            model.implement.side_effect = implement
            runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
            with patch('agent.orchestrator.ModelClient', return_value=model), \
                 patch('agent.orchestrator.ProjectTools', return_value=tools), \
                 patch('agent.orchestrator.verify_project', return_value=VerificationResult(True, local_checks()[:1])), \
                 patch('agent.orchestrator.verify_web_template_structure', return_value=VerificationResult(True, local_checks()[1:2])), \
                 patch('agent.orchestrator.verify_public_acceptance', return_value=local_checks()[2:]):
                result = run_model_agent(runtime, config, tree, [RequirementModule('REQ-1', 'Example', node)])
            self.assertFalse(result.passed)
            self.assertTrue(result.ready_for_evaluation)
            self.assertEqual(model.implement.call_count, 1)
            runtime.events.mark_test_passed.assert_not_called()
            self.assertEqual([call.args[0] for call in runtime.events.mark_test_failed.call_args_list], ['ROOT'])
            runtime.traceability.set_test_pass_status.assert_any_call('TEST-REQ-1', None)
            runtime.traceability.set_test_pass_status.assert_any_call('TEST-ROOT', False)
            model.log_usage.assert_called_once_with()
            report = json.loads((output / '.arc/delivery-report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['handoff_reason'], 'requirement audit unavailable')
            self.assertFalse(report['local_verification_passed'])
            self.assertEqual(report['formal_acceptance'], 'pending')

    def test_invalid_review_does_not_hide_real_public_case_or_build_failures(self):
        audit = CheckResult('requirement audit validity', False, 1, 'unverified')
        for failure in (CheckResult('frontend: npm run build', False, 1, 'compile error'),
                        CheckResult('public interaction regression (reconstructed jsdom, not Stage 3)',
                                    False, 1, 'case failed')):
            checks = tuple(check for check in local_checks() if check.name != failure.name) + (failure, audit)
            with self.subTest(failure=failure.name), tempfile.TemporaryDirectory() as directory, \
                 patch('agent.orchestrator.verify_public_acceptance') as startup:
                result = _finalize_delivery(Path(directory), VerificationResult(False, checks), None)
                self.assertFalse(result.ready_for_evaluation)
                startup.assert_not_called()

    def test_unexpected_exception_still_emits_usage_once(self):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(task_type='web', max_model_turns=None, max_tool_calls=None,
                                     output_dir=Path(directory))
            model = MagicMock()
            with patch('agent.orchestrator.ModelClient', return_value=model), \
                 patch('agent.orchestrator._run_model_agent', side_effect=RuntimeError('unexpected')), \
                 self.assertRaisesRegex(RuntimeError, 'unexpected'):
                run_model_agent(SimpleNamespace(), config, {}, [])
            model.log_usage.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()

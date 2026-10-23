import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.execution_scope import execution_modules
from agent.orchestrator import _BoundaryVerification, run_model_agent
from agent.requirements import root_modules, walk_requirement_ids
from agent.verify import CheckResult, VerificationResult


def contract(count=2):
    return {'id': 'ROOT', 'name': 'Application', 'description': 'Shared contract',
            'children': [{'id': f'REQ-{i}', 'description': f'Required behavior {i}',
                          'scenarios': [{'name': f'Journey {i}', 'steps': ['required']}],
                          'children': []} for i in range(count)]}


def generic_result():
    return VerificationResult(True, (
        CheckResult('frontend: npm run build', True, 0, 'built'),
        CheckResult('project: npm run test', True, 0, 'real journey passed'),
        CheckResult('web template structure', True, 0, 'layout')))


class ExecutionScopeTests(unittest.TestCase):
    def test_small_contract_preserves_original_tree_ids_and_scenarios(self):
        tree = contract()
        before = copy.deepcopy(tree)
        modules = execution_modules(tree, root_modules(tree))
        self.assertEqual(len(modules), 1)
        self.assertIs(modules[0].subtree, tree)
        self.assertEqual(tree, before)
        self.assertEqual(walk_requirement_ids(modules[0].subtree), ['ROOT', 'REQ-0', 'REQ-1'])

    def test_large_incomplete_or_partial_scope_is_not_coalesced(self):
        for mode in ('many', 'long', 'incomplete', 'partial'):
            tree = contract(5 if mode == 'many' else 2)
            if mode == 'long':
                tree['description'] = 'x' * 24_001
            if mode == 'incomplete':
                del tree['children'][0]['description']
            modules = root_modules(tree)
            if mode == 'partial':
                modules[1] = modules[0]
            self.assertIs(execution_modules(tree, modules), modules)

    def test_small_contract_uses_one_context_and_still_audits_all_requirements(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tree = contract()
            model = MagicMock()
            model.plan.return_value = 'Implement the whole required journey'
            model.last_request_interruption = model.last_budget_report = None
            model.last_implementation_deferred = False
            model.last_implementation_handoff = True
            model.review_requirements.return_value = ''
            tools = SimpleNamespace(written_paths=[], source_revision=0, ensure_dependencies=lambda: None)
            def implement(**kwargs):
                tools.written_paths.append('frontend/src/App.jsx')
                kwargs['boundary_feedback']()
                return False
            model.implement.side_effect = implement
            config = SimpleNamespace(task_type='web', max_model_turns=None, max_tool_calls=None,
                                     output_dir=root, requirement_dir=root/'requirements')
            with patch('agent.orchestrator.ModelClient', return_value=model), \
                 patch('agent.orchestrator.ProjectTools', return_value=tools), \
                 patch('agent.orchestrator._prepare_local_candidate', return_value=generic_result()) as check:
                result = run_model_agent(SimpleNamespace(events=MagicMock(), traceability=MagicMock()),
                                         config, tree, root_modules(tree))
            self.assertTrue(result.passed)
            self.assertEqual(model.plan.call_count, 1)
            self.assertEqual(model.implement.call_count, 1)
            self.assertEqual(check.call_count, 1)
            model.review_requirements.assert_called_once_with(tree, tools)
            self.assertTrue(check.call_args.kwargs['include_root'])

    def test_generic_boundary_requires_explicit_full_success_and_unchanged_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = _BoundaryVerification()
            result = generic_result()
            cache.remember(root, 0, result)
            self.assertIsNone(cache.take(root, 0))
            cache.remember(root, 0, result, complete_scope=True)
            self.assertIs(cache.take(root, 0), result)
            self.assertIsNone(cache.take(root, 0))
            cache.remember(root, 0, result, complete_scope=True)
            (root/'test.js').write_text('changed', encoding='utf-8')
            self.assertIsNone(cache.take(root, 0))
            for invalid in (VerificationResult(False, result.checks),
                            VerificationResult(True, result.checks[:1])):
                cache.remember(root, 0, invalid, complete_scope=True)
                self.assertIsNone(cache.take(root, 0))


if __name__ == '__main__':
    unittest.main()

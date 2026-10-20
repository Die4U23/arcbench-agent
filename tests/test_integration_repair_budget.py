"""Integration must leave budget for all observed failure families."""

# Legacy fixtures exercise worker internals; host isolation is tested separately.
import unittest.mock as _sandbox_mock
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agent.candidate_checkpoint import PUBLIC_CHECK
from agent.orchestrator import _integration_allowances, run_model_agent
from agent.repair_policy import published_feedback_patterns
from agent.requirements import RequirementModule
from agent.ticketbooking_contract import PUBLIC_CASES
from agent.verify import CheckResult, VerificationResult


@_sandbox_mock.patch("agent.sandbox_context._WORKER", True)
class IntegrationRepairBudgetTests(unittest.TestCase):
    def test_remaining_half_is_computed_from_current_usage_without_resetting_caps(self):
        model = SimpleNamespace(max_total_tokens=10_000_000, max_model_requests=250,
                                prompt_tokens_used=5_700_000, completion_tokens_used=300_000,
                                model_requests_used=150)
        self.assertEqual(_integration_allowances(model), (2_000_000, 50))
        model.model_requests_used = 251
        model.prompt_tokens_used = 10_000_001
        self.assertEqual(_integration_allowances(model), (0, 0))
        model.max_total_tokens = model.max_model_requests = None
        self.assertEqual(_integration_allowances(model), (None, None))

    def test_three_failing_families_get_repairs_after_integration_consumes_its_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            nodes = [dict(id='REQ-1', children=[dict(id='REQ-1.1')]),
                     dict(id='REQ-2', children=[dict(id='REQ-2.1'), dict(id='REQ-2.2')]),
                     dict(id='REQ-3', children=[dict(id='REQ-3.2')])]
            tree = dict(id='ROOT', children=nodes)
            modules = [RequirementModule(node_id=node['id'], name=node['id'], subtree=node)
                       for node in nodes]
            config = SimpleNamespace(task_type='web', max_model_turns=None, max_tool_calls=None,
                                     output_dir=output, requirement_dir=Path(directory)/'requirements')
            runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
            tools = SimpleNamespace(written_paths=[], ensure_dependencies=lambda: None)
            model = MagicMock()
            model.max_total_tokens, model.max_model_requests = 10_000_000, 250
            model.prompt_tokens_used = model.completion_tokens_used = model.model_requests_used = 0
            model.last_budget_report = None
            model.last_implementation_deferred = model.last_implementation_handoff = False
            model.review_requirements.return_value = ''
            failures = {'REQ-2.1', 'REQ-2.2', 'REQ-3.2'}
            repaired = []

            def plan(*args, **kwargs):
                model.model_requests_used += 1
                return 'plan'

            def implement(**kwargs):
                scope = kwargs['subtree']['id']
                # The old unbounded ROOT call could consume every remaining request.
                requests = kwargs.get('request_allowance')
                if requests is None:
                    requests = model.max_model_requests - model.model_requests_used
                self.assertGreater(model.max_model_requests - model.model_requests_used, 3)
                model.model_requests_used += requests
                model.prompt_tokens_used += kwargs.get('token_allowance') or 100_000
                tools.written_paths.append('frontend/' + scope + '.jsx')
                if scope in failures:
                    failures.remove(scope)
                    repaired.append(scope)
                return False

            def public(*args, **kwargs):
                statuses = []
                failing_seen = set()
                for requirement, source in PUBLIC_CASES:
                    name = re.search(r'''test\(\s*(['"])(.*?)\1''', source)[2]
                    failed = requirement in failures and requirement not in failing_seen
                    if failed:
                        failing_seen.add(requirement)
                    statuses.append((name, 'failed' if failed else 'passed'))
                statuses.extend([('local probe 1', 'passed'), ('local probe 2', 'passed')])
                return (CheckResult(PUBLIC_CHECK, not failures, 1 if failures else 0,
                                    '\n'.join(name + ': observed failure' for name,status in statuses
                                              if status == 'failed'), case_statuses=tuple(statuses)),)

            model.plan.side_effect = plan
            model.implement.side_effect = implement
            passed = VerificationResult(True, (CheckResult('build', True, 0, 'ok'),))
            with patch('agent.orchestrator.ModelClient', return_value=model), \
                 patch('agent.orchestrator.ProjectTools', return_value=tools), \
                 patch('agent.orchestrator.verify_project', return_value=passed), \
                 patch('agent.orchestrator.verify_web_template_structure', return_value=passed), \
                 patch('agent.orchestrator.verify_public_acceptance', side_effect=public):
                result = run_model_agent(runtime, config, tree, modules)
            self.assertTrue(result.passed)
            self.assertEqual(repaired, ['REQ-2.1', 'REQ-2.2', 'REQ-3.2'])
            integration = model.implement.call_args_list[3].kwargs
            self.assertLess(integration['request_allowance'], 100)
            self.assertLessEqual(model.model_requests_used, 247)
            self.assertLess(model.prompt_tokens_used, 9_600_000)

    def test_missing_name_feedback_exposes_required_predicate_without_relaxing_it(self):
        title = 'REQ-3.2: reject invalid passenger information without creating a booking'
        evidence = published_feedback_patterns([title + ': AssertionError: expected 0 to be greater than 0'])
        self.assertIn('/name.+required|(?:enter|provide).+(?:passenger )?name/i', evidence)
        self.assertIn('Distinguish an empty required field', evidence)
        self.assertNotIn('same-city', evidence)

    def test_feedback_loop_preserves_all_predicates_without_claiming_reached_inputs(self):
        title = 'REQ-3.2: reject multiple invalid passenger combinations before confirmation'
        evidence = published_feedback_patterns([title + ': AssertionError: expected 0 to be greater than 0'])
        self.assertIn('nationality.+(?:required|missing)', evidence)
        self.assertIn('id(?: number)?.+', evidence)
        self.assertIn('terms.+(?:required|must be accepted)', evidence)
        self.assertIn('not evidence that every loop input was reached', evidence)
        self.assertEqual(published_feedback_patterns([title + ': timeout before form submission']), '')


if __name__ == '__main__':
    unittest.main()

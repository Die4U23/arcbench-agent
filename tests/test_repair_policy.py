from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agent.candidate_checkpoint import PUBLIC_CHECK
from agent.orchestrator import run_model_agent
from agent.repair_policy import failed_behavior_ids, focused_subtree, repair_guidance
from agent.requirements import RequirementModule
from agent.verify import CheckResult, VerificationResult


# Real Vitest fullName values from a2f4c0e07aef; ancestorTitles was [''].
PLATFORM_FAILED_CASES = (
    ' REQ-1.1: successfully register a new account and remain signed in',
    ' REQ-1.2: sign in with a valid username and password',
    ' REQ-2.1: search with valid criteria and display matching train results',
    ' REQ-2.2: select a train from the result list and open the booking page',
    ' REQ-3.1: open the booking page and display the selected train summary',
    ' REQ-3.2: submit valid passenger information and create a booking record',
)


class RepairPolicyTests(unittest.TestCase):
    def test_real_vitest_empty_ancestor_routes_all_six_failed_families(self):
        statuses = tuple((title, 'failed') for title in PLATFORM_FAILED_CASES)
        public = CheckResult(PUBLIC_CHECK, False, 1, 'Complete report', (), statuses)
        expected = ('REQ-1.1', 'REQ-1.2', 'REQ-2.1', 'REQ-2.2', 'REQ-3.1', 'REQ-3.2')
        generated_tests = CheckResult('frontend: npm run test', False, 1, 'generated test failure')
        self.assertEqual(failed_behavior_ids(VerificationResult(False, (public, generated_tests))), expected)
        self.assertEqual(public.case_statuses, statuses)  # Checkpoint identities stay unchanged.
        self.assertTrue(all(title.startswith(' ') for title, _ in public.case_statuses))
        for name in ('frontend: npm run build', 'web template structure', 'project dependencies'):
            with self.subTest(infrastructure=name):
                broken = CheckResult(name, False, 1, 'infrastructure failure')
                self.assertEqual(failed_behavior_ids(VerificationResult(False, (public, broken))), ())

    def test_incomplete_or_infrastructure_report_does_not_route_to_leaf(self):
        public = CheckResult(PUBLIC_CHECK, False, 1, 'REQ-1.1: failed')
        self.assertEqual(failed_behavior_ids(VerificationResult(False, (public,))), ())
        complete = CheckResult(PUBLIC_CHECK, False, 1, public.output, (),
                               (('REQ-1.1: invalid password', 'failed'),))
        broken = CheckResult('frontend: npm run build', False, 1, 'import failed')
        self.assertEqual(failed_behavior_ids(VerificationResult(False, (complete, broken))), ())

    def test_normalization_does_not_infer_failures_from_other_statuses_or_check_ids(self):
        public = CheckResult(PUBLIC_CHECK, False, 1, 'Incomplete report', ('REQ-1.1',))
        self.assertEqual(failed_behavior_ids(VerificationResult(False, (public,))), ())
        statuses = ((PLATFORM_FAILED_CASES[0], 'passed'),
                    (PLATFORM_FAILED_CASES[1], 'pending'),
                    (' local journey probe: submit', 'failed'),
                    (PLATFORM_FAILED_CASES[2], 'failed'))
        public = CheckResult(PUBLIC_CHECK, False, 1, 'Report', (), statuses)
        self.assertEqual(failed_behavior_ids(VerificationResult(False, (public,))), ('REQ-2.1',))

    def test_focus_preserves_original_tree_and_cross_module_context(self):
        tree = {'id': 'ROOT', 'children': [{'id': 'REQ-1', 'children': [{'id':'REQ-1.1'}]}, {'id':'REQ-3'}]}
        original = deepcopy(tree)
        focused = focused_subtree(tree, 'REQ-1.1')
        self.assertEqual(focused['id'], 'REQ-1.1')
        self.assertIs(focused['project_requirements'], tree)
        self.assertEqual(tree, original)
        self.assertIsNone(focused_subtree(tree, 'REQ-unknown'))

    def test_same_failures_try_each_family_once_then_stop(self):
        # Reproduces a rollback to the same 21/32: a broad signature must not
        # suppress another as-yet-untried family, nor cause endless retries.
        with TemporaryDirectory() as directory:
            nodes = [{'id':'REQ-1.1'}, {'id':'REQ-2.1'}, {'id':'REQ-3.2'}]
            tree = {'id':'ROOT','children':nodes}
            config = SimpleNamespace(task_type='web', max_model_turns=None, max_tool_calls=None,
                output_dir=Path(directory), requirement_dir=Path(directory)/'requirements')
            model = MagicMock(plan=MagicMock(return_value='plan'),
                              review_requirements=MagicMock(return_value=''))
            model.last_budget_report = None
            tools = SimpleNamespace(written_paths=[], ensure_dependencies=lambda:None)
            on_demand_reports = []
            def implement(**kwargs):
                tools.written_paths.append('frontend/src/App.jsx')
                if kwargs.get('verification_feedback'):
                    on_demand_reports.append((kwargs['subtree']['id'], kwargs['verification_feedback']()))
                return False
            model.implement.side_effect = implement
            project = VerificationResult(True, (CheckResult('frontend: npm run build',True,0,'ok'),))
            layout = VerificationResult(True, (CheckResult('web template structure',True,0,'ok'),))
            public = (CheckResult(PUBLIC_CHECK,False,1,
                '\n'.join(n['id']+': AssertionError: duplicate visible facts' for n in nodes), (),
                tuple((n['id']+': visible facts','failed') for n in nodes)),)
            def public_check(*args, startup_only=False, **kwargs):
                return ((CheckResult('web runtime startup', True, 0, 'Owned runtime served HTTP 200'),)
                        if startup_only else public)
            with patch('agent.orchestrator.ModelClient',return_value=model), \
                 patch('agent.orchestrator.ProjectTools',return_value=tools), \
                 patch('agent.orchestrator.verify_project',return_value=project), \
                 patch('agent.orchestrator.verify_web_template_structure',return_value=layout), \
                 patch('agent.orchestrator.verify_public_acceptance',side_effect=public_check) as check:
                result = run_model_agent(SimpleNamespace(events=MagicMock(),traceability=MagicMock()),
                    config, tree, [RequirementModule('ROOT','all',tree)])
            repairs = [c.kwargs for c in model.implement.call_args_list if c.kwargs.get('repair_feedback')]
            self.assertEqual([r['subtree']['id'] for r in repairs], ['REQ-1.1','REQ-2.1','REQ-3.2'])
            self.assertTrue(all(r['subtree']['project_requirements']==tree for r in repairs))
            self.assertTrue(all(r['request_allowance']==12 for r in repairs))
            for repair in repairs:
                full_report, focused_task = repair['repair_feedback'].split('\n\nCurrent repair task:\n')
                self.assertIn(public[0].output, full_report)
                self.assertTrue(focused_task.startswith('Active repair scope: ' + repair['subtree']['id']))
                self.assertIn('unrelated generated self-test failure', focused_task)
            self.assertEqual(len(on_demand_reports), 3)
            for scope, report in on_demand_reports:
                self.assertIn(public[0].output, report)
                self.assertIn('Current repair task:\nActive repair scope: ' + scope, report)
            # Initial verification, three repair slices, fresh final verification,
            # then the independent runtime startup needed for evaluator handoff.
            self.assertEqual(check.call_count,9)
            check.assert_called_with(config.output_dir, {}, startup_only=True)
            self.assertEqual(sum(not call.kwargs.get('startup_only', False)
                                 for call in check.call_args_list), 8)
            self.assertFalse(result.passed)
            self.assertTrue(result.ready_for_evaluation)
            remaining = next(item for item in result.checks if item.name == PUBLIC_CHECK)
            self.assertFalse(remaining.passed)
            self.assertEqual(remaining.case_statuses, public[0].case_statuses)
            report = json.loads((config.output_dir / '.arc/delivery-report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['generation_stop_reason'], 'bounded_repair_exhausted')
            self.assertEqual(report['formal_acceptance'], 'pending')
            self.assertFalse(report['local_verification_passed'])

    def test_feedback_filters_focus_but_retains_validation_and_rollback_rules(self):
        result = VerificationResult(False,(CheckResult(PUBLIC_CHECK,False,1,
            'REQ-1.1: AssertionError: expected 0 to be greater than 0\n'
            'REQ-2.1: AssertionError: Strict visibility text Shanghai must resolve once'),))
        feedback = repair_guidance(result,'REQ-1.1')
        self.assertIn('numeric and format rules',feedback)
        self.assertIn('restored candidate discards',feedback)
        self.assertNotIn('REQ-2.1:',feedback)

    def test_exact_label_failure_guidance_inspects_decorated_existing_control(self):
        result = VerificationResult(False, (CheckResult(PUBLIC_CHECK, False, 1,
            'REQ-1.1: successfully register: Strict locator label /^name$/i must resolve '
            'exactly once: expected +0 to be 1 // Object.is equality\n'
            'REQ-2.1: search: Strict visibility text Shanghai must resolve once'),))
        feedback = repair_guidance(result, 'REQ-1.1')
        self.assertIn('control may exist', feedback.replace('\n', ' '))
        self.assertIn('getByLabelText', feedback)
        self.assertIn('aria-hidden="true"', feedback)
        self.assertIn('explicit aria-label', feedback)
        self.assertIn('CSS outside the DOM label text', feedback)
        self.assertIn('required validation', feedback)
        self.assertIn('real form handler', feedback)
        self.assertIn('Do not change the published selector or adapter', feedback)
        self.assertNotIn('REQ-2.1:', feedback)

    def test_duplicate_city_match_is_diagnosed_without_treating_it_as_a_missing_control(self):
        failure = ('REQ-2.1: search with valid criteria and display matching train results: '
                   'Strict visibility text Shanghai must resolve once: expected 2 to be 1 '
                   '// Object.is equality')
        result = VerificationResult(False, (CheckResult(PUBLIC_CHECK, False, 1, failure),))
        feedback = repair_guidance(result, 'REQ-2.1')
        self.assertIn(failure, feedback)
        self.assertIn('Multiple matching locator nodes', feedback)
        self.assertIn('city substrings inside station names', feedback)
        self.assertIn('full text differs', feedback)
        self.assertIn('current call sites', feedback)
        self.assertIn('Do not hide required data or change selectors', feedback)
        self.assertNotIn('Zero matching locator nodes', feedback)
        self.assertNotIn('accessible role and full name', feedback)

    def test_zero_train_summary_requires_guest_display_diagnosis_and_keeps_submit_auth(self):
        for zero in ('0', '+0'):
            with self.subTest(zero=zero):
                failure = ('REQ-2.2: select a train from the result list and open the booking page: '
                           'Strict visibility text /train information/i must resolve once: '
                           f'expected {zero} to be 1 // Object.is equality')
                result = VerificationResult(False, (CheckResult(PUBLIC_CHECK, False, 1, failure),))
                feedback = repair_guidance(result, 'REQ-2.2')
                self.assertIn('Zero matching locator nodes', feedback)
                self.assertIn('early returns', feedback)
                self.assertIn('blocked flow rather than a wording defect', feedback)
                self.assertIn('without first signing in', feedback)
                self.assertIn('public train summary for that guest flow', feedback)
                self.assertIn('authentication for booking submission and private saved results', feedback)
                self.assertIn('passing signed-out submission rejection', feedback)
                self.assertNotIn('Multiple matching locator nodes', feedback)

    def test_zero_word_boundary_text_checks_alias_semantics_without_changing_other_diagnoses(self):
        for kind, expression, count, expected in (
            ('text', r'/\bParis\b/', '+0', True),
            ('text', r'/\bParis\b/', '2', False),
            ('text', '/train information/i', '0', False),
            ('role', r'textbox name=/\bName\b/', '0', False),
        ):
            with self.subTest(kind=kind, expression=expression, count=count):
                failure = (f'REQ-3.2: review: Strict visibility {kind} {expression} must resolve once: '
                           f'expected {count} to be 1 // Object.is equality')
                result = VerificationResult(False, (CheckResult(PUBLIC_CHECK, False, 1, failure),))
                feedback = repair_guidance(result, 'REQ-3.2')
                self.assertEqual('A whole-word text expression' in feedback, expected)
                self.assertIn(failure, feedback)
                if expected:
                    self.assertIn('anchored individual-location', feedback)
                    self.assertIn("required route's canonical city", feedback)
                    self.assertNotIn('A whole-word text expression', repair_guidance(result, 'REQ-1.1'))

    def test_plain_review_count_failure_inspects_instances_without_inventing_a_locator(self):
        failure = ('REQ-3.2: local probe - one selected-train summary during review: '
                   'expected 2 to be 1 // Object.is equality')
        result = VerificationResult(False, (CheckResult(PUBLIC_CHECK, False, 1, failure),))
        feedback = repair_guidance(result, 'REQ-3.2')
        self.assertIn(failure, feedback)
        self.assertIn('plain numeric count assertion has no locator description', feedback)
        self.assertIn('For a locator count', feedback)
        self.assertIn('entry, review and success', feedback)
        self.assertIn('mounted by both a page and its child', feedback)
        self.assertNotIn('Multiple matching locator nodes', feedback)
        self.assertNotIn('Zero matching locator nodes', feedback)

    def test_booking_number_failure_cannot_establish_the_titled_journey_replacement_defect(self):
        failure = ('REQ-3.1: local probe - an older booking cannot replace a newly selected journey: '
                   'Strict visibility text /booking number/i must resolve once: '
                   'expected +0 to be 1 // Object.is equality')
        result = VerificationResult(False, (CheckResult(PUBLIC_CHECK, False, 1, failure),))
        feedback = repair_guidance(result, 'REQ-3.1')
        self.assertIn('first blocking assertion', feedback)
        self.assertIn('later interactions may not have run', feedback)
        self.assertIn('title alone does not identify the failing subsystem', feedback)
        self.assertIn('actual API feedback', feedback)
        self.assertIn('token propagation', feedback)
        self.assertIn('terms acceptance and selected-journey data', feedback)
        self.assertIn('signed-in header does not prove', feedback)
        self.assertIn('frontend/src/BookingPage.jsx', feedback)
        self.assertIn('backend/src/app.js', feedback)
        self.assertIn('owner/journey isolation and stale-request cancellation', feedback)

    def test_booking_success_case_blocked_at_city_still_traces_real_confirmation(self):
        result = VerificationResult(False, (CheckResult(PUBLIC_CHECK, False, 1,
            'REQ-3.2: submit valid passenger information and create a booking record: '
            'Strict visibility text Shanghai must resolve once: expected 2 to be 1\n'
            'REQ-2.2: open booking page: Strict visibility text /train information/i '
            'must resolve once: expected +0 to be 1'),))
        feedback = repair_guidance(result, 'REQ-3.2')
        self.assertIn('Multiple matching locator nodes', feedback)
        self.assertIn('confirmation request and saved-record reload', feedback)
        self.assertIn('confirmation payload retains terms acceptance', feedback)
        self.assertIn('remaining published sequence', feedback)
        self.assertNotIn('REQ-2.2:', feedback)
        self.assertNotIn('Zero matching locator nodes', feedback)
        self.assertNotIn('public train summary for that guest flow', feedback)

    def test_exact_label_count_does_not_misclassify_as_plain_count_or_missing_visible_text(self):
        result = VerificationResult(False, (CheckResult(PUBLIC_CHECK, False, 1,
            'REQ-1.1: register: Strict locator label /^name$/i must resolve exactly once: '
            'expected +0 to be 1 // Object.is equality'),))
        feedback = repair_guidance(result, 'REQ-1.1')
        self.assertIn('getByLabelText', feedback)
        self.assertNotIn('plain numeric count assertion', feedback)
        self.assertNotIn('Zero matching locator nodes', feedback)
        self.assertNotIn('token propagation', feedback)

if __name__=='__main__':
    unittest.main()

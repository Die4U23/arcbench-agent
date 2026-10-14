import json
from pathlib import Path
import tempfile
import unittest

from arcbench_agent_runtime import AgentRuntime
from agent.candidate_checkpoint import PUBLIC_CHECK
from agent.requirement_progress import (
    _expected_cases, local_requirement_statuses, mark_subtree_progress,
    record_local_verification,
)
from agent.verify import CheckResult, VerificationResult


def requirement_tree():
    return {'id': 'ROOT', 'children': [
        {'id': f'REQ-{parent}', 'children': [
            {'id': f'REQ-{parent}.{leaf}'} for leaf in (1, 2)]}
        for parent in (1, 2, 3)]}


def case_result(*, failed=None, rows=None, extra=(), exit_code=1):
    statuses = tuple((title, 'failed' if title == failed else 'passed')
                     for title in _expected_cases()) if rows is None else rows
    return VerificationResult(False, (
        CheckResult(PUBLIC_CHECK, False, exit_code, 'Complete local assertions; runner failed', (), statuses),
        *extra))


class RequirementProgressTests(unittest.TestCase):
    def test_complete_assertions_survive_runner_failure_without_changing_global_verdict(self):
        result = case_result(extra=(CheckResult('frontend: npm run test', False, 1, 'self-test failure'),))
        statuses, cases = local_requirement_statuses(requirement_tree(), result)
        self.assertIs(statuses['ROOT'], False)
        self.assertTrue(all(value is True for key, value in statuses.items() if key != 'ROOT'))
        self.assertEqual(sum(map(len, cases.values())), len(_expected_cases()))
        self.assertFalse(result.passed)
        self.assertFalse(result.checks[0].passed)
        self.assertEqual(result.checks[0].exit_code, 1)

    def test_failed_case_propagates_only_through_actual_ancestors(self):
        title = next(title for title, node in _expected_cases().items() if node == 'REQ-3.2')
        statuses, _ = local_requirement_statuses(requirement_tree(), case_result(failed=title))
        self.assertFalse(statuses['REQ-3.2'])
        self.assertFalse(statuses['REQ-3'])
        self.assertTrue(statuses['REQ-3.1'])
        self.assertTrue(statuses['REQ-1'])

    def test_missing_skipped_duplicate_unknown_and_ambiguous_reports_stay_unknown(self):
        rows = tuple((title, 'passed') for title in _expected_cases())
        variants = (rows[:-1], rows + rows[:1],
                    ((rows[0][0], 'skipped'), *rows[1:]),
                    (('REQ-1.1: invented case', 'passed'), *rows[1:]), ())
        for variant in variants:
            with self.subTest(variant=variant[:1]):
                statuses, cases = local_requirement_statuses(requirement_tree(), case_result(rows=variant))
                self.assertEqual(cases, {})
                self.assertTrue(all(value is None for key, value in statuses.items() if key != 'ROOT'))

    def test_suite_prefix_is_allowed_but_similar_requirement_id_is_not(self):
        rows = tuple(('TicketBooking suite ' + title, 'passed') for title in _expected_cases())
        tree = requirement_tree()
        tree['children'].append({'id': 'REQ-10', 'children': [{'id': 'REQ-10.1'}]})
        statuses, _ = local_requirement_statuses(tree, case_result(rows=rows))
        self.assertTrue(statuses['REQ-1.1'])
        self.assertIsNone(statuses['REQ-10.1'])
        self.assertIsNone(statuses['REQ-10'])

    def test_explicit_failure_overrides_case_pass_and_failed_parent_does_not_poison_sibling(self):
        extra = (CheckResult('native guard', False, 1, 'explicit failure', ('REQ-3.2',)),
                 CheckResult('requirement audit', False, 1, 'parent obligation', ('REQ-1',)))
        statuses, _ = local_requirement_statuses(requirement_tree(), case_result(extra=extra))
        self.assertFalse(statuses['REQ-3.2'])
        self.assertFalse(statuses['REQ-3'])
        self.assertFalse(statuses['REQ-1'])
        self.assertTrue(statuses['REQ-1.1'])
        self.assertTrue(statuses['REQ-2'])

    def test_unrun_related_guard_is_unknown_not_failed_or_passed(self):
        extra = (CheckResult('native guard', False, 1, 'Not executed: no browser', ('REQ-3.2',)),)
        statuses, _ = local_requirement_statuses(requirement_tree(), case_result(extra=extra))
        self.assertIsNone(statuses['REQ-3.2'])
        self.assertIsNone(statuses['REQ-3'])

    def test_nested_folders_require_every_child_and_their_own_scenarios(self):
        tree = requirement_tree()
        tree['children'][0]['children'].append({'id': 'REQ-1.3'})
        tree['children'][1]['scenarios'] = [{'id': 'SCN-parent', 'name': 'extra parent obligation'}]
        tree['children'] = [{'id': 'GROUP', 'children': tree['children']}]
        statuses, _ = local_requirement_statuses(tree, case_result())
        self.assertIsNone(statuses['REQ-1'])
        self.assertIsNone(statuses['REQ-2'])
        self.assertTrue(statuses['REQ-3'])
        self.assertIsNone(statuses['GROUP'])

    def test_unrelated_check_cannot_supply_protected_case_evidence(self):
        public = case_result().checks[0]
        result = VerificationResult(False, (CheckResult('model self-report', False, 1,
            public.output, (), public.case_statuses),))
        statuses, _ = local_requirement_statuses(requirement_tree(), result)
        self.assertIsNone(statuses['REQ-1.1'])

    def test_real_sdk_records_case_statuses_and_clears_old_green_on_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = AgentRuntime.from_env(project_dir=directory,
                runner_events_path=str(root / '.arc/runner-events.jsonl'),
                traceability_dir=str(root / '.arc/traceability'))
            tree = requirement_tree()
            runtime.traceability.init_db()
            runtime.traceability.store_requirement_tree(tree)
            record_local_verification(runtime, tree, case_result())
            self.assertEqual(runtime.traceability.get_requirement_state('REQ-1.1')['state'], 'PASSED')
            self.assertEqual(runtime.traceability.get_requirement_state('ROOT')['state'], 'FAILED')
            self.assertTrue(runtime.traceability.get_test('TEST-REQ-1.1')['passed'])
            record_local_verification(runtime, tree, case_result(rows=()))
            self.assertIsNone(runtime.traceability.get_test('TEST-REQ-1.1')['passed'])
            self.assertEqual(runtime.traceability.get_requirement_state('REQ-1.1')['state'], 'IMPLEMENTED')
            events = [json.loads(line) for line in (root / '.arc/runner-events.jsonl').read_text().splitlines()]
            passed = [event for event in events if event.get('type') == 'requirement_state'
                      and event.get('status') == 'passed']
            self.assertTrue(passed)
            self.assertTrue(all('formal Stage 3 acceptance pending' in event['message'] for event in passed))

    def test_scope_progress_includes_leaves_and_stops_at_actual_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = AgentRuntime.from_env(project_dir=directory,
                runner_events_path=str(root / '.arc/runner-events.jsonl'),
                traceability_dir=str(root / '.arc/traceability'))
            scope = requirement_tree()['children'][0]
            mark_subtree_progress(runtime, scope, 'mark_design_started', 'Planning scope')
            self.assertEqual(runtime.traceability.get_requirement_state('REQ-1.1')['state'], 'DESIGNING')
            mark_subtree_progress(runtime, scope, 'mark_implementation_started', 'Implementing scope')
            record_local_verification(runtime, {'id': 'ROOT', 'children': [scope]}, case_result(rows=()))
            self.assertEqual(runtime.traceability.get_requirement_state('REQ-1.1')['state'], 'IMPLEMENTING')
            self.assertIsNone(runtime.traceability.get_requirement_state('REQ-2'))

    def test_unknown_does_not_erase_failed_planning_or_implementation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = AgentRuntime.from_env(project_dir=directory,
                runner_events_path=str(root / '.arc/runner-events.jsonl'),
                traceability_dir=str(root / '.arc/traceability'))
            runtime.events.mark_design_failed('REQ-1.1', 'Planning interrupted')
            runtime.events.mark_implementation_failed('REQ-1.2', 'Implementation interrupted')
            record_local_verification(runtime, requirement_tree(), case_result(rows=()))
            for node_id, phase in [('REQ-1.1', 'design'), ('REQ-1.2', 'implement')]:
                row = runtime.traceability.get_requirement_state(node_id)
                self.assertEqual((row['state'], row['phase']), ('FAILED', phase))

    def test_null_children_are_supported_like_the_requirement_walker(self):
        tree = requirement_tree()
        tree['children'][0]['children'][0]['children'] = None
        statuses, _ = local_requirement_statuses(tree, case_result())
        self.assertTrue(statuses['REQ-1.1'])


if __name__ == '__main__':
    unittest.main()

"""Repair tools observe current protected results without committing a partial edit."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock

from agent.model_transport import ModelRequestInterrupted
from agent.tools import ProjectTools, TOOL_SCHEMAS
from tests.test_checkpoint_boundaries import BoundaryFixture, write_files
from tests.test_llm_budget import _FakeMessage, _FakeToolCall, _make_client, _read_call


def verify():
    return _FakeMessage([_FakeToolCall('verify', 'verify_candidate', '{}')])


class RepairVerificationToolTests(unittest.TestCase):
    def test_thinking_reserved_check_rejects_edit_and_preserves_reasoning(self):
        with TemporaryDirectory() as directory:
            tools = ProjectTools(Path(directory))
            tools.write_file('a.js', 'old')
            scripted = [
                _FakeMessage([write_files(**{'a.js': 'new'})], reasoning_content='edit'),
                *[_FakeMessage([_read_call('a.js')], reasoning_content='inspect') for _ in range(8)],
                _FakeMessage([write_files(**{'a.js': 'forbidden'})], reasoning_content='wrong action'),
                verify(), _FakeMessage(None, 'done')]
            scripted[10].reasoning_content = 'verify the current candidate'
            model = _make_client(None, None, scripted, {'MODEL': 'deepseek-flash'})
            model.client.completions.require_reasoning = True
            model.max_idle_tool_turns = 100
            observed = []
            def diagnostic():
                observed.append((Path(directory) / 'a.js').read_text())
                return 'still failed'
            model.implement(task_type='web', subtree={'id': 'ROOT'}, plan='repair',
                project_tools=tools, repair_feedback='failed', request_allowance=12,
                verification_feedback=diagnostic, boundary_feedback=lambda: 'still failed')
            self.assertEqual(observed, ['new'])
            self.assertEqual((Path(directory) / 'a.js').read_text(), 'new')
            requests = model.client.completions.requests
            for request in requests:
                self.assertEqual(request['tool_choice'], 'auto')
                self.assertEqual(request['extra_body']['thinking']['type'], 'enabled')
            self.assertIn('Run verify_candidate now', str(requests[10]['messages'][-1]))
            self.assertEqual(requests[10]['messages'][:-2], requests[9]['messages'])

    def test_slice_reserves_feedback_before_last_two_corrections(self):
        with TemporaryDirectory() as directory:
            fixture = BoundaryFixture(Path(directory))
            model = _make_client(None, None, [
                _FakeMessage([write_files(**{'frontend/src/provider.js': 'new'})]),
                *[_FakeMessage([_read_call('frontend/src/provider.js')]) for _ in range(8)],
                verify(),
                _FakeMessage([write_files(**{'backend/src/consumer.js': 'new'})]),
                _FakeMessage(None, 'done')])
            model.is_deepseek = True
            model.max_idle_tool_turns = 100
            fixture.implement(model, repair_feedback='Repair shared contract', request_allowance=12,
                verification_feedback=lambda: fixture.feedback(boundary=False))
            requests = model.client.completions.requests
            self.assertEqual(requests[9]['tool_choice'], 'auto')
            self.assertEqual(requests[9]['extra_body']['thinking']['type'], 'enabled')
            self.assertEqual(requests[10]['tool_choice'], 'auto')
            self.assertEqual(requests[10]['extra_body']['thinking']['type'], 'enabled')
            self.assertEqual(model.model_requests_used, 12)
            self.assertEqual(fixture.observed, [('new', 'old')])
            self.assertEqual(fixture.boundaries, [('new', 'new')])
            self.assertTrue(model.last_implementation_handoff)

    def test_verified_or_unmodified_source_does_not_force_another_check(self):
        for edited in (False, True):
            with self.subTest(edited=edited), TemporaryDirectory() as directory:
                tools = ProjectTools(Path(directory))
                tools.write_file('a.js', 'old')
                start = ([_FakeMessage([write_files(**{'a.js': 'new'})]), verify()] if edited else [])
                messages = start + [_FakeMessage([_read_call('a.js')]) for _ in range(11-len(start))]
                model = _make_client(None, None, [*messages, _FakeMessage(None, 'done')])
                model.max_idle_tool_turns = 100
                diagnostic = MagicMock(return_value='still failed')
                model.implement(task_type='web', subtree={'id':'ROOT'}, plan='repair', project_tools=tools,
                    repair_feedback='failed', request_allowance=12,
                    verification_feedback=diagnostic, boundary_feedback=lambda:'still failed')
                self.assertEqual(model.client.completions.requests[9]['tool_choice'], 'auto')
                self.assertEqual(diagnostic.call_count, int(edited))

    def test_truncated_reserved_verification_is_discarded_before_boundary(self):
        with TemporaryDirectory() as directory:
            fixture = BoundaryFixture(Path(directory))
            truncated = verify()
            truncated.finish_reason = 'length'
            model = _make_client(None, None, [
                _FakeMessage([write_files(**{'frontend/src/provider.js':'new'})]),
                *[_FakeMessage([_read_call('frontend/src/provider.js')]) for _ in range(8)],
                truncated, truncated])
            model.is_deepseek = True
            model.max_idle_tool_turns = 100
            fixture.implement(model, repair_feedback='Repair shared contract', request_allowance=12,
                verification_feedback=lambda: fixture.feedback(boundary=False))
            self.assertEqual(model.client.completions.requests[9]['tool_choice'], 'auto')
            retry = model.client.completions.requests[10]
            self.assertEqual(retry['tool_choice']['function']['name'], 'verify_candidate')
            self.assertEqual(retry['extra_body']['thinking']['type'], 'disabled')
            self.assertEqual(model.model_requests_used, 11)
            self.assertEqual(fixture.observed, [])
            self.assertEqual(fixture.boundaries, [('new', 'old')])
            self.assertEqual(fixture.source(), ('old', 'old'))
            self.assertEqual(model.last_implementation_stop_reason, 'truncated')

    def test_incomplete_cross_file_edit_is_diagnosed_then_fixed_without_rollback(self):
        with TemporaryDirectory() as directory:
            fixture = BoundaryFixture(Path(directory))
            model = _make_client(None, None, [
                _FakeMessage([write_files(**{'frontend/src/provider.js': 'new'})]),
                verify(),
                _FakeMessage([write_files(**{'backend/src/consumer.js': 'new'})]),
                verify(), _FakeMessage(None, 'done')])
            fixture.implement(model, repair_feedback='Repair shared contract',
                verification_feedback=lambda: fixture.feedback(boundary=False))
            self.assertEqual(fixture.observed, [('new', 'old'), ('new', 'new')])
            self.assertEqual(fixture.boundaries, [('new', 'new')])
            self.assertEqual(fixture.source(), ('new', 'new'))
            self.assertEqual(fixture.tools.source_revision, 0)
            self.assertTrue(model.last_implementation_handoff)
            contents = [m['content'] for m in model.client.completions.requests[-1]['messages']
                        if m.get('role') == 'tool' and m.get('tool_call_id') == 'verify']
            self.assertIn('frontend: npm run build: FAILED', contents[0])
            self.assertEqual(len(contents), 1)
            self.assertEqual(model.model_requests_used, 4)

    def test_unchanged_source_reuses_result_and_changed_source_has_two_run_limit(self):
        with TemporaryDirectory() as directory:
            tools = ProjectTools(Path(directory))
            messages = [verify(), verify(),
                _FakeMessage([write_files(**{'a.js': 'first'})]), verify(),
                _FakeMessage([write_files(**{'a.js': 'second'})]), verify(),
                _FakeMessage(None, 'done')]
            model = _make_client(None, None, messages)
            diagnostic = MagicMock(side_effect=['current failure one', 'current failure two'])
            boundary = MagicMock(return_value='full verification still failed')
            model.implement(task_type='web', subtree={'id': 'ROOT'}, plan='repair',
                project_tools=tools, repair_feedback='failed', verification_feedback=diagnostic,
                boundary_feedback=boundary)
            self.assertEqual(diagnostic.call_count, 2)
            boundary.assert_called_once_with()
            self.assertFalse(model.last_implementation_handoff)
            contents = str(model.client.completions.requests[-1]['messages'])
            self.assertIn('Unchanged source; previous complete local verification', contents)
            self.assertIn('On-demand verification limit reached', contents)
            self.assertEqual((Path(directory)/'a.js').read_text(), 'second')
            self.assertNotIn('verify_candidate', str(TOOL_SCHEMAS))

    def test_request_allocation_still_runs_boundary_after_on_demand_check(self):
        with TemporaryDirectory() as directory:
            model = _make_client(None, None, [verify()])
            boundary = MagicMock(return_value='failed')
            model.implement(task_type='web', subtree={'id': 'ROOT'}, plan='repair',
                project_tools=ProjectTools(Path(directory)), repair_feedback='failed',
                verification_feedback=lambda: 'Current build and test scripts pass.',
                boundary_feedback=boundary, request_allowance=1)
            self.assertEqual(model.model_requests_used, 1)
            boundary.assert_called_once_with()
            self.assertFalse(model.last_implementation_handoff)
            self.assertEqual(model.last_implementation_stop_reason, 'checkpoint_failed')

    def test_pass_hands_off_before_same_batch_or_later_optional_edits(self):
        with TemporaryDirectory() as directory:
            tools = ProjectTools(Path(directory))
            tools.write_file('a.js', 'passing')
            model = _make_client(None, None, [
                _FakeMessage([_FakeToolCall('verify', 'verify_candidate', '{}'),
                              write_files(**{'a.js': 'unnecessary regression'})]),
                _FakeMessage([write_files(**{'a.js': 'later regression'})]),
            ])
            boundary = MagicMock(return_value='Current build and test scripts pass.')
            model.implement(task_type='web', subtree={'id': 'ROOT'}, plan='repair',
                project_tools=tools, repair_feedback='repair',
                verification_feedback=lambda: 'Current build and test scripts pass.',
                boundary_feedback=boundary)
            self.assertEqual(model.model_requests_used, 1)
            self.assertEqual((Path(directory) / 'a.js').read_text(), 'passing')
            boundary.assert_called_once_with()
            self.assertTrue(model.last_implementation_handoff)

    def test_interrupted_verification_is_not_downgraded_to_a_tool_error(self):
        with TemporaryDirectory() as directory:
            model = _make_client(None, None, [verify()])
            with self.assertRaises(ModelRequestInterrupted):
                model.implement(task_type='web', subtree={'id': 'ROOT'}, plan='repair',
                    project_tools=ProjectTools(Path(directory)), repair_feedback='failed',
                    verification_feedback=MagicMock(side_effect=ModelRequestInterrupted(
                        reason='absolute_deadline', deadline_seconds=1)))


if __name__ == '__main__':
    unittest.main()

"""Repair tools observe current protected results without committing a partial edit."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock

from agent.model_transport import ModelRequestInterrupted
from agent.tools import ProjectTools, TOOL_SCHEMAS
from tests.test_checkpoint_boundaries import BoundaryFixture, write_files
from tests.test_llm_budget import _FakeMessage, _FakeToolCall, _make_client


def verify():
    return _FakeMessage([_FakeToolCall('verify', 'verify_candidate', '{}')])


class RepairVerificationToolTests(unittest.TestCase):
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
            self.assertIn('Current build and test scripts pass.', contents[1])

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
            self.assertEqual(model.last_implementation_stop_reason, 'allocation')

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

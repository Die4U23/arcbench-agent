"""Scripted model fault injection; no model clients or external API requests."""
from pathlib import Path
import json
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'platform-agent'))
from agent.platform_execution import (ExecutionLimits, ExecutionStopped, RunControl,
                                     ProjectLease, SourceTransaction, validate_plan)
from agent.platform_generation import PlatformGenerationModel, StaticProjectTools
from agent.model_transport import ModelRequestInterrupted
from agent.sandbox_files import snapshot_project
from agent.platform_settings import PlatformSettings
from agent.platform_static import missing_coverage


def step(identity='one', refs=('R1',), dependencies=()):
    return dict(id=identity, goal='Implement the supplied requirement',
                requirement_ids=list(refs), depends_on=list(dependencies),
                acceptance='Verify the behavior described in the input')


def call(identity, name, **arguments):
    value = dict(id=identity, type='function', function=dict(name=name, arguments=json.dumps(arguments)))
    return NS(id=identity, function=NS(**value['function']), model_dump=lambda **kwargs: value)


def response(content=None, calls=None, finish='stop', usage=True):
    return NS(choices=[NS(finish_reason=finish, message=NS(
        content=json.dumps(content) if content is not None else '', tool_calls=calls or []))],
        usage=NS(prompt_tokens=10, completion_tokens=5) if usage else None)


class Session:
    model = 'generic-model'
    is_deepseek = False
    def __init__(self, script):
        self.script = list(script)
        self.model_requests_used = self.tool_calls_used = 0
        self.prompt_tokens_used = self.completion_tokens_used = 0
        self.calls = []
    def _check_model_budget(self, **kwargs):
        pass
    def _record_model_request(self):
        self.model_requests_used += 1
    def _record_usage(self, result, **kwargs):
        self.prompt_tokens_used += result.usage.prompt_tokens
        self.completion_tokens_used += result.usage.completion_tokens
    def _create_completion(self, stage, **kwargs):
        self.calls.append((stage, kwargs))
        result = self.script.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result() if callable(result) else result


class Tools(StaticProjectTools):
    def call(self, name, arguments):
        if name == 'validate_source':
            failed = any(b'INVALID' in value for value in snapshot_project(self.project_dir).values())
            return json.dumps(dict(passed=not failed, errors=['invalid fixture'] if failed else []))
        return super().call(name, arguments)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'source.txt').write_text('original', encoding='utf-8')
        self.tools = Tools(self.root)
        self.packet = NS(text='An unfamiliar task, determined entirely by these inputs.',
                         manifest={'requirement_ids': ['R1']}, repository=None)

    def model(self, script, mode='react', **limits):
        self.session = Session(script)
        return PlatformGenerationModel(self.session, execution_mode=mode,
            execution_limits=ExecutionLimits(**limits), context_window_tokens=100000,
            output_reserve_tokens=1000)

    def write_response(self, identity, name='source.txt', content='changed', validate=True):
        calls = [call(identity, 'write_files', files=[dict(path=name, content=content)])]
        if validate:
            calls.append(call(identity + '-v', 'validate_source'))
        return response(calls=calls)

    def test_react_executes_actions_observes_and_validates(self):
        model = self.model([response(calls=[call('read', 'read_file', path='source.txt')]),
                            self.write_response('write')])
        result = model.implement(self.packet, self.tools, request_allowance=3)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual((len(self.session.calls), model.execution_control.tools), (2, 3))
        self.assertEqual((self.root / 'source.txt').read_text(), 'changed')
        self.assertTrue(result['validation']['passed'])

    def test_plan_executes_dependency_order_and_records_steps(self):
        self.packet.manifest['requirement_ids'] = ['R1', 'R2']
        model = self.model([response({'steps': [step('second', ('R2',), ('first',)), step('first')]}),
                            self.write_response('a', 'first.txt'), self.write_response('b', 'second.txt')], 'plan_execute')
        result = model.implement(self.packet, self.tools, request_allowance=8)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual([item['id'] for item in result['completed_steps']], ['first', 'second'])
        self.assertEqual(result['turns'], 3)
        self.assertEqual(result['acceptance'], 'static_only')

    def test_failed_step_rolls_back_and_replans_only_remaining_work(self):
        self.packet.manifest['requirement_ids'] = ['R1', 'R2']
        model = self.model([response({'steps': [step('one'), step('two', ('R2',), ('one',))]}),
            self.write_response('good', 'good.txt'), self.write_response('bad', 'bad.txt', 'INVALID'),
            response({'summary': 'finished'}),
            response({'steps': [step('replacement', ('R2',))]}), self.write_response('fix', 'fixed.txt')],
            'plan_execute', step_requests=2)
        result = model.implement(self.packet, self.tools, request_allowance=12)
        self.assertEqual(result['status'], 'complete', result)
        self.assertTrue((self.root / 'good.txt').is_file())
        self.assertFalse((self.root / 'bad.txt').exists())
        self.assertTrue((self.root / 'fixed.txt').is_file())
        self.assertEqual(model.execution_control.replans, 1)
        self.assertEqual(result['plan_history'][0][1]['status'], 'rolled_back')

    def test_malformed_plan_never_runs_writes(self):
        model = self.model([response({'steps': [step(refs=('invented',))]})], 'plan_execute')
        result = model.implement(self.packet, self.tools, request_allowance=8)
        self.assertTrue(result['reason'].startswith('invalid_plan'))
        self.assertEqual(model.execution_control.tools, 0)
        self.assertEqual(len(self.session.calls), 1)

    def test_plan_stage_cannot_write_even_if_model_requests_it(self):
        model = self.model([self.write_response('write')], 'plan_execute')
        result = model.implement(self.packet, self.tools, request_allowance=5)
        self.assertEqual(result['reason'], 'invalid_tool_batch')
        self.assertEqual((self.root / 'source.txt').read_text(), 'original')

    def test_invalid_second_tool_rejects_whole_batch_before_writes(self):
        calls = [call('valid', 'write_files', files=[dict(path='source.txt', content='changed')]),
                 call('invalid', 'run_project_script', script='start')]
        model = self.model([response(calls=calls)])
        result = model.implement(self.packet, self.tools, request_allowance=4)
        self.assertEqual(result['reason'], 'invalid_tool_batch')
        self.assertEqual((self.root / 'source.txt').read_text(), 'original')

    def test_malformed_or_protected_later_write_rejects_earlier_write(self):
        for arguments in ({'files': [{'path': 'later.txt', 'content': 42}]},
                          {'files': [{'path': 'docs/task-ir.json', 'content': '{}'}]},
                          {'files': [{'path': '../outside.txt', 'content': 'escape'}]}):
            calls = [call('first', 'write_files', files=[dict(path='source.txt', content='changed')]),
                     call('second', 'write_files', **arguments)]
            model = self.model([response(calls=calls)])
            self.assertEqual(model.implement(self.packet, self.tools)['reason'], 'invalid_tool_batch')
            self.assertEqual((self.root / 'source.txt').read_text(), 'original')

    def test_duplicate_call_ids_and_actions_after_validation_are_rejected(self):
        for calls in ([call('same', 'read_file', path='source.txt')] * 2,
                      [call('v', 'validate_source'), call('r', 'read_file', path='source.txt')]):
            with self.subTest(calls=calls):
                model = self.model([response(calls=calls)])
                self.assertEqual(model.implement(self.packet, self.tools)['reason'], 'invalid_tool_batch')

    def test_repeated_successful_reads_stop_across_separate_stages(self):
        model = self.model([response(calls=[call(str(i), 'read_file', path='source.txt')]) for i in range(4)],
                           repeated_actions=2)
        for _ in range(2):
            self.assertEqual(model.implement(self.packet, self.tools, request_allowance=1)['reason'], 'request_allowance')
        self.assertEqual(model.implement(self.packet, self.tools, request_allowance=2)['reason'], 'repeated_action')
        before = len(self.session.calls)
        self.assertEqual(model.review_sources(self.packet, self.tools)['reason'], 'repeated_action')
        self.assertEqual(len(self.session.calls), before)

    def test_tool_batch_cap_prevents_any_mutation(self):
        model = self.model([self.write_response('write')], batch_tools=1)
        self.assertEqual(model.implement(self.packet, self.tools)['reason'], 'tool_batch_limit')
        self.assertEqual((self.root / 'source.txt').read_text(), 'original')

    def test_request_limit_rolls_back_and_remains_sticky(self):
        model = self.model([self.write_response('write', validate=False)], requests=1)
        result = model.implement(self.packet, self.tools, request_allowance=5)
        self.assertEqual(result['reason'], 'execution_request_limit')
        self.assertEqual((self.root / 'source.txt').read_text(), 'original')
        model.implement(self.packet, self.tools)
        self.assertEqual(len(self.session.calls), 1)

    def test_timeout_after_response_does_not_execute_tools(self):
        now = [0]
        model = self.model([])
        model.execution_control.clock = lambda: now[0]
        model.execution_control.deadline = 1
        def late():
            now[0] = 2
            return self.write_response('late')
        self.session.script = [late]
        self.assertEqual(model.implement(self.packet, self.tools)['reason'], 'execution_deadline')
        self.assertEqual((self.root / 'source.txt').read_text(), 'original')

    def test_partial_write_exception_is_restored_before_next_action(self):
        model = self.model([self.write_response('bad', validate=False), response({'summary': 'unchanged'})])
        original = self.tools.call
        def fail(name, arguments):
            if name == 'write_files':
                (self.root / 'partial.txt').write_text('partial')
                raise OSError('injected partial write')
            return original(name, arguments)
        with patch.object(self.tools, 'call', side_effect=fail):
            result = model.implement(self.packet, self.tools)
        self.assertEqual(result['status'], 'complete')
        self.assertFalse((self.root / 'partial.txt').exists())

    def test_interruption_truncation_and_unknown_usage_are_not_retried(self):
        for value in (ModelRequestInterrupted(reason='deadline', deadline_seconds=1),
                      response(calls=[call('w', 'write_files', files=[])], finish='length'),
                      response({'summary': 'finished'}, usage=False)):
            with self.subTest(value=value):
                model = self.model([value])
                result = model.implement(self.packet, self.tools, request_allowance=9)
                self.assertEqual(result['status'], 'stopped')
                model.implement(self.packet, self.tools)
                self.assertEqual(len(self.session.calls), 1)

    def test_zero_allowance_performs_no_request(self):
        for mode in ('react', 'plan_execute'):
            model = self.model([], mode)
            self.assertEqual(model.implement(self.packet, self.tools, request_allowance=0)['reason'], 'request_allowance')
            self.assertEqual(len(self.session.calls), 0)

    def test_empty_completion_cannot_restart_a_coverage_storm(self):
        model = self.model([response({'summary': 'done'}) for _ in range(5)], idle_turns=2)
        self.assertEqual(model.implement(self.packet, self.tools)['status'], 'complete')
        self.assertEqual(model.implement(self.packet, self.tools)['reason'], 'no_progress')
        model.review_sources(self.packet, self.tools)
        self.assertEqual(len(self.session.calls), 2)

    def test_model_reported_incomplete_is_not_promoted_to_complete(self):
        model = self.model([response({'status': 'incomplete'})])
        self.assertEqual(model.implement(self.packet, self.tools)['reason'], 'model_incomplete')

    def test_rollback_preserves_budget_and_refuses_external_changes(self):
        metadata = self.root / '.arc' / 'tokens.json'
        metadata.parent.mkdir()
        metadata.write_text('spent')
        control = RunControl()
        checkpoint = SourceTransaction(self.tools, control)
        (self.root / 'source.txt').write_text('controller edit')
        control.source(self.root, changed_by_controller=True)
        control.requests = 2
        checkpoint.rollback()
        self.assertEqual(control.requests, 2)
        self.assertEqual(metadata.read_text(), 'spent')
        checkpoint = SourceTransaction(self.tools, control)
        (self.root / 'source.txt').write_text('external edit')
        with self.assertRaisesRegex(ExecutionStopped, 'concurrent_source_change'):
            checkpoint.rollback()
        self.assertEqual((self.root / 'source.txt').read_text(), 'external edit')

    def test_rollback_failure_is_terminal(self):
        control = RunControl()
        checkpoint = SourceTransaction(self.tools, control)
        (self.root / 'source.txt').write_text('changed')
        control.source(self.root, changed_by_controller=True)
        with patch('agent.platform_execution.apply_snapshot', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(ExecutionStopped, 'rollback_failed'):
                checkpoint.rollback()
        with self.assertRaisesRegex(ExecutionStopped, 'rollback_failed'):
            control.request()

    def test_optional_review_cannot_swallow_rollback_failure(self):
        model = self.model([self.write_response('bad', content='INVALID'), response({'summary': 'done'})])
        with patch('agent.platform_execution.apply_snapshot', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(ExecutionStopped, 'rollback_failed'):
                model.review_sources(self.packet, self.tools, request_allowance=3)

    def test_project_lease_rejects_second_controller_and_releases_after_error(self):
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            with ProjectLease(self.root) as lease:
                with self.assertRaisesRegex(ExecutionStopped, 'project_already_owned'):
                    with ProjectLease(self.root):
                        self.fail('second controller entered')
                raise RuntimeError('injected')
        self.assertFalse(lease.path.exists())
        with ProjectLease(self.root):
            pass

    def test_plan_validation_boundaries(self):
        cases = [[], [step(), step()], [step(dependencies=('unknown',))],
                 [step('a', dependencies=('b',)), step('b', dependencies=('a',))],
                 [step(refs=())], [dict(step(), goal='')], [step(refs=('R1', 'R1'))]]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                validate_plan({'steps': rows}, ['R1'], 4)
        with self.assertRaises(ValueError):
            validate_plan({'steps': [step()]}, ['R1', 'R2'], 4)
        with self.assertRaises(ValueError):
            validate_plan({'steps': [step('a'), step('b')]}, ['R1'], 1)
        self.assertEqual(len(validate_plan({'steps': [step(refs=())]}, [], 1)), 1)

    def test_idle_oscillation_and_replanning_caps_are_sticky(self):
        for reason, operation in (
            ('no_progress', lambda c: c.observe('a', 'a')),
            ('source_oscillation', lambda c: c.observe('a', 'b')),
            ('replan_limit', lambda c: c.replan())):
            control = RunControl(ExecutionLimits(idle_turns=2, repeated_actions=1, replans=1))
            with self.subTest(reason=reason):
                operation(control)
                with self.assertRaisesRegex(ExecutionStopped, reason):
                    operation(control)
                with self.assertRaisesRegex(ExecutionStopped, reason):
                    control.request()

    def test_partial_or_unbacked_coverage_does_not_complete_work(self):
        ir = {'work_items': [{'id': 'R1'}], 'ui_elements': [], 'scenarios': []}
        docs = self.root / 'docs'
        docs.mkdir()
        for status, files, missing in (
            ('partial', ['source.txt'], True), ('covered', [], True),
            ('covered', ['missing.py'], True), ('covered', ['docs/coverage.json'], True),
            ('covered', ['source.txt'], False)):
            (docs / 'coverage.json').write_text(json.dumps({'items': [
                dict(kind='work_item', ref='R1', status=status, files=files)]}))
            self.assertEqual(bool(missing_coverage(self.root, ir)), missing)

    def test_execution_settings_and_model_policy_are_explicit(self):
        config = PlatformSettings.from_env({'ARC_EXECUTION_MODE': 'react', 'ARC_REPLAN_LIMIT': '0',
                                           'ARC_REQUEST_LIMIT': '7', 'ARC_TOOL_BATCH_LIMIT': '2'})
        self.assertEqual(config.execution_limits().requests, 7)
        self.assertEqual(config.execution_limits().replans, 0)
        self.assertIsNone(config.allowed_models)
        for environment in ({'ARC_EXECUTION_MODE': 'other'}, {'ARC_REPLAN_LIMIT': '-1'},
                            {'ARC_PLAN_STEPS': '0'}, {'ARC_TOOL_BATCH_LIMIT': '999'}):
            with self.assertRaises(ValueError):
                PlatformSettings.from_env(environment)


if __name__ == '__main__':
    unittest.main()

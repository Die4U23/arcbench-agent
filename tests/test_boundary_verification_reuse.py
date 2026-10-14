from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agent.candidate_checkpoint import PUBLIC_CHECK
from agent.orchestrator import _BoundaryVerification, run_model_agent
from agent.requirements import RequirementModule
from agent.verify import CheckResult, VerificationResult


def complete_result():
    return VerificationResult(True, (
        CheckResult('frontend: npm run build', True, 0, 'built'),
        CheckResult('web template structure', True, 0, 'layout'),
        CheckResult(PUBLIC_CHECK, True, 0, 'complete local report', (),
                    (('REQ-1.1: registration', 'passed'),)),
    ))


class BoundaryVerificationReuseTests(unittest.TestCase):
    def test_slow_input_scan_falls_back_to_full_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = _BoundaryVerification()
            with patch('agent.orchestrator.time.monotonic', side_effect=[0, 3]):
                cache.remember(root, 0, complete_result())
            self.assertIsNone(cache.take(root, 0))

    def test_cache_is_consumed_once_and_cannot_cross_explicit_slice_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = _BoundaryVerification()
            result = complete_result()
            cache.remember(root, 0, result)
            self.assertIs(cache.take(root, 0), result)
            self.assertIsNone(cache.take(root, 0))
            cache.remember(root, 0, result)
            cache.clear()
            self.assertIsNone(cache.take(root, 0))

    def test_changed_source_manifest_test_dependency_or_runtime_invalidates(self):
        for name in ('frontend/src/App.jsx', 'frontend/package.json',
                     'frontend/.arc/protected.test.tsx', 'frontend/tests/app.test.tsx',
                     'frontend/node_modules/example/index.js', 'backend/data/db.json'):
            with self.subTest(path=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / name
                path.parent.mkdir(parents=True)
                path.write_text('before', encoding='utf-8')
                cache = _BoundaryVerification()
                cache.remember(root, 0, complete_result())
                path.write_text('modified input', encoding='utf-8')
                self.assertIsNone(cache.take(root, 0))

    def test_source_content_change_with_same_size_and_mtime_invalidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'App.jsx'
            path.write_text('before', encoding='utf-8')
            before = path.stat()
            cache = _BoundaryVerification()
            cache.remember(root, 0, complete_result())
            path.write_text('after!', encoding='utf-8')
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertIsNone(cache.take(root, 0))

    def test_restore_revision_and_final_budget_check_never_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = _BoundaryVerification()
            cache.remember(root, 0, complete_result())
            self.assertIsNone(cache.take(root, 1))
            cache.remember(root, 0, complete_result())
            self.assertIsNone(cache.take(root, 0, final=True))
            self.assertIsNone(cache.take(root, 0))

    def test_incomplete_report_and_scan_failure_do_not_create_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = _BoundaryVerification()
            cache.remember(root, 0, VerificationResult(False, (
                CheckResult(PUBLIC_CHECK, False, 1, 'incomplete'),)))
            self.assertIsNone(cache.take(root, 0))
            with patch('agent.orchestrator._boundary_input_fingerprint', return_value=None):
                cache.remember(root, 0, complete_result())
            self.assertIsNone(cache.take(root, 0))

    def run_controller(self, *, mutate_after_boundary=None, audit_repair=False,
                       dependency_changes=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'frontend/src/App.jsx'
            tree = {'id': 'ROOT', 'children': [{'id': 'REQ-1.1'}]}
            config = SimpleNamespace(task_type='web', max_model_turns=None,
                max_tool_calls=None, output_dir=root, requirement_dir=root / 'requirements')
            tools = SimpleNamespace(written_paths=[], source_revision=0)
            boundary_finished = False

            def dependencies():
                nonlocal boundary_finished
                if dependency_changes and boundary_finished:
                    dependency = root / 'frontend/node_modules/example/index.js'
                    dependency.parent.mkdir(parents=True, exist_ok=True)
                    dependency.write_text('changed installed dependency', encoding='utf-8')
                    boundary_finished = False

            tools.ensure_dependencies = dependencies
            model = MagicMock()
            model.plan.return_value = 'Implement required behavior'
            model.last_budget_report = None
            model.last_request_interruption = None
            model.last_implementation_deferred = False
            model.review_requirements.side_effect = ['Missing audited behavior', ''] if audit_repair else ['']

            def implement(**kwargs):
                nonlocal boundary_finished
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(f'complete source {model.implement.call_count}', encoding='utf-8')
                tools.written_paths.append('frontend/src/App.jsx')
                kwargs['boundary_feedback']()
                boundary_finished = True
                if mutate_after_boundary == 'revision':
                    tools.source_revision += 1
                elif mutate_after_boundary:
                    target = root / mutate_after_boundary
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text('changed after boundary', encoding='utf-8')
                model.last_implementation_stop_reason = 'model_finished'
                return False

            model.implement.side_effect = implement
            with ExitStack() as stack:
                stack.enter_context(patch('agent.orchestrator.ModelClient', return_value=model))
                stack.enter_context(patch('agent.orchestrator.ProjectTools', return_value=tools))
                stack.enter_context(patch('agent.orchestrator.collect_visual_references', return_value=['home.png']))
                build = stack.enter_context(patch('agent.orchestrator.verify_project',
                    return_value=VerificationResult(True, complete_result().checks[:1])))
                stack.enter_context(patch('agent.orchestrator.verify_web_template_structure',
                    return_value=VerificationResult(True, complete_result().checks[1:2])))
                public = stack.enter_context(patch('agent.orchestrator.verify_public_acceptance',
                    return_value=complete_result().checks[2:]))
                visual = stack.enter_context(patch('agent.orchestrator.verify_visual_acceptance',
                    return_value=VerificationResult(True, (
                        CheckResult('visual reference: homepage', True, 0, 'fresh review'),))))
                result = run_model_agent(SimpleNamespace(events=MagicMock(), traceability=MagicMock()),
                    config, tree, [RequirementModule('ROOT', 'all requirements', tree)])
            return result, build.call_count, public.call_count, model.review_requirements.call_count, visual.call_count

    def test_immediate_boundary_reuses_local_check_but_runs_coverage_and_visual(self):
        result, builds, public, audits, visual = self.run_controller()
        self.assertTrue(result.passed)
        self.assertEqual((builds, public, audits, visual), (1, 1, 1, 1))

    def test_each_slice_runs_its_own_boundary_audit_and_visual(self):
        result, builds, public, audits, visual = self.run_controller(audit_repair=True)
        self.assertTrue(result.passed)
        self.assertEqual((builds, public, audits, visual), (2, 2, 2, 2))

    def test_changes_between_boundary_and_controller_force_fresh_local_checks(self):
        for changed in ('frontend/src/App.jsx', 'frontend/package.json',
                        'frontend/.arc/protected.test.tsx', 'revision'):
            with self.subTest(change=changed):
                result, builds, public, audits, visual = self.run_controller(mutate_after_boundary=changed)
                self.assertTrue(result.passed)
                self.assertEqual((builds, public, audits, visual), (2, 2, 1, 1))

    def test_dependency_preparation_runs_before_reuse_decision(self):
        result, builds, public, audits, visual = self.run_controller(dependency_changes=True)
        self.assertTrue(result.passed)
        self.assertEqual((builds, public, audits, visual), (2, 2, 1, 1))


if __name__ == '__main__':
    unittest.main()

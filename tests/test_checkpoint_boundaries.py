"""Real candidate protection at diagnostic, module and interruption boundaries."""

# Legacy fixtures exercise worker internals; host isolation is tested separately.
import unittest.mock as _sandbox_mock
from contextlib import ExitStack
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agent.candidate_checkpoint import CandidateCheckpoint, PUBLIC_CHECK
from agent.llm import _fixed_regression_signature
from agent.model_transport import ModelRequestInterrupted
from agent.orchestrator import _prepare_local_candidate, _repair_feedback, run_model_agent
from agent.requirements import RequirementModule
from agent.tools import ProjectTools
from agent.verify import CheckResult, VerificationResult
from tests.test_llm_budget import _FakeMessage, _FakeToolCall, _make_client, _read_files_call


def write_files(**files):
    return _FakeToolCall("edit", "write_files", json.dumps({
        "files": [{"path": path, "content": content} for path, content in files.items()]}))


class BoundaryFixture:
    """Two generated files share a contract; an unfinished migration breaks it."""
    def __init__(self, root, *, baseline_complete=False, force_build=False,
                 missing_report=False, incomplete_report=False, root_failure=False):
        self.root = root
        self.tools = ProjectTools(root)
        self.tools.write_files([
            {"path": "frontend/src/provider.js", "content": "old"},
            {"path": "backend/src/consumer.js", "content": "old"}])
        self.tools.ensure_dependencies = MagicMock()
        self.guard = CandidateCheckpoint(root)
        self.baseline_complete = baseline_complete
        self.force_build = force_build
        self.missing_report = missing_report
        self.incomplete_report = incomplete_report
        self.root_failure = root_failure
        self.config = SimpleNamespace(task_type="web", max_model_turns=None, max_tool_calls=None,
            output_dir=root, requirement_dir=root / "requirements")
        self.tree = {"id": "ROOT", "children": [{"id": "REQ-1"}]}
        self.observed = []
        self.boundaries = []
        self.build_scopes = []
        self.guard.consider((*self.build().checks, *self.layout().checks, *self.public()))

    def source(self):
        return ((self.root / "frontend/src/provider.js").read_text(encoding="utf-8"),
                (self.root / "backend/src/consumer.js").read_text(encoding="utf-8"))

    def build(self, *args, include_root=False, **kwargs):
        self.build_scopes.append(include_root)
        provider, consumer = self.source()
        passed = self.force_build or provider == consumer
        checks = (CheckResult("frontend: npm run build", passed, 0 if passed else 1,
                              "shared import contract"),)
        if include_root and self.root_failure:
            checks += (CheckResult("project: npm run test", False, 1, "root integration remains incomplete"),)
        return VerificationResult(all(check.passed for check in checks), checks)

    def layout(self, *args, **kwargs):
        return VerificationResult(True, (CheckResult("web template structure", True, 0, "layout"),))

    def public(self, *args, **kwargs):
        if kwargs.get("startup_only"):
            return (CheckResult("web runtime startup", True, 0, "isolated startup fixture"),)
        provider, consumer = self.source()
        preserved = provider == consumer and not provider.startswith("broken")
        fixed = preserved and (self.baseline_complete or provider == "new")
        statuses = (("REQ-1: preserved contract", "passed" if preserved else "failed"),
                    ("REQ-1: required upgrade", "passed" if fixed else "failed"))
        if not preserved and self.missing_report:
            statuses = ()
        elif not preserved and self.incomplete_report:
            statuses = statuses[:1]
        return (CheckResult(PUBLIC_CHECK, preserved and fixed, 0 if preserved and fixed else 1,
            "shared contract behavior" if statuses else "public report unavailable", case_statuses=statuses),)

    def install_checks(self, stack):
        stack.enter_context(patch("agent.orchestrator.verify_project", side_effect=self.build))
        stack.enter_context(patch("agent.orchestrator.verify_web_template_structure", side_effect=self.layout))
        stack.enter_context(patch("agent.orchestrator.verify_public_acceptance", side_effect=self.public))

    def feedback(self, *, boundary):
        (self.boundaries if boundary else self.observed).append(self.source())
        local = _prepare_local_candidate(self.config, self.tree, self.guard, self.tools,
            include_root=boundary, allow_restore=boundary)
        return ("Current build and test scripts pass. Formal acceptance pending." if local.passed
                else _repair_feedback(local))

    def implement(self, model, **kwargs):
        with ExitStack() as stack:
            self.install_checks(stack)
            return model.implement(task_type="web", subtree=self.tree, plan="Complete the shared contract",
                project_tools=self.tools, checkpoint_feedback=lambda: self.feedback(boundary=False),
                boundary_feedback=lambda: self.feedback(boundary=True), **kwargs)


@_sandbox_mock.patch("agent.sandbox_context._WORKER", True)
class CheckpointBoundaryTests(unittest.TestCase):
    def test_restored_source_keeps_rejected_build_diagnostic_without_failing_current_build(self):
        with TemporaryDirectory() as directory:
            fixture = BoundaryFixture(Path(directory))
            fixture.tools.write_files([{'path': 'frontend/src/provider.js', 'content': 'new'}])
            with ExitStack() as stack:
                fixture.install_checks(stack)
                result = _prepare_local_candidate(fixture.config, fixture.tree, fixture.guard,
                                                  fixture.tools)
            self.assertEqual(fixture.source(), ('old', 'old'))
            self.assertTrue(next(check.passed for check in result.checks
                                 if check.name == 'frontend: npm run build'))
            note = next(check.output for check in result.checks
                        if check.name == 'candidate regression prevention')
            self.assertIn('before restore; these are not current-source results', note)
            self.assertIn('frontend: npm run build: exit=1', note)
            self.assertIn('shared import contract', _repair_feedback(result))
            self.assertFalse(result.passed)  # The restored baseline still needs its upgrade.

    def test_rejected_case_diagnostics_do_not_become_current_failure_signatures(self):
        with TemporaryDirectory() as directory:
            fixture = BoundaryFixture(Path(directory))
            rejected = CheckResult(PUBLIC_CHECK, False, 1,
                'REQ-9.1: rejected assertion: expected 1 to equal 2\n' + 'x' * 20000,
                case_statuses=fixture.public()[0].case_statuses)
            fixture.tools.write_files([{'path': 'frontend/src/provider.js', 'content': 'new'}])
            note = fixture.guard.consider((*fixture.build().checks, rejected))
            current = 'REQ-1.1: current assertion: expected 0 to equal 1'
            self.assertIn('REQ-9.1: rejected assertion', note)
            self.assertLess(len(note), 9000)
            self.assertEqual(_fixed_regression_signature(current + '\n' + note), (current,))
            self.assertEqual(fixture.source(), ('old', 'old'))

    def test_atomic_repair_completes_related_files_before_full_boundary(self):
        with TemporaryDirectory() as directory:
            fixture = BoundaryFixture(Path(directory))
            model = _make_client(None, None, [
                _FakeMessage([write_files(**{"frontend/src/provider.js": "new"})]),
                *[_FakeMessage([_read_files_call(["frontend/src/provider.js"])]) for _ in range(4)],
                _FakeMessage([write_files(**{"backend/src/consumer.js": "new"})]),
                _FakeMessage(None, "atomic migration complete")])
            self.assertFalse(fixture.implement(model, repair_feedback="Repair related file contract"))
            self.assertEqual(fixture.observed, [])
            self.assertEqual(fixture.boundaries, [("new", "new")])
            self.assertEqual(fixture.source(), ("new", "new"))
            self.assertEqual(fixture.tools.source_revision, 0)
            self.assertTrue(model.last_implementation_handoff)
            self.assertIsNone(model.last_budget_report)

    def test_initial_module_cross_file_failure_is_diagnosed_without_rollback(self):
        with TemporaryDirectory() as directory:
            fixture = BoundaryFixture(Path(directory))
            model = _make_client(None, None, [
                _FakeMessage([write_files(**{"frontend/src/provider.js": "new"})]),
                *[_FakeMessage([_read_files_call(["frontend/src/provider.js"])]) for _ in range(3)],
                _FakeMessage([write_files(**{"backend/src/consumer.js": "new"})]),
                _FakeMessage(None, "migration complete")])
            self.assertFalse(fixture.implement(model))
            self.assertEqual(fixture.observed, [("new", "old")])
            self.assertEqual(fixture.boundaries, [("new", "new")])
            self.assertEqual(fixture.source(), ("new", "new"))
            self.assertEqual(fixture.tools.source_revision, 0)
            self.assertEqual(fixture.guard.passing, {"REQ-1: preserved contract", "REQ-1: required upgrade"})
            self.assertTrue(model.last_implementation_handoff)
            self.assertIsNone(model.last_budget_report)
            self.assertIn(False, fixture.build_scopes)
            self.assertIn(True, fixture.build_scopes)

    def test_mid_behavior_regression_preserves_checkpoint_pointer_until_boundary(self):
        with TemporaryDirectory() as directory:
            fixture = BoundaryFixture(Path(directory), force_build=True)
            saved_files = dict(fixture.guard.files)
            saved_passes = fixture.guard.passing
            observed_files = []
            original_feedback = fixture.feedback
            def feedback(*, boundary):
                note = original_feedback(boundary=boundary)
                if not boundary:
                    observed_files.append((fixture.source(), dict(fixture.guard.files), fixture.guard.passing))
                return note
            fixture.feedback = feedback
            model = _make_client(None, None, [
                _FakeMessage([write_files(**{"frontend/src/provider.js": "broken"})]),
                *[_FakeMessage([_read_files_call(["frontend/src/provider.js"])]) for _ in range(3)],
                _FakeMessage(None, "done")])
            self.assertFalse(fixture.implement(model))
            self.assertEqual(observed_files, [(("broken", "old"), saved_files, saved_passes)])
            self.assertEqual(fixture.source(), ("old", "old"))
            self.assertEqual(fixture.tools.source_revision, 1)
            self.assertFalse(model.last_implementation_handoff)
            self.assertTrue(model.last_implementation_deferred)
            self.assertEqual(model.last_implementation_stop_reason, "checkpoint_failed")

    def test_module_boundary_restores_when_full_public_result_is_unavailable(self):
        for missing in (True, False):
            with self.subTest(missing_report=missing), TemporaryDirectory() as directory:
                fixture = BoundaryFixture(Path(directory), force_build=True,
                    missing_report=missing, incomplete_report=not missing)
                model = _make_client(None, None, [
                    _FakeMessage([write_files(**{"frontend/src/provider.js": "broken"})]),
                    *[_FakeMessage([_read_files_call(["frontend/src/provider.js"])]) for _ in range(3)],
                    _FakeMessage(None, "done")])
                self.assertFalse(fixture.implement(model))
                self.assertEqual(fixture.observed, [("broken", "old")])
                self.assertEqual(fixture.boundaries, [("broken", "old")])
                self.assertEqual(fixture.source(), ("old", "old"))
                self.assertEqual(fixture.tools.source_revision, 1)
                self.assertIn("latest behavioral result was unavailable", model.last_implementation_stop_detail)
                self.assertFalse(model.last_implementation_handoff)

    def test_boundary_restore_rebases_protocol_and_discards_reverted_write_summary(self):
        with TemporaryDirectory() as directory:
            fixture = BoundaryFixture(Path(directory), root_failure=True)
            model = _make_client(None, None, [
                _FakeMessage([write_files(**{"frontend/src/provider.js": "new"})]),
                _FakeMessage([write_files(**{"backend/src/consumer.js": "new"})]),
                *[_FakeMessage([_read_files_call(["frontend/src/provider.js"])]) for _ in range(2)],
                _FakeMessage([write_files(**{"frontend/src/provider.js": "ROLLBACK_ONLY_SOURCE",
                                            "frontend/src/rollback-new.js": "REMOVED_SOURCE"})]),
                _FakeMessage([_read_files_call(["frontend/src/rollback-new.js"])]),
                _FakeMessage(None, "remaining integration deferred")])
            with patch("agent.llm.MAX_CONVERSATION_CHARS", 1):
                self.assertFalse(fixture.implement(model))
            self.assertEqual(fixture.source(), ("new", "new"))
            self.assertEqual(fixture.tools.source_revision, 1)
            self.assertFalse((fixture.root / "frontend/src/rollback-new.js").exists())
            request = model.client.completions.requests[6]["messages"]
            context = json.dumps(request)
            self.assertIn("controller restored an earlier candidate", context)
            self.assertIn("new", context)
            for stale in ("ROLLBACK_ONLY_SOURCE", "REMOVED_SOURCE", "rollback-new.js"):
                self.assertNotIn(stale, context)
            self.assertFalse(any(message["role"] in {"assistant", "tool"} for message in request))
            self.assertFalse(model.last_implementation_handoff)

    def test_initial_module_allocation_checks_full_boundary_once(self):
        with TemporaryDirectory() as directory:
            fixture = BoundaryFixture(Path(directory))
            model = _make_client(None, None, [
                _FakeMessage([write_files(**{"frontend/src/provider.js": "broken"})])])
            self.assertFalse(fixture.implement(model, request_allowance=1))
            self.assertEqual(fixture.observed, [])
            self.assertEqual(fixture.boundaries, [("broken", "old")])
            self.assertEqual(fixture.source(), ("old", "old"))
            self.assertEqual(model.model_requests_used, 1)
            self.assertEqual(model.last_implementation_stop_reason, "allocation")
            self.assertTrue(model.last_implementation_deferred)
            self.assertIsNone(model.last_budget_report)

    def test_initial_module_no_progress_checks_full_boundary_once(self):
        with TemporaryDirectory() as directory:
            fixture = BoundaryFixture(Path(directory))
            model = _make_client(None, None, [
                _FakeMessage([write_files(**{"frontend/src/provider.js": "broken"})]),
                *[_FakeMessage([_read_files_call(["frontend/src/provider.js"])]) for _ in range(2)]],
                {"ARCBENCH_MAX_IDLE_TOOL_TURNS": "2"})
            self.assertFalse(fixture.implement(model))
            self.assertEqual(fixture.observed, [])
            self.assertEqual(fixture.boundaries, [("broken", "old")])
            self.assertEqual(fixture.source(), ("old", "old"))
            self.assertEqual(model.model_requests_used, 3)
            self.assertEqual(model.last_implementation_stop_reason, "no_progress")
            self.assertIsNone(model.last_budget_report)

    def test_request_reserve_and_tool_stop_enforce_without_another_model_request(self):
        for stop in ("delivery_request_reserve", "tool_call_budget"):
            with self.subTest(stop=stop), TemporaryDirectory() as directory:
                fixture = BoundaryFixture(Path(directory))
                model = _make_client(None, 1 if stop == "tool_call_budget" else None, [
                    _FakeMessage([write_files(**{"frontend/src/provider.js": "broken"})])])
                if stop == "delivery_request_reserve":
                    model.model_requests_used = 246
                self.assertTrue(fixture.implement(model))
                self.assertEqual(fixture.boundaries, [("broken", "old")])
                self.assertEqual(fixture.source(), ("old", "old"))
                self.assertEqual(model.client.completions.calls, 1)
                self.assertEqual(model.last_budget_report.budget, stop)
                self.assertEqual(model.last_implementation_stop_reason, stop)
                self.assertFalse(model.last_implementation_handoff)

    def test_non_deepseek_truncation_enforces_before_raising(self):
        with TemporaryDirectory() as directory:
            fixture = BoundaryFixture(Path(directory))
            model = _make_client(None, None, [
                _FakeMessage([write_files(**{"frontend/src/provider.js": "broken"})]),
                _FakeMessage(None, "partial", finish_reason="length")])
            with self.assertRaisesRegex(RuntimeError, "output limit"):
                fixture.implement(model)
            self.assertEqual(fixture.boundaries, [("broken", "old")])
            self.assertEqual(fixture.source(), ("old", "old"))
            self.assertEqual(model.last_implementation_stop_reason, "truncated")
            self.assertEqual(model.client.completions.calls, 2)

    def test_deadline_after_diagnostic_restores_at_final_delivery_without_model_calls(self):
        with TemporaryDirectory() as directory:
            fixture = BoundaryFixture(Path(directory), baseline_complete=True)
            model = _make_client(None, None, [
                _FakeMessage(None, "plan"),
                _FakeMessage([write_files(**{"frontend/src/provider.js": "broken"})]),
                *[_FakeMessage([_read_files_call(["frontend/src/provider.js"])]) for _ in range(3)]])
            original_create = model.client.completions.create
            at_deadline = []
            def complete(**kwargs):
                if model.client.completions.calls == 5:
                    at_deadline.append(fixture.source())
                    raise ModelRequestInterrupted(reason="absolute_deadline", deadline_seconds=.05,
                        stage="implementation", request=model.model_requests_used)
                return original_create(**kwargs)
            model.client.completions.create = MagicMock(side_effect=complete)
            model.log_usage = MagicMock()
            runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
            module = RequirementModule("REQ-1", "Shared contract", fixture.tree["children"][0])
            with ExitStack() as stack:
                fixture.install_checks(stack)
                stack.enter_context(patch("agent.orchestrator.prepare_web_template_structure"))
                stack.enter_context(patch("agent.orchestrator.ModelClient", return_value=model))
                stack.enter_context(patch("agent.orchestrator.ProjectTools", return_value=fixture.tools))
                stack.enter_context(patch("agent.orchestrator.CandidateCheckpoint", return_value=fixture.guard))
                visual = stack.enter_context(patch("agent.orchestrator.verify_visual_acceptance"))
                result = run_model_agent(runtime, fixture.config, fixture.tree, [module])
            self.assertEqual(at_deadline, [("broken", "old")])
            self.assertEqual(fixture.source(), ("old", "old"))
            self.assertEqual(fixture.tools.source_revision, 1)
            self.assertEqual(model.client.completions.create.call_count, 6)
            model.log_usage.assert_called_once_with()
            visual.assert_not_called()
            self.assertIsNone(model.last_budget_report)
            self.assertFalse(result.passed)
            self.assertTrue(result.ready_for_evaluation)
            report = json.loads((fixture.root / ".arc/delivery-report.json").read_text())
            self.assertEqual(report["formal_acceptance"], "pending")
            self.assertEqual(report["model_request_interruption"]["reason"], "absolute_deadline")

    def test_dependency_failure_is_diagnostic_until_boundary_restores_manifest(self):
        with TemporaryDirectory() as directory:
            fixture = BoundaryFixture(Path(directory), baseline_complete=True)
            manifest = fixture.root / "frontend/package.json"
            manifest.write_text('{"scripts":{"build":"vite build"}}', encoding="utf-8")
            fixture.guard.consider((*fixture.build().checks, *fixture.layout().checks, *fixture.public()))
            manifest.write_text('{"scripts":', encoding="utf-8")
            attempts = []
            def prepare():
                attempts.append(fixture.tools.source_revision)
                json.loads(manifest.read_text(encoding="utf-8"))
            fixture.tools.ensure_dependencies = MagicMock(side_effect=prepare)
            with ExitStack() as stack:
                fixture.install_checks(stack)
                diagnostic = _prepare_local_candidate(fixture.config, fixture.tree,
                    fixture.guard, fixture.tools, allow_restore=False)
                self.assertFalse(diagnostic.passed)
                self.assertEqual(manifest.read_text(), '{"scripts":')
                self.assertEqual(fixture.tools.source_revision, 0)
                boundary = _prepare_local_candidate(fixture.config, fixture.tree,
                    fixture.guard, fixture.tools, allow_restore=True)
            self.assertTrue(boundary.passed)
            self.assertEqual(attempts, [0, 0, 1])
            self.assertEqual(json.loads(manifest.read_text()), {"scripts": {"build": "vite build"}})
            self.assertEqual(fixture.tools.source_revision, 1)

    def test_dependency_failure_after_behavior_restore_returns_failed_check(self):
        with TemporaryDirectory() as directory:
            fixture = BoundaryFixture(Path(directory), baseline_complete=True)
            (fixture.root / "frontend/src/provider.js").write_text("broken", encoding="utf-8")
            fixture.tools.ensure_dependencies = MagicMock(side_effect=[None,
                RuntimeError("restored dependencies unavailable")])
            with ExitStack() as stack:
                fixture.install_checks(stack)
                boundary = _prepare_local_candidate(fixture.config, fixture.tree,
                    fixture.guard, fixture.tools, allow_restore=True)
            self.assertFalse(boundary.passed)
            self.assertEqual(fixture.source(), ("old", "old"))
            self.assertEqual(fixture.tools.source_revision, 1)
            self.assertEqual(fixture.tools.ensure_dependencies.call_count, 2)
            self.assertTrue(any(check.name == "project dependencies" and not check.passed
                                for check in boundary.checks))
            self.assertIn("restored dependencies unavailable", boundary.summary())


if __name__ == "__main__":
    unittest.main()

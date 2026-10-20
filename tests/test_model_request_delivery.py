"""Exercise interrupted requests through delivery without making network calls."""

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
from agent.model_transport import ModelRequestInterrupted
from agent.orchestrator import _finalize_delivery, _requirement_test_status, run_model_agent
from agent.requirements import RequirementModule
from agent.tools import ProjectTools
from agent.verify import CheckResult, VerificationResult
from tests.test_llm_budget import _make_client


def interruption(stage="implementation", request=1):
    return ModelRequestInterrupted(reason="absolute_deadline", deadline_seconds=0.05,
                                   stage=stage, request=request)


def project_checks(*, build=True, private_tests=True):
    checks = (CheckResult("frontend: npm run build", build, 0 if build else 1, "build"),)
    if not private_tests:
        checks += (CheckResult("frontend: npm run test", False, 1,
                               "Existing registration feedback assertion failed"),)
    return VerificationResult(all(check.passed for check in checks), checks)


def layout_check(*, passed=True):
    return VerificationResult(passed, (CheckResult("web template structure", passed,
                                                  0 if passed else 1, "layout"),))


def public_check(*, passed=True):
    return (CheckResult(PUBLIC_CHECK, passed, 0 if passed else 1,
                        "Protected local behavior remains separate from formal acceptance",
                        () if passed else ("REQ-1",),
                        (("REQ-1: preserved behavior", "passed" if passed else "failed"),)),)


def startup_check(*, passed=True):
    return (CheckResult("web runtime startup", passed, 0 if passed else 1,
                        "Started" if passed else "Generated server failed to start"),)


@_sandbox_mock.patch("agent.sandbox_context._WORKER", True)
class ModelRequestDeliveryTests(unittest.TestCase):
    def _run_manifest_interruption(self, root, *, mutation="invalid_frontend",
                                   saved_checkpoint=True, persistent_failure=False):
        frontend = root / "frontend/package.json"
        backend = root / "backend/package.json"
        baseline = {
            frontend: json.dumps({"name": "frontend", "scripts": {"build": "vite build"},
                "dependencies": {"react": "19.0.0"}, "devDependencies": {"vite": "6.0.0"}}),
            backend: json.dumps({"name": "backend", "scripts": {"start": "node src/app.js"},
                "dependencies": {"express": "4.21.2"}}),
        }
        for path, content in baseline.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        if saved_checkpoint:
            for name in ("frontend/src/App.jsx", "backend/src/app.js"):
                source = root / name
                source.parent.mkdir(parents=True)
                source.write_text("baseline source", encoding="utf-8")
        guard = CandidateCheckpoint(root)
        if saved_checkpoint:
            guard.consider((*project_checks().checks, *layout_check().checks, *public_check()))
            self.assertEqual(guard.files["frontend/package.json"], baseline[frontend].encode())
            self.assertEqual(guard.files["backend/package.json"], baseline[backend].encode())
        guard.consider = MagicMock(wraps=guard.consider)
        model = _make_client(None, None, [])
        model.last_request_interruption = None
        model.log_usage = MagicMock()
        model.implement = MagicMock(side_effect=AssertionError("Implementation ran after interruption"))
        model.review_requirements = MagicMock(side_effect=AssertionError("Audit ran after interruption"))
        runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
        node = {"id": "REQ-1"}
        tree = {"id": "ROOT", "children": [node]}
        config = SimpleNamespace(task_type="web", max_model_turns=None, max_tool_calls=None,
                                 output_dir=root, requirement_dir=root / "requirements")
        dependencies = []
        post_verify = []

        def validate_manifests():
            packages = {path: json.loads(path.read_text(encoding="utf-8")) for path in baseline}
            if not packages[frontend].get("scripts", {}).get("build"):
                raise RuntimeError("Frontend build script missing")
            if "express" not in packages[backend].get("dependencies", {}):
                raise RuntimeError("Backend express dependency missing")

        def plan(*args, **kwargs):
            model._record_model_request()
            if mutation == "invalid_frontend":
                frontend.write_text('{"dependencies":', encoding="utf-8")
            elif mutation == "deleted_backend":
                backend.unlink()
            else:
                package = json.loads(baseline[backend])
                package["dependencies"] = {}
                backend.write_text(json.dumps(package), encoding="utf-8")
            error = interruption("planning", model.model_requests_used)
            model.last_request_interruption = error
            raise error
        model.plan = MagicMock(side_effect=plan)

        def ensure_dependencies(tools):
            stopped = model.last_request_interruption is not None
            if stopped:
                dependencies.append(tools.source_revision)
            validate_manifests()
            if stopped and persistent_failure:
                raise RuntimeError("Dependency installation remains unavailable after manifest restoration")

        def verify(*args, **kwargs):
            validate_manifests()
            if model.last_request_interruption is not None:
                post_verify.append("full build and tests")
            return project_checks()

        def public(*args, **kwargs):
            validate_manifests()
            if kwargs.get("startup_only"):
                for path, content in baseline.items():
                    self.assertEqual(path.read_text(encoding="utf-8"), content)
                return startup_check()
            return public_check()

        with ExitStack() as stack:
            stack.enter_context(patch("agent.orchestrator.ModelClient", return_value=model))
            stack.enter_context(patch("agent.tools.ProjectTools.ensure_dependencies", autospec=True,
                                      side_effect=ensure_dependencies))
            constructor = stack.enter_context(patch("agent.orchestrator.CandidateCheckpoint", return_value=guard))
            stack.enter_context(patch("agent.orchestrator.verify_project", side_effect=verify))
            stack.enter_context(patch("agent.orchestrator.verify_web_template_structure", return_value=layout_check()))
            public_mock = stack.enter_context(patch("agent.orchestrator.verify_public_acceptance", side_effect=public))
            visual_mock = stack.enter_context(patch("agent.orchestrator.verify_visual_acceptance"))
            result = run_model_agent(runtime, config, tree, [RequirementModule("REQ-1", "Example", node)])
        constructor.assert_called_once_with(root)
        model.plan.assert_called_once()
        model.implement.assert_not_called()
        model.review_requirements.assert_not_called()
        visual_mock.assert_not_called()
        model.log_usage.assert_called_once_with()
        self.assertEqual(model.model_requests_used, 1)
        self.assertEqual(model.prompt_tokens_used + model.completion_tokens_used, 0)
        self.assertIsNone(model.last_budget_report)
        self.assertFalse(result.passed)
        self.assertTrue(any(call.kwargs.get("final") is True for call in guard.consider.call_args_list))
        self.assertIsNone(_requirement_test_status(result, "REQ-1"))
        runtime.events.mark_test_passed.assert_not_called()
        self.assertEqual([call.args[0] for call in runtime.events.mark_test_failed.call_args_list], ['ROOT'])
        report = json.loads((root / ".arc/delivery-report.json").read_text(encoding="utf-8"))
        self.assertFalse(report["local_verification_passed"])
        self.assertEqual(report["formal_acceptance"], "pending")
        self.assertEqual(report["model_request_interruption"]["request"], 1)
        return SimpleNamespace(result=result, guard=guard, dependencies=dependencies,
            post_verify=post_verify, public=public_mock, baseline=baseline)

    def test_interrupted_bad_manifest_restores_before_dependency_retry_and_delivery(self):
        for mutation in ("invalid_frontend", "deleted_backend", "wrong_dependency"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                outcome = self._run_manifest_interruption(root, mutation=mutation)
                self.assertEqual(outcome.dependencies, [0, 1])
                self.assertEqual(outcome.post_verify, ["full build and tests"])
                self.assertTrue(outcome.result.ready_for_evaluation)
                self.assertIn("candidate regression prevention", outcome.result.summary())
                self.assertFalse(any(check.name == "project dependencies" and not check.passed
                                     for check in outcome.result.checks))
                self.assertTrue(any(call.kwargs.get("startup_only") is True
                                    for call in outcome.public.call_args_list))
                for path, content in outcome.baseline.items():
                    self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_restored_manifest_with_persistent_dependency_failure_blocks_delivery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            outcome = self._run_manifest_interruption(root, persistent_failure=True)
            self.assertEqual(outcome.dependencies, [0, 1])
            self.assertEqual(outcome.post_verify, [])
            self.assertFalse(outcome.result.ready_for_evaluation)
            self.assertIn("Dependency installation remains unavailable", outcome.result.summary())
            self.assertTrue(any(check.name == "project dependencies" and not check.passed
                                for check in outcome.result.checks))
            self.assertFalse(any(call.kwargs.get("startup_only") is True
                                 for call in outcome.public.call_args_list))
            for path, content in outcome.baseline.items():
                self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_missing_checkpoint_cannot_retry_or_deliver_bad_dependencies(self):
        with TemporaryDirectory() as directory:
            outcome = self._run_manifest_interruption(Path(directory), saved_checkpoint=False)
            self.assertEqual(outcome.dependencies, [0])
            self.assertEqual(outcome.post_verify, [])
            self.assertIsNone(outcome.guard.files)
            self.assertFalse(outcome.result.ready_for_evaluation)
            self.assertTrue(any(check.name == "project dependencies" and not check.passed
                                for check in outcome.result.checks))
            outcome.public.assert_not_called()

    def _run_interruption(self, root, *, stage, swallow=False, regress=False):
        """Use actual ModelClient guards and CandidateCheckpoint with fake responses."""
        source = root / "frontend/src/App.jsx"
        source.parent.mkdir(parents=True)
        source.write_text("baseline", encoding="utf-8")
        server = root / "backend/src/app.js"
        server.parent.mkdir(parents=True)
        server.write_text("baseline server", encoding="utf-8")
        nodes = [{"id": "REQ-1", "name": "First"}, {"id": "REQ-2", "name": "Second"}]
        tree = {"id": "ROOT", "name": "Interrupted request example", "children": nodes,
                "visual_reference": ["https://example.invalid/reference.png"]}
        modules = [RequirementModule(node["id"], node["name"], node) for node in nodes]
        config = SimpleNamespace(task_type="web", max_model_turns=None, max_tool_calls=None,
                                 output_dir=root, requirement_dir=root / "requirements")
        runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
        model = _make_client(None, None, [])
        model.last_request_interruption = None
        model.last_implementation_handoff = False
        model.last_implementation_deferred = False
        model.log_usage = MagicMock()
        calls = []
        completed = []
        caught = []
        guard = CandidateCheckpoint(root)
        guard.consider = MagicMock(wraps=guard.consider)

        def record_response(phase):
            self.assertIsNone(model.last_request_interruption, "A later model phase ran after interruption")
            calls.append(phase)
            model._record_model_request()
            if phase == stage:
                if regress:
                    # A complete earlier edit must not justify retaining this
                    # incomplete final candidate after the request is aborted.
                    source.write_text("partial incompatible source", encoding="utf-8")
                error = interruption(phase, model.model_requests_used)
                model.last_request_interruption = error
                caught.append(error)
                if not swallow:
                    raise error
                return False
            model._record_usage(SimpleNamespace(usage=SimpleNamespace(
                prompt_tokens=100, completion_tokens=20)), stage=phase)
            completed.append(phase)
            return True

        def plan(*args, **kwargs):
            record_response("planning")
            return "Implement and verify the original requirements"

        def implement(**kwargs):
            phase = "integration" if kwargs["subtree"]["id"] == "ROOT" else "implementation"
            complete = record_response(phase)
            if complete:
                kwargs["project_tools"].write_file("frontend/src/App.jsx", f"complete edit {len(calls)}")
            return False

        def audit(*args, **kwargs):
            record_response("requirement_review")
            return ""

        def visual(*args, **kwargs):
            complete = record_response("visual_review")
            return VerificationResult(complete, (CheckResult("visual reference: example", complete,
                0 if complete else 1, "visual review completed" if complete else "Request interrupted"),))

        model.plan = MagicMock(side_effect=plan)
        model.implement = MagicMock(side_effect=implement)
        model.review_requirements = MagicMock(side_effect=audit)

        def verify(*args, **kwargs):
            return project_checks(build=source.read_text(encoding="utf-8") != "partial incompatible source")

        def public(*args, **kwargs):
            if kwargs.get("startup_only"):
                return startup_check()
            return public_check(passed=source.read_text(encoding="utf-8") != "partial incompatible source")

        with ExitStack() as stack:
            stack.enter_context(patch("agent.orchestrator.ModelClient", return_value=model))
            stack.enter_context(patch("agent.tools.ProjectTools.ensure_dependencies"))
            checkpoint = stack.enter_context(patch("agent.orchestrator.CandidateCheckpoint", return_value=guard))
            verify_mock = stack.enter_context(patch("agent.orchestrator.verify_project", side_effect=verify))
            stack.enter_context(patch("agent.orchestrator.verify_web_template_structure", return_value=layout_check()))
            public_mock = stack.enter_context(patch("agent.orchestrator.verify_public_acceptance", side_effect=public))
            visual_mock = stack.enter_context(patch("agent.orchestrator.verify_visual_acceptance", side_effect=visual))
            result = run_model_agent(runtime, config, tree, modules)
        return SimpleNamespace(result=result, runtime=runtime, model=model, calls=calls, completed=completed,
            interruption=caught[0], guard=guard, checkpoint=checkpoint, verify=verify_mock,
            public=public_mock, visual=visual_mock, source=source)

    def _assert_stopped_honestly(self, outcome, root):
        self.assertEqual(outcome.calls[-1], outcome.interruption.stage)
        self.assertEqual(outcome.model.model_requests_used, len(outcome.completed) + 1)
        self.assertEqual(outcome.model.prompt_tokens_used, 100 * len(outcome.completed))
        self.assertEqual(outcome.model.completion_tokens_used, 20 * len(outcome.completed))
        self.assertIsNone(outcome.model.last_budget_report, "Timeout was mislabeled as budget exhaustion")
        outcome.model.log_usage.assert_called_once_with()
        self.assertFalse(outcome.result.passed)
        self.assertTrue(outcome.result.ready_for_evaluation)
        outcome.runtime.events.mark_test_passed.assert_not_called()
        self.assertEqual([call.args[0] for call in outcome.runtime.events.mark_test_failed.call_args_list], ['ROOT'])
        for node_id in ("REQ-1", "REQ-2"):
            self.assertIsNone(_requirement_test_status(outcome.result, node_id))
        self.assertTrue(any(call.kwargs.get("final") is True for call in outcome.guard.consider.call_args_list))
        self.assertTrue(any(call.kwargs.get("startup_only") is True for call in outcome.public.call_args_list))
        report = json.loads((root / ".arc/delivery-report.json").read_text(encoding="utf-8"))
        self.assertFalse(report["local_verification_passed"])
        self.assertEqual(report["formal_acceptance"], "pending")
        self.assertIsNone(report["budget"])
        self.assertEqual(report["model_request_interruption"]["reason"], "absolute_deadline")
        self.assertEqual(report["model_request_interruption"]["request"], outcome.interruption.request)

    def test_interruption_stops_every_model_stage_and_finalizes_once(self):
        for stage in ("planning", "implementation", "integration", "requirement_review", "visual_review"):
            with self.subTest(stage=stage), TemporaryDirectory() as directory:
                root = Path(directory)
                outcome = self._run_interruption(root, stage=stage)
                self._assert_stopped_honestly(outcome, root)
                self.assertGreaterEqual(outcome.verify.call_count, 2)
                if stage != "visual_review":
                    outcome.visual.assert_not_called()

    def test_swallowed_interruption_latch_still_stops_subsequent_model_calls(self):
        for stage in ("implementation", "requirement_review", "visual_review"):
            with self.subTest(stage=stage), TemporaryDirectory() as directory:
                root = Path(directory)
                outcome = self._run_interruption(root, stage=stage, swallow=True)
                self._assert_stopped_honestly(outcome, root)

    def test_shared_final_checkpoint_restores_partial_source_after_interruption(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            outcome = self._run_interruption(root, stage="implementation", regress=True)
            self._assert_stopped_honestly(outcome, root)
            outcome.checkpoint.assert_called_once_with(root)
            self.assertEqual(outcome.source.read_text(encoding="utf-8"), "baseline")
            self.assertIn("candidate regression prevention", outcome.result.summary())
            self.assertNotIn("partial incompatible source", outcome.guard.files["frontend/src/App.jsx"].decode())

    def test_runnable_failed_candidate_needs_startup_and_preserves_failures(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checks = (*project_checks(private_tests=False).checks, *layout_check().checks,
                      *public_check(passed=False))
            error = interruption(request=4)
            with patch("agent.orchestrator.verify_public_acceptance", return_value=startup_check()) as startup:
                result = _finalize_delivery(root, VerificationResult(False, checks), None, interruption=error)
            self.assertFalse(result.passed)
            self.assertTrue(result.ready_for_evaluation)
            self.assertIn("Existing registration feedback assertion failed", result.summary())
            self.assertTrue(any(check.name == PUBLIC_CHECK and not check.passed for check in result.checks))
            startup.assert_called_once_with(root, {}, startup_only=True)
            report = json.loads((root / ".arc/delivery-report.json").read_text(encoding="utf-8"))
            self.assertFalse(report["local_verification_passed"])
            self.assertEqual(report["formal_acceptance"], "pending")
            self.assertEqual(report["model_request_interruption"]["request"], 4)

    def test_unrunnable_candidate_cannot_be_delivered_after_interruption(self):
        base = (*project_checks().checks, *layout_check().checks)
        cases = {
            "build": (*project_checks(build=False).checks, *layout_check().checks),
            "layout": (*project_checks().checks, *layout_check(passed=False).checks),
            "dependencies": (*base, CheckResult("project dependencies", False, 1, "Install failed")),
            "missing build": layout_check().checks,
        }
        for name, checks in cases.items():
            with self.subTest(reason=name), TemporaryDirectory() as directory, patch(
                    "agent.orchestrator.verify_public_acceptance") as startup:
                result = _finalize_delivery(Path(directory), VerificationResult(False, checks), None,
                                            interruption=interruption())
                self.assertFalse(result.passed)
                self.assertFalse(result.ready_for_evaluation)
                startup.assert_not_called()
        for startup_result in (startup_check(passed=False), ()):
            with self.subTest(startup=startup_result), TemporaryDirectory() as directory, patch(
                    "agent.orchestrator.verify_public_acceptance", return_value=startup_result):
                result = _finalize_delivery(Path(directory), VerificationResult(False, base), None,
                                            interruption=interruption())
                self.assertFalse(result.ready_for_evaluation)

    def test_sticky_interruption_does_not_count_another_request_or_invent_usage(self):
        model = _make_client(None, None, [])
        model._record_model_request()
        model._record_usage(SimpleNamespace(usage=SimpleNamespace(prompt_tokens=90, completion_tokens=20)))
        model._record_model_request()
        error = interruption(request=2)
        model.last_request_interruption = error
        for next_step in (model.raise_if_request_interrupted, model._record_model_request):
            with self.assertRaises(ModelRequestInterrupted) as caught:
                next_step()
            self.assertIs(caught.exception, error)
            self.assertEqual(model.model_requests_used, 2)
            self.assertEqual(model.prompt_tokens_used, 90)
            self.assertEqual(model.completion_tokens_used, 20)
        self.assertIsNone(model.last_budget_report)
        self.assertEqual(model.client.completions.calls, 0)


if __name__ == "__main__":
    unittest.main()

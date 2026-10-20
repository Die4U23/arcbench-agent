
# Legacy fixtures exercise worker internals; host isolation is tested separately.
import unittest.mock as _sandbox_mock
from contextlib import ExitStack
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agent.candidate_checkpoint import PUBLIC_CHECK
from agent.orchestrator import _prepare_local_candidate, run_model_agent
from agent.requirements import RequirementModule
from agent.verify import CheckResult, VerificationResult


@_sandbox_mock.patch("agent.sandbox_context._WORKER", True)
class DeliveryReviewRevisionTests(unittest.TestCase):
    def test_final_restore_archives_old_reviews_and_starts_restored_source(self):
        self._run_case(coverage_missing=True)

    def test_old_passed_audit_is_not_final_evidence_and_visual_failure_still_blocks(self):
        self._run_case(coverage_missing=False)

    def _run_case(self, *, coverage_missing):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "frontend/src/App.jsx"
            source.parent.mkdir(parents=True)
            source.write_text("initial source", encoding="utf-8")
            retained_source = "complete candidate source"
            tree = {"id": "ROOT", "children": [{"id": "REQ-3.2"}]}
            config = SimpleNamespace(task_type="web", max_model_turns=None,
                max_tool_calls=None, output_dir=root,
                requirement_dir=root / "requirements")
            runtime = SimpleNamespace(events=MagicMock(), traceability=MagicMock())
            tools = SimpleNamespace(written_paths=[], source_revision=0,
                ensure_dependencies=lambda: None)
            model = MagicMock()
            model.plan.return_value = "Integrate the complete project"
            model.last_budget_report = None
            model.last_request_interruption = None
            model.last_implementation_deferred = False
            model.review_requirements.return_value = (
                "Booking confirmation still needs source coverage." if coverage_missing else "")
            old_visual_checks = (
                CheckResult("visual reference: homepage", True, 0,
                    "Screenshot was checked for the earlier candidate."),
                CheckResult("browser interaction: booking journey", coverage_missing,
                    0 if coverage_missing else 1,
                    "Declared interaction was checked for the earlier candidate."),
            )
            startup_sources = []
            final_entry_sources = []

            def implement(**kwargs):
                model.last_implementation_stop_reason = "no_progress"
                if model.implement.call_count == 1:
                    source.write_text(retained_source, encoding="utf-8")
                    tools.written_paths.append("frontend/src/App.jsx")
                # The ROOT repair does not change production bytes, so it earns
                # no continuation and reaches the actual bounded-stop branch.
                return False

            def build(_project, **kwargs):
                healthy = source.read_text(encoding="utf-8") != "broken final source"
                return VerificationResult(healthy, (
                    CheckResult("frontend: npm run build", healthy,
                        0 if healthy else 1, "build reads the current source"),))

            def public(_project, _tree, **kwargs):
                if kwargs.get("startup_only"):
                    startup_sources.append(source.read_text(encoding="utf-8"))
                    return (CheckResult("web runtime startup", True, 0,
                        "Restored candidate served HTTP 200."),)
                return (CheckResult(PUBLIC_CHECK, True, 0, "complete local report",
                    (), (("REQ-3.2: local booking journey", "passed"),)),)

            def prepare(*args, **kwargs):
                if kwargs.get("final"):
                    # Inject a damaged current file at the final boundary. The
                    # real checkpoint must restore bytes before the startup probe;
                    # previous review verdicts refer to the pre-restore revision.
                    source.write_text("broken final source", encoding="utf-8")
                    final_entry_sources.append(source.read_text(encoding="utf-8"))
                return _prepare_local_candidate(*args, **kwargs)

            model.implement.side_effect = implement
            with ExitStack() as stack:
                stack.enter_context(patch("agent.orchestrator.ModelClient", return_value=model))
                stack.enter_context(patch("agent.orchestrator.ProjectTools", return_value=tools))
                stack.enter_context(patch("agent.orchestrator.collect_visual_references",
                    return_value=["homepage.png"]))
                stack.enter_context(patch("agent.orchestrator.verify_project", side_effect=build))
                stack.enter_context(patch("agent.orchestrator.verify_web_template_structure",
                    return_value=VerificationResult(True, (
                        CheckResult("web template structure", True, 0, "layout"),))))
                stack.enter_context(patch("agent.orchestrator.verify_public_acceptance", side_effect=public))
                visual = stack.enter_context(patch("agent.orchestrator.verify_visual_acceptance",
                    return_value=VerificationResult(coverage_missing, old_visual_checks)))
                final_prepare = stack.enter_context(patch("agent.orchestrator._prepare_local_candidate",
                    side_effect=prepare))
                result = run_model_agent(runtime, config, tree,
                    [RequirementModule("ROOT", "Complete project", tree)])

            self.assertEqual(model.implement.call_count, 2)
            self.assertEqual(model.review_requirements.call_count, 2)
            self.assertEqual(visual.call_count, 2)
            self.assertEqual(final_prepare.call_count, 1)
            self.assertEqual(final_entry_sources, ["broken final source"])
            self.assertEqual(startup_sources, [retained_source] if coverage_missing else [])
            self.assertEqual(source.read_text(encoding="utf-8"), retained_source)
            self.assertEqual(tools.source_revision, 1)
            self.assertEqual(result.ready_for_evaluation, coverage_missing)
            self.assertFalse(result.passed)
            old_passed_names = {check.name for check in old_visual_checks if check.passed}
            if not coverage_missing:
                old_passed_names.add("requirement coverage")
            self.assertFalse(any(check.name in old_passed_names
                for check in result.checks))
            old_failure_name = "requirement coverage" if coverage_missing else "browser interaction: booking journey"
            failure = next(check for check in result.checks if check.name == old_failure_name)
            self.assertFalse(failure.passed)
            self.assertIn("Earlier source revision evidence", failure.output)
            self.assertIn("final restored source was not re-audited", failure.output)

            report = json.loads((root / ".arc/delivery-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["generation_stop_reason"], "bounded_repair_exhausted")
            self.assertIsNone(report["budget"])
            self.assertFalse(report["local_verification_passed"])
            self.assertEqual(report["formal_acceptance"], "pending")
            original_reviews = [
                asdict(CheckResult("requirement coverage", not coverage_missing,
                    1 if coverage_missing else 0,
                    model.review_requirements.return_value or
                    "Source audit found no missing required behavior; platform acceptance remains separate.")),
                *(asdict(check) for check in old_visual_checks),
            ]
            self.assertEqual(report["earlier_revision_reviews"],
                json.loads(json.dumps(original_reviews)))
            self.assertFalse(any(check["name"] in old_passed_names
                for check in report["checks"]))


if __name__ == "__main__":
    unittest.main()

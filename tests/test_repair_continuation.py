
# Legacy fixtures exercise worker internals; host isolation is tested separately.
import unittest.mock as _sandbox_mock
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agent.candidate_checkpoint import PUBLIC_CHECK
from agent.browser_contract import BROWSER_CHECK
from agent.llm import BudgetExhaustion
from agent.orchestrator import _source_fingerprint, run_model_agent
from agent.requirements import RequirementModule
from agent.verify import CheckResult, VerificationResult


@_sandbox_mock.patch("agent.sandbox_context._WORKER", True)
class RepairContinuationTests(unittest.TestCase):
    def run_repairs(self, steps, *, families=("REQ-1.1", "REQ-2.1"), status_for=None,
                    complete_report=True, case_names=None, native_ids=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "frontend/src/App.jsx"
            tree = {"id": "ROOT", "children": [{"id": node} for node in families]}
            config = SimpleNamespace(task_type="web", max_model_turns=None, max_tool_calls=None,
                output_dir=root, requirement_dir=root / "requirements")
            tools = SimpleNamespace(written_paths=[], ensure_dependencies=lambda: None)
            model = MagicMock(plan=MagicMock(return_value="plan"),
                              review_requirements=MagicMock(return_value=""))
            model.last_budget_report = None
            repairs = []

            def implement(**kwargs):
                tools.written_paths.append("frontend/src/App.jsx")
                if not kwargs.get("repair_feedback"):
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.write_text("baseline", encoding="utf-8")
                    model.last_implementation_deferred = False
                    model.last_implementation_stop_reason = "model_finished"
                    return False
                self.assertLess(len(repairs), len(steps), "unexpected extra repair slice")
                step = steps[len(repairs)]
                repairs.append(kwargs["subtree"]["id"])
                self.assertEqual(repairs[-1], step["family"])
                self.assertEqual(kwargs["request_allowance"], 12)
                if "source" in step:
                    source.write_text(step["source"], encoding="utf-8")
                reason = step["reason"]
                model.last_implementation_deferred = reason != "model_finished"
                model.last_implementation_stop_reason = reason
                if reason == "total_token_budget":
                    model.last_budget_report = BudgetExhaustion(
                        reason, 10_000_000, 10_000_000, 1, (), 250, 300, 250)
                    return True
                model.last_budget_report = None
                return False

            model.implement.side_effect = implement
            full_regressions = []

            def public_checks(_project, _tree, **kwargs):
                if kwargs.get("startup_only"):
                    return (CheckResult("web runtime startup", True, 0, "HTTP 200"),)
                content = source.read_text(encoding="utf-8")
                passed = set(status_for(content)) if status_for is not None else set()
                full_regressions.append(content)
                statuses = tuple((case_names[index] if case_names else node + ": required behavior",
                                  "passed" if node in passed else "failed")
                                 for index, node in enumerate(families))
                # Volatile diagnostics must not reset a family's allowance.
                output = "\n".join(node + f": AssertionError: expected behavior, diagnostic {len(full_regressions)}"
                                   for node in families if node not in passed)
                public = CheckResult(PUBLIC_CHECK, len(passed) == len(families),
                    0 if len(passed) == len(families) else 1, output,
                    case_statuses=statuses if complete_report else ())
                native = ((CheckResult(BROWSER_CHECK, False, 1, 'Native location failed',
                    native_ids, (('local native location', 'failed'),)),) if native_ids else ())
                return (public, *native)

            project = VerificationResult(True, (CheckResult("frontend: npm run build", True, 0, "built"),))
            layout = VerificationResult(True, (CheckResult("web template structure", True, 0, "layout"),))
            with ExitStack() as stack:
                for name, value in (("ModelClient", model), ("ProjectTools", tools),
                                    ("verify_project", project), ("verify_web_template_structure", layout)):
                    stack.enter_context(patch("agent.orchestrator." + name, return_value=value))
                stack.enter_context(patch("agent.orchestrator.verify_public_acceptance", side_effect=public_checks))
                result = run_model_agent(SimpleNamespace(events=MagicMock(), traceability=MagicMock()),
                    config, tree, [RequirementModule("ROOT", "all", tree)])
            self.assertEqual(len(repairs), len(steps))
            return SimpleNamespace(result=result, repairs=repairs, regressions=full_regressions,
                source=source.read_text(encoding="utf-8"),
                report=json.loads((root / ".arc/delivery-report.json").read_text(encoding="utf-8")))

    def test_local_stalls_rotate_to_other_families_without_claiming_success(self):
        observed = self.run_repairs([
            {"family": "REQ-1.1", "reason": "no_progress"},
            {"family": "REQ-2.1", "reason": "repeated_tool_failure"},
            {"family": "REQ-3.2", "reason": "truncated"},
        ], families=("REQ-1.1", "REQ-2.1", "REQ-3.2"))
        self.assertEqual(observed.repairs, ["REQ-1.1", "REQ-2.1", "REQ-3.2"])
        self.assertEqual(len(observed.regressions), 5)  # Includes final local delivery verification.
        self.assertFalse(observed.result.passed)
        self.assertTrue(observed.result.ready_for_evaluation)
        self.assertIsNone(observed.report["budget"])
        self.assertEqual(observed.report["generation_stop_reason"], "bounded_repair_exhausted")

    def test_exhausted_native_families_do_not_hide_untried_public_family(self):
        observed = self.run_repairs([
            {'family': 'REQ-2.1', 'reason': 'no_progress'},
            {'family': 'REQ-2.2', 'reason': 'no_progress'},
            {'family': 'REQ-3.1', 'reason': 'no_progress'},
        ], families=('REQ-2.1', 'REQ-2.2', 'REQ-3.1'), native_ids=('REQ-2.1', 'REQ-2.2'))
        self.assertEqual(observed.repairs, ['REQ-2.1', 'REQ-2.2', 'REQ-3.1'])
        self.assertFalse(observed.result.passed)

    def test_truncated_slice_with_retained_writes_gets_only_one_fair_continuation(self):
        observed = self.run_repairs([
            {'family': 'REQ-1.1', 'reason': 'truncated', 'source': 'complete edit before cutoff'},
            {'family': 'REQ-2.1', 'reason': 'no_progress'},
            {'family': 'REQ-1.1', 'reason': 'truncated', 'source': 'another complete edit'},
        ])
        self.assertEqual(observed.repairs, ['REQ-1.1', 'REQ-2.1', 'REQ-1.1'])
        self.assertEqual(observed.source, 'another complete edit')
        self.assertEqual(observed.report['generation_stop_reason'], 'bounded_repair_exhausted')

    def test_truncated_slice_without_complete_write_does_not_get_continuation(self):
        observed = self.run_repairs([
            {'family': 'REQ-1.1', 'reason': 'truncated'},
        ], families=('REQ-1.1',))
        self.assertEqual(observed.source, 'baseline')
        self.assertEqual(observed.repairs, ['REQ-1.1'])

    def test_truncated_slice_with_reverted_write_does_not_get_continuation(self):
        observed = self.run_repairs([
            {'family': 'REQ-2.1', 'reason': 'truncated', 'source': 'lost passing registration'},
        ], status_for=lambda source: {'REQ-1.1'} if source == 'baseline' else set())
        self.assertEqual(observed.source, 'baseline')
        self.assertEqual(observed.repairs, ['REQ-2.1'])

    def test_partial_allocated_repair_continues_after_other_family_has_a_turn(self):
        observed = self.run_repairs([
            {"family": "REQ-1.1", "reason": "allocation", "source": "partial validation wiring"},
            {"family": "REQ-2.1", "reason": "no_progress"},
            {"family": "REQ-1.1", "reason": "model_finished", "source": "complete validation wiring"},
        ], status_for=lambda source: {"REQ-1.1"} if source.startswith("complete") else set())
        self.assertEqual(observed.repairs, ["REQ-1.1", "REQ-2.1", "REQ-1.1"])
        self.assertEqual(len(observed.regressions), 5)
        self.assertFalse(observed.result.passed)  # The other family's failure remains.
        self.assertTrue(observed.result.ready_for_evaluation)
        self.assertEqual(observed.report["formal_acceptance"], "pending")

    def test_each_family_has_at_most_one_continuation_despite_changing_diagnostics(self):
        observed = self.run_repairs([
            {"family": "REQ-1.1", "reason": "allocation", "source": "revision one"},
            {"family": "REQ-2.1", "reason": "allocation", "source": "revision two"},
            {"family": "REQ-1.1", "reason": "allocation", "source": "revision three"},
            {"family": "REQ-2.1", "reason": "allocation", "source": "revision four"},
        ])
        self.assertEqual(observed.repairs, ["REQ-1.1", "REQ-2.1", "REQ-1.1", "REQ-2.1"])
        self.assertEqual(len(observed.regressions), 6)
        self.assertFalse(observed.result.passed)

    def test_real_vitest_names_give_all_six_families_first_slice_before_one_continuation(self):
        names = (
            ' REQ-1.1: successfully register a new account and remain signed in',
            ' REQ-1.2: sign in with a valid username and password',
            ' REQ-2.1: search with valid criteria and display matching train results',
            ' REQ-2.2: select a train from the result list and open the booking page',
            ' REQ-3.1: open the booking page and display the selected train summary',
            ' REQ-3.2: submit valid passenger information and create a booking record',
        )
        families = tuple(name.strip().split(':', 1)[0] for name in names)
        # Every retained edit earns one continuation, never a third slice.
        steps = [{'family': node, 'reason': 'allocation', 'source': f'revision {index}'}
                 for index, node in enumerate(families * 2, start=1)]
        observed = self.run_repairs(steps, families=families, case_names=names)
        self.assertEqual(observed.repairs, list(families * 2))
        self.assertEqual(len(observed.regressions), 14)
        self.assertFalse(observed.result.passed)
        self.assertTrue(observed.result.ready_for_evaluation)
        self.assertEqual(observed.report['formal_acceptance'], 'pending')

    def test_continuation_can_finish_all_local_checks_without_claiming_formal_acceptance(self):
        observed = self.run_repairs([
            {"family": "REQ-1.1", "reason": "allocation", "source": "partial required behavior"},
            {"family": "REQ-1.1", "reason": "model_finished", "source": "complete required behavior"},
        ], families=("REQ-1.1",),
           status_for=lambda source: {"REQ-1.1"} if source.startswith("complete") else set())
        self.assertTrue(observed.result.passed)
        self.assertEqual(len(observed.regressions), 3)
        self.assertTrue(observed.report["local_verification_passed"])
        self.assertEqual(observed.report["formal_acceptance"], "pending")

    def test_repeated_identical_write_does_not_earn_an_allocated_continuation(self):
        for reason in ("allocation", "model_finished", "checkpoint_failed"):
            with self.subTest(reason=reason):
                observed = self.run_repairs([
                    {"family": "REQ-1.1", "reason": reason, "source": "baseline"},
                ], families=("REQ-1.1",))
                self.assertEqual(len(observed.regressions), 3)
                self.assertEqual(observed.source, "baseline")

    def test_model_finished_or_failed_checkpoint_with_progress_gets_one_fair_continuation(self):
        for reason in ("model_finished", "checkpoint_failed"):
            with self.subTest(reason=reason):
                observed = self.run_repairs([
                    {"family": "REQ-1.1", "reason": reason, "source": "partial required behavior"},
                    {"family": "REQ-2.1", "reason": "no_progress"},
                    {"family": "REQ-1.1", "reason": "model_finished", "source": "complete required behavior"},
                ], status_for=lambda source: {"REQ-1.1"} if source.startswith("complete") else set())
                self.assertEqual(observed.repairs, ["REQ-1.1", "REQ-2.1", "REQ-1.1"])
                self.assertFalse(observed.result.passed)
                self.assertTrue(observed.result.ready_for_evaluation)

    def test_reverted_source_does_not_earn_a_continuation(self):
        observed = self.run_repairs([
            {"family": "REQ-2.1", "reason": "allocation", "source": "regressed existing behavior"},
        ], status_for=lambda source: {"REQ-1.1"} if source == "baseline" else set())
        self.assertEqual(observed.source, "baseline")
        self.assertEqual(len(observed.regressions), 4)  # Restore recheck and final delivery verification.
        self.assertFalse(observed.result.passed)

    def test_true_global_budget_exhaustion_stops_before_the_next_family(self):
        observed = self.run_repairs([
            {"family": "REQ-1.1", "reason": "total_token_budget"},
        ])
        self.assertEqual(observed.repairs, ["REQ-1.1"])
        self.assertEqual(len(observed.regressions), 2)
        self.assertFalse(observed.result.passed)
        self.assertEqual(observed.report["budget"]["budget"], "total_token_budget")
        self.assertEqual(observed.report["formal_acceptance"], "pending")

    def test_incomplete_report_uses_bounded_root_family_despite_message_changes(self):
        observed = self.run_repairs([
            {"family": "ROOT", "reason": "no_progress"},
        ], families=("REQ-1.1",), complete_report=False)
        self.assertEqual(len(observed.regressions), 3)
        self.assertFalse(observed.result.passed)

    def test_fingerprint_ignores_runtime_tests_and_mtimes_but_detects_production_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            production = root / "frontend/src/App.jsx"
            production.parent.mkdir(parents=True)
            production.write_text("export default function App() {}", encoding="utf-8")
            before = _source_fingerprint(root)
            production.touch()
            for name in (".arc/events.jsonl", "backend/data/accounts.json", "backend/db.json",
                         "frontend/src/__tests__/generated.jsx", "frontend/src/App.test.jsx",
                         "requirements/support.js", "node_modules/example/index.js"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("unrelated mutation", encoding="utf-8")
            self.assertEqual(_source_fingerprint(root), before)
            production.write_text("export default function App() { return 'fixed'; }", encoding="utf-8")
            self.assertNotEqual(_source_fingerprint(root), before)


if __name__ == "__main__":
    unittest.main()

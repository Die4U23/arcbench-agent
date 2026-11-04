from pathlib import Path
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "platform-agent"))
sys.path.insert(0, str(root / "platform-agent/arcbench-agent-runtime/src"))

import agent.platform_generation as module
from agent.platform_runtime_check import RuntimeCheckResult, RuntimeFailure, tool as runtime_tool
from agent.platform_scaffold import SCAFFOLD_FILES, SRC_APP_JSX
from agent.platform_static import validate_source
from agent.sandbox_files import snapshot_project

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "runtime_check"
HAS_NODE = bool(shutil.which("node") and runtime_tool("npm"))

REQUIREMENTS = """\
id: ROOT
name: Orchard Ledger
type: FOLDER
description: A synthetic application that is not registered anywhere.
environment: {}
data: []
children:
  - id: REQ-1
    name: Overview page
    type: ATOMIC
    description: Show the overview heading.
    scenarios:
      - id: REQ-1-S1
        name: Overview heading
        steps:
          - Given: A visitor with an existing account member_one@example.org opens the site.
            When: The overview page loads.
            Then: The heading Overview is visible.
    children: []
  - id: REQ-2
    name: Ledger page
    type: ATOMIC
    description: Show the ledger list.
    scenarios:
      - id: REQ-2-S1
        name: Ledger list
        steps:
          - Given: The visitor is on the overview page.
            When: The visitor opens the ledger.
            Then: The ledger list is visible.
    children: []
"""

SPEC_ONE = """\
import { test, expect } from '@playwright/test';
import { ACCOUNT } from './helpers';

test('overview heading', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
  expect(ACCOUNT.email).toBe('member_one@example.org');
});
"""

SPEC_TWO = """\
import { test, expect } from '@playwright/test';

test('ledger list', async ({ page }) => {
  await page.goto('/ledger');
  await expect(page.getByRole('list', { name: 'Ledger' })).toBeVisible();
});
"""

HELPERS = """\
// Existing account prepared before scenarios start
export const ACCOUNT = { id: 'acct-1', email: 'member_one@example.org', password: 'Orchard!2026' };
"""

APP_REPAIRED = """import React from 'react';

export function App() {
  return (
    <main>
      <h1>Overview</h1>
    </main>
  );
}

export default App;
"""

AUTHORED_SPEC = """import { test, expect } from '@playwright/test';

test('authored check', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Runtime Check Demo' })).toBeVisible();
});
"""


class LeaveMissing:
    """Scripted implement step that leaves the coverage ledger untouched."""

    def __call__(self, tools):
        return None


def write_acceptance_docs(app):
    docs = Path(app) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "task-ir.json").write_text('{"work_items":[],"ui_elements":[],"scenarios":[]}\n', encoding="utf-8")
    (docs / "coverage.json").write_text('{"items":[]}\n', encoding="utf-8")


def failed_result(passed, failed, name="REQ-1 > overview heading"):
    return RuntimeCheckResult(
        status="failed", stage="tests", passed=passed, failed=failed, total=passed + failed,
        failures=[RuntimeFailure(name, "REQ-1.spec.ts",
                                 "Error: expect(locator).toBeVisible() failed\nLocator: getByRole('heading', { name: 'Overview' })")],
        env_supplied={"PLAYWRIGHT_BASE_URL": "http://127.0.0.1:1"})


def passed_result(total):
    return RuntimeCheckResult(status="passed", stage="tests", passed=total, failed=0, total=total)


class ScriptedModel:
    """Offline stand-in for PlatformGenerationModel: runs scripted tool edits per implement call."""

    def __init__(self, steps=()):
        self.steps = list(steps)
        self.calls = []
        self.reviews = []
        self.author_calls = []

    def review_sources(self, packet, tools, *, feedback=None, request_allowance=None):
        self.reviews.append(dict(feedback=feedback, request_allowance=request_allowance))
        return dict(status="complete", summary="scripted-review", changed_paths=[], turns=1)

    def _cover(self, tools):
        path = Path(tools.project_dir) / "docs" / "coverage.json"
        if not path.is_file():
            return
        document = json.loads(path.read_text(encoding="utf-8"))
        changed = False
        for item in document.get("items") or []:
            if isinstance(item, dict) and item.get("status") == "missing":
                item["status"] = "covered"
                sources = snapshot_project(Path(tools.project_dir))
                item["files"] = item.get("files") or [
                    next(name for name in sources if name.endswith((".jsx", ".js", ".html")))
                ]
                changed = True
        if changed:
            tools.call("write_files", {"files": [{"path": "docs/coverage.json", "content": json.dumps(document, indent=2) + "\n"}]})

    def implement(self, packet, tools, *, feedback=None, request_allowance=None):
        text = getattr(packet, "text", "") or ""
        if text.startswith("Write one Playwright test per scenario."):
            # Authored specs are a separate model call. They must not consume repair steps.
            self.author_calls.append(text)
            return dict(status="complete", summary=AUTHORED_SPEC, spec=AUTHORED_SPEC, turns=1, changed_paths=[])
        self.calls.append(dict(packet=packet, feedback=feedback, request_allowance=request_allowance,
                               selection=list(packet.manifest["requirement_ids"])))
        step = self.steps.pop(0) if self.steps else None
        if callable(step):
            step(tools)
        if not isinstance(step, LeaveMissing):
            self._cover(tools)
        return dict(status="complete", summary="scripted", changed_paths=[], turns=1)


def write_app(content):
    def step(tools):
        tools.call("write_files", {"files": [{"path": "src/App.jsx", "content": content}]})
    return step


class PlatformIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.task = self.path / "task"
        (self.task / "tests").mkdir(parents=True)
        (self.task / "requirements.yaml").write_text(REQUIREMENTS, encoding="utf-8")
        (self.task / "tests" / "REQ-1.spec.ts").write_text(SPEC_ONE, encoding="utf-8")
        (self.task / "tests" / "REQ-2.spec.ts").write_text(SPEC_TWO, encoding="utf-8")
        (self.task / "tests" / "helpers.ts").write_text(HELPERS, encoding="utf-8")
        self.app = self.path / "application"
        self.reports = self.app / ".arc" / "platform-budget" / "orchard-ledger"

    def run_main(self, model, *, env=None, runtime_check=None, task=None, app=None):
        task = task or self.task
        app = app or self.app
        environment = {"MODEL": "deepseek-flash", "OPENAI_BASE_URL": "https://runner.example/api",
                       "ARC_RUNTIME_CHECK": "1", "ARC_RUNTIME_ROUNDS": "2", "ARC_RUNTIME_SECONDS": "1500"}
        environment.update(env or {})
        events = Mock()
        runtime = SimpleNamespace(events=events, traceability=Mock())
        check = runtime_check if runtime_check is not None else Mock(side_effect=AssertionError("runtime check must not run"))
        # Only this test's ARC_* settings may influence the run; patch.dict restores the caller's environment.
        with patch.dict(os.environ, environment), \
             patch.object(module, "ModelClient"), patch.object(module, "PlatformGenerationModel", return_value=model), \
             patch("arcbench_agent_runtime.AgentRuntime.from_env", return_value=runtime), \
             patch.object(sys, "argv", ["main.py", str(task), "--output-dir", str(app)]), \
             patch.object(module, "run_runtime_check", check), \
             self.assertLogs(level="INFO") as logs:
            for name in [name for name in os.environ if name.startswith("ARC_") and name not in environment]:
                os.environ.pop(name)
            code = module.main()
        self.assertEqual(code, 0)
        return SimpleNamespace(events=events, logs=logs.output, check=check)

    def log_payload(self, logs, marker):
        for line in logs:
            if marker + " " in line:
                return json.loads(line.split(marker + " ", 1)[1])
        self.fail(f"{marker} not logged")

    def read_report(self, name="runtime-check.json"):
        return json.loads((self.reports / name).read_text(encoding="utf-8"))

    def copy_check(self, results):
        """A run_runtime_check stand-in that records the project copy it received."""
        seen = []
        queue = list(results)

        def check(project_dir, tests_dir=None, *, work_dir, suites=None, **kwargs):
            project_dir = Path(project_dir)
            self.assertNotEqual(project_dir.resolve(), self.app.resolve())
            self.assertNotIn(self.app.resolve(), project_dir.resolve().parents)
            self.assertIsNone(tests_dir)
            suites = list(suites or [])
            self.assertTrue(suites, "runtime check must receive at least the input suite")
            self.assertEqual(suites[0][0], "input")
            self.assertEqual(Path(suites[0][1]).resolve(), (self.task / "tests").resolve())
            copied = snapshot_project(project_dir)
            seen.append(dict(project_dir=project_dir, files=copied, kwargs=kwargs, suites=suites,
                             node_modules_present=(project_dir / "node_modules").exists()))
            if hasattr(self, "clock"):
                self.clock.advance(self.clock_step)
            # Leave build output and data behind in the copy, as a real check would.
            (project_dir / "node_modules").mkdir(exist_ok=True)
            (project_dir / "node_modules" / "left-behind.txt").write_text("x", encoding="utf-8")
            (project_dir / "dist").mkdir(exist_ok=True)
            (project_dir / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
            (project_dir / "data").mkdir(exist_ok=True)
            (project_dir / "data" / "store.json").write_text("{}", encoding="utf-8")
            return queue.pop(0)

        return check, seen

    def test_unknown_task_runs_with_input_tests_and_scaffold(self):
        model = ScriptedModel()
        check, seen = self.copy_check([passed_result(2)])
        run = self.run_main(model, runtime_check=check)
        inputs = self.log_payload(run.logs, "PLATFORM_INPUTS")
        self.assertEqual(inputs["task_id"], "orchard-ledger")
        self.assertFalse(inputs["registered"])
        self.assertEqual(inputs["tests_source"], "input")
        self.assertEqual(Path(inputs["tests_dir"]).resolve(), (self.task / "tests").resolve())
        self.assertTrue(inputs["scaffolded"])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0]["selection"], ["REQ-1", "REQ-2"])
        packet = model.calls[0]["packet"]
        self.assertTrue(packet.text.startswith("SOURCE_COMPLETENESS: ALL_TEXT_SOURCES_INCLUDED\n"))
        self.assertIn("ENTIRE task", packet.text)
        self.assertIn("SOURCE application/docs/task-ir.json", packet.text)
        self.assertIn("SOURCE application/docs/coverage.json", packet.text)
        self.assertNotIn("docs/contract.json", packet.text)
        self.assertIn("accessible-name semantics", packet.text)
        self.assertIn("Detected starter layout: empty", packet.text)
        self.assertIn("pre-wired full-stack scaffold", packet.text)
        self.assertEqual(inputs["entities"], {"parsed": 0, "inferred": 0})
        self.assertEqual(inputs["ui_elements"], {"parsed": 0, "inferred": 0})
        self.assertEqual(inputs["scenarios"], {"parsed": 2, "inferred": 0})
        self.assertTrue((self.app / "docs" / "task-ir.json").is_file())
        self.assertFalse((self.app / "docs" / "contract.json").exists())
        self.assertIn("SOURCE application/server/store.js", packet.text)
        self.assertIn("seed/<collection>.json", packet.text)
        self.assertIn("server/routes/index.js", packet.text)
        self.assertIn("SEED EVIDENCE SUMMARY", packet.text)
        self.assertIn("Test constant ACCOUNT (helpers.ts)", packet.text)
        self.assertIn("Given [REQ-1/REQ-1-S1]: A visitor with an existing account member_one@example.org", packet.text)
        self.assertNotIn("DRAFT", packet.text.split("SEED EVIDENCE SUMMARY", 1)[1].split("\nSOURCE ", 1)[0])
        evidence = json.loads("".join(chunk.text for chunk in packet.repository.file_chunks("evidence/seed-evidence.json")))
        self.assertTrue(any(item["name"] == "ACCOUNT" and item["seed_hint"] for item in evidence["test_constants"]))
        self.assertEqual(evidence["given_steps"][0]["requirement_id"], "REQ-1")
        for name in SCAFFOLD_FILES:
            self.assertTrue((self.app / name).is_file(), name)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["files"]["src/App.jsx"].replace(b"\r\n", b"\n"), SRC_APP_JSX.encode("utf-8"))
        report = self.read_report()
        self.assertEqual(report["final_status"], "passed")
        self.assertEqual([entry["status"] for entry in report["rounds"]], ["passed"])
        run.events.mark_run_completed.assert_called_once()
        run.events.mark_test_passed.assert_not_called()
        self.assertEqual(run.events.mark_implementation_done.call_count, 2)
        for name in ("node_modules", "dist", "data"):
            self.assertFalse((self.app / name).exists(), name)

    def test_runtime_failure_is_repaired_and_rechecked(self):
        model = ScriptedModel([None, write_app(APP_REPAIRED)])
        check, seen = self.copy_check([failed_result(1, 1), passed_result(2)])
        run = self.run_main(model, runtime_check=check)
        self.assertEqual(len(model.calls), 2)
        feedback = model.calls[1]["feedback"]
        self.assertIn("expect(locator).toBeVisible() failed", feedback)
        self.assertIn("source=input", feedback)
        self.assertIn("confidence=4", feedback)
        self.assertEqual(model.calls[1]["request_allowance"], 16)
        self.assertEqual(seen[0]["files"]["src/App.jsx"].replace(b"\r\n", b"\n"), SRC_APP_JSX.encode("utf-8"))
        self.assertEqual(seen[1]["files"]["src/App.jsx"], APP_REPAIRED.encode("utf-8"))
        self.assertNotIn("node_modules/left-behind.txt", seen[1]["files"])
        self.assertEqual((self.app / "src/App.jsx").read_text(encoding="utf-8"), APP_REPAIRED)
        report = self.read_report()
        self.assertEqual([entry["status"] for entry in report["rounds"]], ["failed", "passed"])
        self.assertEqual(report["rounds"][0]["repair"]["status"], "complete")
        self.assertEqual(report["best_round"], 2)
        self.assertEqual((report["final_status"], report["last_round_status"]), ("passed", "passed"))
        # A successful install for identical manifests lets the second round reuse node_modules.
        self.assertEqual([entry["node_modules_present"] for entry in seen], [False, True])
        self.assertAlmostEqual(seen[0]["kwargs"]["deadline"], seen[1]["kwargs"]["deadline"])
        self.assertEqual(seen[0]["kwargs"]["timeouts"], {"test": 10.0})
        checks = [json.loads(line.split("PLATFORM_RUNTIME_CHECK ", 1)[1]) for line in run.logs if "PLATFORM_RUNTIME_CHECK " in line]
        self.assertEqual([entry["round"] for entry in checks], [1, 2])
        actions = json.loads((self.reports / "actions.json").read_text(encoding="utf-8"))
        # One review batch per IR work item (ROOT, REQ-1, REQ-2), then the runtime repair.
        self.assertEqual([action["stage"] for action in actions],
                         ["implement", "locator_review", "locator_review", "locator_review", "runtime_repair"])

    def test_regressing_repair_is_reverted_to_best_snapshot(self):
        def break_app(tools):
            tools.call("write_files", {"files": [
                {"path": "src/App.jsx", "content": APP_REPAIRED.replace("Overview", "Broken")},
                {"path": "src/extra.js", "content": "export const added = true;\n"}]})
        model = ScriptedModel([None, break_app])
        check, seen = self.copy_check([failed_result(3, 1), failed_result(1, 3)])
        self.run_main(model, runtime_check=check)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(seen[1]["files"]["src/App.jsx"], APP_REPAIRED.replace("Overview", "Broken").encode("utf-8"))
        self.assertEqual((self.app / "src/App.jsx").read_text(encoding="utf-8"), SRC_APP_JSX)
        self.assertFalse((self.app / "src/extra.js").exists())
        self.assertEqual(snapshot_project(self.app), seen[0]["files"])
        report = self.read_report()
        self.assertEqual([entry["status"] for entry in report["rounds"]], ["failed", "failed"])
        self.assertEqual(report["rounds"][1]["action"], "restored_round_1")
        self.assertEqual(report["best_round"], 1)
        self.assertEqual((report["final_status"], report["last_round_status"]), ("failed", "failed"))
        self.assertEqual(report["stopped"], "rounds")
        self.assertTrue((self.reports / "tokens.json").is_file())

    def test_ranking_prefers_fewer_failures_and_later_round_on_full_tie(self):
        for second, expected_best, expected_action in ((failed_result(2, 1), 2, "kept"),
                                                       (failed_result(2, 3), 1, "restored_round_1"),
                                                       (failed_result(2, 2), 2, "kept")):
            with self.subTest(second=(second.passed, second.failed)):
                shutil.rmtree(self.app, ignore_errors=True)
                model = ScriptedModel([None, write_app(APP_REPAIRED)])
                check, seen = self.copy_check([failed_result(2, 2), second])
                self.run_main(model, runtime_check=check)
                report = self.read_report()
                self.assertEqual(report["best_round"], expected_best)
                self.assertEqual(report["rounds"][1]["action"], expected_action)
                expected_app = APP_REPAIRED if expected_best == 2 else SRC_APP_JSX
                self.assertEqual((self.app / "src/App.jsx").read_text(encoding="utf-8"), expected_app)

    def test_failed_install_forces_reinstall_next_round(self):
        model = ScriptedModel([None, write_app(APP_REPAIRED)])
        install_failed = RuntimeCheckResult(status="install_failed", stage="install", log_tail="npm ERR! preinstall")
        check, seen = self.copy_check([install_failed, passed_result(2)])
        self.run_main(model, runtime_check=check)
        report = self.read_report()
        self.assertEqual([entry["install_reusable"] for entry in report["rounds"]], [False, True])
        self.assertEqual([entry["node_modules_present"] for entry in seen], [False, False])
        self.assertEqual(report["final_status"], "passed")

    def test_repair_is_skipped_when_wall_budget_is_too_low(self):
        class FakeClock:
            def __init__(self):
                self.now = 1000.0
            def __call__(self):
                return self.now
            def advance(self, seconds):
                self.now += seconds
        self.clock, self.clock_step = FakeClock(), 350
        model = ScriptedModel([None, write_app(APP_REPAIRED)])
        check, seen = self.copy_check([failed_result(1, 1), passed_result(2)])
        with patch.object(module, "monotonic", self.clock):
            self.run_main(model, runtime_check=check, env={"ARC_RUNTIME_SECONDS": "400", "ARC_RUNTIME_ROUNDS": "3"})
        # 400 s budget -> repair needs min(300, 25%) = 100 s; the check consumed 350 s, leaving 50 s.
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0]["kwargs"]["deadline"], 1400.0)
        report = self.read_report()
        self.assertEqual(report["stopped"], "wall_budget_before_repair")
        self.assertEqual(report["repair_minimum_seconds"], 100.0)
        self.assertEqual((self.app / "src/App.jsx").read_text(encoding="utf-8"), SRC_APP_JSX)
        # With a generous budget the same scenario repairs and passes.
        shutil.rmtree(self.app, ignore_errors=True)
        self.clock, self.clock_step = FakeClock(), 10
        model = ScriptedModel([None, write_app(APP_REPAIRED)])
        check, seen = self.copy_check([failed_result(1, 1), passed_result(2)])
        with patch.object(module, "monotonic", self.clock):
            self.run_main(model, runtime_check=check, env={"ARC_RUNTIME_SECONDS": "400", "ARC_RUNTIME_ROUNDS": "3"})
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(self.read_report()["final_status"], "passed")

    def test_exception_during_repair_restores_best_and_delivers(self):
        def explode(tools):
            tools.call("write_files", {"files": [{"path": "src/App.jsx", "content": "export default 1;\n"}]})
            raise RuntimeError("model transport exploded")
        model = ScriptedModel([None, explode])
        check, seen = self.copy_check([failed_result(1, 1), passed_result(2)])
        run = self.run_main(model, runtime_check=check)
        self.assertEqual(len(seen), 1)
        self.assertEqual((self.app / "src/App.jsx").read_text(encoding="utf-8"), SRC_APP_JSX)
        report = self.read_report()
        self.assertEqual(report["stopped"], "repair_error")
        self.assertEqual(report["rounds"][0]["action"], "restored_after_repair")
        self.assertIn("RuntimeError: model transport exploded", report["rounds"][0]["repair"]["error"])
        self.assertEqual((report["final_status"], report["best_round"]), ("failed", 1))
        run.events.mark_run_completed.assert_called_once()
        self.assertTrue(json.loads((self.reports / "static-validation.json").read_text(encoding="utf-8"))["passed"])

    def test_precondition_only_failures_do_not_trigger_repair(self):
        model = ScriptedModel()
        only_preconditions = RuntimeCheckResult(
            status="failed", stage="tests", passed=1, failed=0, total=2, precondition=1, env_unset=["DEMO_SECOND_BASE_URL"],
            preconditions=[RuntimeFailure("needs second instance", "REQ-2.spec.ts", "Error: DEMO_SECOND_BASE_URL must be set", "harness_precondition")])
        check, seen = self.copy_check([only_preconditions, passed_result(2)])
        self.run_main(model, runtime_check=check)
        self.assertEqual(len(model.calls), 1)
        report = self.read_report()
        self.assertEqual(report["stopped"], "only_harness_preconditions")
        self.assertEqual(report["rounds"][0]["harness_preconditions"], [dict(name="needs second instance", file="REQ-2.spec.ts")])
        self.assertEqual(report["rounds"][0]["env_unset"], ["DEMO_SECOND_BASE_URL"])

    def test_repair_that_breaks_the_build_is_reverted_even_at_equal_pass_count(self):
        model = ScriptedModel([None, write_app("export default function App() { return <main /> }\n")])
        check, seen = self.copy_check([failed_result(0, 2), RuntimeCheckResult(status="build_failed", stage="build", log_tail="vite: error")])
        self.run_main(model, runtime_check=check)
        self.assertEqual((self.app / "src/App.jsx").read_text(encoding="utf-8"), SRC_APP_JSX)
        report = self.read_report()
        self.assertEqual(report["rounds"][1]["action"], "restored_round_1")
        self.assertEqual(report["best_round"], 1)

    def test_invalid_review_is_not_delivered_when_runtime_is_disabled(self):
        model = ScriptedModel()
        def invalid_review(packet, tools, **kwargs):
            tools.call('write_files', {'files': [{'path': 'src/App.jsx', 'content': 'export const = ;'}]})
            return dict(status='complete', turns=1)
        model.review_sources = invalid_review
        with self.assertRaisesRegex(RuntimeError, 'Interaction scope parser failed|Final delivery source is invalid'):
            self.run_main(model, env={'ARC_RUNTIME_CHECK': '0'})

    def test_runtime_check_disabled_by_environment(self):
        model = ScriptedModel()
        run = self.run_main(model, env={"ARC_RUNTIME_CHECK": "0"})
        run.check.assert_not_called()
        report = self.read_report()
        self.assertFalse(report["enabled"])
        self.assertEqual(report["skipped"], "disabled_by_settings")
        self.assertEqual(report["rounds"], [])
        self.assertEqual(len(model.calls), 1)

    def test_task_without_supplied_tests_checks_probes_only(self):
        task = self.path / "isolated" / "task"
        task.mkdir(parents=True)
        (task / "requirements.yaml").write_text("""\
id: ROOT
name: Quiet Ledger
type: FOLDER
description: A synthetic ledger with no supplied tests.
children:
  - id: REQ-1
    name: Overview page
    type: ATOMIC
    description: Show the overview.
    scenarios:
      - id: REQ-1-S1
        name: Open overview
        steps:
          - Given: A visitor arrives.
            When: The visitor clicks "Overview"(role=link).
            Then: The heading "Overview"(role=heading) is visible.
    children: []
""", encoding="utf-8")
        model = ScriptedModel()
        seen = []

        def check(project_dir, tests_dir=None, *, work_dir, suites=None, **kwargs):
            seen.append((tests_dir, [(name, Path(directory).name) for name, directory in (suites or [])]))
            return RuntimeCheckResult(status="passed", stage="tests", passed=0, failed=0, total=0,
                                      signals={"probes": {"passed": 1, "failed": 0}},
                                      diagnostics={"playwright": {"how": "test"}, "log_tail": ""},
                                      api_probes=[dict(method="GET", path="/api/health", status=200, ok=True)])

        self.run_main(model, task=task, runtime_check=check)
        self.assertEqual(len(seen), 1)
        self.assertIsNone(seen[0][0])
        self.assertEqual(seen[0][1], [])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.author_calls, [])
        report = json.loads((self.app / ".arc/platform-budget/quiet-ledger/runtime-check.json").read_text(encoding="utf-8"))
        self.assertIsNone(report["skipped"])
        self.assertEqual(report["final_status"], "passed")
        self.assertEqual(report["rounds"][0]["stage"], "tests")

    def test_harness_problem_skips_loop_without_repair(self):
        model = ScriptedModel()
        check, seen = self.copy_check([RuntimeCheckResult(status="unavailable", stage="playwright", log_tail="no browser")])
        self.run_main(model, runtime_check=check)
        self.assertEqual(len(model.calls), 1)
        self.assertGreaterEqual(len(model.reviews), 1)
        self.assertIn("COVERAGE_REVIEW", model.reviews[-1]["feedback"])
        self.assertEqual(model.reviews[-1]["request_allowance"], 4)
        report = self.read_report()
        self.assertEqual(report["stopped"], "unavailable_after_review")
        self.assertNotEqual(report["final_status"], "passed")
        self.assertEqual(report["rounds"][0]["action"], "reviewed")
        self.assertIn("notes", report["rounds"][0])
        self.assertIn("signals", report["rounds"][0])
        self.assertIn("diagnostics", report["rounds"][0])

    def test_unavailable_with_failing_probe_still_repairs(self):
        model = ScriptedModel([None, write_app(APP_REPAIRED)])
        missing = RuntimeCheckResult(
            status="unavailable", stage="playwright",
            api_probes=[dict(method="GET", path="/api/health", status=500, ok=False, body="down")],
            notes=dict(method="install-chromium", package=None, version=None, chromium_revision=None,
                       browser_dirs=["/ms-playwright"]),
            tests_log_tail=("x" * 2000) + " browser missing",
        )
        check, _seen = self.copy_check([missing, passed_result(2)])
        self.run_main(model, runtime_check=check)
        self.assertEqual(len(model.calls), 2)
        self.assertIn("/api/health", model.calls[1]["feedback"])
        report = self.read_report()
        self.assertEqual(report["final_status"], "passed")
        self.assertEqual(report["rounds"][0]["notes"]["method"], "install-chromium")
        self.assertLessEqual(len(report["rounds"][0]["tests_log_tail"]), 1500)
        self.assertIn("browser missing", report["rounds"][0]["tests_log_tail"])

    @unittest.skipUnless(HAS_NODE, "node/npm not available")
    def test_real_runtime_check_leaves_delivered_project_clean(self):
        app = self.path / "fixture-app"
        shutil.copytree(FIXTURES / "app", app)
        (app / "deploy.sh").write_text("npm install --no-audit --no-fund\nnpm run build\nexec npm run start\n", encoding="utf-8", newline="\n")
        task = self.path / "fixture-task"
        (task / "tests").mkdir(parents=True)
        (task / "requirements.yaml").write_text(REQUIREMENTS.replace("Orchard Ledger", "Runtime Fixture"), encoding="utf-8")
        shutil.copy(FIXTURES / "specs" / "demo.spec.ts", task / "tests" / "demo.spec.ts")
        before = snapshot_project(app)
        model = ScriptedModel()
        self.run_main(model, runtime_check=module.run_runtime_check, task=task, app=app,
                      env={"ARC_RUNTIME_ROUNDS": "1", "ARC_RUNTIME_SECONDS": "600"})
        report = json.loads((app / ".arc/platform-budget/runtime-fixture/runtime-check.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["rounds"]), 1)
        entry = report["rounds"][0]
        if entry["status"] == "unavailable":
            self.skipTest("Playwright or Chromium unavailable for the real runtime check")
        self.assertEqual(entry["status"], "failed", entry)
        self.assertEqual(entry["env_supplied"], ["DEMO_BASE_URL", "DEMO_RUN_ID"])
        for name in ("node_modules", "dist", "data", "package-lock.json", "tests", "playwright.config.cjs"):
            self.assertFalse((app / name).exists(), name)
        after = snapshot_project(app)
        self.assertIn("docs/task-ir.json", after)
        self.assertIn("docs/coverage.json", after)
        after.pop("docs/task-ir.json")
        after.pop("docs/coverage.json")
        self.assertNotIn("docs/contract.json", after)
        self.assertEqual(after, before)
        self.assertEqual((entry["signals"]["input"]["passed"], entry["signals"]["input"]["failed"]), (1, 1))
        self.assertIn("diagnostics", entry)
        self.assertEqual((entry["passed"], entry["failed"], entry["total"]), (1, 1, 2))
        self.assertEqual(len(model.calls), 1)

    def test_partial_sources_use_batched_implementation(self):
        model = ScriptedModel()
        self.run_logs = self.run_main(model, env={"ARC_PACKET_CHARS": "2000", "ARC_RUNTIME_CHECK": "0"}).logs
        self.assertEqual([call["selection"] for call in model.calls], [["REQ-1"], ["REQ-2"]])
        first, second = model.calls
        self.assertTrue(first["packet"].text.startswith("SOURCE_COMPLETENESS: PARTIAL_READ_OMITTED_RANGES\n"))
        self.assertIn("selected requirement(s) REQ-1", first["packet"].text)
        self.assertIn("SOURCE tests/REQ-1.spec.ts", first["packet"].text)
        self.assertIn("SOURCE tests/helpers.ts", first["packet"].text)
        self.assertNotIn("SOURCE tests/REQ-2.spec.ts", first["packet"].text)
        self.assertIn("SOURCE tests/REQ-2.spec.ts", second["packet"].text)
        # The sum of batch allowances is capped at implement_request_allowance (64).
        self.assertEqual(first["request_allowance"], 32)
        self.assertEqual(second["request_allowance"], 32)
        actions = json.loads((self.reports / "actions.json").read_text(encoding="utf-8"))
        # Two implement batches, then one review per IR work item.
        self.assertEqual([action["stage"] for action in actions],
                         ["implement", "implement", "locator_review", "locator_review", "locator_review"])
        implemented = [action for action in actions if action["stage"] == "implement"]
        self.assertEqual(len(set(action["item"] for action in implemented)), 2)
        batching = self.log_payload(self.run_logs, "PLATFORM_BATCHING")
        self.assertEqual(batching["items"], 2)

    def test_seed_validation_errors_surface_in_validate_source(self):
        from agent.platform_scaffold import prepare_scaffold
        app = self.path / "seeded"
        self.assertTrue(prepare_scaffold(app))
        write_acceptance_docs(app)
        (app / "seed" / "users.json").write_text('{"collection": "users", "records": [{"id": "u1"}, {"id": "u1"}]}', encoding="utf-8")
        (app / "seed" / "broken.json").write_text("{not json", encoding="utf-8")
        result = validate_source(app)
        self.assertFalse(result["passed"])
        paths = {error["path"] for error in result["errors"]}
        self.assertEqual(paths, {"seed/users.json", "seed/broken.json"})
        (app / "seed" / "broken.json").unlink()
        (app / "seed" / "users.json").write_text('{"collection": "users", "records": [{"id": "u1"}]}', encoding="utf-8")
        self.assertTrue(validate_source(app)["passed"])

    def test_seed_validation_applies_only_to_scaffold_loader(self):
        app = self.path / "custom-seeds"
        (app / "server").mkdir(parents=True)
        (app / "seed").mkdir()
        (app / "package.json").write_text('{"scripts":{"start":"node server/index.js"}}', encoding="utf-8")
        (app / "deploy.sh").write_text("exec node server/index.js\n", encoding="utf-8")
        (app / "server" / "index.js").write_text("console.log('custom seed format');\n", encoding="utf-8")
        (app / "server" / "seed.js").write_text("export const seeds = ['custom'];\n", encoding="utf-8")
        (app / "seed" / "items.json").write_text('["a", "b"]', encoding="utf-8")
        write_acceptance_docs(app)
        result = validate_source(app)
        self.assertTrue(result["passed"], result)

    def test_unseal_reopens_edits_between_rounds(self):
        from agent.platform_scaffold import prepare_scaffold
        app = self.path / "sealed"
        prepare_scaffold(app)
        write_acceptance_docs(app)
        tools = module.StaticProjectTools(app)
        self.assertTrue(json.loads(tools.call("validate_source", {}))["passed"])
        with self.assertRaisesRegex(ValueError, "sealed"):
            tools.call("write_files", {"files": [{"path": "src/App.jsx", "content": APP_REPAIRED}]})
        tools.unseal()
        tools.call("write_files", {"files": [{"path": "src/App.jsx", "content": APP_REPAIRED}]})
        self.assertEqual((app / "src/App.jsx").read_text(encoding="utf-8"), APP_REPAIRED)
        with self.assertRaisesRegex(ValueError, "immutable"):
            tools.call("write_files", {"files": [{"path": "deploy.sh", "content": "changed"}]})

    def test_coverage_ledger_rejects_complete_until_the_file_is_marked(self):
        model = ScriptedModel([LeaveMissing()])
        run = self.run_main(model, env={"ARC_IMPLEMENT_REQUESTS": "4", "ARC_RUNTIME_CHECK": "0"})
        self.assertEqual(len(model.calls), 2)
        self.assertIsNone(model.calls[0]["feedback"])
        self.assertIn("COVERAGE_REVIEW", model.calls[1]["feedback"])
        self.assertIn("REQ-1", model.calls[1]["feedback"])
        document = json.loads((self.app / "docs" / "coverage.json").read_text(encoding="utf-8"))
        self.assertTrue(document["items"])
        self.assertTrue(all(item["status"] == "covered" for item in document["items"]))
        self.assertTrue(any("PLATFORM_COVERAGE" in line and '"status": "rejected"' in line for line in run.logs))

    def test_coverage_rejection_stops_when_implement_allowance_is_exhausted(self):
        model = ScriptedModel([LeaveMissing()])
        run = self.run_main(model, env={"ARC_IMPLEMENT_REQUESTS": "1", "ARC_RUNTIME_CHECK": "0"})
        self.assertEqual(len(model.calls), 1)
        self.assertTrue(any("PLATFORM_COVERAGE" in line and '"status": "incomplete"' in line for line in run.logs))
        document = json.loads((self.app / "docs" / "coverage.json").read_text(encoding="utf-8"))
        self.assertTrue(any(item["status"] == "missing" for item in document["items"]))
        run.events.mark_implementation_done.assert_not_called()
        run.events.mark_run_completed.assert_called_once()

    def test_review_batches_use_allowance_four_and_the_total_cap(self):
        from agent.platform_settings import PlatformSettings
        self.assertEqual(PlatformSettings().review_request_allowance, 4)
        self.assertEqual(module.review_request_budget(0), 8)
        self.assertEqual(module.review_request_budget(3), 11)
        self.assertEqual(module.review_request_budget(56), 64)
        self.assertEqual(module.review_request_budget(100), 64)
        model = ScriptedModel()
        self.run_main(model, env={"ARC_RUNTIME_CHECK": "0"})
        self.assertEqual([item["request_allowance"] for item in model.reviews], [4, 4, 3])
        self.assertIn("accessible-name semantics", model.reviews[0]["feedback"])
        self.assertIn("Playwright strict mode", model.reviews[0]["feedback"])

    def test_ranking_keeps_higher_input_and_compiled_passes(self):
        def ranked(input_passed, compiled_passed, failed):
            return RuntimeCheckResult(
                status="failed", stage="tests",
                passed=input_passed + compiled_passed, failed=failed,
                total=input_passed + compiled_passed + failed,
                failures=[RuntimeFailure("flow", "flow.spec.ts", "Error: expect(locator).toBeVisible() failed")],
                signals={
                    "input": {"passed": input_passed, "failed": 0, "skipped": 0, "failures": []},
                    "compiled": {"passed": compiled_passed, "failed": 0, "skipped": 0, "failures": []},
                    "authored": {"passed": 0, "failed": 0, "skipped": 0, "failures": []},
                    "probes": {"passed": 0, "failed": 0, "skipped": 0, "failures": []},
                },
            )
        model = ScriptedModel([None, write_app(APP_REPAIRED)])
        check, _seen = self.copy_check([ranked(3, 1, 2), ranked(1, 8, 0)])
        self.run_main(model, runtime_check=check)
        report = self.read_report()
        self.assertEqual(report["best_round"], 1)
        self.assertEqual(report["rounds"][1]["action"], "restored_round_1")
        self.assertEqual((self.app / "src/App.jsx").read_text(encoding="utf-8"), SRC_APP_JSX)
        self.assertGreater(module.candidate_rank(ranked(3, 1, 2)), module.candidate_rank(ranked(1, 8, 0)))
        self.assertGreater(module.candidate_rank(ranked(2, 4, 3)), module.candidate_rank(ranked(2, 1, 0)))

    def test_runtime_logs_include_signals_and_diagnostics(self):
        model = ScriptedModel()
        observed = RuntimeCheckResult(
            status="passed", stage="tests", passed=1, failed=0, total=1,
            signals={"input": {"passed": 1, "failed": 0, "skipped": 0, "failures": []},
                     "authored": {"passed": 0, "failed": 0, "skipped": 0, "failures": []}},
            diagnostics={"playwright": {"how": "preinstalled", "version": "1.40.0", "revision": "1148", "browser_dirs": []},
                         "log_tail": ""})
        check, _seen = self.copy_check([observed])
        run = self.run_main(model, runtime_check=check)
        inputs = self.log_payload(run.logs, "PLATFORM_INPUTS")
        self.assertEqual(inputs["tests_source"], "input")
        self.assertIn("parsed", inputs["entities"])
        self.assertIn("inferred", inputs["ui_elements"])
        self.assertIn("parsed", inputs["scenarios"])
        entry = self.log_payload(run.logs, "PLATFORM_RUNTIME_CHECK")
        self.assertEqual(entry["signals"]["input"], {"passed": 1, "failed": 0})
        self.assertEqual(entry["diagnostics"]["playwright"]["how"], "preinstalled")


if __name__ == "__main__":
    unittest.main()

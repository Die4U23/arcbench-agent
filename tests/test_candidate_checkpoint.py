from pathlib import Path
import tempfile
import unittest

from agent.candidate_checkpoint import CandidateCheckpoint, PUBLIC_CHECK, public_checks_ready
from agent.verify import CheckResult
from agent.browser_contract import browser_guard_result


def checks(passed=("a",), *, build=True, cases=("a", "b")):
    return (CheckResult("frontend: npm run build", build, 0 if build else 1, "build"),
            CheckResult("web template structure", True, 0, "layout"),
            CheckResult(PUBLIC_CHECK, len(passed) == len(cases), 0, "local",
                        case_statuses=tuple((name, "passed" if name in passed else "failed") for name in cases)))


class CandidateCheckpointTests(unittest.TestCase):
    def test_chromium_regression_restores_candidate_even_when_jsdom_all_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.project(root)
            guard = CandidateCheckpoint(root)
            guard.consider((*checks(("a", "b")), browser_guard_result(True, "real CSS passed")))
            (root / "frontend/src/App.jsx").write_text("CSS accessible name regression", encoding="utf-8")
            note = guard.consider((*checks(("a", "b")), browser_guard_result(False, "Name *")))
            self.assertIn("local Chromium", note)
            self.assertEqual((root / "frontend/src/App.jsx").read_text(), "original")

    def test_missing_browser_result_cannot_replace_final_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.project(root)
            guard = CandidateCheckpoint(root)
            guard.consider((*checks(("a", "b")), browser_guard_result(True, "real CSS passed")))
            (root / "frontend/src/App.jsx").write_text("unchecked browser", encoding="utf-8")
            guard.consider(checks(("a", "b")), final=True)
            self.assertEqual((root / "frontend/src/App.jsx").read_text(), "original")

    def project(self, root):
        for name, text in {"frontend/src/App.jsx": "original", "backend/src/app.js": "server",
                           "backend/data/db.json": "runtime", "backend/db.json": "runtime",
                           "requirements.yaml": "acceptance", "requirements/requirements.yaml": "requirements",
                           "frontend/.arc/public-case.test.tsx": "protected", ".env": "secret"}.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def test_fixed_regression_runs_after_build_even_with_failing_self_tests(self):
        project = (CheckResult("frontend: npm run build", True, 0, "built"),
                   CheckResult("frontend: npm run test", False, 1, "self test failure"))
        self.assertTrue(public_checks_ready(project, True))
        self.assertFalse(public_checks_ready(project, False))
        self.assertFalse(public_checks_ready((CheckResult("frontend: npm run build", False, 1, "bad"),), True))

    def test_losing_a_pass_restores_source_and_removes_new_source_without_reverting_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.project(root)
            guard = CandidateCheckpoint(root)
            guard.consider(checks())
            (root / "frontend/src/App.jsx").write_text("regression", encoding="utf-8")
            (root / "frontend/src/new.jsx").write_text("new", encoding="utf-8")
            protected = ("backend/data/db.json", "backend/db.json", "requirements.yaml", "requirements/requirements.yaml", "frontend/.arc/public-case.test.tsx", ".env")
            for name in protected:
                (root / name).write_text("preserve", encoding="utf-8")
            # Same count with a different passing case is still a regression.
            note = guard.consider(checks(("b",)))
            self.assertIn("Rejected regression: a", note)
            self.assertEqual((root / "frontend/src/App.jsx").read_text(), "original")
            self.assertFalse((root / "frontend/src/new.jsx").exists())
            for name in protected:
                self.assertEqual((root / name).read_text(), "preserve")

    def test_monotonic_improvement_becomes_new_checkpoint_and_budget_uses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.project(root)
            guard = CandidateCheckpoint(root)
            guard.consider(checks())
            (root / "frontend/src/App.jsx").write_text("better", encoding="utf-8")
            guard.consider(checks(("a", "b")))
            (root / "frontend/src/App.jsx").write_text("unchecked last edit", encoding="utf-8")
            guard.consider(checks()[:2], final=True)
            self.assertEqual((root / "frontend/src/App.jsx").read_text(), "better")
            self.assertEqual(guard.passing, {"a", "b"})

    def test_incomplete_or_different_case_set_cannot_replace_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.project(root)
            guard = CandidateCheckpoint(root)
            guard.consider(checks())
            (root / "frontend/src/App.jsx").write_text("different oracle", encoding="utf-8")
            guard.consider(checks(("x", "y"), cases=("x", "y")))
            self.assertEqual(guard.files["frontend/src/App.jsx"], b"original")
            guard.consider(checks()[:2], final=True)
            self.assertEqual((root / "frontend/src/App.jsx").read_text(), "original")

    def test_build_failure_rolls_back_and_linked_source_never_receives_a_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.project(root)
            guard = CandidateCheckpoint(root); guard.consider(checks())
            (root / "frontend/src/App.jsx").write_text("syntax error", encoding="utf-8")
            guard.consider(checks(build=False))
            self.assertEqual((root / "frontend/src/App.jsx").read_text(), "original")
            path = root / "frontend/src/App.jsx"
            path.unlink()
            outside = root / "outside.txt"; outside.write_text("safe", encoding="utf-8")
            try:
                path.symlink_to(outside)
            except OSError:
                self.skipTest("symlink creation unavailable")
            with self.assertRaisesRegex(ValueError, "linked source"):
                guard.consider(checks(build=False))
            self.assertEqual(outside.read_text(), "safe")


if __name__ == "__main__":
    unittest.main()

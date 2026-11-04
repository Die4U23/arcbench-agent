"""Offline checks for browser matching, diagnostics, and the locator walkthrough."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "platform-agent"))

from agent.platform_generation import WALKTHROUGH_INSTRUCTIONS, walkthrough_feedback
from agent.platform_runtime_check import (
    RuntimeCheckResult, choose_playwright_package, clean_env, read_chromium_revision, runtime_log_fields,
)
from agent.platform_settings import PlatformSettings
from tools.platform_module_closure import compute_closure

FORBIDDEN = ("BookStack", "Shelf", "Login", "bookstack", "账号", "数据")


def package_tree(root_dir: Path, revision: str, version: str, *, cli: bool) -> Path:
    package = root_dir / "node_modules" / "@playwright" / "test"
    core = root_dir / "node_modules" / "playwright-core"
    core.mkdir(parents=True)
    package.mkdir(parents=True)
    (core / "browsers.json").write_text(json.dumps(dict(browsers=[dict(name="chromium", revision=revision)])), encoding="utf-8")
    (core / "package.json").write_text(json.dumps(dict(name="playwright-core", version=version)), encoding="utf-8")
    (package / "package.json").write_text(json.dumps(dict(name="@playwright/test", version=version)), encoding="utf-8")
    if cli:
        (package / "cli.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
    return package


class BrowserMatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def test_revision_match_prefers_preinstalled_package(self):
        browsers = self.path / "browsers"
        (browsers / "chromium-1243").mkdir(parents=True)
        preinstalled = package_tree(self.path / "image", "1243", "1.57.0", cli=True)
        project = package_tree(self.path / "app", "9999", "9.9.9", cli=True)
        chosen, notes = choose_playwright_package([preinstalled], [project], [browsers])
        self.assertEqual(chosen, preinstalled)
        self.assertEqual(notes["method"], "preinstalled")
        self.assertEqual(notes["chromium_revision"], "1243")
        self.assertEqual(notes["version"], "1.57.0")
        self.assertEqual(read_chromium_revision(preinstalled), "1243")

    def test_project_package_with_mismatched_revision_is_not_selected(self):
        browsers = self.path / "browsers"
        (browsers / "chromium_headless_shell-1243").mkdir(parents=True)
        project = package_tree(self.path / "app", "9999", "9.9.9", cli=True)
        chosen, notes = choose_playwright_package([], [project], [browsers])
        self.assertIsNone(chosen)
        self.assertNotEqual(notes.get("package"), str(project))
        self.assertEqual(notes["method"], "install-chromium")

    def test_matching_revision_without_cli_is_inferred(self):
        browsers = self.path / "browsers"
        (browsers / "chromium-1187").mkdir(parents=True)
        declared = package_tree(self.path / "image", "1187", "1.49.0", cli=False)
        chosen, notes = choose_playwright_package([declared], [], [browsers])
        self.assertIsNone(chosen)
        self.assertEqual(notes["method"], "inferred")
        self.assertEqual(notes["version"], "1.49.0")
        self.assertEqual(notes["chromium_revision"], "1187")

    def test_clean_env_pins_browser_dir_before_replacing_home(self):
        browsers = self.path / "browsers"
        (browsers / "chromium-1243").mkdir(parents=True)
        home = self.path / "empty-home"
        with patch("agent.platform_runtime_check.browser_cache_dirs", return_value=[browsers]):
            env = clean_env(home=home)
        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], str(browsers))
        self.assertEqual(env["HOME"], str(home))

    def test_diagnostics_include_notes_and_redacted_tail_only_without_a_verdict(self):
        notes = dict(method="preinstalled", package="/usr/lib/node_modules/@playwright/test", version="1.57.0",
                     chromium_revision="1148", browser_dirs=["/ms-playwright"])
        pending = RuntimeCheckResult(status="unavailable", stage="playwright", notes=notes, tests_log_tail="tail " + ("y" * 2000))
        fields = runtime_log_fields(pending)
        self.assertEqual(fields["notes"]["chromium_revision"], "1148")
        self.assertEqual(len(fields["tests_log_tail"]), 1500)
        verdict = RuntimeCheckResult(status="passed", stage="tests", notes=notes, tests_log_tail="secret tail")
        self.assertNotIn("tests_log_tail", runtime_log_fields(verdict))
        self.assertIn("notes", runtime_log_fields(verdict))


class ReviewAndDependencyTests(unittest.TestCase):
    def test_review_allowance_defaults_and_prompt_has_no_task_words(self):
        settings = PlatformSettings.from_env({})
        self.assertEqual(settings.review_request_allowance, 4)
        overridden = PlatformSettings.from_env({"ARC_REVIEW_REQUESTS": "4"})
        self.assertEqual(overridden.review_request_allowance, 4)
        for word in FORBIDDEN:
            self.assertNotIn(word, WALKTHROUGH_INSTRUCTIONS)
        feedback = walkthrough_feedback(None)
        self.assertIn("no change", feedback)
        self.assertIn("page.getByRole", feedback)
        self.assertLessEqual(len(feedback), 20000)

    def test_requirements_do_not_pin_python_playwright_and_entry_does_not_import_it(self):
        text = (root / "platform-agent" / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("playwright==", text)
        closure = compute_closure()
        for name in closure["required"]:
            path = root / "platform-agent" / Path(*name.split(".")).with_suffix(".py")
            if name == "main":
                path = root / "platform-agent" / "main.py"
            source = path.read_text(encoding="utf-8")
            self.assertNotRegex(source, r"(?m)^(?:import playwright|from playwright)\b")


if __name__ == "__main__":
    unittest.main()

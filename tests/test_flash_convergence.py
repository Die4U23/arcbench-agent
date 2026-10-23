import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent.model_policy import configure_flash_models
from agent.tool_context import initial_file_inventory
from agent.tools import ProjectTools
from agent.verify import CheckResult, verify_project


class FlashPolicyTests(unittest.TestCase):
    def test_default_and_platform_identifiers_are_preserved(self):
        env = {}
        configure_flash_models(env)
        self.assertEqual(set(env.values()), {"deepseek-flash"})
        env = {"MODEL": " deepseek-v4-flash ", "VISUAL_MODEL": "deepseek-v4-flash-vision-exp"}
        configure_flash_models(env)
        self.assertEqual(env["MODEL"], "deepseek-v4-flash")
        self.assertEqual(env["VISUAL_MODEL"], "deepseek-v4-flash-vision-exp")

    def test_pro_unknown_and_vision_text_are_rejected_without_partial_update(self):
        for field in ("MODEL", "VISUAL_MODEL", "VISUAL_REVIEW_MODEL"):
            for name in ("deepseek-pro", "deepseek-v4-pro", "flash-pro", "deepseek-flash-other"):
                env = {field: name}
                with self.subTest(field=field, name=name), self.assertRaises(RuntimeError):
                    configure_flash_models(env)
                self.assertEqual(env, {field: name})
        with self.assertRaises(RuntimeError):
            configure_flash_models({"MODEL": "deepseek-v4-flash-vision-exp"})


class DelegatedVerificationTests(unittest.TestCase):
    def setUp(self):
        worker = patch('agent.sandbox_context._WORKER', True)
        worker.start()
        self.addCleanup(worker.stop)

    def run_fixture(self, wrapper, *, hooks=None, fail=False, repetitions=1):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ("frontend", "backend"):
                (root / folder).mkdir()
                (root / folder / "package.json").write_text(json.dumps({"scripts": {"test": "node test.js"}}))
            (root / "package.json").write_text(json.dumps({"scripts": {"test": wrapper, **(hooks or {})}}))
            def run(package, script):
                passed = not (fail and (package.name == "backend" or package == root))
                return CheckResult(f"{package.name}: npm run {script}", passed, 0 if passed else 7, "assertion failed" if not passed else "done")
            with patch("agent.verify._run_npm_script", side_effect=run) as execute:
                results = [verify_project(root) for _ in range(repetitions)]
            return results, [(p.name, s) for p, s in (call.args for call in execute.call_args_list)]

    def test_exact_wrapper_runs_children_once_and_retains_root_result(self):
        results, calls = self.run_fixture("npm --prefix frontend run test && npm --prefix backend run test")
        self.assertEqual(len(calls), 2)
        self.assertTrue(results[0].passed)
        self.assertEqual(len(results[0].checks), 3)
        self.assertIn("wrapper not rerun", results[0].checks[-1].output)

    def test_child_failure_is_preserved_in_root_result(self):
        results, calls = self.run_fixture("npm --prefix frontend run test && npm --prefix backend run test", fail=True)
        self.assertEqual(len(calls), 2)
        self.assertFalse(results[0].passed)
        self.assertEqual(results[0].checks[-1].exit_code, 7)
        self.assertIn("assertion failed", results[0].checks[-1].output)

    def test_hooks_extra_commands_flags_and_unverified_targets_execute(self):
        for wrapper, hooks in (
            ("npm --prefix frontend run test", {"pretest": "node prepare.js"}),
            ("npm --prefix frontend run test", {"posttest": "node audit.js"}),
            ("npm --prefix frontend run test && node integration.js", None),
            ("npm --prefix frontend run test -- --coverage", None),
            ("npm --prefix frontend run build", None),
            ("npm --prefix frontend run test && npm --prefix frontend run test", None),
        ):
            with self.subTest(wrapper=wrapper, hooks=hooks):
                _, calls = self.run_fixture(wrapper, hooks=hooks)
                self.assertEqual(len(calls), 3)

    def test_changed_inputs_or_unavailable_fingerprint_force_real_execution(self):
        for stamps in (["old", "new", "new", "new"], [None] * 4):
            with self.subTest(stamps=stamps), patch("agent.verify.verification_input_fingerprint", side_effect=stamps):
                _, calls = self.run_fixture("npm --prefix frontend run test && npm --prefix backend run test")
                self.assertEqual(len(calls), 3)

    def test_fresh_verification_never_reuses_previous_execution(self):
        _, calls = self.run_fixture("npm --prefix frontend run test && npm --prefix backend run test", repetitions=2)
        self.assertEqual(len(calls), 4)


class InventoryTests(unittest.TestCase):
    def test_inventory_uses_file_policy_and_omits_source_contents(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "App.js").write_text("private source body")
            (root / "credentials.json").write_text("private credential")
            result = initial_file_inventory(ProjectTools(root))
            self.assertIn("App.js", result)
            self.assertNotIn("credentials.json", result)
            self.assertNotIn("private", result)


if __name__ == "__main__":
    unittest.main()

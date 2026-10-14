"""Replay incomplete first writes without any model request or generated website."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from agent.initial_bootstrap import initial_bootstrap_gaps
from agent.tools import ProjectTools
from tests.test_llm_budget import _FakeMessage, _FakeToolCall, _make_client, _read_call, _write_call


def batch(files):
    return _FakeMessage([_FakeToolCall("batch", "write_files", json.dumps({"files": [
        {"path": path, "content": content} for path, content in files.items()]}))])


class InitialBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for folder in ("frontend", "backend"):
            (self.root / folder).mkdir()
        self.tools = ProjectTools(self.root)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_observed_missing_manifest_then_html_entry_then_node_test(self):
        self.assertIn("frontend/package.json is missing", initial_bootstrap_gaps(self.root))
        self.write("frontend/package.json", '{"scripts":{"build":"vite build"}}')
        self.write("frontend/index.html", '<script type="module" src="/src/main.jsx"></script>')
        self.write("backend/package.json", '{"scripts":{"test":"node tests/run.test.js"}}')
        gaps = "\n".join(initial_bootstrap_gaps(self.root))
        self.assertIn("frontend/src/main.jsx", gaps)
        self.assertIn("backend/tests/run.test.js", gaps)
        self.write("frontend/src/main.jsx", "// entry")
        self.write("backend/tests/run.test.js", "// tests")
        self.assertEqual(initial_bootstrap_gaps(self.root), ())

    def test_unknown_toolchain_custom_config_and_pre_hooks_are_not_guessed(self):
        self.write("frontend/package.json", '{"scripts":{"build":"custom-builder --entry generated"}}')
        self.write("backend/package.json", '{"scripts":{"pretest":"generate-tests","test":"node generated.js"}}')
        self.assertEqual(initial_bootstrap_gaps(self.root), ())
        self.write("frontend/package.json", '{"scripts":{"build":"vite build"}}')
        self.write("frontend/vite.config.js", "// custom rollup input")
        self.assertEqual(initial_bootstrap_gaps(self.root), ())
        self.write("frontend/index.html", '<script type="module" src="/virtual-entry.js"></script>')
        self.assertEqual(initial_bootstrap_gaps(self.root), ())

    def test_remote_html_script_and_non_object_manifest(self):
        self.write("frontend/package.json", '[]')
        self.assertIn("JSON object", "\n".join(initial_bootstrap_gaps(self.root)))
        self.write("frontend/package.json", '{"scripts":{"build":"vite build"}}')
        self.write("frontend/index.html", '<script type="module" src="https://example.test/app.js"></script>')
        self.assertEqual(initial_bootstrap_gaps(self.root), ())

    def test_node_directory_and_extensionless_entries_use_full_verifier(self):
        self.write("frontend/package.json", '{"scripts":{"build":"custom-builder"}}')
        for entry in (".", "tests", "server"):
            with self.subTest(entry=entry):
                self.write("backend/package.json", json.dumps({"scripts": {"start": "node " + entry}}))
                self.assertEqual(initial_bootstrap_gaps(self.root), ())

    def implement(self, messages, **kwargs):
        model = _make_client(None, None, messages)
        periodic = MagicMock(return_value="build: FAILED: a real verifier still decides acceptance")
        boundary = MagicMock(return_value="build: FAILED: incomplete")
        model.implement(task_type="web", subtree={"id": "ROOT"}, plan="create project",
            project_tools=self.tools, checkpoint_feedback=periodic, boundary_feedback=boundary, **kwargs)
        return model, periodic, boundary

    def test_partial_scaffold_skips_periodic_but_still_runs_final_boundary(self):
        model, periodic, boundary = self.implement([
            *[_FakeMessage([_write_call(f"piece-{n}.txt")]) for n in range(4)],
            _FakeMessage(None, "done")])
        periodic.assert_not_called()
        boundary.assert_called_once()
        self.assertFalse(model.last_implementation_handoff)
        requests = model.client.completions.requests
        self.assertIn("Initial scaffold is incomplete", str(requests[-1]["messages"]))

    def test_completion_of_declared_inputs_enables_periodic_check(self):
        _, periodic, boundary = self.implement([
            batch({"frontend/package.json": '{"scripts":{"build":"vite build"}}',
                   "frontend/index.html": '<script type="module" src="/src/main.jsx"></script>'}),
            _FakeMessage([_write_call("piece-1.txt")]),
            _FakeMessage([_write_call("piece-2.txt")]),
            batch({"frontend/src/main.jsx": "// complete entry"}),
            _FakeMessage(None, "done")])
        periodic.assert_called_once()
        boundary.assert_called_once()

    def test_incomplete_scaffold_forced_edit_can_create_missing_files(self):
        model, periodic, boundary = self.implement([
            *[_FakeMessage([_write_call(f"piece-{n}.txt")]) for n in range(4)],
            *[_FakeMessage([_read_call("piece-0.txt")]) for _ in range(7)],
            _FakeMessage(None, "done")])
        forced = [request["tool_choice"] for request in model.client.completions.requests
                  if isinstance(request.get("tool_choice"), dict)]
        self.assertTrue(forced)
        self.assertTrue(all(choice["function"]["name"] == "write_files" for choice in forced))
        periodic.assert_not_called()
        boundary.assert_called_once()

    def test_diagnostic_error_falls_back_to_real_verifier(self):
        with patch("agent.llm.initial_bootstrap_gaps", side_effect=OSError("unreadable")):
            _, periodic, boundary = self.implement([
                *[_FakeMessage([_write_call(f"piece-{n}.txt")]) for n in range(4)],
                _FakeMessage(None, "done")])
        periodic.assert_called_once()
        boundary.assert_called_once()

    def test_missing_entry_at_budget_boundary_never_bypasses_full_verification(self):
        model, periodic, boundary = self.implement([
            batch({"frontend/package.json": '{"scripts":{"build":"vite build"}}'}),
        ], request_allowance=1)
        periodic.assert_not_called()
        boundary.assert_called_once()
        self.assertFalse(model.last_implementation_handoff)

    def test_no_new_writes_do_not_repeat_periodic_verifier(self):
        _, periodic, boundary = self.implement([
            batch({"frontend/package.json": '{"scripts":{"build":"vite build"}}',
                   "frontend/index.html": '<script type="module" src="/src/main.jsx"></script>',
                   "frontend/src/main.jsx": "// complete entry"}),
            *[_FakeMessage([_read_call("frontend/src/main.jsx")]) for _ in range(10)],
            _FakeMessage(None, "done")])
        periodic.assert_called_once()
        boundary.assert_called_once()


if __name__ == "__main__":
    unittest.main()

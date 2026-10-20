"""Revalidate every batch, retain only complete unchanged source evidence."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent.tools import MAX_FILE_CHARS, ProjectTools
from tests.test_llm_budget import (_FakeMessage, _FakeToolCall, _make_client,
                                   _read_files_call)


def write(path, content):
    return _FakeToolCall("write-" + path, "write_files",
                        json.dumps({"files": [{"path": path, "content": content}]}))


class SelectiveReadCacheTests(unittest.TestCase):
    def test_unrelated_edit_preserves_b_across_overlapping_batches_and_rereads_a(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("a", "b", "c"):
                (root / (name + ".js")).write_text((name + " source") * (20 if name == "b" else 1))
            model = _make_client(10, 10, [
                _FakeMessage([_read_files_call(["a.js", "b.js"])]),
                _FakeMessage([write("a.js", "a changed")]),
                _FakeMessage([_read_files_call(["b.js", "c.js"])]),
                _FakeMessage([_read_files_call(["b.js", "a.js"])]),
                _FakeMessage(None)])
            model.implement(task_type="library", subtree={"id": "X"}, plan="fixed",
                            project_tools=ProjectTools(root))
            messages = model.client.completions.requests
            bc = json.loads(messages[3]["messages"][-1]["content"])["files"]
            ba = json.loads(messages[4]["messages"][-1]["content"])["files"]
            self.assertIn("unchanged", bc[0]["content"])
            self.assertEqual(bc[1]["content"], "c source")
            self.assertIn("unchanged", ba[0]["content"])
            self.assertEqual(ba[1]["content"], "a changed")

    def test_identical_batch_detects_external_change_and_external_delete(self):
        for delete in (False, True):
            with self.subTest(delete=delete), TemporaryDirectory() as folder:
                root = Path(folder)
                path = root / "a.js"
                path.write_text("original")
                tools = ProjectTools(root)
                original = tools.call
                calls = 0

                def call(name, args):
                    nonlocal calls
                    value = original(name, args)
                    calls += 1
                    if calls == 1:
                        if delete:
                            path.unlink()
                        else:
                            path.write_text("external update")
                    return value
                tools.call = call
                model = _make_client(5, 5, [
                    _FakeMessage([_read_files_call(["a.js"])]),
                    _FakeMessage([_read_files_call(["a.js"])]), _FakeMessage(None)])
                model.implement(task_type="library", subtree={"id": "X"}, plan="fixed", project_tools=tools)
                reread = model.client.completions.requests[2]["messages"][-1]["content"]
                self.assertIn("FileNotFoundError" if delete else "external update", reread)
                self.assertNotIn("same read already returned", reread)

    def test_truncated_prefix_is_never_reported_as_complete_unchanged_source(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.js").write_text("x" * (MAX_FILE_CHARS + 1))
            tools = ProjectTools(root)
            model = _make_client(5, 5, [
                _FakeMessage([_read_files_call(["a.js"])]),
                _FakeMessage([_read_files_call(["a.js"])]), _FakeMessage(None)])
            model.implement(task_type="library", subtree={"id": "X"}, plan="fixed", project_tools=tools)
            reread = model.client.completions.requests[2]["messages"][-1]["content"]
            self.assertNotIn("unchanged since", reread)
            self.assertTrue(json.loads(reread)["truncated"])

    def test_changed_then_restored_content_is_returned_after_write_invalidation(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.js").write_text("original")
            tools = ProjectTools(root)
            model = _make_client(6, 6, [
                _FakeMessage([_read_files_call(["a.js"])]),
                _FakeMessage([write("a.js", "new")]),
                _FakeMessage([write("a.js", "original")]),
                _FakeMessage([_read_files_call(["a.js"])]), _FakeMessage(None)])
            model.implement(task_type="library", subtree={"id": "X"}, plan="fixed", project_tools=tools)
            self.assertEqual(json.loads(model.client.completions.requests[4]["messages"][-1]["content"])
                             ["files"][0]["content"], "original")


if __name__ == "__main__":
    unittest.main()

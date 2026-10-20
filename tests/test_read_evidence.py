import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent.read_evidence import ReadEvidence, UNCHANGED
from agent.tools import ProjectTools
from tests.test_llm_budget import _FakeMessage, _FakeToolCall, _make_client, _read_files_call


SOURCE = "export const keepExactSource = 'retained';\n" * 100


def single(path="a.js", **kwargs):
    return _FakeToolCall("read", "read_file", json.dumps({"path": path, **kwargs}))


class ReadEvidenceTests(unittest.TestCase):
    def test_only_actual_source_survives_compaction_not_a_reference_to_dropped_source(self):
        cache = ReadEvidence()
        source = {"role": "tool", "content": SOURCE}
        ref = {"role": "tool", "content": UNCHANGED}
        args = {"path": "a.js"}
        cache.remember("read_file", args, source)
        cache.remember("read_file", args, ref)
        cache.retain([[source], [ref]])
        self.assertEqual(cache.filter("read_file", args, SOURCE), UNCHANGED)
        cache.retain([[ref]])
        self.assertEqual(cache.filter("read_file", args, SOURCE), SOURCE)

    def test_single_and_batch_share_complete_file_evidence_in_both_directions(self):
        for first in ("read_file", "read_files"):
            with self.subTest(first=first):
                cache = ReadEvidence()
                batch = json.dumps({"files": [{"path": "./a.js", "content": SOURCE}], "truncated": False})
                args = {"path": "a.js", "start_line": 1}
                cache.remember(first, args, {"content": SOURCE if first == "read_file" else batch})
                if first == "read_file":
                    self.assertEqual(json.loads(cache.filter("read_files", {}, batch))["files"][0]["content"], UNCHANGED)
                else:
                    self.assertEqual(cache.filter("read_file", args, SOURCE), UNCHANGED)

    def test_ranges_are_distinct_and_never_prove_a_complete_file(self):
        cache = ReadEvidence()
        args = {"path": "a.js", "start_line": 3, "end_line": 5}
        cache.remember("read_file", args, {"content": SOURCE})
        self.assertEqual(cache.filter("read_file", args, SOURCE), UNCHANGED)
        for other in ({"path": "a.js"}, dict(args, start_line=2), dict(args, end_line=6)):
            self.assertEqual(cache.filter("read_file", other, SOURCE), SOURCE)

    def test_truncated_malformed_and_short_payloads_do_not_justify_suppression(self):
        cache = ReadEvidence()
        args = {"path": "a.js"}
        partial = SOURCE + "\n...[truncated]"
        cache.remember("read_file", args, {"content": partial})
        self.assertEqual(cache.filter("read_file", args, partial), partial)
        for payload in ('{"files":', "[]", "null",
                        json.dumps({"files": [{"path": "a.js", "content": SOURCE}], "truncated": True})):
            cache.remember("read_files", {}, {"content": payload})
        self.assertEqual(cache.filter("read_file", args, SOURCE), SOURCE)
        cache.remember("read_file", args, {"content": "tiny"})
        self.assertEqual(cache.filter("read_file", args, "tiny"), "tiny")
        batch = json.dumps({"files": [{"path": "a.js", "content": "tiny"}], "truncated": False})
        self.assertEqual(cache.filter("read_files", {}, batch), batch)

    def test_invalidation_removes_all_ranges_of_only_the_changed_file(self):
        cache = ReadEvidence()
        for path in ("a.js", "b.js"):
            for start in (1, 3):
                cache.remember("read_file", {"path": path, "start_line": start}, {"content": SOURCE})
        cache.invalidate(["./a.js"])
        for start in (1, 3):
            self.assertEqual(cache.filter("read_file", {"path": "a.js", "start_line": start}, SOURCE), SOURCE)
            self.assertEqual(cache.filter("read_file", {"path": "b.js", "start_line": start}, SOURCE), UNCHANGED)
        cache.clear()
        self.assertEqual(cache.filter("read_file", {"path": "b.js"}, SOURCE), SOURCE)

    def test_successful_write_is_evidence_only_for_exact_bytes_while_message_remains(self):
        cache = ReadEvidence()
        owner = {"role": "assistant", "tool_calls": []}
        cache.remember_write("write_files", {"files": [{"path": "a.js", "content": SOURCE}]}, owner)
        self.assertEqual(cache.filter("read_file", {"path": "a.js"}, SOURCE), UNCHANGED)
        self.assertEqual(cache.filter("read_file", {"path": "a.js"}, SOURCE + "normalized"), SOURCE + "normalized")
        cache.retain([[owner]])
        self.assertEqual(cache.filter("read_file", {"path": "a.js"}, SOURCE), UNCHANGED)
        cache.retain([])
        self.assertEqual(cache.filter("read_file", {"path": "a.js"}, SOURCE), SOURCE)


class SourceReuseIntegrationTests(unittest.TestCase):
    def test_successful_full_write_avoids_rereading_its_contents_into_context(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write = _FakeToolCall("write", "write_files", json.dumps({"files": [{"path": "a.js", "content": SOURCE}]}))
            model = _make_client(None, None, [_FakeMessage([write]), _FakeMessage([single()]), _FakeMessage(None)])
            model.implement(task_type="library", subtree={"id": "X"}, plan="fixed", project_tools=ProjectTools(root))
            self.assertEqual(model.client.completions.requests[2]["messages"][-1]["content"], UNCHANGED)
            self.assertEqual((root / "a.js").read_text(encoding="utf-8"), SOURCE)

    def test_failed_full_write_does_not_register_the_proposed_source(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.js").write_text(SOURCE, encoding="utf-8")
            tools = ProjectTools(root)
            original = tools.call
            def call(name, args):
                if name == "write_files":
                    raise ValueError("simulated write rejection")
                return original(name, args)
            tools.call = call
            write = _FakeToolCall("write", "write_files", json.dumps({"files": [{"path": "a.js", "content": SOURCE}]}))
            model = _make_client(None, None, [_FakeMessage([write]), _FakeMessage([single()]), _FakeMessage(None)])
            model.implement(task_type="library", subtree={"id": "X"}, plan="fixed", project_tools=tools)
            self.assertEqual(model.client.completions.requests[2]["messages"][-1]["content"], SOURCE)

    def test_mixed_reads_preserve_request_prefix_and_emit_saved_characters(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.js").write_text(SOURCE, encoding="utf-8")
            model = _make_client(None, None, [
                _FakeMessage([single()]),
                _FakeMessage([_read_files_call(["a.js"])]),
                _FakeMessage([single("./a.js")]),
                _FakeMessage(None)])
            with patch("agent.llm.emit_metric") as metric:
                model.implement(task_type="library", subtree={"id": "X"}, plan="fixed", project_tools=ProjectTools(root))
            requests = model.client.completions.requests
            self.assertEqual(requests[2]["messages"][:len(requests[1]["messages"])], requests[1]["messages"])
            self.assertEqual(requests[3]["messages"][-1]["content"], UNCHANGED)
            self.assertEqual(json.loads(requests[2]["messages"][-1]["content"])["files"][0]["content"], UNCHANGED)
            savings = [call.kwargs for call in metric.call_args_list if call.args == ("source_read_reused",)]
            self.assertEqual(len(savings), 2)
            self.assertTrue(all(item["removed_chars"] > 0 for item in savings))

    def test_single_read_still_checks_external_change_and_deletion(self):
        for delete in (False, True):
            with self.subTest(delete=delete), TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "a.js"
                path.write_text(SOURCE, encoding="utf-8")
                tools = ProjectTools(root)
                original = tools.call
                calls = 0
                def call(name, arguments):
                    nonlocal calls
                    value = original(name, arguments)
                    calls += 1
                    if calls == 1:
                        if delete:
                            path.unlink()
                        else:
                            path.write_text(SOURCE + "external change", encoding="utf-8")
                    return value
                tools.call = call
                model = _make_client(None, None, [_FakeMessage([single()]), _FakeMessage([single()]), _FakeMessage(None)])
                model.implement(task_type="library", subtree={"id": "X"}, plan="fixed", project_tools=tools)
                response = model.client.completions.requests[2]["messages"][-1]["content"]
                self.assertIn("FileNotFoundError" if delete else "external change", response)
                self.assertNotIn(UNCHANGED, response)

    def test_compaction_keeps_cached_source_only_when_payload_is_retained(self):
        for drop in (False, True):
            with self.subTest(drop=drop), TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "a.js").write_text(SOURCE, encoding="utf-8")
                reasoning = "r" * 80_000 if drop else "inspect current source"
                model = _make_client(None, None, [
                    _FakeMessage([single()], reasoning_content=reasoning),
                    _FakeMessage([single()]),
                    _FakeMessage(None)])
                with patch("agent.llm.MAX_CONVERSATION_CHARS", 100_000):
                    model.implement(task_type="library", subtree={"id": "X"},
                                    plan="large immutable task " * 6000, project_tools=ProjectTools(root))
                response = model.client.completions.requests[2]["messages"][-1]["content"]
                # Force compaction after the first read without making its
                # complete exchange too large for the retained suffix.
                if drop:
                    self.assertEqual(response, SOURCE)
                else:
                    self.assertEqual(response, UNCHANGED)


if __name__ == "__main__":
    unittest.main()

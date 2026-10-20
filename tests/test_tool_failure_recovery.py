"""Offline tool-loop recovery uses current source, not changing failed arguments."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock

from agent.tools import ProjectTools
from tests.test_llm_budget import _FakeMessage, _FakeToolCall, _make_client, _read_files_call


SOURCE = "frontend/src/App.js"
OTHER = "frontend/src/other.js"
BASELINE = "first = one;\nrepeat\nrepeat\n"


def call(name, **arguments):
    return _FakeToolCall(name + "-" + str(len(json.dumps(arguments))), name, json.dumps(arguments))


def replace(old_text, new_text="replacement", path=SOURCE):
    return call("replace_text", path=path, old_text=old_text, new_text=new_text)


def message(*calls):
    return _FakeMessage(list(calls))


class SourceToolFailureRecoveryTests(unittest.TestCase):
    def project(self, root):
        tools = ProjectTools(root)
        tools.write_file(SOURCE, BASELINE)
        tools.write_file(OTHER, "other")
        tools.call = MagicMock(wraps=tools.call)
        return tools

    def implement(self, model, tools, *, checkpoint=None):
        boundary = MagicMock(return_value="Current build and test scripts pass. Formal acceptance pending.")
        exhausted = model.implement(task_type="web", subtree={"id": "ROOT"}, plan="Repair original behavior",
            project_tools=tools, checkpoint_feedback=checkpoint, boundary_feedback=boundary)
        self.assertFalse(exhausted)
        self.assertIsNone(model.last_budget_report)
        boundary.assert_called_once_with()
        return boundary

    def test_varied_zero_and_multiple_matches_with_successful_unrelated_reads_stop(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools = self.project(root)
            model = _make_client(None, None, [
                message(replace("missing-first"), _read_files_call([OTHER])),
                message(replace("repeat", "different"), _read_files_call([OTHER])),
                message(replace("missing-third", "yet another"), _read_files_call([OTHER])),
                _FakeMessage(None, "unused")])
            self.implement(model, tools)
            self.assertEqual(model.model_requests_used, 3)
            self.assertEqual(model.last_implementation_stop_reason, "repeated_tool_failure")
            self.assertTrue(model.last_implementation_deferred)
            self.assertFalse(model.last_implementation_handoff)
            self.assertEqual((root / SOURCE).read_text(), BASELINE)
            self.assertEqual(model.client.completions.requests[2]["tool_choice"]["function"]["name"], "read_file")
            context = json.dumps(model.client.completions.requests[2]["messages"])
            self.assertIn("fresh read_file/read_files", context)
            self.assertIn("Unrelated reads and no-op writes", context)
            # The third guessed edit was refused before calling ProjectTools.
            self.assertEqual(sum(c.args[0] == "replace_text" for c in tools.call.call_args_list), 2)

    def test_unrelated_read_turn_does_not_unlock_target_edit(self):
        with TemporaryDirectory() as directory:
            tools = self.project(Path(directory))
            model = _make_client(None, None, [message(replace("first guess")), message(replace("second guess")),
                message(_read_files_call([OTHER])), message(replace("first = one;", "must not write"))])
            self.implement(model, tools)
            self.assertEqual(model.model_requests_used, 4)
            self.assertEqual(model.last_implementation_stop_reason, "repeated_tool_failure")
            self.assertEqual((Path(directory) / SOURCE).read_text(), BASELINE)

    def test_fresh_target_read_bypasses_cached_read_and_unlocks_valid_edit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools = self.project(root)
            model = _make_client(None, None, [message(_read_files_call([SOURCE])),
                message(replace("first wrong")), message(replace("repeat")),
                message(_read_files_call([SOURCE])), message(replace("first = one;", "first = two;")),
                _FakeMessage(None, "done")])
            self.implement(model, tools)
            self.assertEqual(model.model_requests_used, 6)
            self.assertTrue(model.last_implementation_handoff)
            self.assertIn("first = two;", (root / SOURCE).read_text())
            self.assertEqual(sum(c.args[0] == "read_files" for c in tools.call.call_args_list), 2)
            fresh_tool = [m["content"] for m in model.client.completions.requests[4]["messages"]
                          if m["role"] == "tool"][-1]
            self.assertIn("first = one;", fresh_tool)
            self.assertNotIn("unchanged since earlier read", fresh_tool)
            self.assertNotIn("same read already returned", fresh_tool)

    def test_different_missing_read_arguments_and_mixed_successes_still_stop(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools = self.project(root)
            missing = "frontend/src/absent.js"
            model = _make_client(None, None, [
                message(call("read_file", path=missing, start_line=1), _read_files_call([OTHER])),
                message(_read_files_call([OTHER, missing])),
                message(call("read_file", path="./" + missing, start_line=2)),
                _FakeMessage(None, "unused")])
            self.implement(model, tools)
            self.assertEqual(model.model_requests_used, 3)
            self.assertEqual(model.last_implementation_stop_reason, "repeated_tool_failure")
            self.assertEqual(model.client.completions.requests[2]["tool_choice"]["function"]["name"], "list_files")
            self.assertFalse((root / missing).exists())

    def test_real_parent_listing_discovers_an_existing_path_without_fake_file_creation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools = self.project(root)
            missing = "frontend/src/absent.js"
            model = _make_client(None, None, [message(call("read_file", path=missing)),
                message(_read_files_call([missing])), message(call("list_files", path="frontend/src")),
                message(_read_files_call([SOURCE])), message(replace("first = one;", "first = two;")),
                _FakeMessage(None, "done")])
            self.implement(model, tools)
            self.assertTrue(model.last_implementation_handoff)
            self.assertIn("first = two;", (root / SOURCE).read_text())
            self.assertFalse((root / missing).exists())
            self.assertEqual(model.model_requests_used, 6)

    def test_parent_listing_does_not_reset_repeated_missing_file_count(self):
        with TemporaryDirectory() as directory:
            tools = self.project(Path(directory))
            missing = "frontend/src/absent.js"
            model = _make_client(None, None, [message(call("read_file", path=missing)),
                message(_read_files_call([missing])), message(call("list_files", path="frontend/src")),
                message(call("read_file", path=missing, start_line=2))])
            self.implement(model, tools)
            self.assertEqual(model.model_requests_used, 4)
            self.assertEqual(model.last_implementation_stop_reason, "repeated_tool_failure")

    def test_unrelated_real_change_and_no_op_write_do_not_reset_target_errors(self):
        for changed in (False, True):
            with self.subTest(changed=changed), TemporaryDirectory() as directory:
                root = Path(directory)
                tools = self.project(root)
                scripted = [message(replace("wrong one")), message(replace("wrong two")),
                    message(call("write_file", path=OTHER, content="updated" if changed else "other")),
                    message(replace("wrong three"))]
                model = _make_client(None, None, scripted)
                self.implement(model, tools)
                self.assertEqual(model.model_requests_used, 4)
                self.assertEqual(model.last_implementation_stop_reason, "repeated_tool_failure")
                self.assertEqual((root / SOURCE).read_text(), BASELINE)

    def test_guarded_source_cannot_be_overwritten_through_another_edit_tool(self):
        for edit in (call("write_file", path=SOURCE, content="guessed overwrite"),
                     call("write_files", files=[{"path": SOURCE, "content": "guessed overwrite"}])):
            with self.subTest(tool=edit.function.name), TemporaryDirectory() as directory:
                root = Path(directory)
                tools = self.project(root)
                model = _make_client(None, None, [message(replace("wrong one")),
                    message(replace("wrong two")), message(edit)])
                self.implement(model, tools)
                self.assertEqual((root / SOURCE).read_text(), BASELINE)
                self.assertEqual(model.last_implementation_stop_reason, "repeated_tool_failure")
                self.assertFalse(any(c.args[0] == edit.function.name for c in tools.call.call_args_list))

    def test_actual_target_change_restarts_failure_count(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools = self.project(root)
            model = _make_client(None, None, [message(replace("wrong before change")),
                message(call("write_file", path=SOURCE, content="changed = one;")),
                message(replace("wrong after change one")), message(replace("wrong after change two")),
                message(call("read_file", path=SOURCE)), message(replace("changed = one;", "changed = two;")),
                _FakeMessage(None, "done")])
            self.implement(model, tools)
            self.assertEqual(model.model_requests_used, 7)
            self.assertTrue(model.last_implementation_handoff)
            self.assertEqual((root / SOURCE).read_text(), "changed = two;")

    def test_large_file_fresh_range_read_unlocks_its_unique_anchor_edit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools = self.project(root)
            large = "// unchanged source line\n" * 4000 + "unique_anchor = one;\n"
            (root / SOURCE).write_text(large, encoding="utf-8")
            self.assertGreater(len(large), 30_000)
            model = _make_client(None, None, [message(replace("wrong one")), message(replace("wrong two")),
                message(call("read_file", path=SOURCE, start_line=4001, end_line=4001)),
                message(replace("unique_anchor = one;", "unique_anchor = two;")), _FakeMessage(None, "done")])
            self.implement(model, tools)
            self.assertTrue(model.last_implementation_handoff)
            self.assertTrue((root / SOURCE).read_text().endswith("unique_anchor = two;\n"))
            fresh_tool = [m["content"] for m in model.client.completions.requests[3]["messages"]
                          if m["role"] == "tool"][-1]
            self.assertEqual(fresh_tool, "unique_anchor = one;")

    def test_truncated_real_batch_read_can_refresh_an_anchor_it_returned(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools = self.project(root)
            large = "unique_anchor = one;\n" + "// unchanged source line\n" * 4000
            (root / SOURCE).write_text(large, encoding="utf-8")
            model = _make_client(None, None, [message(replace("wrong one")), message(replace("wrong two")),
                message(_read_files_call([SOURCE])), message(replace("unique_anchor = one;", "unique_anchor = two;")),
                _FakeMessage(None, "done")])
            self.implement(model, tools)
            self.assertTrue(model.last_implementation_handoff)
            self.assertTrue((root / SOURCE).read_text().startswith("unique_anchor = two;\n"))
            fresh_tool = [m["content"] for m in model.client.completions.requests[3]["messages"]
                          if m["role"] == "tool"][-1]
            self.assertTrue(json.loads(fresh_tool)["truncated"])
            self.assertIn("unique_anchor = one;", fresh_tool)

    def test_empty_out_of_range_read_does_not_unlock_a_guessed_edit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools = self.project(root)
            model = _make_client(None, None, [message(replace("wrong one")), message(replace("wrong two")),
                message(call("read_file", path=SOURCE, start_line=999, end_line=999)),
                message(replace("first = one;", "must not write"))])
            self.implement(model, tools)
            self.assertEqual(model.last_implementation_stop_reason, "repeated_tool_failure")
            self.assertEqual((root / SOURCE).read_text(), BASELINE)

    def test_one_error_allows_corrected_exact_edit_without_forcing_a_read(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools = self.project(root)
            model = _make_client(None, None, [message(replace("wrong")),
                message(replace("first = one;", "first = two;")), _FakeMessage(None, "done")])
            self.implement(model, tools)
            self.assertEqual(model.model_requests_used, 3)
            self.assertEqual(model.client.completions.requests[1]["tool_choice"], "auto")
            self.assertTrue(model.last_implementation_handoff)
            self.assertIn("first = two;", (root / SOURCE).read_text())

    def test_controller_source_revision_restarts_failure_tracking_and_rebases_context(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools = self.project(root)
            observed = []
            def checkpoint():
                observed.append(tools.source_revision)
                (root / SOURCE).write_text("restored = one;", encoding="utf-8")
                tools.source_revision += 1
                return "Controller restored earlier source; required behavior still fails"
            model = _make_client(None, None, [message(call("write_file", path=OTHER, content="updated")),
                message(replace("wrong one")), message(replace("wrong two")),
                message(_read_files_call([OTHER])), message(replace("wrong after restore")),
                message(_read_files_call([SOURCE])), message(replace("restored = one;", "restored = two;")),
                _FakeMessage(None, "done")])
            self.implement(model, tools, checkpoint=checkpoint)
            self.assertEqual(observed, [0])
            self.assertEqual(model.model_requests_used, 8)
            self.assertTrue(model.last_implementation_handoff)
            self.assertEqual((root / SOURCE).read_text(), "restored = two;")
            context = json.dumps(model.client.completions.requests[4]["messages"])
            self.assertIn("controller restored an earlier candidate", context)
            self.assertNotIn("wrong one", context)
            self.assertNotIn("wrong two", context)
            self.assertEqual(model.client.completions.requests[4]["tool_choice"], "auto")

    def test_recovery_read_preserves_deepseek_thinking(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools = self.project(root)
            model = _make_client(None, None, [message(replace("wrong one")), message(replace("wrong two")),
                message(_read_files_call([SOURCE])), message(replace("first = one;", "first = two;")),
                _FakeMessage(None, "done")], {"MODEL": "deepseek-flash", "OPENAI_BASE_URL": "https://api.deepseek.com"})
            self.implement(model, tools)
            recovery = model.client.completions.requests[2]
            self.assertEqual(recovery["extra_body"], {"thinking": {"type": "enabled"}})
            self.assertEqual(recovery["reasoning_effort"], "low")
            self.assertEqual(recovery["tool_choice"], "auto")
            self.assertEqual(model.client.completions.requests[3]["extra_body"]["thinking"]["type"], "enabled")
            self.assertTrue(model.last_implementation_handoff)

    def test_thinking_refresh_refuses_unrelated_write_before_execution(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools = self.project(root)
            scripted = [message(replace("wrong one")), message(replace("wrong two")),
                message(call("write_file", path=OTHER, content="must not execute"))]
            for response in scripted:
                response.reasoning_content = "inspect and repair source"
            model = _make_client(None, None, scripted, {"MODEL": "deepseek-flash"})
            model.client.completions.require_reasoning = True
            self.implement(model, tools)
            self.assertEqual((root / OTHER).read_text(), "other")
            self.assertEqual((root / SOURCE).read_text(), BASELINE)
            self.assertEqual(model.last_implementation_stop_reason, "repeated_tool_failure")
            self.assertFalse(any(c.args[0] == "write_file" for c in tools.call.call_args_list))
            request = model.client.completions.requests[2]
            self.assertEqual(request["tool_choice"], "auto")
            self.assertEqual(request["extra_body"]["thinking"]["type"], "enabled")


if __name__ == "__main__":
    unittest.main()

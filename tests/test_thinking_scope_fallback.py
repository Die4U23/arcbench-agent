from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.llm import DEEPSEEK_THINKING_IMPLEMENTATION_MAX_TOKENS, MAX_COMPLETION_TOKENS_PER_REQUEST
from agent.tools import ProjectTools, TOOL_SCHEMAS
from tests.test_llm_budget import _FakeMessage, _make_client, _read_call, _write_call


def _pure():
    return _FakeMessage(content="", reasoning_content="unfinished reasoning", finish_reason="length")


def _usage(reasoning=20, completion=20):
    return SimpleNamespace(prompt_tokens=100, completion_tokens=completion,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning))


def _client(messages, usage=None):
    model = _make_client(None, None, messages, {
        "MODEL": "deepseek-flash", "OPENAI_BASE_URL": "https://api.deepseek.com",
    })
    original = model.client.completions.create

    def create(**kwargs):
        response = original(**kwargs)
        if model.client.completions.calls == 1:
            response.usage = usage
        return response

    model.client.completions.create = create
    return model


def _mode(request):
    return request.get("extra_body", {}).get("thinking", {}).get("type")


class ThinkingScopeFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tools = ProjectTools(Path(self.temp.name))
        self.project = {"id": "ROOT", "children": [
            {"id": "REQ-1", "description": "first requirement"},
            {"id": "REQ-2", "description": "second requirement"},
        ]}
        self.scope = dict(self.project["children"][0], project_requirements=self.project)

    def implement(self, model, **kwargs):
        defaults = dict(task_type="web", subtree=self.scope, plan="plan", project_tools=self.tools)
        defaults.update(kwargs)
        return model.implement(**defaults)

    def test_compaction_continuation_and_planning_keep_their_modes(self):
        model = _client([_pure(), _FakeMessage([_write_call("a.js")]),
            _FakeMessage([_read_call("a.js")]), _FakeMessage(), _FakeMessage(),
            _FakeMessage(content="implementation plan")], _usage())
        boundary = MagicMock(return_value="FAILED: outstanding requirement")
        with patch("agent.llm.MAX_CONVERSATION_CHARS", 1):
            self.implement(model, boundary_feedback=boundary)
        self.implement(model, plan="revised plan", repair_feedback="new failure",
            boundary_feedback=boundary)
        model.plan("web", self.scope)
        requests = model.client.completions.requests
        self.assertEqual([_mode(r) for r in requests[:5]],
            ["enabled", "disabled", "disabled", "disabled", "disabled"])
        self.assertNotEqual(_mode(requests[5]), "disabled")
        self.assertEqual(requests[0]["max_tokens"], DEEPSEEK_THINKING_IMPLEMENTATION_MAX_TOKENS)
        self.assertTrue(all(r["max_tokens"] == MAX_COMPLETION_TOKENS_PER_REQUEST for r in requests[1:5]))
        self.assertTrue(all(r["tools"] == TOOL_SCHEMAS for r in requests[:5]))
        self.assertIn("Compact progress ledger", str(requests[2]["messages"]))
        self.assertEqual(boundary.call_count, 2)
        self.assertFalse(model.last_implementation_handoff)
        self.assertEqual((model.prompt_tokens_used, model.completion_tokens_used), (100, 20))

    def test_allocation_stop_still_verifies_and_preserves_same_scope_fallback(self):
        model = _client([_pure(), _FakeMessage()], _usage())
        boundary = MagicMock(return_value="FAILED: incomplete candidate")
        self.implement(model, request_allowance=1, boundary_feedback=boundary)
        self.assertEqual(model.model_requests_used, 1)
        self.assertEqual(model.last_implementation_stop_reason, "allocation")
        self.assertTrue(model.last_implementation_deferred)
        self.assertFalse(model.last_implementation_handoff)
        self.implement(model, boundary_feedback=boundary)
        self.assertEqual([_mode(r) for r in model.client.completions.requests], ["enabled", "disabled"])
        self.assertEqual(boundary.call_count, 2)

    def test_scope_project_task_model_and_instance_are_isolated(self):
        cases = ("scope", "requirements", "directory", "task", "model", "instance")
        for case in cases:
            with self.subTest(case=case):
                model = _client([_pure(), _FakeMessage()], _usage())
                self.implement(model, request_allowance=1)
                kwargs = {}
                if case == "scope":
                    kwargs["subtree"] = dict(self.scope, id="REQ-2")
                elif case == "requirements":
                    changed = copy.deepcopy(self.project)
                    changed["children"][0]["description"] = "changed requirement"
                    kwargs["subtree"] = dict(self.scope, project_requirements=changed)
                elif case == "directory":
                    other = Path(self.temp.name) / "other-project"
                    other.mkdir(exist_ok=True)
                    kwargs["project_tools"] = ProjectTools(other)
                elif case == "task":
                    kwargs["task_type"] = "other-task"
                elif case == "model":
                    # Offline alternate-model fixture; no provider request occurs.
                    model.model = "alternate-model-fixture"
                else:
                    model = _client([_FakeMessage()])
                self.implement(model, **kwargs)
                self.assertEqual(_mode(model.client.completions.requests[-1]), "enabled")

    def test_anonymous_scopes_do_not_share_fallback(self):
        for scope_id in (None, "", " "):
            with self.subTest(scope_id=scope_id):
                model = _client([_pure(), _FakeMessage()], _usage())
                scope = dict(self.scope, id=scope_id)
                self.implement(model, subtree=scope, request_allowance=1)
                self.implement(model, subtree=scope)
                self.assertEqual(_mode(model.client.completions.requests[-1]), "enabled")

    def test_only_proven_pure_reasoning_length_is_sticky_and_partial_tools_never_run(self):
        cases = [
            ("missing usage", _pure(), None),
            ("missing details", _pure(), SimpleNamespace(prompt_tokens=100, completion_tokens=20)),
            ("unknown reasoning", _pure(), _usage(None)),
            ("mixed tokens", _pure(), _usage(19)),
            ("inconsistent tokens", _pure(), _usage(21)),
            ("zero tokens", _pure(), _usage(0, 0)),
            ("visible text", _FakeMessage(content="partial", finish_reason="length"), _usage()),
            ("partial tools", _FakeMessage([_write_call("partial.js")], content="", finish_reason="length"), _usage()),
            ("normal stop", _FakeMessage(content="", finish_reason="stop"), _usage()),
        ]
        for label, first, usage in cases:
            with self.subTest(case=label):
                model = _client([first, _FakeMessage()], usage)
                self.implement(model, request_allowance=1)
                self.implement(model)
                self.assertEqual(_mode(model.client.completions.requests[-1]), "enabled")
                self.assertFalse((Path(self.temp.name) / "partial.js").exists())

    def test_source_restore_drops_stale_protocol_but_retains_scope_mode(self):
        source = Path(self.temp.name) / "source.js"
        source.write_text("STALE_SOURCE", encoding="utf-8")
        model = _client([_pure(), _FakeMessage([_write_call("a.js")]),
            _FakeMessage([_read_call("source.js")]), _FakeMessage()], _usage())
        original_call = self.tools.call

        def restore_after_read(name, arguments):
            result = original_call(name, arguments)
            if name == "read_file":
                source.write_text("restored source", encoding="utf-8")
                self.tools.source_revision += 1
            return result

        with patch.object(self.tools, "call", side_effect=restore_after_read):
            self.implement(model)
        final = model.client.completions.requests[-1]
        self.assertEqual(_mode(final), "disabled")
        self.assertNotIn("STALE_SOURCE", str(final["messages"]))
        self.assertFalse(any(m.get("role") == "tool" or m.get("tool_calls") for m in final["messages"]))

    def test_scope_metadata_changes_do_not_change_project_identity(self):
        model = _client([_pure(), _FakeMessage()], _usage())
        self.implement(model, request_allowance=1)
        self.implement(model, subtree=dict(self.scope, traceability={"status": "retry"}))
        self.assertEqual(_mode(model.client.completions.requests[-1]), "disabled")


if __name__ == "__main__":
    unittest.main()

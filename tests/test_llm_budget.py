from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.llm import (
    DEEPSEEK_THINKING_IMPLEMENTATION_MAX_TOKENS,
    MAX_COMPLETION_TOKENS_PER_REQUEST,
    ModelBudgetExceeded,
    ModelClient,
)
from agent.tools import ProjectTools


class _FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.type = "function"
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(
        self,
        tool_calls: list[_FakeToolCall] | None = None,
        content: str = "done",
        reasoning_content: str | None = None,
        finish_reason: str | None = None,
    ) -> None:
        self.tool_calls = tool_calls
        self.content = content
        self.reasoning_content = reasoning_content
        self.finish_reason = finish_reason

    def model_dump(self, exclude_none: bool = True) -> dict:
        if self.tool_calls is None:
            return {"role": "assistant", "content": self.content}
        payload = {
            "role": "assistant",
            "content": self.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
                for call in self.tool_calls
            ],
        }
        if self.reasoning_content is not None:
            payload["reasoning_content"] = self.reasoning_content
        return payload


class _FakeCompletions:
    def __init__(self, scripted_messages: list[_FakeMessage]) -> None:
        self._messages = list(scripted_messages)
        self.calls = 0
        self.requests = []
        self.require_reasoning = False

    def create(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        if self.require_reasoning and kwargs.get("tools") and kwargs.get("extra_body", {}).get("thinking", {}).get("type") == "enabled":
            if any(
                "reasoning_content" not in message
                for message in kwargs["messages"]
                if message.get("role") == "assistant"
            ):
                raise ValueError("reasoning_content must be passed back with tools")
        message = self._messages.pop(0)
        choice = type("Choice", (), {"message": message, "finish_reason": message.finish_reason})()
        return type("Response", (), {"choices": [choice]})()


class _FakeClient:
    def __init__(self, scripted_messages: list[_FakeMessage]) -> None:
        self.completions = _FakeCompletions(scripted_messages)
        self.chat = type("Chat", (), {"completions": self.completions})()


def _write_call(path: str) -> _FakeToolCall:
    arguments = json.dumps({"path": path, "content": "// generated"})
    return _FakeToolCall(f"call-{path}", "write_file", arguments)


def _read_call(path: str) -> _FakeToolCall:
    return _FakeToolCall(f"call-{path}", "read_file", json.dumps({"path": path}))


def _read_files_call(paths: list[str]) -> _FakeToolCall:
    return _FakeToolCall("call-read-files", "read_files", json.dumps({"paths": paths}))


def _make_client(
    max_turns: int | None,
    max_tool_calls: int | None,
    scripted: list[_FakeMessage],
    env_overrides: dict[str, str] | None = None,
) -> ModelClient:
    environment = {"OPENAI_API_KEY": "test-key", "MODEL": "test-model"}
    environment.update(env_overrides or {})
    with patch.dict(os.environ, environment, clear=True):
        model = ModelClient(max_turns=max_turns, max_tool_calls=max_tool_calls)
    model.client = _FakeClient(scripted)
    return model


def _repair_review(missing: str, *, quote: str = "test task",
                   evidence: str = "return null;", requirement_id: str = "ROOT",
                   path: str = "frontend/src/App.jsx") -> str:
    return json.dumps({"verdict": "repair", "issues": [{
        "requirement_id": requirement_id, "requirement_quote": quote,
        "path": path, "evidence": evidence, "missing_behavior": missing,
    }]})


class ImplementBudgetTests(unittest.TestCase):
    def test_public_contract_reaches_planning_implementation_and_source_audit(self) -> None:
        tree = {"id": "ROOT", "name": "Railway Ticket Booking Demo", "children": [
            {"id": node, "name": node, "description": "contract"}
            for node in ("REQ-1.1", "REQ-1.2", "REQ-2.1", "REQ-2.2", "REQ-3.1", "REQ-3.2")
        ]}
        model = _make_client(None, None, [
            _FakeMessage(content="plan"), _FakeMessage(content="done"),
            _FakeMessage(content='{"verdict":"pass","issues":[]}'),
        ])
        model.plan("web", tree)
        with tempfile.TemporaryDirectory() as directory:
            tools = ProjectTools(Path(directory))
            model.implement(task_type="web", subtree=tree, plan="plan", project_tools=tools)
            model.review_requirements(tree, tools)
        for request in model.client.completions.requests:
            contents = "\n".join(message["content"] for message in request["messages"])
            self.assertIn("Published acceptance interaction evidence", contents)
            self.assertIn("page.getByLabel(/^date$/i).fill", contents)

    def test_default_website_budget_is_eight_million_with_250_requests(self) -> None:
        model = _make_client(None, None, [])
        self.assertEqual(model.max_total_tokens, 8_000_000)
        self.assertEqual(model.max_model_requests, 250)
        model.prompt_tokens_used = 7_999_999
        model.model_requests_used = 249
        model._check_model_budget()
        model.prompt_tokens_used += 1
        with self.assertRaises(ModelBudgetExceeded) as raised:
            model._check_model_budget()
        self.assertEqual(raised.exception.report.budget, "total_token_budget")
        self.assertEqual(raised.exception.report.limit, 8_000_000)
        model.prompt_tokens_used = 0
        model.model_requests_used = 250
        with self.assertRaises(ModelBudgetExceeded) as raised:
            model._check_model_budget()
        self.assertEqual(raised.exception.report.budget, "model_request_budget")
        self.assertEqual(raised.exception.report.limit, 250)

    def test_delivery_reserve_stops_next_request_before_hard_token_cap(self) -> None:
        model = _make_client(None, None, [])
        model.prompt_tokens_used = 7_600_000
        with self.assertRaises(ModelBudgetExceeded) as raised:
            model._record_model_request()
        self.assertEqual(raised.exception.report.budget, "delivery_token_reserve")
        self.assertEqual(model.model_requests_used, 0)
        self.assertEqual(model.client.completions.calls, 0)
        self.assertIn("reserve=400000", raised.exception.report.summary())
        self.assertLess(raised.exception.report.used, raised.exception.report.limit)

    def test_delivery_reserve_scales_to_budget_and_request_limit(self) -> None:
        model = _make_client(None, None, [])
        model.max_total_tokens = 4_000_000
        model.prompt_tokens_used = 3_800_000
        with self.assertRaises(ModelBudgetExceeded) as raised:
            model._record_model_request()
        self.assertIn("reserve=200000", raised.exception.report.detail)
        model.prompt_tokens_used = 0
        model.model_requests_used = 247
        with self.assertRaises(ModelBudgetExceeded) as raised:
            model._record_model_request()
        self.assertEqual(raised.exception.report.budget, "delivery_request_reserve")
        self.assertEqual(model.model_requests_used, 247)

    def test_near_budget_implementation_returns_to_verification_without_api_call(self) -> None:
        model = _make_client(None, None, [])
        model.prompt_tokens_used = 7_600_000
        self.assertTrue(model.implement(task_type="web", subtree=self.subtree,
            plan="test plan", project_tools=self.tools))
        self.assertEqual(model.client.completions.calls, 0)
        self.assertEqual(model.last_budget_report.budget, "delivery_token_reserve")

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.project_dir = Path(self._tempdir.name)
        self.tools = ProjectTools(self.project_dir)
        self.subtree = {"id": "ROOT", "name": "test task"}

    def _implement(self, model: ModelClient) -> bool:
        return model.implement(
            task_type="web",
            subtree=self.subtree,
            plan="test plan",
            project_tools=self.tools,
        )

    def test_returns_false_when_model_stops_on_its_own(self) -> None:
        scripted = [
            _FakeMessage([_write_call("a.js")]),
            _FakeMessage(None, "implementation complete"),
        ]
        model = _make_client(max_turns=5, max_tool_calls=10, scripted=scripted)

        exhausted = self._implement(model)

        self.assertFalse(exhausted)
        self.assertEqual(model.client.completions.calls, 2)
        self.assertEqual(model.model_requests_used, 2)
        self.assertTrue((self.project_dir / "a.js").is_file())

    def test_truncated_implementation_is_not_treated_as_completion(self) -> None:
        model = _make_client(
            max_turns=2,
            max_tool_calls=2,
            scripted=[_FakeMessage(None, "partial", finish_reason="length")],
        )
        with self.assertRaisesRegex(RuntimeError, "output limit"):
            self._implement(model)
        self.assertEqual(model.client.completions.calls, 1)

    def test_deepseek_truncation_retries_once_without_using_partial_output(self) -> None:
        model = _make_client(
            max_turns=2,
            max_tool_calls=2,
            scripted=[
                _FakeMessage([_write_call("partial.js")], finish_reason="length"),
                _FakeMessage([_write_call("complete.js")]),
                _FakeMessage(None, "done"),
            ],
            env_overrides={"MODEL": "deepseek-flash", "OPENAI_BASE_URL": "https://api.deepseek.com"},
        )

        self.assertFalse(self._implement(model))
        self.assertFalse((self.project_dir / "partial.js").exists())
        self.assertTrue((self.project_dir / "complete.js").exists())
        self.assertEqual(model.model_requests_used, 3)
        self.assertEqual(model.client.completions.requests[1]["max_tokens"], MAX_COMPLETION_TOKENS_PER_REQUEST)
        self.assertIn("previous response exceeded", model.client.completions.requests[1]["messages"][-1]["content"])

    def test_deepseek_second_truncation_stops_without_tool_side_effects(self) -> None:
        model = _make_client(
            max_turns=2,
            max_tool_calls=2,
            scripted=[
                _FakeMessage([_write_call("partial.js")], finish_reason="length"),
                _FakeMessage([_write_call("still-partial.js")], finish_reason="length"),
            ],
            env_overrides={"MODEL": "deepseek-flash", "OPENAI_BASE_URL": "https://api.deepseek.com"},
        )

        with self.assertRaisesRegex(RuntimeError, "output limit"):
            self._implement(model)
        self.assertEqual(model.client.completions.calls, 2)
        self.assertFalse((self.project_dir / "partial.js").exists())
        self.assertFalse((self.project_dir / "still-partial.js").exists())

    def test_turn_budget_falls_through_to_verification(self) -> None:
        scripted = [
            _FakeMessage([_write_call("a.js")]),
            _FakeMessage([_write_call("b.js")]),
            _FakeMessage([_write_call("c.js")]),
        ]
        model = _make_client(max_turns=3, max_tool_calls=10, scripted=scripted)

        exhausted = self._implement(model)

        self.assertTrue(exhausted)
        self.assertEqual(model.client.completions.calls, 3)
        self.assertEqual(model.model_requests_used, 3)
        for name in ("a.js", "b.js", "c.js"):
            self.assertTrue((self.project_dir / name).is_file())

    def test_tool_call_batch_over_budget_is_rejected_before_any_call_runs(self) -> None:
        scripted = [_FakeMessage([_write_call("a.js"), _write_call("b.js"), _write_call("c.js")])]
        model = _make_client(max_turns=5, max_tool_calls=2, scripted=scripted)

        exhausted = self._implement(model)

        self.assertTrue(exhausted)
        self.assertEqual(model.client.completions.calls, 1)
        self.assertEqual(model.model_requests_used, 1)
        self.assertEqual(model.tool_calls_used, 0)
        self.assertFalse((self.project_dir / "a.js").exists())
        self.assertFalse((self.project_dir / "b.js").exists())
        self.assertFalse((self.project_dir / "c.js").exists())
        self.assertEqual(model.last_budget_report.requested, 3)
        self.assertEqual(model.last_budget_report.tool_names, ("write_file", "write_file", "write_file"))

    def test_does_not_wake_model_when_tool_budget_is_already_spent(self) -> None:
        model = _make_client(max_turns=5, max_tool_calls=2, scripted=[])
        model.tool_calls_used = 2

        exhausted = self._implement(model)

        self.assertTrue(exhausted)
        self.assertEqual(model.client.completions.calls, 0)
        self.assertEqual(model.last_budget_report.used, 2)
        self.assertEqual(model.last_budget_report.requested, 0)

    def test_optional_pass_limits_do_not_crash_before_model_request(self) -> None:
        model = _make_client(max_turns=None, max_tool_calls=None, scripted=[_FakeMessage(None, "done")])

        self.assertFalse(self._implement(model))
        self.assertEqual(model.client.completions.calls, 1)

    def test_requirement_review_flags_missing_reload_behavior_and_excludes_credentials(self) -> None:
        self.tools.write_file("frontend/src/App.jsx", "export default function App() { return null; }")
        (self.project_dir / ".env").write_text("secret-value", encoding="utf-8")
        model = _make_client(None, None, [_FakeMessage(None,
            _repair_review("App.jsx cannot restore the confirmed booking after reload"))])
        model.is_deepseek = True
        feedback = model.review_requirements(self.subtree, self.tools)
        self.assertIn("cannot restore", feedback)
        request = json.dumps(model.client.completions.requests[0])
        self.assertIn("frontend/src/App.jsx", request)
        self.assertNotIn("secret-value", request)
        self.assertEqual(model.model_requests_used, 1)
        self.assertEqual(model.client.completions.requests[0]["max_tokens"], 6000)
        self.assertEqual(model.client.completions.requests[0]["extra_body"], {"thinking": {"type": "disabled"}})

    def test_unchanged_production_reuses_failed_audit_until_source_changes(self) -> None:
        self.tools.write_file("frontend/src/App.jsx", "export default function App() { return null; }")
        model = _make_client(None, None, [
            _FakeMessage(None, _repair_review("old account state remains")),
            _FakeMessage(None, '{"verdict":"pass","issues":[]}'),
        ])
        first = model.review_requirements(self.subtree, self.tools)
        self.tools.write_file("frontend/src/__tests__/app.test.jsx", "// extra tests")
        self.assertEqual(model.review_requirements(self.subtree, self.tools), first)
        self.assertEqual(model.model_requests_used, 1)
        self.tools.write_file("frontend/src/App.jsx", "export default function App() { return 'updated'; }")
        self.assertEqual(model.review_requirements(self.subtree, self.tools), "")
        self.assertEqual(model.model_requests_used, 2)

    def test_truncated_source_audit_is_not_reused_for_an_unseen_tail_change(self) -> None:
        source = self.project_dir / "frontend/src/App.jsx"
        source.parent.mkdir(parents=True)
        prefix = "//" + "x" * 31_000
        source.write_text(prefix + "\n// initial tail", encoding="utf-8")
        model = _make_client(None, None, [
            _FakeMessage(None, '{"verdict":"pass","issues":[]}'),
            _FakeMessage(None, _repair_review("inspect unseen source", evidence="//xxx")),
        ])
        self.assertEqual(model.review_requirements(self.subtree, self.tools), "")
        source.write_text(prefix + "\n// changed tail", encoding="utf-8")
        self.assertIn("inspect unseen source", model.review_requirements(self.subtree, self.tools))
        self.assertEqual(model.model_requests_used, 2)

    def test_requirement_change_invalidates_passing_source_audit(self) -> None:
        self.tools.write_file("frontend/src/App.jsx", "export default function App() { return null; }")
        model = _make_client(None, None, [
            _FakeMessage(None, '{"verdict":"pass","issues":[]}'),
            _FakeMessage(None, _repair_review("missing added behavior", quote="Additional required behavior")),
        ])
        self.assertEqual(model.review_requirements(self.subtree, self.tools), "")
        self.assertEqual(model.review_requirements(self.subtree, self.tools), "")
        changed_requirements = {**self.subtree, "description": "Additional required behavior"}
        self.assertIn("missing added behavior", model.review_requirements(changed_requirements, self.tools))
        self.assertEqual(model.model_requests_used, 2)

    def test_requirement_review_rejects_invented_obligation(self) -> None:
        self.tools.write_file("frontend/src/App.jsx", "export default function App() { return null; }")
        model = _make_client(None, None, [_FakeMessage(None,
            _repair_review("unconfirmed draft disappears", quote="Persist unconfirmed drafts after reload"))])
        with self.assertRaisesRegex(RuntimeError, "outside the supplied requirement"):
            model.review_requirements(self.subtree, self.tools)
        self.assertIsNone(model._requirement_review_cache)

    def test_requirement_review_rejects_unknown_requirement_and_source(self) -> None:
        self.tools.write_file("frontend/src/App.jsx", "export default function App() { return null; }")
        for overrides, error in [
            ({"requirement_id": "REQ-NOT-SUPPLIED"}, "outside the supplied requirement"),
            ({"path": "frontend/src/not-read.jsx"}, "outside the supplied production source"),
            ({"evidence": "setBooking(previousUserBooking)"}, "outside the supplied production source"),
        ]:
            with self.subTest(overrides=overrides):
                model = _make_client(None, None, [_FakeMessage(None, _repair_review("gap", **overrides))])
                with self.assertRaisesRegex(RuntimeError, error):
                    model.review_requirements(self.subtree, self.tools)

    def test_requirement_review_rejects_contradictory_and_unevidenced_verdicts(self) -> None:
        issue = json.loads(_repair_review("gap"))["issues"]
        for payload, error in [
            ({"verdict": "pass", "issues": issue}, "contradictory"),
            ({"verdict": "repair", "issues": []}, "contradictory"),
            ({"verdict": "repair", "issues": ["already correct, no defect"]}, "unevidenced"),
        ]:
            with self.subTest(payload=payload):
                model = _make_client(None, None, [_FakeMessage(None, json.dumps(payload))])
                with self.assertRaisesRegex(RuntimeError, error):
                    model.review_requirements(self.subtree, self.tools)

    def test_requirement_review_anchors_issue_to_matching_child_requirement(self) -> None:
        self.tools.write_file("frontend/src/App.jsx", "export default function App() {\n  return null;\n}")
        tree = {"id": "ROOT", "children": [
            {"id": "REQ-3.2", "description": "Confirmed bookings persist after reload."},
            {"id": "REQ-1", "description": "Visitors can sign in."},
        ]}
        model = _make_client(None, None, [_FakeMessage(None, _repair_review(
            "reload renders no confirmation", requirement_id="REQ-3.2",
            quote="Confirmed bookings persist after reload.", evidence="return null;"))])
        self.assertIn("REQ-3.2 frontend/src/App.jsx", model.review_requirements(tree, self.tools))
        model = _make_client(None, None, [_FakeMessage(None, _repair_review(
            "gap", requirement_id="REQ-1", quote="Confirmed bookings persist after reload."))])
        with self.assertRaisesRegex(RuntimeError, "outside the supplied requirement"):
            model.review_requirements(tree, self.tools)

    def test_repeated_inspection_switches_to_edit_tools(self) -> None:
        inspect = _FakeToolCall("call-list", "list_files", json.dumps({"path": "."}))
        write = _FakeToolCall(
            "call-write",
            "write_files",
            json.dumps({"files": [{"path": "tests/behavior.test.js", "content": "test('behavior', () => {});"}]}),
        )

        model = _make_client(
            max_turns=None,
            max_tool_calls=None,
            scripted=[
                *[_FakeMessage([inspect]) for _ in range(4)],
                _FakeMessage([inspect]),
                _FakeMessage([write]),
                _FakeMessage(None, "done"),
            ],
            env_overrides={
                "ARCBENCH_MAX_IDLE_TOOL_TURNS": "8",
                "MODEL": "deepseek-flash",
                "OPENAI_BASE_URL": "https://api.deepseek.com",
            },
        )

        self.assertFalse(self._implement(model))
        rescue_request = model.client.completions.requests[4]
        self.assertTrue({"write_files", "replace_text"}.issubset(
            {tool["function"]["name"] for tool in rescue_request["tools"]}
        ))
        self.assertEqual(rescue_request["tool_choice"], {"type": "function", "function": {"name": "write_files"}})
        self.assertEqual(rescue_request["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertTrue(any(message.get("role") == "assistant" for message in rescue_request["messages"]))
        next_request = model.client.completions.requests[6]
        self.assertEqual(next_request["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertTrue(any(message.get("role") == "assistant" for message in next_request["messages"]))
        self.assertTrue((self.project_dir / "tests/behavior.test.js").is_file())

    def test_failed_project_rescue_preserves_read_context_and_uses_precise_edit(self) -> None:
        (self.project_dir / "backend.js").write_text("function broken() { return false; }", encoding="utf-8")
        replace = _FakeToolCall("call-replace", "replace_text", json.dumps({
            "path": "backend.js", "old_text": "return false;", "new_text": "return true;",
        }))
        model = _make_client(
            max_turns=None,
            max_tool_calls=None,
            scripted=[
                *[_FakeMessage([_read_files_call(["backend.js"])], reasoning_content="analysis") for _ in range(3)],
                _FakeMessage([replace]),
                _FakeMessage(None, "done"),
            ],
            env_overrides={
                "ARCBENCH_MAX_IDLE_TOOL_TURNS": "6",
                "MODEL": "deepseek-flash",
                "OPENAI_BASE_URL": "https://api.deepseek.com",
            },
        )
        model.client.completions.require_reasoning = True

        self.assertFalse(model.implement(
            task_type="web", subtree=self.subtree, plan="fix backend",
            project_tools=self.tools, repair_feedback="backend test failed",
        ))
        rescue_request = model.client.completions.requests[3]
        self.assertEqual(rescue_request["tool_choice"], {
            "type": "function", "function": {"name": "replace_text"},
        })
        self.assertIn("function broken()", json.dumps(rescue_request["messages"]))
        self.assertIn("return true;", (self.project_dir / "backend.js").read_text(encoding="utf-8"))

    def test_deepseek_planning_uses_short_low_effort_request(self) -> None:
        model = _make_client(
            max_turns=None,
            max_tool_calls=None,
            scripted=[_FakeMessage(None, "ordered plan")],
            env_overrides={"MODEL": "deepseek-flash", "OPENAI_BASE_URL": "https://api.deepseek.com"},
        )

        self.assertEqual(model.plan("web", self.subtree), "ordered plan")
        request = model.client.completions.requests[0]
        self.assertEqual(request["reasoning_effort"], "low")
        self.assertIn("below 1,500 words", request["messages"][0]["content"])

    def test_truncated_plan_gets_one_short_retry(self) -> None:
        model = _make_client(
            max_turns=None,
            max_tool_calls=None,
            scripted=[
                _FakeMessage(None, "incomplete", finish_reason="length"),
                _FakeMessage(None, "compact plan"),
            ],
        )

        self.assertEqual(model.plan("web", self.subtree), "compact plan")
        self.assertEqual(model.model_requests_used, 2)
        self.assertIn("below 900 words", model.client.completions.requests[1]["messages"][-1]["content"])

    def test_unchanged_reads_are_summarized_until_a_file_changes(self) -> None:
        inspect = _FakeToolCall("call-list", "list_files", json.dumps({"path": "."}))
        model = _make_client(
            max_turns=6,
            max_tool_calls=6,
            scripted=[
                _FakeMessage([inspect]),
                _FakeMessage([inspect]),
                _FakeMessage([_write_call("a.js")]),
                _FakeMessage([inspect]),
                _FakeMessage(None, "done"),
            ],
        )

        self.assertFalse(self._implement(model))
        repeated = model.client.completions.requests[2]["messages"][-1]["content"]
        self.assertIn("same read already returned", repeated)
        after_change = model.client.completions.requests[4]["messages"][-1]["content"]
        self.assertIn("a.js", after_change)
        self.assertNotIn("same read already returned", after_change)

    def test_overlapping_read_batches_do_not_repeat_unchanged_file_content(self) -> None:
        (self.project_dir / "a.js").write_text("old a", encoding="utf-8")
        (self.project_dir / "b.js").write_text("b content", encoding="utf-8")
        model = _make_client(
            max_turns=6,
            max_tool_calls=6,
            scripted=[
                _FakeMessage([_read_files_call(["a.js", "b.js"])]),
                _FakeMessage([_read_files_call(["a.js"])]),
                _FakeMessage([_FakeToolCall(
                    "call-update", "write_files",
                    json.dumps({"files": [{"path": "a.js", "content": "new a"}]}),
                )]),
                _FakeMessage([_read_files_call(["a.js"])]),
                _FakeMessage(None, "done"),
            ],
        )

        self.assertFalse(self._implement(model))
        repeated = model.client.completions.requests[2]["messages"][-1]["content"]
        self.assertIn("unchanged since earlier read", repeated)
        after_change = model.client.completions.requests[4]["messages"][-1]["content"]
        self.assertIn("new a", after_change)

    def test_tool_call_limit_remains_global_across_implementation_passes(self) -> None:
        model = _make_client(
            max_turns=5,
            max_tool_calls=2,
            scripted=[
                _FakeMessage([_write_call("first.js")]),
                _FakeMessage(None, "first pass done"),
                _FakeMessage([_write_call("second.js"), _write_call("third.js")]),
            ],
        )

        self.assertFalse(self._implement(model))
        self.assertTrue(self._implement(model))
        self.assertEqual(model.tool_calls_used, 1)
        self.assertEqual(model.last_budget_report.budget, "tool_call_budget")
        self.assertTrue((self.project_dir / "first.js").exists())
        self.assertFalse((self.project_dir / "second.js").exists())
        self.assertFalse((self.project_dir / "third.js").exists())

    def test_identical_failed_tool_turns_stop_before_spending_more_requests(self) -> None:
        model = _make_client(
            max_turns=None,
            max_tool_calls=None,
            scripted=[_FakeMessage([_read_call("missing.txt")]) for _ in range(4)],
        )
        self.tools.call = lambda name, args: (_ for _ in ()).throw(FileNotFoundError("missing.txt"))

        self.assertTrue(self._implement(model))
        self.assertEqual(model.client.completions.calls, 3)
        self.assertEqual(model.last_budget_report.budget, "identical_failed_tool_turn_limit")
        self.assertIn("missing.txt", model.last_budget_report.summary())
        self.assertIn("Change the tool arguments", json.dumps(model.client.completions.requests[2]["messages"]))

    def test_changed_tool_call_recovers_after_repeated_failure_warning(self) -> None:
        model = _make_client(
            max_turns=None,
            max_tool_calls=None,
            scripted=[
                _FakeMessage([_read_call("missing.txt")]),
                _FakeMessage([_read_call("missing.txt")]),
                _FakeMessage([_read_call("present.txt")]),
                _FakeMessage(None, "done"),
            ],
        )
        self.tools.call = lambda name, args: (
            "contents" if args["path"] == "present.txt"
            else (_ for _ in ()).throw(FileNotFoundError("missing.txt"))
        )

        self.assertFalse(self._implement(model))
        self.assertEqual(model.client.completions.calls, 4)
        self.assertIsNone(model.last_budget_report)

    def test_idle_tool_turns_handoff_after_writes_and_passing_checkpoint(self) -> None:
        model = _make_client(
            max_turns=None,
            max_tool_calls=None,
            scripted=[_FakeMessage([_write_call("a.js")])]
            + [_FakeMessage([_read_call("a.js")]) for _ in range(12)],
        )
        checkpoint = MagicMock(return_value="Current build and test scripts pass. Finish uncovered requirements.")

        self.assertFalse(model.implement(
            task_type="web", subtree=self.subtree, plan="test plan",
            project_tools=self.tools, checkpoint_feedback=checkpoint,
        ))
        self.assertEqual(model.model_requests_used, 5)
        self.assertTrue(model.last_implementation_handoff)
        self.assertIsNone(model.last_budget_report)
        checkpoint.assert_called_once_with()

    def test_idle_tool_turns_without_project_writes_fail(self) -> None:
        model = _make_client(
            max_turns=None,
            max_tool_calls=None,
            scripted=[_FakeMessage([_read_call("a.js")]) for _ in range(12)],
        )
        self.tools.call = lambda name, args: "ok"

        self.assertTrue(self._implement(model))
        self.assertEqual(model.model_requests_used, 12)
        self.assertEqual(model.last_budget_report.budget, "no_progress_tool_turn_limit")
        self.assertFalse(model.last_implementation_handoff)
        self.assertIn("inspected the project repeatedly without changing a file", json.dumps(model.client.completions.requests[6]["messages"]))

    def test_resumed_passing_project_hands_off_without_new_writes(self) -> None:
        source = self.project_dir / "frontend" / "src" / "App.tsx"
        source.parent.mkdir(parents=True)
        source.write_text("export default function App() { return null; }", encoding="utf-8")
        model = _make_client(
            max_turns=None,
            max_tool_calls=None,
            scripted=[_FakeMessage([_read_call("frontend/src/App.tsx")]) for _ in range(16)],
        )
        checkpoint = MagicMock(return_value="Current build and test scripts pass. Finish uncovered requirements.")

        self.assertFalse(model.implement(
            task_type="web", subtree=self.subtree, plan="test plan",
            project_tools=self.tools, checkpoint_feedback=checkpoint,
        ))
        self.assertTrue(model.last_implementation_handoff)
        self.assertIsNone(model.last_budget_report)
        self.assertEqual(model.client.completions.requests[-1]["tool_choice"]["function"]["name"], "replace_text")

    def test_idle_integration_without_checkpoint_returns_for_verification(self) -> None:
        source = self.project_dir / "frontend" / "src" / "App.tsx"
        source.parent.mkdir(parents=True)
        source.write_text("export default function App() { return null; }", encoding="utf-8")
        model = _make_client(max_turns=None, max_tool_calls=None,
                             scripted=[_FakeMessage([_read_call("frontend/src/App.tsx")]) for _ in range(12)])
        self.assertFalse(self._implement(model))
        self.assertTrue(model.last_implementation_deferred)
        self.assertFalse(model.last_implementation_handoff)
        self.assertIsNone(model.last_budget_report)

    def test_identical_writes_do_not_keep_passing_module_alive(self) -> None:
        model = _make_client(
            max_turns=None,
            max_tool_calls=None,
            scripted=[_FakeMessage([_write_call("a.js")]) for _ in range(8)],
        )
        checkpoint = MagicMock(return_value="Current build and test scripts pass. Finish uncovered requirements.")

        self.assertFalse(model.implement(
            task_type="web", subtree=self.subtree, plan="test plan",
            project_tools=self.tools, checkpoint_feedback=checkpoint,
        ))
        self.assertEqual(model.model_requests_used, 5)
        self.assertEqual(self.tools.changed_paths, ["a.js"])
        self.assertTrue(model.last_implementation_handoff)

    def test_failed_checkpoint_does_not_trigger_early_handoff(self) -> None:
        model = _make_client(
            max_turns=None,
            max_tool_calls=None,
            scripted=[_FakeMessage([_write_call("a.js")])]
            + [_FakeMessage([_read_call("a.js")]) for _ in range(12)],
        )
        checkpoint = MagicMock(return_value="web template structure: FAILED")

        self.assertTrue(model.implement(
            task_type="web", subtree=self.subtree, plan="test plan",
            project_tools=self.tools, checkpoint_feedback=checkpoint,
        ))
        self.assertEqual(model.model_requests_used, 13)
        self.assertEqual(model.last_budget_report.budget, "no_progress_tool_turn_limit")
        self.assertFalse(model.last_implementation_handoff)

    def test_passing_module_hands_off_despite_continuous_optional_writes(self) -> None:
        model = _make_client(max_turns=None, max_tool_calls=None,
            scripted=[_FakeMessage([_write_call(f"file-{i}.js")]) for i in range(20)])
        checkpoint = MagicMock(return_value="Current build and test scripts pass.")
        self.assertFalse(model.implement(task_type="web", subtree=self.subtree,
            plan="test plan", project_tools=self.tools, checkpoint_feedback=checkpoint))
        self.assertEqual(model.model_requests_used, 6)
        self.assertEqual(checkpoint.call_count, 2)
        self.assertTrue(model.last_implementation_handoff)

    def test_module_allocation_defers_work_without_claiming_success_or_global_exhaustion(self) -> None:
        model = _make_client(max_turns=None, max_tool_calls=None, scripted=[])
        self.assertFalse(model.implement(task_type="web", subtree=self.subtree,
            plan="test plan", project_tools=self.tools, token_allowance=0))
        self.assertTrue(model.last_implementation_deferred)
        self.assertFalse(model.last_implementation_handoff)
        self.assertIsNone(model.last_budget_report)
        self.assertEqual(model.model_requests_used, 0)

    def test_request_allocation_defers_before_consuming_global_request_budget(self) -> None:
        model = _make_client(max_turns=None, max_tool_calls=None,
            scripted=[_FakeMessage([_write_call("first.js")])])
        self.assertFalse(model.implement(task_type="web", subtree=self.subtree,
            plan="test plan", project_tools=self.tools, request_allowance=1))
        self.assertTrue(model.last_implementation_deferred)
        self.assertEqual(model.model_requests_used, 1)
        self.assertIsNone(model.last_budget_report)

    def test_terminal_response_records_actual_token_overshoot(self) -> None:
        model = _make_client(max_turns=None, max_tool_calls=None, scripted=[])
        model.max_total_tokens = 100
        response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=90, completion_tokens=20))
        model._record_usage(response)
        self.assertEqual(model.last_budget_report.budget, "total_token_budget")
        self.assertEqual(model.last_budget_report.used, 110)

    def test_green_repair_returns_before_more_model_edits(self) -> None:
        model = _make_client(max_turns=None, max_tool_calls=None,
            scripted=[_FakeMessage([_write_call(f"repair-{i}.js")]) for i in range(20)])
        checkpoint = MagicMock(return_value="Current build and test scripts pass.")
        self.assertFalse(model.implement(task_type="web", subtree=self.subtree,
            plan="repair required behavior", project_tools=self.tools,
            repair_feedback="FAIL selected train summary", checkpoint_feedback=checkpoint))
        self.assertTrue(model.last_implementation_handoff)
        self.assertIsNone(model.last_budget_report)
        self.assertEqual(checkpoint.call_count, 1)
        # The first checkpoint is before turn 4's request; a green repair
        # must not use the two optional final editing turns.
        self.assertEqual(model.model_requests_used, 4)

    def test_final_checkpoint_failure_requires_repair_before_handoff(self) -> None:
        model = _make_client(max_turns=None, max_tool_calls=None,
            scripted=[_FakeMessage([_write_call(f"file-{i}.js")]) for i in range(20)])
        checkpoint = MagicMock(side_effect=["Current build and test scripts pass.",
            "backend tests: FAILED", "Current build and test scripts pass.",
            "Current build and test scripts pass."])
        self.assertFalse(model.implement(task_type="web", subtree=self.subtree,
            plan="test plan", project_tools=self.tools, checkpoint_feedback=checkpoint))
        self.assertEqual(model.model_requests_used, 12)
        self.assertEqual(checkpoint.call_count, 4)
        self.assertTrue(model.last_implementation_handoff)

    def test_idle_tool_turns_do_not_handoff_if_checkpoint_raises(self) -> None:
        model = _make_client(
            max_turns=None,
            max_tool_calls=None,
            scripted=[_FakeMessage([_write_call("a.js")])]
            + [_FakeMessage([_read_call("a.js")]) for _ in range(12)],
        )
        checkpoint = MagicMock(side_effect=RuntimeError("build unavailable"))

        self.assertTrue(model.implement(
            task_type="web", subtree=self.subtree, plan="test plan",
            project_tools=self.tools, checkpoint_feedback=checkpoint,
        ))
        self.assertEqual(model.last_budget_report.budget, "no_progress_tool_turn_limit")
        self.assertIn("build unavailable", model.last_budget_report.summary())

    def test_budget_notice_uses_remaining_global_requests(self) -> None:
        model = _make_client(
            max_turns=None,
            max_tool_calls=None,
            scripted=[
                _FakeMessage([_write_call("first.js")]),
                _FakeMessage([_write_call("second.js")]),
                _FakeMessage(None, "done"),
            ],
        )
        model.max_model_requests = 5

        self.assertFalse(self._implement(model))
        requests = model.client.completions.requests
        self.assertEqual(len(requests), 3)
        self.assertNotIn("Budget notice", json.dumps(requests[1]["messages"]))
        self.assertIn("Budget notice", json.dumps(requests[2]["messages"]))

    def test_batching_related_reads_reduces_model_wakeups(self) -> None:
        paths = ["a.txt", "b.txt", "c.txt"]
        for path in paths:
            (self.project_dir / path).write_text(path, encoding="utf-8")

        sequential = _make_client(
            max_turns=8,
            max_tool_calls=8,
            scripted=[_FakeMessage([_read_call(path)]) for path in paths]
            + [_FakeMessage(None, "inspection complete")],
        )

        batched = _make_client(
            max_turns=8,
            max_tool_calls=8,
            scripted=[_FakeMessage([_read_files_call(paths)]), _FakeMessage(None, "inspection complete")],
        )

        self.assertFalse(self._implement(sequential))
        self.assertFalse(self._implement(batched))

        sequential_wakeups = sequential.model_requests_used
        batched_wakeups = batched.model_requests_used
        self.assertEqual(sequential_wakeups, 4)
        self.assertEqual(batched_wakeups, 2)
        self.assertEqual(batched.tool_calls_used, 1)
        self.assertEqual(sequential_wakeups - batched_wakeups, 2)

    def test_provider_token_usage_is_accumulated(self) -> None:
        model = _make_client(max_turns=2, max_tool_calls=4, scripted=[])

        model._record_usage(SimpleNamespace(usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30)))
        model._record_usage(SimpleNamespace(usage=None))

        self.assertEqual(model.prompt_tokens_used, 120)
        self.assertEqual(model.completion_tokens_used, 30)

    def test_old_tool_payloads_are_compacted_but_task_and_recent_protocol_remain(self) -> None:
        calls = [
            _FakeToolCall(f"call-{index}", "read_files", json.dumps({"paths": [f"f{index}.txt"]}))
            for index in range(6)
        ]
        scripted = [_FakeMessage([call]) for call in calls] + [_FakeMessage(None, "done")]
        model = _make_client(
            max_turns=8, max_tool_calls=8, scripted=scripted,
            env_overrides={"ARCBENCH_MAX_IDLE_TOOL_TURNS": "20"},
        )
        self.subtree = {"id": "REQ-1", "name": "keep this requirement"}
        self.tools.call = lambda name, args: ("x" * 24_000) + f" END-PAYLOAD-{args['paths'][0]}"

        with patch("agent.llm.MAX_CONVERSATION_CHARS", 100_000):
            exhausted = self._implement(model)

        self.assertFalse(exhausted)
        requests = model.client.completions.requests
        self.assertEqual(len(requests), 7)
        later = requests[-1]["messages"]
        serialized_later = json.dumps(later)
        self.assertIn("keep this requirement", serialized_later)
        self.assertIn("test plan", serialized_later)
        self.assertIn("END-PAYLOAD-f5.txt", serialized_later)
        self.assertNotIn("END-PAYLOAD-f0.txt", serialized_later)
        self.assertLess(len(serialized_later), 75_000)

        # Every retained assistant tool call has its matching tool result; compaction
        # must never leave orphan tool messages that break Chat Completions requests.
        for request in requests[1:]:
            messages = request["messages"]
            call_ids = {
                call["id"]
                for message in messages
                if message.get("role") == "assistant"
                for call in message.get("tool_calls", [])
            }
            result_ids = {message["tool_call_id"] for message in messages if message.get("role") == "tool"}
            self.assertEqual(call_ids, result_ids)

    def test_visual_payload_is_sent_once_while_text_task_context_persists(self) -> None:
        model = _make_client(
            max_turns=3,
            max_tool_calls=3,
            scripted=[_FakeMessage([_write_call("a.txt")]), _FakeMessage(None, "done")],
        )
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,REFERENCE"}}
        with patch.object(model, "_visual_inputs", return_value=[{"type": "text", "text": "visual ref"}, image]):
            self.assertFalse(self._implement(model))

        requests = model.client.completions.requests
        self.assertEqual(requests[0]["messages"][2]["content"][1], image)
        self.assertIsInstance(requests[1]["messages"][1]["content"], str)
        self.assertIn("test plan", requests[1]["messages"][1]["content"])
        self.assertEqual(requests[0]["messages"][:2], requests[1]["messages"][:2])
        self.assertNotIn("REFERENCE", json.dumps(requests[1]["messages"]))

    def test_text_implementation_can_use_separate_visual_models(self) -> None:
        model = _make_client(2, 2, [_FakeMessage([_write_call("a.txt")]), _FakeMessage(None, "done")], {
            "MODEL": "glm-4.5-flash", "VISUAL_MODEL": "glm-4.5v",
            "OPENAI_BASE_URL": "https://open.bigmodel.cn/api/paas/v4",
            "ARCBENCH_VISUAL_IMPLEMENTATION": "disabled",
            "ARCBENCH_IMPLEMENTATION_THINKING": "disabled",
        })
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,REFERENCE"}}
        with patch.object(model, "_visual_inputs", return_value=[image]):
            self.assertFalse(self._implement(model))
        for request in model.client.completions.requests:
            self.assertEqual(request["model"], "glm-4.5-flash")
            self.assertNotIn("REFERENCE", json.dumps(request["messages"]))
            self.assertEqual(request["extra_body"], {"thinking": {"type": "disabled"}})

    def test_compaction_preserves_thinking_mode_tool_history(self) -> None:
        calls = [
            _FakeToolCall(f"reasoning-call-{index}", "read_files", json.dumps({"paths": [f"f{index}.txt"]}))
            for index in range(4)
        ]
        scripted = [
            _FakeMessage([call], reasoning_content=f"provider reasoning {index}")
            for index, call in enumerate(calls)
        ] + [_FakeMessage(None, "done", reasoning_content="final reasoning")]
        model = _make_client(max_turns=6, max_tool_calls=6, scripted=scripted)
        model.client.completions.require_reasoning = True
        self.tools.call = lambda name, args: ("x" * 40_000) + f" Read {args['paths'][0]}"

        with patch("agent.llm.MAX_CONVERSATION_CHARS", 100_000):
            self.assertFalse(self._implement(model))

        final_messages = model.client.completions.requests[-1]["messages"]
        self.assertIn("Compact progress ledger", final_messages[2]["content"])
        retained_assistants = [message for message in final_messages if message["role"] == "assistant"]
        self.assertLessEqual(len(retained_assistants), 2)
        self.assertTrue(all(message.get("reasoning_content") for message in retained_assistants))

    def test_deepseek_implementation_keeps_default_thinking(self) -> None:
        model = _make_client(
            max_turns=2,
            max_tool_calls=2,
            scripted=[_FakeMessage([_write_call("a.txt")]), _FakeMessage(None, "done")],
            env_overrides={"MODEL": "deepseek-flash", "OPENAI_BASE_URL": "https://api.deepseek.com"},
        )

        self.assertFalse(self._implement(model))
        self.assertTrue(all(
            request["extra_body"] == {"thinking": {"type": "enabled"}}
            for request in model.client.completions.requests
        ))
        self.assertTrue(all(
            request["reasoning_effort"] == "low"
            and request["max_tokens"] == DEEPSEEK_THINKING_IMPLEMENTATION_MAX_TOKENS
            for request in model.client.completions.requests
        ))

    def test_deepseek_thinking_can_be_disabled_explicitly(self) -> None:
        model = _make_client(
            max_turns=1,
            max_tool_calls=2,
            scripted=[_FakeMessage(None, "done")],
            env_overrides={
                "MODEL": "deepseek-flash",
                "OPENAI_BASE_URL": "https://api.deepseek.com",
                "ARCBENCH_IMPLEMENTATION_THINKING": "disabled",
            },
        )

        self.assertFalse(self._implement(model))
        self.assertEqual(
            model.client.completions.requests[0]["extra_body"],
            {"thinking": {"type": "disabled"}},
        )
        self.assertNotIn("reasoning_effort", model.client.completions.requests[0])
        self.assertEqual(model.client.completions.requests[0]["max_tokens"], MAX_COMPLETION_TOKENS_PER_REQUEST)

    def test_checkpoint_failure_reaches_model_before_turn_budget_is_spent(self) -> None:
        model = _make_client(
            max_turns=5,
            max_tool_calls=6,
            scripted=[_FakeMessage([_write_call("a.txt")]), _FakeMessage(None, "fixed")],
        )
        checkpoint = MagicMock(return_value="frontend: npm run test FAILED: missing tests/*.test.js")

        exhausted = model.implement(
            task_type="web",
            subtree=self.subtree,
            plan="test plan",
            project_tools=self.tools,
            checkpoint_feedback=checkpoint,
        )

        self.assertFalse(exhausted)
        checkpoint.assert_called_once_with()
        second_request = model.client.completions.requests[1]["messages"]
        self.assertTrue(any(
            "missing tests/*.test.js" in message["content"]
            for message in second_request
            if message["role"] == "user" and isinstance(message["content"], str)
        ))

    def test_unlimited_run_continues_past_old_limit_and_replaces_checkpoint_feedback(self) -> None:
        calls = [
            _FakeToolCall(f"call-{index}", "list_files", json.dumps({"path": "."}))
            for index in range(38)
        ]
        model = _make_client(
            max_turns=None,
            max_tool_calls=None,
            scripted=[_FakeMessage([_write_call("a.js")])]
            + [_FakeMessage([call]) for call in calls] + [_FakeMessage(None, "done")],
            env_overrides={"ARCBENCH_MAX_MODEL_REQUESTS": "50", "ARCBENCH_MAX_IDLE_TOOL_TURNS": "50"},
        )
        original_call = self.tools.call
        self.tools.call = lambda name, args: original_call(name, args) if name == "write_file" else "ok"
        checkpoint = MagicMock(side_effect=[f"failure {index}" for index in range(6)])

        self.assertFalse(model.implement(
            task_type="web",
            subtree=self.subtree,
            plan="test plan",
            project_tools=self.tools,
            checkpoint_feedback=checkpoint,
        ))

        self.assertEqual(model.model_requests_used, 40)
        self.assertEqual(model.tool_calls_used, 39)
        self.assertEqual(checkpoint.call_count, 6)
        final_messages = model.client.completions.requests[-1]["messages"]
        self.assertTrue(any(
            message["role"] == "user" and "failure 5" in message["content"]
            for message in final_messages
        ))
        self.assertEqual(final_messages[1]["role"], "user")
        self.assertIn('"id": "ROOT"', final_messages[1]["content"])


if __name__ == "__main__":
    unittest.main()

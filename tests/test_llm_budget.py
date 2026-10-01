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
        if self.require_reasoning and kwargs.get("tools"):
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


class ImplementBudgetTests(unittest.TestCase):
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
        self.assertIn("No project file has changed", json.dumps(model.client.completions.requests[6]["messages"]))

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
        model = _make_client(max_turns=8, max_tool_calls=8, scripted=scripted)
        self.subtree = {"id": "REQ-1", "name": "keep this requirement"}
        self.tools.call = lambda name, args: ("x" * 24_000) + f" END-PAYLOAD-{args['paths'][0]}"

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
        final_user_message = model.client.completions.requests[-1]["messages"][2]["content"]
        self.assertIn("failure 5", final_user_message)
        self.assertNotIn("failure 0", final_user_message)


if __name__ == "__main__":
    unittest.main()

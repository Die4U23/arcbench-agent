from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.llm import ModelClient
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
    def __init__(self, tool_calls: list[_FakeToolCall] | None = None, content: str = "done") -> None:
        self.tool_calls = tool_calls
        self.content = content

    def model_dump(self, exclude_none: bool = True) -> dict:
        if self.tool_calls is None:
            return {"role": "assistant", "content": self.content}
        return {
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


class _FakeCompletions:
    def __init__(self, scripted_messages: list[_FakeMessage]) -> None:
        self._messages = list(scripted_messages)
        self.calls = 0
        self.seen: list[list[dict]] = []

    def create(self, **kwargs):
        self.calls += 1
        self.seen.append(list(kwargs.get("messages", [])))
        message = self._messages.pop(0)
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


class _FakeClient:
    def __init__(self, scripted_messages: list[_FakeMessage]) -> None:
        self.completions = _FakeCompletions(scripted_messages)
        self.chat = type("Chat", (), {"completions": self.completions})()


def _write_call(path: str) -> _FakeToolCall:
    arguments = json.dumps({"path": path, "content": "// generated"})
    return _FakeToolCall(f"call-{path}", "write_file", arguments)


def _make_client(max_turns: int, max_tool_calls: int, scripted: list[_FakeMessage]) -> ModelClient:
    environment = {"OPENAI_API_KEY": "test-key", "MODEL": "test-model"}
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
        self.assertTrue((self.project_dir / "a.js").is_file())

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
        for name in ("a.js", "b.js", "c.js"):
            self.assertTrue((self.project_dir / name).is_file())

    def test_tool_call_budget_stops_mid_message_without_extra_api_call(self) -> None:
        scripted = [_FakeMessage([_write_call("a.js"), _write_call("b.js"), _write_call("c.js")])]
        model = _make_client(max_turns=5, max_tool_calls=2, scripted=scripted)

        exhausted = self._implement(model)

        self.assertTrue(exhausted)
        self.assertEqual(model.client.completions.calls, 1)
        self.assertEqual(model.tool_calls_used, 2)
        self.assertTrue((self.project_dir / "a.js").is_file())
        self.assertTrue((self.project_dir / "b.js").is_file())
        self.assertFalse((self.project_dir / "c.js").exists())

    def test_budget_is_reset_per_implementation_pass(self) -> None:
        """Each implement() call must reset its tool-call budget so repair is not
        starved by calls spent in an earlier pass."""
        scripted = [
            _FakeMessage([_write_call("a.js")]),
            _FakeMessage(None, "done"),
            _FakeMessage([_write_call("b.js")]),
            _FakeMessage(None, "done"),
        ]
        model = _make_client(max_turns=3, max_tool_calls=2, scripted=scripted)
        first = model.implement(
            task_type="web",
            subtree=self.subtree,
            plan="pass 1",
            project_tools=self.tools,
        )
        self.assertFalse(first)
        self.assertEqual(model.tool_calls_used, 1)  # used 1 of 2
        # Second pass must start with a fresh budget, or this would fail.
        second = model.implement(
            task_type="web",
            subtree=self.subtree,
            plan="pass 2",
            project_tools=self.tools,
        )
        self.assertFalse(second)
        self.assertEqual(model.tool_calls_used, 1)

    def test_budget_warning_is_injected(self) -> None:
        """When remaining calls or turns hit the threshold, a user message
        reminding the model to wrap up is appended before the next API call."""
        scripted = [
            _FakeMessage([_write_call("a.js")]),
            _FakeMessage([_write_call("b.js")]),
            _FakeMessage(None, "done"),
        ]
        # 5 turns, threshold = max(3, 1) = 3 → warning at turn 2 (remaining=3)
        # 10 calls, threshold = max(5, 2) = 5 → warning when remaining <= 5
        model = _make_client(max_turns=5, max_tool_calls=10, scripted=scripted)
        self._implement(model)
        # The second API call is turn_index=1, remaining_turns=4 (>3)
        # but first call used 1 tool (remaining=9 > 5), so no warning yet.
        # Third API call is turn_index=2, remaining_turns=3 (<=3), warning fires.
        self.assertEqual(model.client.completions.calls, 3)
        # collect messages sent at each call
        message_history = model.client.completions.seen
        # call 1 (turn 0): system + user, no warning
        self.assertNotIn("Budget notice", _serialize_messages(message_history[0]))
        # call 2 (turn 1): system + user + assistant + tool, no warning
        self.assertNotIn("Budget notice", _serialize_messages(message_history[1]))
        # call 3 (turn 2): must contain budget notice
        self.assertIn("Budget notice", _serialize_messages(message_history[2]))


def _serialize_messages(msgs: list[dict]) -> str:
    return "\n".join(str(m.get("content", "")) for m in msgs)


if __name__ == "__main__":
    unittest.main()

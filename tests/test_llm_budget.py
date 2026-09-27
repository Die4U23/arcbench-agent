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

    def create(self, **kwargs):
        self.calls += 1
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


if __name__ == "__main__":
    unittest.main()

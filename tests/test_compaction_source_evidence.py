import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.llm import ModelClient
from agent.tools import ProjectTools
from tests.test_llm_budget import _FakeMessage, _FakeToolCall, _make_client


class CompactionSourceEvidenceTests(unittest.TestCase):
    def test_latest_complete_source_and_reasoning_survive_a_compaction_boundary(self):
        calls = [_FakeToolCall(f"read-{i}", "read_files", json.dumps({"paths": [f"f{i}.js"]}))
                 for i in range(9)]
        messages = [_FakeMessage([call], reasoning_content=f"reasoning-{i}")
                    for i, call in enumerate(calls)] + [_FakeMessage(None, "done")]
        model = _make_client(None, None, messages, {"ARCBENCH_MAX_IDLE_TOOL_TURNS": "30"})
        with tempfile.TemporaryDirectory() as directory:
            tools = ProjectTools(Path(directory))
            tools.call = lambda name, args: "x" * 12_000 + f" EXACT-ANCHOR-{args['paths'][0]}"
            with patch("agent.llm.MAX_CONVERSATION_CHARS", 90_000):
                model.implement(task_type="web", subtree={"id": "REQ-1"}, plan="repair", project_tools=tools)
        compacted = 0
        for i, request in enumerate(model.client.completions.requests[1:]):
            serialized = json.dumps(request["messages"])
            self.assertIn(f"EXACT-ANCHOR-f{i}.js", serialized)
            self.assertIn(f"reasoning-{i}", serialized)
            if "Compact progress ledger" in serialized:
                compacted += 1
                self.assertNotIn("EXACT-ANCHOR-f0.js", serialized)
            call_ids = [call["id"] for message in request["messages"]
                        for call in message.get("tool_calls", [])]
            result_ids = [message["tool_call_id"] for message in request["messages"]
                          if message["role"] == "tool"]
            self.assertEqual(call_ids, result_ids)
            self.assertEqual(len(call_ids), len(set(call_ids)))
        self.assertGreater(compacted, 0)

    def test_oversized_exchange_is_summarized_without_orphan_protocol(self):
        call = _FakeToolCall("huge-read", "read_files", json.dumps({"paths": ["large.js"]}))
        model = _make_client(None, None, [_FakeMessage([call], reasoning_content="r" * 70_000),
                                        _FakeMessage(None, "done")])
        with tempfile.TemporaryDirectory() as directory:
            tools = ProjectTools(Path(directory))
            tools.call = lambda name, args: "x" * 35_000 + " OMITTED-END"
            with patch("agent.llm.MAX_CONVERSATION_CHARS", 90_000):
                model.implement(task_type="web", subtree={"id": "REQ-1"}, plan="repair", project_tools=tools)
        final = model.client.completions.requests[-1]["messages"]
        self.assertNotIn("OMITTED-END", json.dumps(final))
        self.assertFalse(any(message["role"] in {"assistant", "tool"} for message in final))
        self.assertLess(ModelClient._message_context_chars(final), 90_000)


if __name__ == "__main__":
    unittest.main()

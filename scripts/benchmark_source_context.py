"""Fixed offline source-read replay; characters are not provider tokens or cost."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    from agent.llm import ModelClient
    from agent.prompt_context import requirement_context
    from agent.requirements import load_requirement_tree
    from agent.tools import ProjectTools

    tree = load_requirement_tree(args.requirements)
    scopes = [tree, *tree.get("children", [])]
    contexts = [requirement_context("web", dict(scope, project_requirements=tree)) for scope in scopes]
    results = {}
    for case in ("mixed_repeated_reads", "unique_reads_control"):
        # Equal operations and final files on both revisions. Only the model
        # responses are scripted; source reading and editing use real tools.
        operations = [
            ("read_files", {"paths": ["a.js", "b.js"]}),
            ("read_file", {"path": "a.js" if case == "mixed_repeated_reads" else "c.js"}),
            ("read_file", {"path": "b.js" if case == "mixed_repeated_reads" else "d.js",
                           "start_line": 1, "end_line": 40}),
            ("read_file", {"path": "b.js" if case == "mixed_repeated_reads" else "e.js",
                           "start_line": 1, "end_line": 40}),
            ("replace_text", {"path": "a.js", "old_text": "value0 = 0", "new_text": "value0 = 1"}),
            ("read_file", {"path": "a.js"}),
            ("read_file", {"path": "b.js" if case == "mixed_repeated_reads" else "f.js"}),
            ("read_files", {"paths": ["b.js", "g.js"]}),
        ]
        requests, trace = [], []

        class Message:
            def __init__(self, index):
                self.content, self.reasoning_content = "", "Use the current source evidence."
                self.tool_calls = None
                if index < len(operations):
                    name, arguments = operations[index]
                    self.tool_calls = [SimpleNamespace(id=f"call-{index}", type="function",
                        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))]

            def model_dump(self, exclude_none=True):
                payload = {"role": "assistant", "content": self.content,
                           "reasoning_content": self.reasoning_content}
                if self.tool_calls:
                    payload["tool_calls"] = [{"id": call.id, "type": call.type,
                        "function": vars(call.function)} for call in self.tool_calls]
                return payload

        def completion(**kwargs):
            message = Message(len(requests))
            requests.append(json.loads(json.dumps(kwargs["messages"])))
            return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])

        with TemporaryDirectory() as directory, patch.dict(os.environ, {
                "OPENAI_API_KEY": "offline-fixture-only", "MODEL": "deepseek-flash",
                "OPENAI_BASE_URL": "https://api.deepseek.com",
                "ARCBENCH_VISUAL_IMPLEMENTATION": "disabled"}, clear=True):
            root = Path(directory)
            for name in "abcdefg":
                (root / f"{name}.js").write_text("".join(
                    f"export const value{i} = {i}; // {name} source line with a stable unique anchor\n"
                    for i in range(160)), encoding="utf-8")
            model = ModelClient(max_turns=None, max_tool_calls=None)
            model.client = SimpleNamespace(chat=SimpleNamespace(
                completions=SimpleNamespace(create=completion)))
            tools = ProjectTools(root)
            original_call = tools.call
            def tool_call(name, arguments):
                if name not in {"read_file", "read_files", "replace_text"}:
                    raise AssertionError("Unplanned tool in offline replay")
                trace.append((name, arguments))
                return original_call(name, arguments)
            tools.call = tool_call
            model.implement(task_type="library", subtree={"id": "BENCH"}, plan="Inspect and edit", project_tools=tools)
            if trace != operations or len(requests) != len(operations) + 1:
                raise AssertionError("Replay changed its tool workload or request count")
            final_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in sorted(root.glob("*.js"))}
        encoded = [json.dumps(request, ensure_ascii=False) for request in requests]
        # Prefix changes are a diagnostic proxy only, never a cache-hit claim.
        fresh_chars, previous = 0, ""
        for request in encoded:
            shared = 0
            for before, after in zip(previous, request):
                if before != after:
                    break
                shared += 1
            fresh_chars += len(request) - shared
            previous = request
        results[case] = {"requests": len(requests), "input_chars": sum(map(len, encoded)),
                        "fresh_prefix_chars": fresh_chars, "final_hashes": final_hashes,
                        "tool_trace": trace}
    report = {"method": "Offline fixed tool replay; no model, provider tokens, cache hits or billed cost",
              "source": str(args.source.resolve()), "cases": results,
              "requirement_context_chars": {scope["id"]: len(context) for scope, context in zip(scopes, contexts)}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report, "cases": {name: {key: value for key, value in result.items()
          if key not in {"tool_trace", "final_hashes"}} for name, result in results.items()}},
          ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

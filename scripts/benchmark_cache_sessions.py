"""Offline A/B of unchanged mock work across module and repair sessions.

Character-prefix overlap is only a cache opportunity proxy. It is not the
provider's token count, cache residency, billing, or a prediction of 99% hits.
No model request is sent and no credentials are loaded.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def legacy_requirement_context(task_type, subtree):
    return (f"Task type: {task_type}\nRequirement subtree:\n"
            + json.dumps(subtree, ensure_ascii=False, indent=2))


def common_prefix(left, right):
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return index
    return min(len(left), len(right))


def summarize(requests):
    previous = ""
    seen = []
    total = adjacent = best = conservative = 0
    groups = set()
    schemas = set()
    samples = []
    for request in requests:
        encoded = json.dumps(request["messages"], ensure_ascii=False, default=str)
        # Provider-side tokenization of these options is not published. Keep a
        # separate conservative estimate that assumes any mode change separates
        # cache pools, instead of silently assuming tool_choice is irrelevant.
        group = json.dumps({key: request.get(key) for key in
            ("model", "tools", "tool_choice", "extra_body", "reasoning_effort")},
            sort_keys=True, ensure_ascii=False, default=str)
        near = common_prefix(previous, encoded)
        longest = max((common_prefix(old, encoded) for _, old in seen), default=0)
        compatible = max((common_prefix(old, encoded) for old_group, old in seen
                          if old_group == group), default=0)
        samples.append({"input_chars": len(encoded), "previous_prefix_chars": near,
                        "best_prior_prefix_chars": longest,
                        "same_mode_best_prior_prefix_chars": compatible})
        total += len(encoded)
        adjacent += near
        best += longest
        conservative += compatible
        seen.append((group, encoded))
        groups.add(group)
        schemas.add(json.dumps(request.get("tools"), sort_keys=True, ensure_ascii=False))
        previous = encoded
    return {
        "requests": len(requests), "input_chars": total, "request_mode_groups": len(groups),
        "tool_schema_variants": len(schemas),
        "previous_shared_prefix_chars": adjacent, "previous_fresh_chars": total - adjacent,
        "best_prior_shared_prefix_chars": best, "best_prior_fresh_chars": total - best,
        "same_mode_shared_prefix_chars": conservative, "same_mode_fresh_chars": total - conservative,
        "previous_prefix_ratio": round(adjacent / total, 6),
        "best_prior_prefix_ratio": round(best / total, 6),
        "same_mode_prefix_ratio": round(conservative / total, 6),
        "samples": samples,
    }


def capture(tree, *, baseline=False):
    from agent.llm import ModelClient
    from agent.repair_policy import focused_subtree

    scopes = [str(node["id"]) for node in tree.get("children", []) if isinstance(node, dict)]
    if not scopes:
        raise ValueError("The workload requires a project with child requirement modules")
    leaves = []
    pending = [tree]
    while pending:
        node = pending.pop(0)
        children = [child for child in node.get("children", []) if isinstance(child, dict)]
        if children:
            pending.extend(children)
        else:
            leaves.append(str(node["id"]))
    # Three module sessions, then four focused repairs, including repeated scope
    # with changed feedback/plan. No identical previous tool outputs are replayed.
    sessions = [(scope, None) for scope in scopes[:3]]
    sessions += [(leaves[index], f"Observed assertion {repair_index}: complete the required state transition")
                 for repair_index, index in enumerate((0, len(leaves) - 1, 0, len(leaves) - 1))]
    requests = []
    tool_trace = []

    class Message:
        def __init__(self, call=None):
            self.content = "" if call else "finished mock slice"
            self.reasoning_content = "Inspect the specified source, then make the bounded edit."
            self.tool_calls = [call] if call else None

        def model_dump(self, exclude_none=True):
            result = {"role": "assistant", "content": self.content,
                      "reasoning_content": self.reasoning_content}
            if self.tool_calls:
                result["tool_calls"] = [{"id": call.id, "type": "function",
                    "function": vars(call.function)} for call in self.tool_calls]
            return result

    environment = {"OPENAI_API_KEY": "offline-test-only", "OPENAI_BASE_URL": "https://api.deepseek.com",
        "MODEL": "deepseek-flash", "ARCBENCH_MAX_MODEL_REQUESTS": "250",
        "ARCBENCH_MAX_TOTAL_TOKENS": "10000000", "ARCBENCH_VISUAL_IMPLEMENTATION": "disabled"}
    context_patch = patch("agent.llm.requirement_context", legacy_requirement_context) if baseline else nullcontext()
    with patch.dict(os.environ, environment, clear=True), context_patch:
        model = ModelClient(max_turns=None, max_tool_calls=None)
        model._visual_inputs = lambda *args: []
        for session, (scope, feedback) in enumerate(sessions):
            index = 0
            changed_paths = []

            def completion(stage, **kwargs):
                nonlocal index
                requests.append(json.loads(json.dumps(kwargs, ensure_ascii=False)))
                if index == 8:
                    message = Message()
                else:
                    choice = kwargs.get("tool_choice")
                    name = choice["function"]["name"] if isinstance(choice, dict) else "read_files"
                    if name == "read_files":
                        arguments = {"paths": [f"fixture/source-{index}.js"]}
                    elif name == "write_files":
                        arguments = {"files": [{"path": "fixture/change.js", "content": "export const changed = true;"}]}
                    elif name == "replace_text":
                        arguments = {"path": "fixture/change.js", "old_text": "false", "new_text": "true"}
                    else:
                        raise AssertionError(f"Unexpected forced tool {name}")
                    call = SimpleNamespace(id=f"session-{session}-call-{index}",
                        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))
                    message = Message(call)
                index += 1
                return SimpleNamespace(choices=[SimpleNamespace(message=message,
                    finish_reason="tool_calls" if message.tool_calls else "stop")], usage=None)

            def tool_call(name, arguments):
                tool_trace.append((session, name, arguments))
                if name == "read_files":
                    path = arguments["paths"][0]
                    content = (f"// Offline fixture {path}\n"
                        "export function normalize(value) { return String(value).trim(); }\n"
                        "export function validate(value) { return normalize(value).length > 0; }\n")
                    return json.dumps({"files": [{"path": path, "content": content, "truncated": False}]})
                changed_paths.append("fixture/change.js")
                return json.dumps({"changed": ["fixture/change.js"]})

            model._create_completion = completion
            tools = SimpleNamespace(written_paths=changed_paths, changed_paths=changed_paths, call=tool_call)
            subtree = focused_subtree(tree, scope)
            model.implement(task_type="web", subtree=subtree,
                plan=f"Complete scope {scope}; inspect current source and implement phase {session}.",
                project_tools=tools, repair_feedback=feedback)
            if index != 9:
                raise AssertionError(f"Mock session {session} stopped after {index} requests")
    return requests, tool_trace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", required=True, type=Path,
                        help="Existing read-only requirement directory; never loads runtime config or keys")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from agent.requirements import load_requirement_tree
    tree = load_requirement_tree(args.requirements)
    baseline, before_trace = capture(tree, baseline=True)
    candidate, after_trace = capture(tree)
    if before_trace != after_trace:
        raise AssertionError("A/B tool workloads differ")
    result = {
        "method": "Serialized message character LCP; offline potential only, not provider cache tokens or cost",
        "cache_limits": "No residency/eviction/tokenization/prefix-unit assumptions; no padding or paid calls",
        "workload": "3 initial module sessions plus 4 focused repair sessions; 9 requests per session",
        "tool_trace_identical": True, "model": "deepseek-flash",
        "baseline": summarize(baseline), "candidate": summarize(candidate),
    }
    for metric in ("input_chars", "previous_fresh_chars", "best_prior_fresh_chars", "same_mode_fresh_chars"):
        result[metric + "_reduction_ratio"] = round(1 - result["candidate"][metric] / result["baseline"][metric], 6)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: ({k: v for k, v in value.items() if k != "samples"} if key in {"baseline", "candidate"} else value)
                      for key, value in result.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

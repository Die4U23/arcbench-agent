"""Replay one deterministic failing-tool loop without contacting a model provider."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-zip", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source = args.source_zip.resolve() if args.source_zip else Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(source))
    from agent.llm import ModelClient

    os.environ.update({
        "OPENAI_API_KEY": "offline-probe",
        "MODEL": "offline-model",
        "ARCBENCH_MAX_MODEL_REQUESTS": "24",
        "ARCBENCH_MAX_TOTAL_TOKENS": "300000",
    })

    class FakeMessage:
        def __init__(self, index: int) -> None:
            self.content = ""
            self.tool_calls = [SimpleNamespace(
                id=f"call-{index}",
                function=SimpleNamespace(name="read_file", arguments='{"path":"missing.txt"}'),
            )]

        def model_dump(self, exclude_none: bool = True) -> dict:
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": call.id, "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            } for call in self.tool_calls]}

    class FakeCompletions:
        def __init__(self) -> None:
            self.requests: list[int] = []

        def create(self, **kwargs: object) -> SimpleNamespace:
            self.requests.append(len(json.dumps(kwargs["messages"], ensure_ascii=False)))
            return SimpleNamespace(
                choices=[SimpleNamespace(message=FakeMessage(len(self.requests)), finish_reason="tool_calls")],
                usage=None,
            )

    def failing_tool(name: str, arguments: dict) -> str:
        raise FileNotFoundError("missing.txt")

    completions = FakeCompletions()
    model = ModelClient(max_turns=None, max_tool_calls=None)
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    stopped = model.implement(
        task_type="web",
        subtree={"id": "REQ-STALL", "name": "offline failure loop"},
        plan="Read the missing file and continue.",
        project_tools=SimpleNamespace(call=failing_tool, written_paths=[]),
    )
    if not stopped:
        raise RuntimeError("The failing-tool replay unexpectedly completed")
    cumulative = 0
    samples = []
    for index, prompt_chars in enumerate(completions.requests, 1):
        cumulative += prompt_chars
        samples.append({"request": index, "prompt_chars": prompt_chars, "cumulative_prompt_chars": cumulative})
    result = {
        "source": source.name,
        "method": "offline identical read_file failure replay; no provider request",
        "requests": len(samples),
        "cumulative_prompt_chars": cumulative,
        "stop_reason": getattr(model.last_budget_report, "budget", "unknown"),
        "samples": samples,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("source", "requests", "cumulative_prompt_chars", "stop_reason")}))


if __name__ == "__main__":
    main()

"""Offline prompt-prefix probe for a checkout or an uploaded Agent ZIP.

No provider request is sent. This measures serialized prompt characters and
the exact prefix shared with the immediately preceding synthetic request.
"""

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
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--tool-result-chars", type=int, default=8_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.rounds < 1 or args.tool_result_chars < 1:
        parser.error("rounds and tool-result-chars must be positive")

    source = args.source_zip.resolve() if args.source_zip else Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(source))
    from agent.llm import ModelClient

    os.environ["OPENAI_API_KEY"] = "offline-probe"
    os.environ["OPENAI_BASE_URL"] = "https://api.deepseek.com"
    os.environ["MODEL"] = "deepseek-flash"
    os.environ["ARCBENCH_MAX_MODEL_REQUESTS"] = str(args.rounds + 5)
    os.environ["ARCBENCH_MAX_TOTAL_TOKENS"] = "1000000000"

    class FakeMessage:
        def __init__(self, index: int) -> None:
            self.content = "done" if index == args.rounds else ""
            self.reasoning_content = "reasoning " + ("r" * 500)
            self.tool_calls = None if index == args.rounds else [SimpleNamespace(
                id=f"call-{index}",
                function=SimpleNamespace(name="read_files", arguments=json.dumps({"paths": [f"file-{index}.js"]})),
            )]

        def model_dump(self, exclude_none: bool = True) -> dict:
            result = {"role": "assistant", "content": self.content, "reasoning_content": self.reasoning_content}
            if self.tool_calls:
                result["tool_calls"] = [{
                    "id": call.id, "type": "function",
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                } for call in self.tool_calls]
            return result

    class FakeCompletions:
        def __init__(self) -> None:
            self.requests: list[str] = []

        def create(self, **kwargs: object) -> SimpleNamespace:
            self.requests.append(json.dumps(kwargs["messages"], ensure_ascii=False, default=str))
            message = FakeMessage(len(self.requests) - 1)
            finish_reason = "stop" if message.tool_calls is None else "tool_calls"
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
                usage=None,
            )

    completions = FakeCompletions()
    model = ModelClient(max_turns=args.rounds + 2, max_tool_calls=args.rounds + 2)
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    tools = SimpleNamespace(
        written_paths=[],
        call=lambda name, arguments: "x" * args.tool_result_chars + arguments["paths"][0],
    )
    exhausted = model.implement(
        task_type="web", subtree={"id": "REQ-PROBE", "name": "offline prefix probe"},
        plan="Read the next relevant file, then finish.", project_tools=tools,
    )
    if exhausted or len(completions.requests) != args.rounds + 1:
        raise RuntimeError("The probe stopped before all scripted requests completed")

    samples = []
    previous = ""
    cumulative_prompt = 0
    cumulative_fresh = 0
    for index, request in enumerate(completions.requests, 1):
        prefix = 0
        for left, right in zip(previous, request):
            if left != right:
                break
            prefix += 1
        fresh = len(request) - prefix
        cumulative_prompt += len(request)
        cumulative_fresh += fresh
        samples.append({
            "request": index, "prompt_chars": len(request), "shared_prefix_chars": prefix,
            "fresh_chars": fresh, "cumulative_prompt_chars": cumulative_prompt,
            "cumulative_fresh_chars": cumulative_fresh,
        })
        previous = request
    result = {
        "source": str(source), "rounds": args.rounds,
        "tool_result_chars": args.tool_result_chars,
        "method": "exact serialized-message prefix versus previous request; offline proxy, not provider cache usage",
        "samples": samples,
    }
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(json.dumps({
        "source": source.name, "requests": len(samples),
        "prompt_chars": cumulative_prompt, "fresh_chars": cumulative_fresh,
        "fresh_ratio": round(cumulative_fresh / cumulative_prompt, 4),
    }))


if __name__ == "__main__":
    main()

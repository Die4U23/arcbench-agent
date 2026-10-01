"""Replay one successful write followed by repeated reads, without a model API."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.llm import ModelClient
from agent.tools import ProjectTools


class FakeMessage:
    def __init__(self, index: int) -> None:
        name = "write_file" if index == 0 else "read_file"
        arguments = {"path": "app.js", "content": "// generated"} if index == 0 else {"path": "app.js"}
        self.content = ""
        self.tool_calls = [SimpleNamespace(
            id=f"call-{index}",
            function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
        )]

    def model_dump(self, exclude_none: bool = True) -> dict:
        return {
            "role": "assistant", "content": "", "tool_calls": [{
                "id": call.id, "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            } for call in self.tool_calls],
        }


class FakeCompletions:
    def __init__(self) -> None:
        self.prompt_chars: list[int] = []

    def create(self, **kwargs: object) -> SimpleNamespace:
        index = len(self.prompt_chars)
        self.prompt_chars.append(len(json.dumps(kwargs["messages"], ensure_ascii=False)))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=FakeMessage(index), finish_reason="tool_calls")],
            usage=None,
        )


def replay(*, old_policy: bool) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "offline-probe",
            "MODEL": "offline-model",
            "ARCBENCH_MAX_MODEL_REQUESTS": "24",
            "ARCBENCH_MAX_IDLE_TOOL_TURNS": "12",
        }):
            model = ModelClient(max_turns=None, max_tool_calls=None)
        completions = FakeCompletions()
        model.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        tools = ProjectTools(Path(directory))
        policy = (
            patch.multiple("agent.llm", EARLY_CHECKPOINT_TURN=16,
                           CHECKPOINT_INTERVAL_TURNS=12, PASSING_CHECKPOINT_IDLE_TURNS=12)
            if old_policy else nullcontext()
        )
        with policy:
            exhausted = model.implement(
                task_type="web",
                subtree={"id": "REQ-1", "name": "offline convergence probe"},
                plan="Create app.js, then inspect it.",
                project_tools=tools,
                checkpoint_feedback=lambda: "Current build and test scripts pass. Finish uncovered requirements.",
            )
        if exhausted or not model.last_implementation_handoff:
            raise RuntimeError("Expected a successful module handoff")

        cumulative = 0
        samples = []
        for index, chars in enumerate(completions.prompt_chars, 1):
            cumulative += chars
            samples.append({"request": index, "cumulative_prompt_chars": cumulative})
        return {"requests": len(samples), "cumulative_prompt_chars": cumulative, "samples": samples}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plot", type=Path)
    args = parser.parse_args()
    result = {
        "method": "offline fixed sequence: one file write, then repeated reads; checkpoint always passes",
        "unit": "serialized prompt characters, not provider tokens or cost",
        "scenarios": {"before": replay(old_policy=True), "after": replay(old_policy=False)},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        figure, axis = plt.subplots(figsize=(8, 4.3), constrained_layout=True)
        for label, data in result["scenarios"].items():
            axis.plot(
                [item["request"] for item in data["samples"]],
                [item["cumulative_prompt_chars"] / 1000 for item in data["samples"]],
                marker="o", linewidth=2, label=f"{label} ({data['requests']} requests)",
            )
        axis.set_title("Verified idle module handoff (offline replay)")
        axis.set_xlabel("Synthetic model request")
        axis.set_ylabel("Thousand serialized prompt characters")
        axis.grid(alpha=0.25)
        axis.legend()
        args.plot.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(args.plot, dpi=170)
    print(json.dumps({key: {"requests": value["requests"], "cumulative_prompt_chars": value["cumulative_prompt_chars"]}
                      for key, value in result["scenarios"].items()}))


if __name__ == "__main__":
    main()

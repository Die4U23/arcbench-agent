"""Compare idle tool-call stopping with the run-wide cap using fake responses."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace


def replay(idle_limit: int) -> dict:
    from agent.llm import ModelClient

    os.environ.update({
        "OPENAI_API_KEY": "offline-probe",
        "MODEL": "offline-model",
        "ARCBENCH_MAX_MODEL_REQUESTS": "24",
        "ARCBENCH_MAX_TOTAL_TOKENS": "300000",
        "ARCBENCH_MAX_IDLE_TOOL_TURNS": str(idle_limit),
    })

    class FakeMessage:
        def __init__(self, index: int) -> None:
            self.content = ""
            self.tool_calls = [SimpleNamespace(
                id=f"call-{index}",
                function=SimpleNamespace(name="list_files", arguments='{"path":"."}'),
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

    completions = FakeCompletions()
    model = ModelClient(max_turns=None, max_tool_calls=None)
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    stopped = model.implement(
        task_type="web",
        subtree={"id": "REQ-IDLE", "name": "offline idle loop"},
        plan="Inspect the project, then implement the requirement.",
        project_tools=SimpleNamespace(call=lambda name, arguments: "files unchanged", written_paths=[]),
    )
    if not stopped:
        raise RuntimeError("Idle replay unexpectedly completed")
    cumulative = 0
    samples = []
    for index, prompt_chars in enumerate(completions.requests, 1):
        cumulative += prompt_chars
        samples.append({"request": index, "prompt_chars": prompt_chars, "cumulative_prompt_chars": cumulative})
    return {
        "idle_limit": idle_limit,
        "requests": len(samples),
        "cumulative_prompt_chars": cumulative,
        "stop_reason": getattr(model.last_budget_report, "budget", "unknown"),
        "samples": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plot", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    result = {
        "method": "offline repeated successful list_files calls without writes; no provider request",
        "scenarios": {"run_cap_only": replay(50), "idle_guard": replay(12)},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.plot is not None:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        figure, axis = plt.subplots(figsize=(8, 4.3), constrained_layout=True)
        for label, data in result["scenarios"].items():
            samples = data["samples"]
            axis.plot(
                [item["request"] for item in samples],
                [item["cumulative_prompt_chars"] / 1000 for item in samples],
                marker="o", markersize=3, linewidth=2,
                label=f"{label} ({data['requests']} requests)",
            )
        axis.set_title("Repeated successful reads without file changes (offline)")
        axis.set_xlabel("Synthetic model request")
        axis.set_ylabel("Thousand serialized prompt characters")
        axis.grid(alpha=0.25)
        axis.legend()
        args.plot.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(args.plot)
        svg = args.plot.read_text(encoding="utf-8")
        args.plot.write_text(re.sub(r"[ \t]+(?=\r?$)", "", svg, flags=re.MULTILINE), encoding="utf-8")
        figure.savefig(args.plot.with_suffix(".png"), dpi=170)
    print(json.dumps({label: {key: data[key] for key in ("requests", "cumulative_prompt_chars", "stop_reason")}
                      for label, data in result["scenarios"].items()}))


if __name__ == "__main__":
    main()

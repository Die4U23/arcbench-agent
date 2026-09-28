from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentConfig:
    requirement_dir: Path
    output_dir: Path
    task_type: str
    demo_mode: bool
    max_model_turns: int | None = None
    max_tool_calls: int | None = None
    command_timeout_seconds: int = 120


def parse_config() -> AgentConfig:
    parser = argparse.ArgumentParser(description="Run the ARC-Bench software engineering agent.")
    parser.add_argument(
        "requirement_path",
        nargs="?",
        default=os.environ.get("ARCBENCH_TASK_DIR", "requirements"),
        help="Task directory containing requirements.yaml or README.md/requirements.md.",
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("ARCBENCH_OUTPUT_DIR", "."),
        help="Target application workspace.",
    )
    parser.add_argument(
        "--type",
        dest="task_type",
        default=os.environ.get("ARCBENCH_TASK_TYPE", "web"),
        help="ARC-Bench task type.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run the deterministic offline demo without model credentials.",
    )
    parser.add_argument(
        "--max-model-turns",
        type=int,
        default=os.environ.get("ARCBENCH_MAX_MODEL_TURNS"),
        help="Optional model response limit per implementation pass; unlimited by default.",
    )
    parser.add_argument(
        "--max-tool-calls",
        type=int,
        default=os.environ.get("ARCBENCH_MAX_TOOL_CALLS"),
        help="Optional project tool-call limit per run; unlimited by default.",
    )
    args = parser.parse_args()
    if args.max_model_turns is not None and args.max_model_turns < 1:
        parser.error("--max-model-turns must be at least 1")
    if args.max_tool_calls is not None and args.max_tool_calls < 1:
        parser.error("--max-tool-calls must be at least 1")
    return AgentConfig(
        requirement_dir=Path(args.requirement_path).expanduser().resolve(),
        output_dir=Path(args.output_dir).expanduser().resolve(),
        task_type=args.task_type,
        demo_mode=args.demo,
        max_model_turns=args.max_model_turns,
        max_tool_calls=args.max_tool_calls,
    )

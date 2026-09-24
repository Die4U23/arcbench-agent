from __future__ import annotations

import logging
import sys
from pathlib import Path

# The downloaded starter keeps the SDK in a src-layout package. This fallback
# makes the checkout runnable before installing its local SDK dependency.
runtime_source = Path(__file__).resolve().parent / "arcbench-agent-runtime" / "src"
if runtime_source.is_dir():
    sys.path.insert(0, str(runtime_source))

from arcbench_agent_runtime import AgentRuntime

from agent.config import parse_config
from agent.orchestrator import run_agent


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = parse_config()
    runtime = AgentRuntime.from_env(project_dir=str(config.output_dir))
    try:
        return run_agent(runtime, config)
    except Exception as exc:
        logging.exception("Agent run failed")
        runtime.events.mark_run_failed(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

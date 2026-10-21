from __future__ import annotations

import os

# User-authorized Flash-only candidate. Reject stale Pro configuration before HTTP.
for _field in ("MODEL", "VISUAL_MODEL", "VISUAL_REVIEW_MODEL"):
    _model = os.environ.get(_field, "").strip() or "deepseek-flash"
    if _model != "deepseek-flash":
        raise RuntimeError("Flash-only candidate rejects non-Flash configuration in " + _field)
    os.environ[_field] = _model
os.environ["ARCBENCH_MODEL_DEADLINE_SECONDS"] = "300"
# This authorized candidate uses the full 10M allowance, including under stale
# lower external defaults. It cannot exceed 10M/250 by environment overrides.
os.environ["ARCBENCH_MAX_TOTAL_TOKENS"] = "10000000"
os.environ["ARCBENCH_MAX_MODEL_REQUESTS"] = "250"

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
from agent.docker_sandbox import DockerSandbox


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("Candidate limits: total_tokens=%s model_requests=%s",
                 os.environ["ARCBENCH_MAX_TOTAL_TOKENS"], os.environ["ARCBENCH_MAX_MODEL_REQUESTS"])
    config = parse_config()
    runtime = AgentRuntime.from_env(project_dir=str(config.output_dir))
    try:
        if not config.demo_mode:
            DockerSandbox().check_available()
        return run_agent(runtime, config)
    except Exception as exc:
        logging.exception("Agent run failed")
        runtime.events.mark_run_failed(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

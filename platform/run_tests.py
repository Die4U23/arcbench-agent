"""Run the complete platform regression and report unexpected skipped checks."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent


def forbidden_model_client(*args, **kwargs):
    raise AssertionError("External model clients are forbidden in release validation")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-runtime", action="store_true", help="Reject missing browser/Node checks")
    parser.add_argument("--report", type=Path, default=ROOT / "test-results" / "summary.json")
    args = parser.parse_args()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "platform-agent" / "arcbench-agent-runtime" / "src"))
    os.environ["OPENAI_API_KEY"] = "offline-validation-placeholder"
    os.environ["OPENAI_BASE_URL"] = "https://offline.invalid/v1"
    started = time.monotonic()
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_platform_*.py")
    suite.addTests(loader.loadTestsFromName("test_platform_generation"))
    suite.addTests(loader.loadTestsFromName("test_release_integrity"))
    count = suite.countTestCases()
    if count < 173:
        raise RuntimeError(f"Incomplete release test collection: {count} < 173")
    with patch("openai.AsyncOpenAI", side_effect=forbidden_model_client), patch(
        "openai.OpenAI", side_effect=forbidden_model_client
    ):
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    skipped = [{"test": test.id(), "reason": reason} for test, reason in result.skipped]
    unexpected = [item for item in skipped if item["reason"] != "no public inputs in working copy"]
    summary = {
        "discovered": count, "run": result.testsRun,
        "passed": result.testsRun - len(result.skipped) - len(result.failures) - len(result.errors),
        "skipped": skipped, "failures": [test.id() for test, _ in result.failures],
        "errors": [test.id() for test, _ in result.errors],
        "unexpected_skips": unexpected, "seconds": round(time.monotonic() - started, 3),
        "external_model_clients": "disabled", "formal_status": "UNRUN",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if result.wasSuccessful() and (not args.require_runtime or not unexpected) else 1


if __name__ == "__main__":
    raise SystemExit(main())

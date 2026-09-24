from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

from agent.config import parse_config


class ParseConfigBudgetTests(unittest.TestCase):
    def test_defaults_leave_room_for_multi_file_implementation(self) -> None:
        with patch.object(sys, "argv", ["main.py"]), patch.dict(os.environ, {}, clear=True):
            config = parse_config()

        self.assertEqual(config.max_model_turns, 36)
        self.assertEqual(config.max_tool_calls, 96)

    def test_budget_can_be_overridden_from_cli_and_environment(self) -> None:
        environment = {"ARCBENCH_MAX_MODEL_TURNS": "42", "ARCBENCH_MAX_TOOL_CALLS": "64"}
        with patch.object(sys, "argv", ["main.py", "task", "--max-model-turns", "24"]), patch.dict(os.environ, environment, clear=True):
            config = parse_config()

        self.assertEqual(config.max_model_turns, 24)
        self.assertEqual(config.max_tool_calls, 64)

    def test_non_positive_budget_is_rejected(self) -> None:
        with patch.object(sys, "argv", ["main.py", "--max-model-turns", "0"]), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as raised:
                parse_config()

        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

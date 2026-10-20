import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.run_token_trial import TokenBudget, TokenStop, INPUT_BOUND, OUTPUT_BOUND


class TokenTrialTests(unittest.TestCase):
    def test_unknown_usage_blocks_restart_without_erasing_reservation(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "usage.json"
            with TokenBudget(path).trial("baseline-1", {} ) as budget:
                budget.reserve()
            with self.assertRaisesRegex(TokenStop, "Unknown usage"):
                with TokenBudget(path).trial("candidate-1", {}):
                    self.fail("Unknown request must not be retried")
            data = json.loads(path.read_text())
            self.assertEqual(data["requests"][0]["reserved_tokens"], INPUT_BOUND + OUTPUT_BOUND)
            self.assertEqual(data["trials"][0]["outcome"], "interrupted")

    def test_full_input_window_is_reserved_before_every_request(self):
        with TemporaryDirectory() as folder:
            with TokenBudget(Path(folder) / "usage.json").trial("baseline-1", {}) as budget:
                request = budget.reserve()
                budget.settle(request, {"prompt_tokens": 950_000, "completion_tokens": 0}, "deepseek-flash")
                with self.assertRaisesRegex(TokenStop, "token headroom"):
                    budget.reserve()

    def test_request_limit_and_no_double_settlement(self):
        with TemporaryDirectory() as folder:
            with TokenBudget(Path(folder) / "usage.json").trial("baseline-1", {}) as budget:
                for _ in range(8):
                    request = budget.reserve()
                    budget.settle(request, {"prompt_tokens": 200, "completion_tokens": 10}, "deepseek-flash")
                with self.assertRaisesRegex(TokenStop, "settle twice"):
                    budget.settle(request, {"prompt_tokens": 0, "completion_tokens": 0}, "deepseek-flash")
                with self.assertRaisesRegex(TokenStop, "Request limit"):
                    budget.reserve()

    def test_unexpected_model_or_invalid_usage_stops_all_followup(self):
        for model, usage in (("deepseek-v4-pro", {"prompt_tokens": 1, "completion_tokens": 1}),
                             ("deepseek-flash", {"prompt_tokens": -1, "completion_tokens": 1})):
            with self.subTest(model=model, usage=usage), TemporaryDirectory() as folder:
                path = Path(folder) / "usage.json"
                with TokenBudget(path).trial("baseline-1", {}) as budget:
                    record = budget.reserve()
                    with self.assertRaises(TokenStop):
                        budget.settle(record, usage, model)
                with self.assertRaises(TokenStop):
                    with TokenBudget(path).trial("candidate-1", {}):
                        self.fail("Stopped study must remain stopped")

    def test_deadline_and_lock_are_enforced(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "usage.json"
            with TokenBudget(path).trial("baseline-1", {}) as budget:
                with patch.object(budget, "remaining_seconds", return_value=100):
                    with self.assertRaisesRegex(TokenStop, "time"):
                        budget.reserve()
                with self.assertRaises(FileExistsError):
                    with TokenBudget(path).trial("candidate-1", {}):
                        self.fail("Parallel trial acquired the same budget")


if __name__ == "__main__":
    unittest.main()

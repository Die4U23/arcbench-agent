import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent.experiment_budget import (ExperimentBudget, ExperimentStopped, INPUT_BOUND,
                                    OUTPUT_BOUND, RESERVATION, usage_cost)


def request():
    return {"model": "deepseek-flash", "max_tokens": OUTPUT_BOUND, "temperature": 0,
            "extra_body": {"thinking": {"type": "disabled"}},
            "messages": [{"role": "user", "content": "fixed repair"}]}


def usage(prompt=100, output=10):
    return {"prompt_tokens": prompt, "completion_tokens": output,
            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": prompt}


class ExperimentBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "study.json"

    def test_reservation_is_durable_before_send_and_settles_at_peak_upper_cost(self):
        with ExperimentBudget(self.path) as budget:
            budget.start_round({"case": "repair"})
            identifier = budget.reserve(request())
            disk = json.loads(self.path.read_text())
            self.assertEqual(disk["requests"][0]["status"], "reserved")
            self.assertEqual(disk["requests"][0]["reserved_cny"], str(RESERVATION))
            budget.settle(identifier, usage=usage(), actual_model="deepseek-flash",
                          provider_request_id="test-response")
            self.assertEqual(budget.data["requests"][0]["upper_cost_cny"], "0.00028")
            with self.assertRaisesRegex(ExperimentStopped, "already settled"):
                budget.settle(identifier, usage=usage(), actual_model="deepseek-flash",
                              provider_request_id="duplicate")
            budget.finish_round("passed")
        with ExperimentBudget(self.path) as budget:
            self.assertEqual(len(budget.data["requests"]), 1)

    def test_unknown_request_survives_restart_and_is_never_released(self):
        with ExperimentBudget(self.path) as budget:
            budget.start_round({})
            budget.reserve(request())
            budget.finish_round("interrupted")
        with self.assertRaisesRegex(ExperimentStopped, "unknown usage"):
            with ExperimentBudget(self.path):
                self.fail("Unresolved request was automatically reopened")
        disk = json.loads(self.path.read_text())
        self.assertEqual(disk["requests"][0]["upper_cost_cny"], str(RESERVATION))

    def test_exclusive_lock_prevents_parallel_writer_and_crashed_lock_is_not_removed(self):
        with ExperimentBudget(self.path):
            with self.assertRaisesRegex(ExperimentStopped, "lock exists"):
                with ExperimentBudget(self.path):
                    self.fail("Second writer entered")
        lock = self.path.with_suffix(".lock")
        lock.write_text('{"pid":0}')
        with self.assertRaises(ExperimentStopped):
            with ExperimentBudget(self.path):
                pass
        self.assertTrue(lock.exists())

    def test_money_request_token_and_time_limits_stop_before_reservation(self):
        for target, value in (("ROUND_CAP", 2), ("STUDY_SPEND_CAP", 2),
                              ("TOKEN_CAP", INPUT_BOUND), ("REQUEST_CAP", 0)):
            path = self.path.with_name(target + ".json")
            with patch("agent.experiment_budget." + target, value):
                with ExperimentBudget(path) as budget:
                    budget.start_round({})
                    with self.assertRaises(ExperimentStopped):
                        budget.reserve(request())
                    self.assertEqual(budget.data["requests"], [])
        with ExperimentBudget(self.path) as budget:
            budget.start_round({})
            with patch.object(budget, "remaining_seconds", return_value=479):
                with self.assertRaises(ExperimentStopped):
                    budget.reserve(request())
            self.assertEqual(budget.data["requests"], [])

    def test_missing_mismatched_or_out_of_bound_usage_stops_study(self):
        for i, bad in enumerate(({}, usage(INPUT_BOUND + 1), usage(100, OUTPUT_BOUND + 1),
                                 dict(usage(), prompt_cache_hit_tokens=1))):
            path = self.path.with_name(f"bad{i}.json")
            with ExperimentBudget(path) as budget:
                budget.start_round({})
                identifier = budget.reserve(request())
                with self.assertRaises(ExperimentStopped):
                    budget.settle(identifier, usage=bad, actual_model="deepseek-flash",
                                  provider_request_id="unknown")
                self.assertIsNotNone(budget.data["stopped"])
                self.assertEqual(budget.data["requests"][0]["status"], "reserved")

    def test_rejects_different_model_images_server_tools_and_unreviewed_flags(self):
        mutations = ({"model": "deepseek-v4-pro"}, {"max_tokens": 100000},
                     {"temperature": 1}, {"extra_headers": {"x": "y"}},
                     {"messages": [{"role": "user", "content": [{"type": "image_url"}]}]},
                     {"tools": [{"type": "web_search"}]},
                     {"messages": [{"role": "user", "content": "x" * 65536}]})
        with ExperimentBudget(self.path) as budget:
            budget.start_round({})
            for update in mutations:
                with self.assertRaises(ExperimentStopped):
                    budget.reserve(dict(request(), **update))
            self.assertEqual(budget.data["requests"], [])

    def test_failed_durable_write_cannot_yield_permission_to_send(self):
        with ExperimentBudget(self.path) as budget:
            budget.start_round({})
            with patch.object(budget, "save", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    budget.reserve(request())

    def test_old_cost_cannot_be_reset_or_tampered_on_restart(self):
        with ExperimentBudget(self.path) as budget:
            budget.start_round({})
            identifier = budget.reserve(request())
            budget.settle(identifier, usage=usage(), actual_model="deepseek-flash",
                          provider_request_id="test-response")
            budget.finish_round("failed")
        data = json.loads(self.path.read_text())
        data["requests"][0]["upper_cost_cny"] = "0"
        self.path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ExperimentStopped, "mismatch"):
            with ExperimentBudget(self.path):
                pass


if __name__ == "__main__":
    unittest.main()

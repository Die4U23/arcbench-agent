"""Transport observations use fake responses only; no model/network calls."""
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from agent.model_transport import ModelRequestInterrupted
from agent.request_metrics import MARKER, observe_operation, usage_fields
from scripts.benchmark_trials import request_report
from tests.test_llm_budget import _make_client


class RequestMetricsTests(unittest.TestCase):
    def attempt(self, *, response=None, error=None):
        model = _make_client(None, None, [])
        model._record_model_request()
        with patch.object(model.client.chat.completions, "create",
                          return_value=response, side_effect=error), \
                self.assertLogs("agent.request_metrics", level="INFO") as logs:
            if error:
                with self.assertRaises(type(error)):
                    model._create_completion("implementation", model="flash",
                        messages=[{"role": "user", "content": "PROMPT_SECRET"}])
            else:
                self.assertIs(model._create_completion("implementation", model="flash",
                    messages=[{"role": "user", "content": "PROMPT_SECRET"}]), response)
        return logs.output

    def test_records_actual_model_raw_usage_and_no_payloads(self):
        response = NS(model="flash-actual", usage=NS(prompt_tokens=10, completion_tokens=3,
            prompt_cache_hit_tokens=6, prompt_cache_miss_tokens=4), content="RESPONSE_SECRET")
        lines = self.attempt(response=response)
        report = request_report(lines)
        row = report["groups"][0]
        self.assertEqual(row["actual_model"], "flash-actual")
        self.assertEqual(row["requested_model"], "flash")
        self.assertEqual(row["provider_cache_hit_ratio"], 0.6)
        self.assertEqual(row["prompt_tokens"], 10)
        self.assertIsNone(row["verified_cost"])
        self.assertNotIn("SECRET", "\n".join(lines))

    def test_cached_tokens_without_reported_miss_are_not_invented(self):
        response = NS(usage=NS(prompt_tokens=100, completion_tokens=2,
            prompt_tokens_details=NS(cached_tokens=80)))
        fields = usage_fields(response)
        self.assertEqual(fields["cache_hit_tokens"], 80)
        self.assertIsNone(fields["cache_miss_tokens"])
        row = request_report(self.attempt(response=response))["groups"][0]
        self.assertIsNone(row["provider_cache_hit_ratio"])

    def test_transport_interrupt_and_api_error_both_remain_attempts(self):
        for error in (ModelRequestInterrupted(reason="deadline", deadline_seconds=1),
                      RuntimeError("ERROR_SECRET")):
            lines = self.attempt(error=error)
            row = request_report(lines)["groups"][0]
            self.assertEqual(row["attempts"], 1)
            self.assertEqual(row["errors"], 1)
            self.assertIsNone(row["prompt_tokens"])
            self.assertNotIn("ERROR_SECRET", "\n".join(lines))

    def test_start_without_end_is_counted_and_duplicate_logs_rejected(self):
        lines = self.attempt(response=NS(model="flash", usage=None))
        row = request_report(lines[:1])["groups"][0]
        self.assertEqual(row["incomplete"], 1)
        self.assertIsNone(row["seconds"])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            request_report(lines + lines)

    def test_partial_usage_does_not_become_complete_total(self):
        first = self.attempt(response=NS(model="flash", usage=NS(prompt_tokens=8,
            completion_tokens=2, prompt_cache_hit_tokens=4, prompt_cache_miss_tokens=4)))
        second = self.attempt(response=NS(model="flash", usage=None))
        row = request_report(first + second)["groups"][0]
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["prompt_tokens_known_sum"], 8)
        self.assertEqual(row["prompt_tokens_observed_attempts"], 1)
        self.assertIsNone(row["prompt_tokens"])
        self.assertIsNone(row["provider_cache_hit_ratio"])

    def test_weighted_ratio_and_distinct_actual_model_groups(self):
        lines = []
        for model, hit, miss in (("flash", 9, 1), ("flash", 0, 90), ("claude", 1, 1)):
            lines += self.attempt(response=NS(model=model, usage=NS(
                prompt_tokens=hit + miss, completion_tokens=1,
                prompt_cache_hit_tokens=hit, prompt_cache_miss_tokens=miss)))
        groups = request_report(lines)["groups"]
        self.assertEqual(len(groups), 2)
        flash = next(g for g in groups if g["actual_model"] == "flash")
        self.assertEqual(flash["provider_cache_hit_ratio"], 0.09)

    def test_invalid_numeric_usage_and_untrusted_model_text_are_not_logged(self):
        lines = self.attempt(response=NS(model="Bearer SECRET\nbad", usage=NS(
            prompt_tokens=-1, completion_tokens=True, prompt_cache_hit_tokens="99")))
        row = request_report(lines)["groups"][0]
        self.assertIsNone(row["actual_model"])
        self.assertIsNone(row["prompt_tokens"])
        self.assertIsNone(row["completion_tokens"])
        self.assertNotIn("SECRET", "\n".join(lines))

    def test_mismatched_finish_is_rejected(self):
        lines = self.attempt(response=NS(model="flash", usage=None))
        event = json.loads(lines[-1].split(MARKER)[1])
        event["provider"] = "another-provider"
        with self.assertRaisesRegex(ValueError, "Mismatched"):
            request_report([lines[0], MARKER + json.dumps(event)])

    def test_operation_observation_preserves_result_and_failure(self):
        @observe_operation("fixture")
        def operation(error=False):
            if error:
                raise ValueError("PRIVATE_ERROR")
            return NS(passed=False)
        with self.assertLogs("agent.request_metrics", level="INFO") as logs:
            self.assertFalse(operation().passed)
            with self.assertRaises(ValueError):
                operation(True)
        spans = request_report(logs.output)["operations"]
        self.assertEqual([s["status"] for s in spans], ["returned", "error"])
        self.assertFalse(spans[0]["local_passed"])
        self.assertNotIn("PRIVATE_ERROR", "\n".join(logs.output))
        incomplete = request_report(logs.output[:1])["operations"][0]
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertIsNone(incomplete["seconds"])


if __name__ == "__main__":
    unittest.main()

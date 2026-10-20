"""Budget/transport integration tests; provider, balance, and HTTP clients mocked."""
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from agent.experiment_budget import ExperimentBudget, ExperimentStopped
from scripts.run_flash_pilot import GuardedTransport
from tests.test_experiment_budget import request, usage


class FlashPilotTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "ledger.json"
        self.values = {"OPENAI_API_KEY": "FAKE_TEST_KEY_ONLY"}

    def test_durable_reservation_precedes_send_and_client_is_direct_without_redirects(self):
        with ExperimentBudget(self.path) as budget:
            budget.start_round({})
            response = NS(model="deepseek-flash", id="fake-response",
                          model_dump=lambda: {"usage": usage()})
            def sent(**kwargs):
                self.assertEqual(json.loads(self.path.read_text())["requests"][0]["status"], "reserved")
                return response
            client = NS(create=sent)
            with patch("scripts.run_flash_pilot.balance", side_effect=[Decimal("5"), Decimal("4.99986")]), \
                    patch("scripts.run_flash_pilot.CompletionClient", return_value=client) as transport, \
                    patch("scripts.run_flash_pilot.httpx.AsyncClient") as http:
                wrapped = GuardedTransport(budget, self.values, self.root, lambda: None)
                self.assertIs(wrapped.create(**request()), response)
                http.assert_called_once_with(trust_env=False, follow_redirects=False)
                self.assertEqual(transport.call_args.kwargs["base_url"], "https://api.deepseek.com")
            self.assertEqual(budget.data["requests"][0]["account_delta_cny"], "0.00014")
            self.assertNotIn("FAKE_TEST_KEY", self.path.read_text())
            budget.finish_round("passed")

    def test_unknown_transport_never_retries_and_retains_cost_across_restart(self):
        with ExperimentBudget(self.path) as budget:
            budget.start_round({})
            send = Mock(side_effect=TimeoutError("unavailable"))
            with patch("scripts.run_flash_pilot.balance", return_value=Decimal("5")), \
                    patch("scripts.run_flash_pilot.CompletionClient", return_value=NS(create=send)), \
                    patch("scripts.run_flash_pilot.httpx.AsyncClient"):
                wrapped = GuardedTransport(budget, self.values, self.root, lambda: None)
                with self.assertRaises(TimeoutError):
                    wrapped.create(**request())
                send.assert_called_once()
                with self.assertRaises(ExperimentStopped):
                    wrapped.create(**request())
                send.assert_called_once()
            self.assertEqual(budget.data["requests"][0]["status"], "reserved")
            budget.finish_round("interrupted")
        with self.assertRaises(ExperimentStopped):
            with ExperimentBudget(self.path):
                pass

    def test_protected_input_or_wrong_model_cannot_reach_paid_transport(self):
        with ExperimentBudget(self.path) as budget:
            budget.start_round({})
            with patch("scripts.run_flash_pilot.balance") as read, \
                    patch("scripts.run_flash_pilot.CompletionClient") as send:
                wrapped = GuardedTransport(budget, self.values, self.root, lambda: None)
                with self.assertRaises(ExperimentStopped):
                    wrapped.create(**dict(request(), model="deepseek-v4-pro"))
                wrapped.protection_check = Mock(side_effect=ExperimentStopped("changed assertions"))
                with self.assertRaises(ExperimentStopped):
                    wrapped.create(**request())
                read.assert_not_called()
                send.assert_not_called()
            self.assertEqual(budget.data["requests"], [])

    def test_unexplained_balance_delta_stops_after_one_response(self):
        with ExperimentBudget(self.path) as budget:
            budget.start_round({})
            response = NS(model="deepseek-flash", id="fake-response",
                          model_dump=lambda: {"usage": usage()})
            with patch("scripts.run_flash_pilot.balance", side_effect=[Decimal("5"), Decimal("3")]), \
                    patch("scripts.run_flash_pilot.CompletionClient", return_value=NS(create=lambda **k: response)), \
                    patch("scripts.run_flash_pilot.httpx.AsyncClient"):
                wrapped = GuardedTransport(budget, self.values, self.root, lambda: None)
                with self.assertRaises(ExperimentStopped):
                    wrapped.create(**request())
            self.assertIsNotNone(budget.data["stopped"])
            self.assertEqual(len(budget.data["requests"]), 1)


if __name__ == "__main__":
    unittest.main()

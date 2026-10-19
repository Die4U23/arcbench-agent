"""Deterministic startup deadline checks; no network or model calls."""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from agent.contract_checks import _wait_for_homepage


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        if seconds <= 0:
            raise AssertionError("Retries must yield for a positive interval")
        self.sleeps.append(seconds)
        self.now += seconds


class StartupDeadlineTests(unittest.TestCase):
    def run_probe(self, outcomes, *, timeout=0.25, response_delay=0.0):
        clock = FakeClock()
        calls = []
        server = SimpleNamespace(poll=lambda: None, returncode=None)

        @contextmanager
        def open_response(url, **kwargs):
            calls.append(kwargs["timeout"])
            if len(calls) > 20:
                raise AssertionError("Startup loop escaped its deadline")
            clock.now += response_delay
            outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            yield SimpleNamespace(status=outcome)

        with patch("agent.contract_checks.time.monotonic", clock.monotonic), \
             patch("agent.contract_checks.time.sleep", clock.sleep), \
             patch("agent.contract_checks.urlopen", open_response):
            try:
                _wait_for_homepage(server, timeout=timeout)
            except RuntimeError as exc:
                return clock, calls, exc
        return clock, calls, None

    def test_successful_non_ready_statuses_obey_deadline_and_sleep(self):
        for status in (204, 206):
            with self.subTest(status=status):
                clock, calls, error = self.run_probe([status])
                self.assertIsNotNone(error)
                self.assertIn("did not serve", str(error))
                self.assertAlmostEqual(clock.now, 0.25)
                self.assertEqual(len(calls), 3)
                self.assertEqual(len(clock.sleeps), 3)
                self.assertAlmostEqual(calls[-1], 0.05)

    def test_connection_and_http_errors_share_bounded_retry(self):
        errors = (
            URLError("connection refused"),
            HTTPError("http://127.0.0.1:3000", 404, "Not Found", {}, None),
        )
        for outcome in errors:
            with self.subTest(error=type(outcome).__name__):
                clock, calls, error = self.run_probe([outcome])
                self.assertIsNotNone(error)
                self.assertAlmostEqual(clock.now, 0.25)
                self.assertEqual(len(calls), 3)

    def test_ready_after_non_ready_response_succeeds(self):
        clock, calls, error = self.run_probe([204, 200])
        self.assertIsNone(error)
        self.assertEqual(len(calls), 2)
        self.assertEqual(clock.sleeps, [0.1])

    def test_immediate_ready_needs_no_sleep(self):
        clock, calls, error = self.run_probe([200])
        self.assertIsNone(error)
        self.assertEqual(len(calls), 1)
        self.assertEqual(clock.sleeps, [])

    def test_late_success_does_not_escape_deadline(self):
        clock, calls, error = self.run_probe([200], response_delay=0.3)
        self.assertIsNotNone(error)
        self.assertIn("did not serve", str(error))
        self.assertEqual(len(calls), 1)

    def test_exited_server_fails_without_http_request(self):
        server = SimpleNamespace(poll=lambda: 7, returncode=7)
        with patch("agent.contract_checks.urlopen") as request:
            with self.assertRaisesRegex(RuntimeError, "exited with code 7"):
                _wait_for_homepage(server)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()


import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile

from agent.browser_contract import browser_guard_result
from agent.native_verification import _combine, _run_owned_browser
from agent.repair_policy import failed_behavior_ids, repair_guidance
from agent.candidate_checkpoint import PUBLIC_CHECK
from agent.station_units import check_locations, unavailable, CASES
from agent.verify import CheckResult, VerificationResult


class StationUnitTests(unittest.TestCase):
    def test_real_chromium_rejects_joined_aliases_and_accepts_individual_locations(self):
        from playwright.sync_api import sync_playwright
        from agent.visual_acceptance import _ensure_chromium
        with sync_playwright() as playwright:
            browser=_ensure_chromium(playwright)
            try:
                page=browser.new_page()
                origin=r'^(?:Shanghai|ShanghaiHongqiao)$'
                arrival=r'^(?:Beijing|BeijingNan)$'
                page.set_content('<div>Shanghai/ShanghaiHongqiao → Beijing/BeijingNan</div>')
                with self.assertRaisesRegex(ValueError,'departure location'):
                    check_locations(page,origin,arrival)
                page.set_content('<span>ShanghaiHongqiao</span><span>BeijingNan</span>')
                check_locations(page,origin,arrival)
                page.set_content('<span>Shanghai</span><span>ShanghaiHongqiao</span><span>Beijing</span>')
                with self.assertRaisesRegex(ValueError,'found 2'):
                    check_locations(page,origin,arrival)
            finally:
                browser.close()

    def test_composite_preserves_original_pass_and_exposes_new_failure_family(self):
        original=browser_guard_result(True,'Original native passed')
        location=unavailable('departure individual visible named location found0')
        composite=_combine(original,location)
        self.assertFalse(composite.passed)
        self.assertEqual(composite.case_statuses[0],original.case_statuses[0])
        self.assertEqual(len(composite.case_statuses),3)
        public=CheckResult(PUBLIC_CHECK,True,0,'32/32',(),(('REQ-2.1: search','passed'),))
        result=VerificationResult(False,(public,composite))
        self.assertEqual(failed_behavior_ids(result),('REQ-2.1','REQ-2.2'))
        self.assertIn('departure and arrival',repair_guidance(result,'REQ-2.1'))

    def test_original_probe_is_not_reexecuted_when_new_probe_runs(self):
        original=browser_guard_result(True,'Original native passed')
        locations=CheckResult('Native locations',True,0,'passed',(),tuple((c,'passed') for c in CASES))
        with tempfile.TemporaryDirectory() as directory, \
             patch('agent.native_verification.subprocess.Popen') as server, \
             patch('agent.native_verification._wait_for_server'), \
             patch('agent.native_verification._stop_owned_server') as stop, \
             patch('agent.native_verification.verify_browser_contract') as original_probe, \
             patch('agent.native_verification.verify_station_units',return_value=locations) as location_probe:
            result=_run_owned_browser(Path(directory),original)
            self.assertTrue(result.passed)
            original_probe.assert_not_called()
            location_probe.assert_called_once()
            stop.assert_called_once_with(server.return_value)


if __name__=='__main__':unittest.main()

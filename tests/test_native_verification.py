
# Legacy fixtures exercise worker internals; host isolation is tested separately.
import unittest.mock as _sandbox_mock
import re
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.browser_contract import BROWSER_CHECK, browser_guard_result
from agent.candidate_checkpoint import PUBLIC_CHECK
from agent.native_verification import _run_owned_browser, verify_public_acceptance
from agent.repair_policy import failed_behavior_ids, repair_guidance
from agent.verify import CheckResult, VerificationResult


@_sandbox_mock.patch("agent.sandbox_context._WORKER", True)
class NativeVerificationTests(unittest.TestCase):
    def test_all_assertions_passed_with_nonzero_exit_is_a_runner_failure(self):
        public = CheckResult(PUBLIC_CHECK, False, 1, '32/32 passed', (),
                             tuple((f'case {index}', 'passed') for index in range(32)))
        placeholder = browser_guard_result(False, 'Not executed: fix preceding jsdom')
        native = browser_guard_result(True, 'Native executed')
        with patch('agent.native_verification.original_public_acceptance',
                   return_value=(public, placeholder)), \
             patch('agent.native_verification._run_owned_browser', return_value=native):
            annotated, browser = verify_public_acceptance(Path('generated'), {})
        self.assertFalse(annotated.passed)
        self.assertEqual(annotated.exit_code, public.exit_code)
        self.assertEqual(annotated.case_statuses, public.case_statuses)
        self.assertEqual(annotated.requirement_ids, public.requirement_ids)
        self.assertIn('Runner-level failure', annotated.output)
        self.assertIn('cause is unknown', annotated.output)
        self.assertIn('before changing passing business behavior', annotated.output)
        self.assertIs(browser, native)
        self.assertEqual(failed_behavior_ids(VerificationResult(False, (annotated, browser))), ())

    def test_runner_annotation_does_not_infer_cause_from_incomplete_cases(self):
        for statuses, exit_code, passed in (((), 1, False),
                                           ((('case', 'failed'),), 1, False),
                                           ((('case', 'passed'),), None, False),
                                           ((('case', 'passed'),), 0, True)):
            public = CheckResult(PUBLIC_CHECK, passed, exit_code, 'original report', (), statuses)
            with self.subTest(statuses=statuses, exit_code=exit_code), \
                 patch('agent.native_verification.original_public_acceptance', return_value=(public,)):
                checks = verify_public_acceptance(Path('generated'), {})
            self.assertIs(checks[0], public)

    def incomplete_cases(self):
        return (CheckResult(PUBLIC_CHECK, False, 1, 'REQ-3.2: later failure',
                            ('REQ-3.2',), (('REQ-1.1: registration', 'passed'),
                                          ('REQ-3.2: booking', 'failed'))),
                browser_guard_result(False, 'Not executed: fix preceding jsdom'))

    def test_native_registration_failure_is_not_hidden_by_later_jsdom_failure(self):
        original = self.incomplete_cases()
        native = browser_guard_result(False, 'registration: get_by_label(/^male$/i) timeout')
        with patch('agent.native_verification.original_public_acceptance',return_value=original), \
             patch('agent.native_verification._run_owned_browser',return_value=native) as probe:
            checks = verify_public_acceptance(Path('generated'),{})
        self.assertIs(checks[0],original[0])
        self.assertIs(checks[1],native)
        probe.assert_called_once()
        result=VerificationResult(False,checks)
        self.assertEqual(failed_behavior_ids(result),('REQ-1.1','REQ-3.2'))
        self.assertIn('get_by_label(/^male$/i)',repair_guidance(result,'REQ-1.1'))

    def test_original_probe_result_is_reused_while_location_checks_still_run(self):
        public,_=self.incomplete_cases()
        for native in (browser_guard_result(True,'Native executed'),
                       browser_guard_result(False,'Native executed failure')):
            original=(public,native)
            with patch('agent.native_verification.original_public_acceptance',return_value=original), \
                 patch('agent.native_verification._run_owned_browser',return_value=native) as probe:
                checks=verify_public_acceptance(Path('generated'),{})
                self.assertIs(checks[0],public)
                probe.assert_called_once_with(Path('generated'),native)

    def test_no_browser_run_without_complete_jsdom_case_report_or_during_startup(self):
        for original,startup in (((CheckResult(PUBLIC_CHECK,False,1,'Missing dependencies'),),False),
                                 (self.incomplete_cases(),True)):
            with patch('agent.native_verification.original_public_acceptance',return_value=original), \
                 patch('agent.native_verification._run_owned_browser') as probe:
                self.assertIs(verify_public_acceptance(Path('generated'),{},startup_only=startup),original)
                probe.assert_not_called()

    def test_occupied_port_does_not_probe_or_stop_a_foreign_server(self):
        with socket.socket() as listener, tempfile.TemporaryDirectory() as directory:
            try:
                listener.bind(('127.0.0.1',3000))
                listener.listen()
            except OSError:
                self.skipTest('Port3000 already occupied before isolated fixture')
            with patch('agent.native_verification.subprocess.Popen') as start, \
                 patch('agent.native_verification.verify_browser_contract') as probe, \
                 patch('agent.native_verification._stop_owned_server') as stop:
                result=_run_owned_browser(Path(directory))
                self.assertFalse(result.passed)
                start.assert_not_called()
                probe.assert_not_called()
                stop.assert_not_called()

    def test_real_chromium_label_regex_differs_from_role_name(self):
        from playwright.sync_api import sync_playwright
        from agent.visual_acceptance import _ensure_chromium
        with sync_playwright() as playwright:
            browser=_ensure_chromium(playwright)
            try:
                page=browser.new_page()
                page.set_content('<label><input type="radio" name="gender"> Male</label>')
                male=re.compile('^male$',re.I)
                self.assertEqual(page.get_by_role('radio',name=male).count(),1)
                self.assertEqual(page.get_by_label(male).count(),0)
                page.set_content('<label><input type="radio" name="gender" aria-label="Male"> Male</label>')
                page.get_by_label(male).check()
                self.assertTrue(page.get_by_label(male).is_checked())
            finally:
                browser.close()


if __name__=='__main__':
    unittest.main()

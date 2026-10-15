import json
from types import SimpleNamespace
import unittest

from agent.booking_completion import CASE, completion_result, exercise_completion
from agent.browser_contract import browser_guard_result
from agent.candidate_checkpoint import PUBLIC_CHECK
from agent.native_verification import _combine
from agent.repair_policy import failed_behavior_ids
from agent.verify import CheckResult, VerificationResult


class BookingCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        from agent.visual_acceptance import _ensure_chromium
        cls.runtime = sync_playwright().start()
        try:
            cls.browser = _ensure_chromium(cls.runtime)
        except Exception:
            cls.runtime.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def fixture(self, *, duplicate_review=False, missing_name=False, reload_empty=False,
                changed_number=False):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        details = ('<p>Booking number: BK-1 — ' +
                   ('undefined' if missing_name else 'Chromium Passenger') +
                   ', 123456789, Vietnam, Business Class, Sun, May 31.</p>')
        review = '<p>Sun, May 31</p>' * (2 if duplicate_review else 1)
        page.set_content(review + '<button>Confirm</button><script>' +
                         'document.querySelector("button").onclick=()=>{document.body.innerHTML=' +
                         json.dumps(details) + ';};</script>')
        events = []

        def reload_document():
            events.append('reload')
            page.set_content('<p>Select a journey</p>' if reload_empty else
                             details.replace('BK-1', 'BK-2') if changed_number else details)

        return SimpleNamespace(get_by_text=page.get_by_text, get_by_role=page.get_by_role,
                               reload=reload_document), events

    def test_complete_confirmation_and_same_record_reload_pass(self):
        page, events = self.fixture()
        exercise_completion(page)
        self.assertEqual(events, ['reload'])

    def test_review_failure_does_not_hide_later_response_and_reload_defects(self):
        page, events = self.fixture(duplicate_review=True, missing_name=True, reload_empty=True)
        with self.assertRaises(ValueError) as raised:
            exercise_completion(page)
        for stage in ('Review summary:', 'Confirmation response/details:', 'Reload persistence:'):
            self.assertIn(stage, str(raised.exception))
        self.assertEqual(events, ['reload'])

    def test_reload_must_keep_the_same_concrete_record(self):
        page, _ = self.fixture(changed_number=True)
        with self.assertRaisesRegex(ValueError, 'Reload changed booking number from BK-1 to BK-2'):
            exercise_completion(page)

    def test_actual_completion_failure_precedes_later_jsdom_search_failure(self):
        public = CheckResult(PUBLIC_CHECK, False, 1, 'search failure', ('REQ-2.1',),
                             (('REQ-2.1: search', 'failed'),))
        native = _combine(browser_guard_result(True, 'entry passed'),
                          completion_result(False, 'Reload persistence failed'))
        self.assertFalse(native.passed)
        self.assertIn((CASE, 'failed'), native.case_statuses)
        self.assertEqual(failed_behavior_ids(VerificationResult(False, (public, native))),
                         ('REQ-3.2', 'REQ-2.1'))

    def test_unexecuted_completion_does_not_override_registration_failure(self):
        public = CheckResult(PUBLIC_CHECK, False, 1, 'search failure', ('REQ-2.1',),
                             (('REQ-2.1: search', 'failed'),))
        native = _combine(browser_guard_result(False, 'registration failed'),
                          completion_result(False, 'Not executed'))
        self.assertEqual(failed_behavior_ids(VerificationResult(False, (public, native))),
                         ('REQ-1.1', 'REQ-3.2', 'REQ-2.1'))


if __name__ == '__main__':
    unittest.main()

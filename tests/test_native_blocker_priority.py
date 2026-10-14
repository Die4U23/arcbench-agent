import unittest

from agent.browser_contract import BROWSER_CASE, BROWSER_CHECK
from agent.candidate_checkpoint import PUBLIC_CHECK
from agent.repair_policy import failed_behavior_ids
from agent.verify import CheckResult, VerificationResult


NAME_FAILURE = ('search and booking with real CSS and backend: Locator.fill: Timeout 3000ms exceeded.\n'
                'waiting for get_by_role("textbox", name=re.compile(r"^name$", re.IGNORECASE))')


class NativeBlockerPriorityTests(unittest.TestCase):
    def result(self, output, *, original_status='failed', complete=True):
        public = CheckResult(PUBLIC_CHECK, False, 1, 'Local failures', ('REQ-2.1', 'REQ-3.2'),
            (('REQ-2.1: search', 'failed'), ('REQ-3.2: booking', 'failed')) if complete else ())
        native = CheckResult(BROWSER_CHECK, False, 1, output,
            ('REQ-1.1', 'REQ-2.1', 'REQ-2.2', 'REQ-3.2'),
            ((BROWSER_CASE, original_status), ('additional location', 'failed')))
        return VerificationResult(False, (public, native))

    def test_actual_booking_name_blocker_precedes_merged_location_and_static_registration_ids(self):
        result = self.result(NAME_FAILURE + '\nNative individual locations: found 2')
        self.assertEqual(failed_behavior_ids(result), ('REQ-3.2', 'REQ-1.1', 'REQ-2.1', 'REQ-2.2'))

    def test_registration_or_other_native_failures_keep_their_existing_order(self):
        for output in ('registration: waiting for get_by_label(/^male$/i)',
                       'search and booking with real CSS and backend: waiting for button Confirm'):
            with self.subTest(output=output):
                self.assertEqual(failed_behavior_ids(self.result(output)),
                    ('REQ-1.1', 'REQ-2.1', 'REQ-2.2', 'REQ-3.2'))

    def test_passed_original_or_incomplete_public_report_cannot_establish_this_blocker(self):
        self.assertEqual(failed_behavior_ids(self.result(NAME_FAILURE, original_status='passed')),
            ('REQ-1.1', 'REQ-2.1', 'REQ-2.2', 'REQ-3.2'))
        self.assertEqual(failed_behavior_ids(self.result(NAME_FAILURE, complete=False)), ())

    def test_native_priority_retains_other_complete_public_failure_families(self):
        for output, original_status, expected_first in (
            (NAME_FAILURE, 'failed', 'REQ-3.2'),
            ('registration failed', 'failed', 'REQ-1.1'),
            ('Original native passed; location found 2', 'passed', 'REQ-1.1'),
        ):
            with self.subTest(output=output):
                result = self.result(output, original_status=original_status)
                public, native = result.checks
                public = CheckResult(PUBLIC_CHECK, False, 1, public.output, (),
                    (*public.case_statuses, (' REQ-3.1: selected train summary', 'failed')))
                ids = failed_behavior_ids(VerificationResult(False, (public, native)))
                self.assertEqual(ids[0], expected_first)
                self.assertEqual(ids[-1], 'REQ-3.1')
                self.assertEqual(len(ids), len(set(ids)))

    def test_native_failure_does_not_bypass_infrastructure_gate(self):
        result = self.result(NAME_FAILURE)
        for name in ('frontend: npm run build', 'web template structure',
                     'project dependencies', 'frontend JavaScript undefined names'):
            with self.subTest(check=name):
                broken = CheckResult(name, False, 1, 'broken infrastructure')
                self.assertEqual(failed_behavior_ids(
                    VerificationResult(False, (*result.checks, broken))), ())


if __name__ == '__main__':
    unittest.main()

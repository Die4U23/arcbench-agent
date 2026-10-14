import unittest

from agent.candidate_checkpoint import PUBLIC_CHECK
from agent.repair_policy import repair_guidance
from agent.verify import CheckResult, VerificationResult


def guidance(output, scope):
    result = VerificationResult(False, (CheckResult(PUBLIC_CHECK, False, 1, output),))
    return repair_guidance(result, scope)


class RepairGuidanceContextTests(unittest.TestCase):
    def test_observed_registered_wording_is_compared_with_unchanged_duplicate_expression(self):
        failure=('REQ-1.1: reject a duplicate username without creating a signed-in session: '
                 'AssertionError: expected 0 to be greater than 0 | '
                 'Observed DOM snapshot: alerts=["Username is already registered."]')
        feedback=guidance(failure,'REQ-1.1')
        self.assertIn('already registered', feedback)
        self.assertIn('already exists, duplicate, taken or conflict', feedback)
        self.assertIn('leave no session', feedback)
        self.assertIn('Do not change the published expression', feedback)
        self.assertIn('/username.+(?:already exists|duplicate|taken|conflict)', feedback)
        self.assertNotIn('observed duplicate-account', guidance(
            'REQ-1.1: unrelated count: expected 0 to be greater than 0', 'REQ-1.1'))

    def test_visible_role_label_and_text_keep_locator_counts_separate_from_plain_equality(self):
        locators = (
            ('REQ-1.2', 'role link name=/login/i'),
            ('REQ-1.1', 'label /^name$/i'),
            ('REQ-2.1', 'text Shanghai'),
        )
        for scope, locator in locators:
            for count in ('+0', '2'):
                with self.subTest(locator=locator, count=count):
                    failure = (f'{scope}: published interaction: Strict visibility {locator} '
                               f'must resolve once: expected {count} to be 1 // Object.is equality')
                    feedback = guidance(failure, scope)
                    self.assertIn(failure, feedback)
                    self.assertIn('Zero matching locator nodes' if count == '+0'
                                  else 'Multiple matching locator nodes', feedback)
                    self.assertNotIn('plain numeric count assertion', feedback)
                    self.assertIn('Do not hide required data or change selectors', feedback)
        label_feedback = guidance(
            'REQ-1.1: register: Strict visibility label /^name$/i must resolve once: '
            'expected +0 to be 1', 'REQ-1.1')
        self.assertIn('getByLabelText', label_feedback)
        self.assertIn('required validation', label_feedback)

    def test_login_missing_link_diagnoses_signout_setup_before_credentials(self):
        failure = ('REQ-1.2: sign in with a valid username and password: '
                   'Strict visibility role link name=/login/i must resolve once: '
                   'expected +0 to be 1 // Object.is equality')
        feedback = guidance(failure, 'REQ-1.2')
        self.assertIn('sign-out setup before credential submission', feedback)
        self.assertIn('current DOM, user state, stored token and route', feedback)
        self.assertIn('State updater functions must be pure', feedback)
        self.assertIn('development StrictMode', feedback)
        self.assertIn('diagnostic hypothesis, not an official case result', feedback)
        self.assertIn('Do not invent an extra Login link', feedback)
        self.assertIn('without reaching the login request', feedback)
        self.assertNotIn('complete nested payload schema', feedback)

    def test_booking_zero_success_requests_current_nested_schema_and_backend_errors(self):
        failure = ('REQ-3.2: submit valid passenger information and create a booking record: '
                   'Strict visibility text /booking number|order number|success|confirmed/i '
                   'must resolve once: expected +0 to be 1 // Object.is equality')
        feedback = guidance(failure, 'REQ-3.2')
        self.assertIn('actual backend errors', feedback)
        self.assertIn('complete nested payload schema', feedback)
        self.assertIn('a top-level field does not satisfy a required nested field', feedback)
        self.assertIn('current source field names', feedback)
        self.assertIn('actual API feedback before changing success text', feedback)
        self.assertNotIn('acceptedTerms', feedback)
        self.assertNotIn('selectedJourney', feedback)
        self.assertIn('owner/journey isolation and stale-request cancellation', feedback)

    def test_first_success_failure_keeps_remaining_public_lifecycle_unverified(self):
        feedback = guidance(
            'REQ-3.2: create bookings across supported route and seat combinations: '
            'Strict visibility text /confirmed/i must resolve once: expected +0 to be 1',
            'REQ-3.2')
        self.assertIn('later interactions may not have run', feedback)
        self.assertIn('After confirmation succeeds', feedback)
        self.assertIn('success heading and booking-number notice', feedback)
        self.assertIn('storage helpers are actually called', feedback)
        self.assertIn('restore the owned record from the real API on reload', feedback)
        self.assertIn('imports, helper definitions and a generated flow id alone', feedback)
        self.assertIn('same browser without an intervening sign-out', feedback)
        self.assertIn('registration reachable while signed in', feedback)
        self.assertIn('session replacement and owned-result isolation', feedback)
        self.assertIn('Do not add a sign-out step to the public case', feedback)

    def test_city_failure_also_checks_dates_without_changing_required_facts(self):
        feedback = guidance(
            'REQ-2.1: trim surrounding whitespace in search criteria and still show results: '
            'Strict visibility text Shanghai must resolve once: expected 2 to be 1', 'REQ-2.1')
        self.assertIn('Visible example dates, result headings and train-card dates', feedback)
        self.assertIn('every remaining published expression', feedback)
        self.assertIn('keep required facts visible', feedback)
        self.assertNotIn('complete nested payload schema', feedback)

    def test_focused_login_guidance_does_not_route_a_different_case_to_booking_changes(self):
        feedback = guidance(
            'REQ-1.2: login: Strict visibility role link name=/login/i must resolve once: '
            'expected +0 to be 1\n'
            'REQ-3.2: booking: Strict visibility text /confirmed/i must resolve once: '
            'expected +0 to be 1', 'REQ-1.2')
        self.assertIn('sign-out setup', feedback)
        self.assertNotIn('REQ-3.2:', feedback)
        self.assertNotIn('complete nested payload schema', feedback)
        self.assertNotIn('storage helpers are actually called', feedback)
        self.assertNotIn('another account in the same browser', feedback)


if __name__ == '__main__':
    unittest.main()

import unittest
from unittest.mock import patch

from agent.contract_checks import observed_failure_context


class FailureDOMFeedbackTests(unittest.TestCase):
    def test_preserves_real_alert_and_reached_review_heading(self):
        message = ('expected 0 to be 1\nIgnored nodes: comments, script, style\n'
                   '\x1b[36m<html><body><h2>Please confirm the following information</h2>'
                   '<p role="alert" class="field-error">Please correct the highlighted fields.</p>'
                   '<input value="secret-password"><button>Confirm</button></body></html>\x1b[39m')
        result = observed_failure_context([message])
        self.assertIn('Please correct the highlighted fields.', result)
        self.assertIn('Please confirm the following information', result)
        self.assertNotIn('secret-password', result)
        self.assertNotIn('HTTP', result)

    def test_signed_in_navigation_is_observation_not_a_credentials_claim(self):
        result = observed_failure_context(['Ignored nodes: style\n<body><header>'
            '<a href="/">Sign out</a><span>some-user</span></header></body>'])
        self.assertIn('auth_links=["Sign out"]', result)
        self.assertNotIn('some-user', result)
        self.assertNotIn('Login failed', result)

    def test_nested_markup_and_entities_are_read_as_text(self):
        result = observed_failure_context(['Ignored nodes: style\n<body>'
            '<p data-field-error="journey">Choose <strong>a journey</strong> &amp; date</p></body>'])
        self.assertIn('Choose a journey & date', result)

    def test_no_dump_does_not_invent_feedback(self):
        self.assertEqual(observed_failure_context(['expect count to be 1']), '')

    def test_valueless_attribute_does_not_discard_real_error(self):
        result = observed_failure_context(['Ignored nodes: style\n<body><div class>'
            '<p role="alert">Real message</p></div></body>'])
        self.assertIn('Real message', result)

    def test_optional_parser_failure_does_not_replace_case_verdicts(self):
        with patch('agent.contract_checks._FailureDOM.feed', side_effect=RuntimeError('damaged DOM')):
            self.assertEqual(observed_failure_context(['Ignored nodes: style\n<body>']), '')

    def test_context_is_bounded_and_prioritizes_actual_errors(self):
        message = 'Ignored nodes: style\n<body>' + ''.join(
            f'<p role="alert">error{i} ' + 'x' * 200 + '</p>' for i in range(10)) + '</body>'
        result = observed_failure_context([message])
        self.assertLessEqual(len(result), 280)
        self.assertIn('error0', result)
        self.assertNotIn('error9', result)


if __name__ == '__main__':
    unittest.main()

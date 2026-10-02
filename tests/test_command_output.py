import unittest

from agent.command_output import compact_command_output, failure_details


class CommandOutputTests(unittest.TestCase):
    def test_failure_details_exclude_passing_requirement_ids(self):
        text = (' ✓ tests/auth.test.jsx > REQ-1 login passed 10ms\n'
                ' ✓ tests/booking.test.jsx > REQ-3 booking passed 20ms\n'
                ' FAIL tests/search.test.jsx > REQ-2 selected train\n'
                'TestingLibraryElementError: missing train-summary\n'
                ' ❯ tests/search.test.jsx:180:32\n'
                '⎯⎯⎯[1/1]⎯\nTests 1 failed | 2 passed\n')
        output = failure_details(text)
        self.assertIn('REQ-2', output)
        self.assertIn('search.test.jsx:180:32', output)
        self.assertNotIn('REQ-1', output)
        self.assertNotIn('REQ-3', output)

    def test_preserves_validation_error_buried_inside_large_dom(self):
        text = "FAIL registration\nUnable to find signed-in header\nIgnored nodes: comments, script, style\n<body>\n"
        text += '<div class="irrelevant">\n' * 3000
        text += '\x1b[0mName must be 2-100 characters with no spaces\x1b[0m\n'
        text += '</div>\n' * 3000 + '</body>\n ❯ app.test.jsx:175:9\n Tests 1 failed | 12 passed\n'
        output = compact_command_output(text, 1000)
        self.assertIn('Name must be 2-100 characters with no spaces', output)
        self.assertIn('Unable to find signed-in header', output)
        self.assertIn('app.test.jsx:175:9', output)
        self.assertIn('Tests 1 failed | 12 passed', output)
        self.assertNotIn('<div', output)
        self.assertNotIn('\x1b', output)

    def test_preserves_ordinary_build_failure_and_tail_within_limit(self):
        text = 'Error: missing import\n' + 'build detail\n' * 1000 + 'Build failed: module.js:42'
        output = compact_command_output(text, 1000)
        self.assertIn('Error: missing import', output)
        self.assertIn('Build failed: module.js:42', output)
        self.assertLess(len(output), 1100)

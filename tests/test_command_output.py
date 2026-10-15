import unittest

from agent.command_output import compact_command_output, failure_details


class CommandOutputTests(unittest.TestCase):
    def test_http_assertion_keeps_server_root_cause_and_relevant_stack(self):
        text = (
            'stderr | tests/api.test.js > creates booking\n'
            'TypeError: db.transaction is not a function\n'
            '    at createBooking (/project/backend/server.js:89:14)\n'
            '    at Layer.handle (/project/node_modules/express/router.js:95:5)\n'
            'environment: { PRIVATE_TOKEN: "do-not-copy" }\n'
            ' ✓ tests/api.test.js > REQ-1 health check\n'
            ' FAIL tests/api.test.js > REQ-3 creates booking\n'
            'AssertionError: expected 500 to be 201\n'
            ' ❯ tests/api.test.js:52:29\n'
            '⎯⎯⎯[1/1]⎯\n'
        )
        output = failure_details(compact_command_output(text, 10000))
        self.assertIn('db.transaction is not a function', output)
        self.assertIn('/project/backend/server.js:89:14', output)
        self.assertIn('expected 500 to be 201', output)
        self.assertIn('REQ-3', output)
        self.assertNotIn('REQ-1', output)
        self.assertNotIn('PRIVATE_TOKEN', output)

    def test_unhandled_error_after_failure_keeps_dom_validation(self):
        text = (
            ' FAIL tests/form.test.jsx > REQ-2 submits form\n'
            'TestingLibraryElementError: missing success message\n'
            'Ignored nodes: comments, script, style\n<body>\n'
            '<div>\nName is required\n</div>\n</body>\n'
            ' ❯ tests/form.test.jsx:30:3\n'
            '⎯⎯⎯[1/1]⎯\n'
            '⎯⎯⎯⎯ Unhandled Rejection ⎯⎯⎯⎯\n'
            'ReferenceError: session is not defined\n'
            ' ❯ submit src/form.jsx:44:12\n'
        )
        output = failure_details(compact_command_output(text, 10000))
        self.assertIn('Name is required', output)
        self.assertIn('missing success message', output)
        self.assertIn('ReferenceError: session is not defined', output)
        self.assertIn('src/form.jsx:44:12', output)
        self.assertNotIn('<body>', output)

    def test_repeated_runtime_logs_cannot_crowd_out_failure_block(self):
        error = ('TypeError: database unavailable\n'
                 '    at createBooking (/project/server.js:89:14)\n')
        text = error * 500
        text += 'ReferenceError: retryState is not defined\n'
        text += ''.join(f'    at caller{i} (/project/server.js:{i}:1)\n' for i in range(500))
        text += (' FAIL tests/api.test.js > REQ-3 creates booking\n'
                 'AssertionError: expected 500 to be 201\n⎯⎯⎯[1/1]⎯\n')
        output = failure_details(text)
        self.assertEqual(output.count('TypeError: database unavailable'), 1)
        self.assertIn('ReferenceError: retryState is not defined', output)
        self.assertIn('expected 500 to be 201', output)
        self.assertLess(len(output), 2500)

    def test_oversized_runtime_messages_and_stacks_have_a_shared_budget(self):
        text = ''
        for index in range(50):
            text += f'TypeError: failure {index} ' + 'x' * 10000 + '\n'
            text += '    at handler (/project/' + 'long/' * 1000 + 'server.js:4:2)\n'
        text += (' FAIL tests/api.test.js > REQ-3 creates booking\n'
                 'AssertionError: expected 500 to be 201\n⎯⎯⎯[1/1]⎯\n')
        output = failure_details(text)
        self.assertIn('TypeError: failure 0', output)
        self.assertIn('expected 500 to be 201', output)
        self.assertLess(len(output), 2500)

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

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.summarize_token_study import trace_diagnostics, summarize


def script(name, code=0, error=''):
    return {'name':'run_project_script', 'result':json.dumps({
        'command':'npm run '+name, 'exit_code':code, 'error':error})}


class TokenStudySummaryTests(unittest.TestCase):
    def trace(self, events):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'trace.jsonl'
            path.write_text(''.join(json.dumps(e)+'\n' for e in events),encoding='utf-8')
            return trace_diagnostics(path)

    def test_checks_across_an_edit_do_not_establish_a_passing_revision(self):
        result=self.trace([script('build'),{'name':'write_file'},script('test')])
        self.assertIsNone(result['first_passing_build_and_test_tool_call'])
        self.assertIsNone(result['tool_calls_after_first_passing_build_and_test'])

    def test_counts_post_acceptance_actions_and_nonexistent_scripts(self):
        result=self.trace([script('build'),script('test'),{'name':'read_file'},
                           script('check',1,'package.json does not define script `check`'),
                           {'name':'replace_text','error_type':'ValueError'}])
        self.assertEqual(result['first_passing_build_and_test_tool_call'],2)
        self.assertEqual(result['tool_calls_after_first_passing_build_and_test'],3)
        self.assertEqual(result['failed_script_invocations'],1)
        self.assertEqual(result['nonexistent_script_invocations'],1)
        self.assertEqual(result['rejected_tool_calls'],1)

    def test_unknown_usage_cannot_be_summarized_as_zero(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)
            (path/'token-usage.json').write_text(json.dumps({'requests':[{'status':'reserved'}]}))
            with self.assertRaisesRegex(ValueError,'Unknown usage'):
                summarize(path)


if __name__=='__main__':
    unittest.main()

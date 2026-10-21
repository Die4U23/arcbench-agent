import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent.tool_context import initial_file_inventory, failure_source_context
from agent.tools import ProjectTools
from tests.test_llm_budget import _FakeMessage, _FakeToolCall, _make_client


class ToolContextTests(unittest.TestCase):
    def test_inventory_bounds_and_path_policy_without_reading_source(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'allowed.js').write_text('body must not be exposed')
            (root / '.env').write_text('must not be exposed')
            (root / 'credentials.json').write_text('must not be exposed')
            tools = ProjectTools(root)
            inventory = initial_file_inventory(tools)
            self.assertIn('allowed.js', inventory)
            self.assertNotIn('.env', inventory)
            self.assertNotIn('credentials.json', inventory)
            self.assertNotIn('body must', inventory)
            for i in range(120):
                (root / f'f{i}.js').touch()
            self.assertLess(len(initial_file_inventory(tools)), 4500)

    def test_failure_retains_original_report_and_quotes_the_actual_assertion(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'acceptance.cjs').write_text('header\nassert.equal(discount(101,25),75);\nfooter\n')
            tools = ProjectTools(root)
            result = {'results': [{'exit_code': 1, 'output': '25 != 75 at (/work/acceptance.cjs:2:8)'}]}
            enriched = json.loads(failure_source_context('run_project_scripts', json.dumps(result), tools))
            self.assertEqual(enriched['results'], result['results'])
            self.assertIn('assert.equal(discount(101,25),75)', enriched['failure_source_context'][0]['content'])
            self.assertEqual(enriched['failure_source_context'][0]['failure_line'], 2)
            self.assertEqual(tools.changed_paths, [])

    def test_success_unknown_paths_and_protected_paths_add_no_context(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.env').write_text('secret')
            tools = ProjectTools(root)
            for output in ('at (/work/.env:1:1)', 'at (/work/../outside.js:1:1)',
                           'at (/work/.arc/test.js:1:1)', 'at (/elsewhere/test.js:1:1)',
                           'at (/work/missing.js:1:1)'):
                result = json.dumps({'exit_code': 1, 'output': output})
                self.assertEqual(failure_source_context('run_project_script', result, tools), result)
            result = json.dumps({'exit_code': 0, 'output': 'at (/work/.env:1:1)'})
            self.assertEqual(failure_source_context('run_project_script', result, tools), result)

    def test_explicit_read_remains_available_after_initial_inventory(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'source.js').write_text('exact current body')
            read = _FakeToolCall('read', 'read_file', '{"path":"source.js"}')
            model = _make_client(None, None, [_FakeMessage([read]), _FakeMessage(None)])
            model.implement(task_type='library', subtree={'id':'R'}, plan='repair', project_tools=ProjectTools(root))
            requests = model.client.completions.requests
            self.assertIn('source.js', requests[0]['messages'][-1]['content'])
            self.assertEqual(requests[1]['messages'][-1]['content'], 'exact current body')


if __name__ == '__main__':
    unittest.main()

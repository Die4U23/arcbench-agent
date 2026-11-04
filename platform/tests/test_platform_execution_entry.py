"""Exercise the real entry/model/tools pipeline with a scripted provider."""
from pathlib import Path
import json
import os
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'platform-agent'))
sys.path.insert(0, str(ROOT / 'platform-agent/arcbench-agent-runtime/src'))
from agent import platform_generation as entry
from agent.platform_generation import StaticProjectTools
from test_platform_execution import Session, call, response, step


class ExecutionEntryTests(unittest.TestCase):
    def run_task(self, mode, title, existing):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task, app = root / 'task', root / 'app'
            task.mkdir()
            (task / 'requirements.yaml').write_text(
                'id: ROOT\nname: ' + title + '\ntype: FOLDER\nenvironment: {}\ndata: []\nchildren:\n'
                '  - id: ITEM-X\n    name: Workspace\n    type: ATOMIC\n'
                '    description: Provide the specified workspace.\n    scenarios: []\n    children: []\n', encoding='utf-8')
            if existing:
                app.mkdir()
                (app / 'package.json').write_text('{"scripts":{"start":"node app.js"}}')
                (app / 'deploy.sh').write_text('#!/bin/sh\nexec node app.js\n')
                (app / 'app.js').write_text('console.log("starter");\n')
            session = Session([])
            session.model = 'unfamiliar-model'
            executed = []

            def provider(stage, **kwargs):
                session.calls.append((stage, kwargs))
                if 'Plan the supplied task' in kwargs['messages'][0]['content']:
                    return response({'steps': [step('build', ('ITEM-X',))]})
                coverage = json.loads((app / 'docs/coverage.json').read_text(encoding='utf-8'))
                for item in coverage['items']:
                    item.update(status='covered', files=['app.js' if existing else 'src/App.jsx'])
                identity = 'edit-' + str(len(session.calls))
                executed.append(identity)
                return response(calls=[call(identity, 'write_files', files=[{
                    'path': 'docs/coverage.json', 'content': json.dumps(coverage)}]),
                    call(identity + '-validate', 'validate_source')])

            session._create_completion = provider
            environment = {key: value for key, value in os.environ.items() if not key.startswith('ARC_')}
            environment.update(MODEL=session.model, OPENAI_BASE_URL='https://offline.invalid/v1',
                ARC_EXECUTION_MODE=mode, ARC_RUNTIME_CHECK='0', ARC_MAX_INPUT_TOKENS='200000',
                ARC_MAX_OUTPUT_TOKENS='1000', ARC_TOKEN_LIMIT='2000000')
            runtime = NS(events=Mock(), traceability=Mock())
            with patch.dict(os.environ, environment, clear=True), \
                 patch.object(entry, 'ModelClient', return_value=session), \
                 patch('arcbench_agent_runtime.AgentRuntime.from_env', return_value=runtime), \
                 patch.object(sys, 'argv', ['main.py', str(task), '--output-dir', str(app)]):
                self.assertEqual(entry.main(), 0)
            actions_file = next((app / '.arc/platform-budget').rglob('actions.json'))
            actions = json.loads(actions_file.read_text(encoding='utf-8'))
            initial = next(item['result'] for item in actions if item['stage'] == 'implement')
            self.assertEqual(initial['status'], 'complete', initial)
            if mode == 'plan_execute':
                self.assertEqual(initial['completed_steps'][0]['id'], 'build')
            else:
                self.assertNotIn('completed_steps', initial)
            self.assertGreater(len(executed), 0)
            self.assertLessEqual(len(session.calls), 4)
            self.assertEqual((app / 'app.js').exists(), existing)
            self.assertEqual((app / 'src/App.jsx').exists(), not existing)
            ledger = json.loads(next((app / '.arc/platform-budget').rglob('tokens.json')).read_text())
            self.assertEqual(ledger['requests'], len(session.calls))
            self.assertIsNone(ledger['pending'])

    def test_real_entry_both_modes_and_unfamiliar_layouts(self):
        for mode in ('react', 'plan_execute'):
            for title, existing in (('Copper Workspace', False), ('雨水工作台', True)):
                with self.subTest(mode=mode, title=title, existing=existing):
                    self.run_task(mode, title, existing)

    def test_model_cannot_rewrite_requirements_or_controller_ir(self):
        with tempfile.TemporaryDirectory() as temp:
            tools = StaticProjectTools(Path(temp))
            for path in ('docs/task-ir.json', 'requirements/source.yaml', 'tests/case.spec.ts', '.arc/tokens.json'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    tools.call('write_files', {'files': [{'path': path, 'content': '{}'}]})


if __name__ == '__main__':
    unittest.main()

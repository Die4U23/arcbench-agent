import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from agent.tools import ProjectTools
from tests.test_llm_budget import _FakeMessage, _FakeToolCall, _make_client, _write_call


ENV = {'MODEL':'deepseek-v4-pro', 'OPENAI_BASE_URL':'https://api.deepseek.com',
       'ARCBENCH_MAX_TOTAL_TOKENS':'10000000', 'ARCBENCH_MAX_MODEL_REQUESTS':'250'}


class ProEditOutputTests(unittest.TestCase):
    def implement(self, model, root):
        return model.implement(task_type='web',subtree={'id':'ROOT','description':'Original contract'},
            plan='Small complete edit',project_tools=ProjectTools(root),reference_dir=root)

    def test_complete_nonthinking_edit_between_old_and_new_caps_can_execute(self):
        model = _make_client(1,2,[_FakeMessage([_write_call('complete.jsx')])],
            dict(ENV, ARCBENCH_IMPLEMENTATION_THINKING='disabled'))
        create = model.client.completions.create
        def provider(**kwargs):
            response = create(**kwargs)
            # Provider fixture: this complete edit needs 16K output allowance.
            response.choices[0].finish_reason = 'tool_calls' if kwargs['max_tokens'] >= 16000 else 'length'
            response.usage = SimpleNamespace(prompt_tokens=100,completion_tokens=16000)
            return response
        model.client.completions.create = provider
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.implement(model,root)
            self.assertTrue((root/'complete.jsx').exists())
        self.assertEqual(model.max_total_tokens,10000000)
        self.assertEqual(model.max_model_requests,250)
        self.assertEqual(model.completion_tokens_used,16000)

    def test_named_edit_choice_retains_room_after_inspection(self):
        inspect = _FakeToolCall('list','list_files',json.dumps({'path':'.'}))
        write = _FakeToolCall('write','write_files',json.dumps({'files':[{'path':'small.jsx','content':'// fixture'}]}))
        model = _make_client(None,None,[*[_FakeMessage([inspect]) for _ in range(5)],
            _FakeMessage([write]),_FakeMessage(None,'done')],dict(ENV,ARCBENCH_MAX_IDLE_TOOL_TURNS='8',
                ARCBENCH_IMPLEMENTATION_THINKING='disabled'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.implement(model,root)
            self.assertTrue((root/'small.jsx').exists())
        request=model.client.completions.requests[4]
        self.assertEqual(request['tool_choice']['function']['name'],'write_files')
        self.assertEqual(request['extra_body']['thinking']['type'],'disabled')
        self.assertGreaterEqual(request['max_tokens'],16000)

    def test_flash_proxy_and_visual_override_keep_previous_output_policy(self):
        for env in (dict(ENV,MODEL='deepseek-flash'),dict(ENV,OPENAI_BASE_URL='https://proxy.example/v1'),
                    dict(ENV,ARCBENCH_VISUAL_IMPLEMENTATION='enabled',VISUAL_MODEL='deepseek-flash')):
            with self.subTest(env=env):
                model=_make_client(1,1,[],env)
                actual='deepseek-flash' if env.get('VISUAL_MODEL') else env['MODEL']
                self.assertEqual(model._edit_output_cap(actual),12000)

    def test_truncated_response_still_discards_tools_and_logs_only_shape(self):
        private='PRIVATE_TOOL_PAYLOAD_SENTINEL'
        call=_FakeToolCall('private','write_file',json.dumps({'path':'partial.jsx','content':private}))
        model=_make_client(1,2,[_FakeMessage([call],content=private,finish_reason='length')],
            dict(ENV,ARCBENCH_IMPLEMENTATION_THINKING='disabled'))
        with tempfile.TemporaryDirectory() as directory, self.assertLogs('agent.llm',level='WARNING') as logs:
            root=Path(directory)
            self.implement(model,root)
            self.assertFalse((root/'partial.jsx').exists())
        self.assertIn('Discarded output shape:', '\n'.join(logs.output))
        self.assertNotIn(private,'\n'.join(logs.output))
        self.assertEqual(model.model_requests_used,1)


if __name__ == '__main__':
    unittest.main()

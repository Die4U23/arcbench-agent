from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from agent.tools import ProjectTools
from tests.test_llm_budget import _make_client, _FakeMessage, _write_call


ENV={'MODEL':'deepseek-v4-pro','VISUAL_MODEL':'deepseek-flash',
     'OPENAI_BASE_URL':'https://api.deepseek.com'}
IMAGE={'type':'image_url','image_url':{'url':'data:image/png;base64,OFFLINE_REFERENCE'}}


class ProVisualRoutingTests(unittest.TestCase):
    def test_visual_plan_uses_flash_but_code_tools_use_pro_with_the_plan(self):
        model=_make_client(2,2,[_FakeMessage(None,'Visual checklist: two-column layout'),
            _FakeMessage([_write_call('component.jsx')]),_FakeMessage(None,'done')],ENV)
        with tempfile.TemporaryDirectory() as directory, patch.object(model,'_visual_inputs',return_value=[IMAGE]):
            plan=model.plan('web',{'id':'ROOT','description':'original requirement'},reference_dir=Path(directory))
            model.implement(task_type='web',subtree={'id':'ROOT'},plan=plan,
                project_tools=ProjectTools(Path(directory)),reference_dir=Path(directory))
        requests=model.client.completions.requests
        self.assertEqual(requests[0]['model'],'deepseek-flash')
        self.assertIn('OFFLINE_REFERENCE',json.dumps(requests[0]['messages']))
        self.assertGreater(len(requests),1)
        for request in requests[1:]:
            self.assertEqual(request['model'],'deepseek-v4-pro')
            self.assertNotIn('OFFLINE_REFERENCE',json.dumps(request['messages']))
            self.assertIn('two-column layout',json.dumps(request['messages']))

    def test_explicit_visual_implementation_override_still_routes_attached_images(self):
        environment=dict(ENV,ARCBENCH_VISUAL_IMPLEMENTATION='enabled')
        model=_make_client(1,1,[_FakeMessage([_write_call('component.jsx')])],environment)
        with tempfile.TemporaryDirectory() as directory, patch.object(model,'_visual_inputs',return_value=[IMAGE]):
            model.implement(task_type='web',subtree={'id':'ROOT'},plan='plan',
                project_tools=ProjectTools(Path(directory)),reference_dir=Path(directory))
        request=model.client.completions.requests[0]
        self.assertEqual(request['model'],'deepseek-flash')
        self.assertIn('OFFLINE_REFERENCE',json.dumps(request['messages']))

    def test_flash_only_and_proxy_defaults_keep_existing_visual_behavior(self):
        for environment in (dict(ENV,MODEL='deepseek-flash'),
                            dict(ENV,OPENAI_BASE_URL='https://proxy.example/v1')):
            with self.subTest(environment=environment):
                model=_make_client(1,1,[],environment)
                self.assertEqual(model.visual_implementation,'enabled')


if __name__=='__main__':
    unittest.main()

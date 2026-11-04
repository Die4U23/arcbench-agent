from pathlib import Path
import json,sys,tempfile,unittest
from unittest.mock import patch
root=Path(__file__).resolve().parent
sys.path.insert(0,str(root/'platform-agent'))
sys.path.insert(0,str(root/'platform-agent/arcbench-agent-runtime/src'))
from agent.competition_model import same_campaign_model
from agent.platform_generation import PlatformTokenBudget,PlatformGenerationModel,StaticProjectTools
from agent.campaign_budget import CampaignBudgetStopped

def write_acceptance_docs(app):
    docs=Path(app)/'docs'
    docs.mkdir(parents=True, exist_ok=True)
    (docs/'task-ir.json').write_text('{"work_items":[],"ui_elements":[],"scenarios":[]}\n',encoding='utf-8')
    (docs/'coverage.json').write_text('{"items":[]}\n',encoding='utf-8')

class PlatformGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'budget.json'
    def budget(self,**kw):
        return PlatformTokenBudget(self.path,model='deepseek-flash',endpoint='https://runner.example/api',limit=2000000,**kw)
    def test_cap_reserved_before_request_and_unknown_kept(self):
        budget=self.budget()
        identity=budget.reserve('implement',1048576,16384)
        with self.assertRaises(CampaignBudgetStopped): budget.reserve('implement',1048576,16384)
        budget.freeze(identity,'interrupted')
        self.assertIsNotNone(json.loads(self.path.read_text())['pending'])
        with self.assertRaises(CampaignBudgetStopped): budget.reserve('implement',1048576,16384)
        with self.assertRaises(FileExistsError): self.budget()
    def test_usage_settlement_and_cumulative_cap(self):
        budget=self.budget()
        identity=budget.reserve('implement',1048576,16384)
        budget.settle(identity,dict(prompt_tokens=1000000,completion_tokens=10000))
        self.assertEqual(budget.state['used'],1010000)
        with self.assertRaises(CampaignBudgetStopped): budget.reserve('implement',1048576,16384)
    def test_proxy_flash_alias_is_the_same_campaign_model(self):
        self.assertTrue(same_campaign_model('deepseek-v4-flash', 'deepseek-flash'))
        self.assertFalse(same_campaign_model('deepseek-v4-flash', 'deepseek-v4-pro'))
    def test_model_and_usage_bounds_rejected(self):
        with self.assertRaises(ValueError):
            PlatformTokenBudget(self.path,model='deepseek-v4-pro',endpoint='https://runner.example',limit=2000000)
        budget=self.budget()
        identity=budget.reserve('implement',1048576,16384)
        with self.assertRaises(CampaignBudgetStopped): budget.settle(identity,dict(prompt_tokens=3))
        self.assertEqual(budget.state['stopped'],'usage_unknown_or_out_of_bounds')
    def test_tools_cannot_execute_application(self):
        tools=StaticProjectTools(Path(self.temp.name))
        with patch('subprocess.run',side_effect=AssertionError('execution forbidden')):
            for name in ('run_project_script','run_project_scripts'):
                with self.assertRaises(ValueError): tools.call(name,{})
        from types import SimpleNamespace
        model=PlatformGenerationModel(SimpleNamespace(),context_window_tokens=100000)
        names={s['function']['name'] for s in model._tool_schemas('implement')}
        self.assertFalse(names & {'run_project_script','run_project_scripts'})
        self.assertIn('write_files',names)

    def test_complete_existing_file_write_is_accepted_without_retry(self):
        app=Path(self.temp.name)/'rewrite';app.mkdir()
        original='// retained source\n'*400+'export const value=1;\n'
        (app/'main.js').write_text(original,encoding='utf-8')
        tools=StaticProjectTools(app)
        updated=original.replace('value=1','value=2')
        tools.call('write_files',{'files':[{'path':'main.js','content':updated}]})
        self.assertEqual((app/'main.js').read_text(encoding='utf-8'),updated)

    def test_generic_scaffold_is_preserved_and_has_no_business_source(self):
        from agent.platform_scaffold import prepare_scaffold,DEPLOY,SCAFFOLD_FILES
        app=Path(self.temp.name)/'fresh'
        self.assertTrue(prepare_scaffold(app))
        created={p.relative_to(app).as_posix() for p in app.rglob('*') if p.is_file()}
        self.assertEqual(created,set(SCAFFOLD_FILES))
        # Neutral wiring only: no routes, UI content, seed records or domain words.
        self.assertEqual((app/'server/routes/index.js').read_text(encoding='utf-8').strip(),
                         "import { Router } from 'express';\n\nexport const router = Router();\n\nexport default router;")
        self.assertFalse(list((app/'seed').glob('*.json')))
        import re
        for name in created:
            text=(app/name).read_text(encoding='utf-8').lower()
            for word in ('bookstack','prestashop','ctrip','12306','stack overflow','shelf','booking','station','train'):
                self.assertIsNone(re.search(r'\b'+re.escape(word)+r'\b',text),f'{word} in scaffold {name}')
        self.assertFalse(prepare_scaffold(app))
        tools=StaticProjectTools(app)
        with self.assertRaisesRegex(ValueError,'immutable'):
            tools.call('write_files',{'files':[{'path':'deploy.sh','content':'changed'}]})
        self.assertEqual((app/'deploy.sh').read_text(encoding='utf-8'),DEPLOY)
        existing=Path(self.temp.name)/'existing';existing.mkdir()
        (existing/'package.json').write_text('{"name":"keep-existing"}')
        self.assertFalse(prepare_scaffold(existing))
        self.assertFalse((existing/'deploy.sh').exists())

    def test_passing_source_is_sealed_against_later_edits(self):
        app=Path(self.temp.name)/'sealed';app.mkdir()
        (app/'package.json').write_text('{}')
        (app/'deploy.sh').write_text('exec node main.js')
        (app/'main.js').write_text('console.log("ready")')
        write_acceptance_docs(app)
        tools=StaticProjectTools(app)
        self.assertTrue(json.loads(tools.call('validate_source',{}))['passed'])
        for name,args in [('write_files',{'files':[{'path':'main.js','content':'broken'}]}),
                          ('replace_text',{'path':'main.js','old_text':'ready','new_text':'changed'})]:
            with self.assertRaisesRegex(ValueError,'sealed'):
                tools.call(name,args)
        self.assertEqual((app/'main.js').read_text(),'console.log("ready")')
    def test_all_six_registered_tests_and_requirements_load(self):
        from agent.platform_inputs import bundle_kwargs,resolve_task_inputs
        from agent.task_bundle import load_task_bundle
        from agent.competition_context import assemble_context,build_work_plan,global_contract_schema
        task=Path(self.temp.name)/'harbor'
        task.mkdir()
        (task/'requirements.yaml').write_text(
            'id: ROOT\nname: Harbor Atlas\ntype: FOLDER\ndescription: A synthetic map catalog.\n'
            'children:\n  - id: REQ-1\n    name: Show the atlas\n    type: ATOMIC\n'
            '    description: Show the opening page.\n    scenarios:\n      - id: SC-1\n'
            '        name: Open\n        steps:\n          - Given: A visitor arrives.\n'
            '            When: The page opens.\n            Then: The atlas is visible.\n'
            '    children: []\n',
            encoding='utf-8')
        tests=task/'tests'
        tests.mkdir()
        (tests/'open.spec.ts').write_text("test('opens the atlas', async () => {});\n", encoding='utf-8')
        inputs=resolve_task_inputs(task, registry=[], packaged_root=task/'missing-public-inputs')
        self.assertIsNone(inputs.registry_entry)
        self.assertEqual(inputs.tests_source,'input')
        self.assertEqual(inputs.task_id,'harbor-atlas')
        bundle=load_task_bundle(**bundle_kwargs(inputs))
        work=build_work_plan(bundle)
        self.assertTrue(work)
        packet=assemble_context(bundle,work[0],Path(self.temp.name),48000,global_contract=global_contract_schema(bundle),source_paths=[])
        self.assertIn('harbor-atlas',packet.text)
        self.assertIn('REQ-1',packet.text)

    def test_official_shaped_entry_delivers_without_claiming_tests_passed(self):
        import os
        import agent.platform_generation as module
        from types import SimpleNamespace
        from agent.competition_model import _DESIGN_CONTRACT
        temporary=Path(self.temp.name)
        package=temporary/'submission'
        package.mkdir()
        app=temporary/'application'
        app.mkdir()
        (app/'package.json').write_text('{"name":"harbor-atlas","scripts":{"build":"node build.js","start":"node main.js"}}\n',encoding='utf-8')
        (app/'deploy.sh').write_text('npm install --no-audit --no-fund\nnpm run build\nexec npm run start\n',encoding='utf-8')
        (app/'main.js').write_text("console.log('harbor atlas ready')\n",encoding='utf-8')
        requirements=temporary/'task'
        requirements.mkdir()
        (requirements/'requirements.yaml').write_text(
            'id: ROOT\nname: Harbor Atlas\ntype: FOLDER\ndescription: A synthetic map catalog.\n'
            'children:\n  - id: REQ-1\n    name: Show the atlas\n    type: ATOMIC\n'
            '    description: Show "Atlas"(role=heading) on the opening page.\n    scenarios:\n'
            '      - id: SC-1\n        name: Open\n        steps:\n          - Given: A visitor arrives.\n'
            '            When: The page opens.\n            Then: The heading "Atlas"(role=heading) is visible.\n'
            '    children: []\n',
            encoding='utf-8')
        (requirements/'tests').mkdir()
        (requirements/'tests'/'open.spec.ts').write_text("test('opens the atlas', async () => {});\n",encoding='utf-8')
        def implement(packet, tools, feedback=None, request_allowance=None):
            path=app/'docs'/'coverage.json'
            document=json.loads(path.read_text(encoding='utf-8'))
            for item in document['items']:
                item['status']='covered'
                item['files']=['main.js']
            path.write_text(json.dumps(document),encoding='utf-8')
            return dict(status='complete',summary='offline control-flow fixture',changed_paths=['docs/coverage.json'])
        fake=unittest.mock.Mock()
        fake.design.return_value=dict(status='complete',**{k:[] for k in _DESIGN_CONTRACT})
        fake.implement.side_effect=implement
        fake.review_sources.return_value=dict(status='complete',turns=1)
        events=unittest.mock.Mock()
        runtime=SimpleNamespace(events=events,traceability=unittest.mock.Mock())
        with patch.object(module,'__file__',str(package/'agent/platform_generation.py')), \
             patch.object(module,'ModelClient'),patch.object(module,'PlatformGenerationModel',return_value=fake), \
             patch('arcbench_agent_runtime.AgentRuntime.from_env',return_value=runtime), \
             patch.object(sys,'argv',['main.py',str(requirements),'--output-dir',str(app)]), \
             patch.dict(os.environ,{'MODEL':'deepseek-flash','OPENAI_BASE_URL':'https://runner.example/api','ARC_RUNTIME_CHECK':'0'}), \
             patch.object(module.StaticProjectTools,'call',return_value=json.dumps(dict(passed=True,errors=[],formal_status='UNVERIFIED'))), \
             patch.object(module,'run_runtime_check',side_effect=AssertionError('runtime check disabled by settings')), \
             patch('subprocess.run',side_effect=AssertionError('application execution forbidden')):
            self.assertEqual(module.main(),0)
        task_dirs=sorted(path for path in (app/'.arc'/'platform-budget').iterdir() if path.is_dir())
        self.assertEqual(len(task_dirs),1)
        runtime_report=json.loads((task_dirs[0]/'runtime-check.json').read_text(encoding='utf-8'))
        self.assertEqual(runtime_report['skipped'],'disabled_by_settings')
        events.mark_run_completed.assert_called_once()
        events.mark_test_passed.assert_not_called()
        self.assertEqual(fake.implement.call_count,1)
        allowances=[call.kwargs['request_allowance'] for call in fake.review_sources.call_args_list]
        self.assertEqual(allowances,[4,4])
        self.assertIn('accessible-name semantics',fake.review_sources.call_args.kwargs['feedback'])
        self.assertNotIn('LOCATOR_WALKTHROUGH',fake.review_sources.call_args.kwargs['feedback'])
        fake.design.assert_not_called()
        packet=fake.implement.call_args.args[0]
        self.assertIn('ENTIRE task',packet.text)
        self.assertIn('SOURCE requirements/requirements.yaml',packet.text)
        self.assertIn('Detected starter layout: single-package',packet.text)
        self.assertIn('role=heading name=Atlas',packet.text)
        self.assertNotIn('pre-wired full-stack scaffold',packet.text)
        self.assertTrue(packet.text.startswith('SOURCE_COMPLETENESS: ALL_TEXT_SOURCES_INCLUDED\n'))
        self.assertLess(len(packet.text.encode('utf-8')),300000)
        self.assertEqual((app/'main.js').read_text(encoding='utf-8'),"console.log('harbor atlas ready')\n")
        self.assertFalse((app/'src').exists())
        self.assertFalse((app/'docs'/'contract.json').exists())
        from types import SimpleNamespace
        adapter=PlatformGenerationModel(SimpleNamespace(),context_window_tokens=393216)
        adapter._whole_task_visible=True
        names={schema['function']['name'] for schema in adapter._tool_schemas('implement')}
        self.assertEqual(names,{'write_files','replace_text','validate_source'})

    def test_static_validation_never_executes_app_and_finds_import_and_syntax_errors(self):
        from agent.platform_static import validate_source
        app=Path(self.temp.name)/'app';app.mkdir()
        (app/'package.json').write_text('{"scripts":{"start":"node index.js"}}')
        (app/'deploy.sh').write_text('exec node index.js')
        marker=app/'EXECUTED'
        (app/'index.js').write_text("require('node:fs').writeFileSync('EXECUTED','bad'); export const App=()=> <main>hello</main>;")
        write_acceptance_docs(app)
        result=validate_source(app)
        self.assertTrue(result['passed'],result)
        self.assertFalse(marker.exists())
        (app/'index.js').write_text("import Missing from './missing'; export default Missing;")
        result=validate_source(app)
        self.assertFalse(result['passed'])
        self.assertIn('Missing relative import',result['errors'][0]['error'])
        (app/'index.js').write_text('const = broken')
        self.assertFalse(validate_source(app)['passed'])

    def test_runtime_identifier_is_module_scoped_and_ts_emitted_import_resolves(self):
        from agent.platform_static import validate_source
        app=Path(self.temp.name)/'scopes';app.mkdir()
        (app/'package.json').write_text('{}')
        (app/'deploy.sh').write_text('exec node main.js')
        (app/'main.js').write_text("const ROOT=document.getElementById('root'); import './render.js';")
        write_acceptance_docs(app)
        (app/'render.js').write_text("export function render(){ROOT.innerHTML='ready';}")
        result=validate_source(app)
        self.assertTrue(any(e['path']=='render.js' and 'Undeclared runtime identifier ROOT' in e['error'] for e in result['errors']),result)
        (app/'render.js').write_text("const ROOT=document.getElementById('root'); export function render(){ROOT.innerHTML='ready';}")
        (app/'service.ts').write_text("import {value} from './value.js'; export const get=():number=>value;")
        (app/'value.ts').write_text('export const value=1;')
        self.assertTrue(validate_source(app)['passed'])

    def test_fresh_deploy_must_install_build_dependencies(self):
        from agent.platform_static import validate_source
        app=Path(self.temp.name)/'deploy';app.mkdir()
        (app/'package.json').write_text('{"devDependencies":{"vite":"5.4.0"},"scripts":{"build":"vite build"}}')
        (app/'index.js').write_text('console.log("ok")')
        (app/'deploy.sh').write_text('npm run build\nexec node index.js')
        write_acceptance_docs(app)
        self.assertTrue(any('no node_modules' in e['error'] for e in validate_source(app)['errors']))
        (app/'deploy.sh').write_text('npm install --include=dev\nnpm run build\nexec node index.js')
        self.assertTrue(validate_source(app)['passed'])

    def test_passing_static_tool_ends_without_extra_model_request(self):
        from types import SimpleNamespace
        session=SimpleNamespace(tool_calls_used=0,_check_model_budget=unittest.mock.Mock())
        model=PlatformGenerationModel(session,context_window_tokens=100000)
        model._whole_task_visible=True
        call=SimpleNamespace(id='c1',function=SimpleNamespace(name='validate_source',arguments='{}'),model_dump=lambda **kw:dict(id='c1',type='function',function=dict(name='validate_source',arguments='{}')))
        response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='',tool_calls=[call]))])
        packet=SimpleNamespace(text='complete source',repository=None,manifest={})
        tools=SimpleNamespace(call=unittest.mock.Mock(return_value='{"passed":true}'))
        with patch.object(model,'_request',return_value=response) as request,patch.object(model,'_check_context',return_value=True),patch.object(model,'_remember_project_source'):
            result=model._run(packet,tools,stage='competition_implementation',mode='implement',instructions='',max_tokens=100,request_allowance=3,initial_messages=[])
        self.assertEqual(result['status'],'complete')
        self.assertEqual(request.call_count,1)

    def test_candidate_rank_prefers_input_then_compiled_passes(self):
        from agent.platform_generation import candidate_rank
        from agent.platform_runtime_check import RuntimeCheckResult
        def result(input_passed, compiled_passed, failed):
            return RuntimeCheckResult(status='failed', stage='tests', passed=input_passed+compiled_passed, failed=failed,
                signals={'input': {'passed': input_passed}, 'compiled': {'passed': compiled_passed},
                         'authored': {'passed': 0}, 'probes': {'passed': 0}})
        self.assertGreater(candidate_rank(result(2, 0, 9)), candidate_rank(result(1, 9, 0)))
        self.assertGreater(candidate_rank(result(2, 4, 3)), candidate_rank(result(2, 1, 0)))
        self.assertEqual(candidate_rank(result(1, 1, 1))[:5], (True, 1, 1, 0, 0))

if __name__=='__main__':unittest.main()

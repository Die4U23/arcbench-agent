from pathlib import Path
import json, os, re, shutil, subprocess, sys, tempfile, time, unittest
from unittest.mock import patch
root=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(root/'platform-agent'))
from agent import platform_runtime_check as rc
from agent.platform_runtime_check import RuntimeCheckResult,RuntimeFailure,discover_test_env,run_runtime_check

FIXTURES=Path(__file__).resolve().parent/'fixtures'/'runtime_check'
PLAYWRIGHT_CACHE=Path(tempfile.gettempdir())/'arc-runtime-check-playwright-cache'
HAS_NODE=bool(shutil.which('node') and rc.tool('npm'))
STRICT_RUN_IDS=[r'^[a-zA-Z0-9-]{1,20}$',r'^[A-Za-z0-9_-]{1,40}$',r'^[a-z0-9]+$']

def write(path,text):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(text,encoding='utf-8')

def force_kill(pid):
    if rc.pid_alive(pid):
        if rc.WINDOWS: subprocess.run(['taskkill','/T','/F','/PID',str(pid)],capture_output=True)
        else: os.kill(pid,9)

class TempCase(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.dir=Path(self.temp.name)

class DiscoverEnvTests(TempCase):
    def test_generic_name_rules(self):
        write(self.dir/'helpers.ts','''const run = process.env.ALPHA_RUN_ID;
if (!run || !/^[a-zA-Z0-9-]{1,20}$/.test(run)) throw new Error('bad');
const base = process.env.ALPHA_BASE_URL; const pw = process.env.PLAYWRIGHT_BASE_URL ?? info.project.use.baseURL;
const template = process.env.ALPHA_BASE_URL_TEMPLATE; const other = process.env.SOMETHING_ELSE;''')
        write(self.dir/'nested'/'more.js',"const b = process.env['BETA_RUN_ID']; const x = process.env[\"ALPHA_SECOND_BASE_URL\"];")
        write(self.dir/'node_modules'/'ignored.js','process.env.IGNORED_RUN_ID')
        env=discover_test_env(self.dir,'http://127.0.0.1:4000')
        self.assertEqual(set(env),{'ALPHA_RUN_ID','BETA_RUN_ID','ALPHA_BASE_URL','PLAYWRIGHT_BASE_URL'})
        self.assertEqual(env['ALPHA_BASE_URL'],'http://127.0.0.1:4000')
        self.assertEqual(env['PLAYWRIGHT_BASE_URL'],'http://127.0.0.1:4000')
        self.assertEqual(env['ALPHA_RUN_ID'],env['BETA_RUN_ID'])
        for pattern in STRICT_RUN_IDS: self.assertRegex(env['ALPHA_RUN_ID'],pattern)
        self.assertRegex(env['ALPHA_RUN_ID'],r'^[a-z][a-z0-9]{11}$')
        self.assertNotEqual(env['ALPHA_RUN_ID'],discover_test_env(self.dir,'http://x')['ALPHA_RUN_ID'])
    def test_no_references_sets_nothing(self):
        write(self.dir/'a.spec.ts',"test('x', async () => {});")
        self.assertEqual(discover_test_env(self.dir,'http://x'),{})
    def test_public_inputs_follow_generic_rules(self):
        inputs=sorted((root/'platform-agent'/'public-inputs').glob('*/tests'))
        if not inputs: self.skipTest('no public inputs in working copy')
        for tests in inputs:
            text=' '.join(p.read_text(encoding='utf-8',errors='replace') for p in tests.rglob('*.ts'))
            names=set(re.findall(r'process\.env\.([A-Z0-9_]+)',text))
            env=discover_test_env(tests,'http://127.0.0.1:3000')
            for name in names:
                if name.endswith('RUN_ID'):
                    for pattern in STRICT_RUN_IDS: self.assertRegex(env[name],pattern)
                if name.endswith('_TEMPLATE'): self.assertNotIn(name,env)
            self.assertTrue(set(env)<=names)

class ReportParsingTests(unittest.TestCase):
    def test_captured_sample_report(self):
        report=json.loads((FIXTURES/'sample-report.json').read_text(encoding='utf-8'))
        passed,failed,skipped,failures=rc.parse_playwright_report(report)
        self.assertEqual((passed,failed,skipped),(1,1,0))
        self.assertEqual(failures[0].name,'intentionally fails')
        self.assertEqual(failures[0].file,'demo.spec.ts')
        self.assertTrue(failures[0].error.startswith('Error: expect(locator).toBeVisible() failed'))
        self.assertIn('Missing Heading',failures[0].error)
        self.assertNotIn('\x1b',failures[0].error)
    def test_nested_suites_skips_flaky_and_global_errors(self):
        result=lambda message:dict(status='failed',errors=[dict(message=message)])
        spec=lambda title,status,results=():dict(title=title,file='a.spec.ts',tests=[dict(status=status,results=list(results))])
        group=dict(title='group',file='a.spec.ts',specs=[
            spec('one','unexpected',[result('\x1b[31mError: boom\x1b[39m\n'+'x'*3000)]),
            spec('two','skipped'),spec('three','flaky')])
        report=dict(suites=[dict(title='a.spec.ts',file='a.spec.ts',specs=[],suites=[group])],
                    errors=[dict(message='Error: cannot load b.spec.ts',location=dict(file='b.spec.ts'))])
        passed,failed,skipped,failures=rc.parse_playwright_report(report)
        self.assertEqual((passed,failed,skipped),(1,1,1))
        self.assertEqual(failures[0].name,'group > one')
        self.assertTrue(failures[0].error.startswith('Error: boom'))
        self.assertLess(len(failures[0].error),rc.ERROR_LIMIT+50)
        self.assertEqual((failures[1].name,failures[1].file),('(test harness error)','b.spec.ts'))
    def test_feedback_groups_by_first_error_line(self):
        failures=[RuntimeFailure(f't{i}','a.spec.ts',f'Error: same\nLocator: {i}') for i in range(7)]
        failures.append(RuntimeFailure('other','b.spec.ts','Error: different'))
        result=RuntimeCheckResult('failed','tests',passed=2,failed=8,total=10,failures=failures,env_supplied=dict(X_RUN_ID='abc'))
        feedback=result.to_feedback(limit=1)
        json.dumps(feedback)
        self.assertEqual(feedback['counts'],dict(passed=2,failed=8,skipped=0,precondition=0,total=10))
        self.assertEqual(len(feedback['failure_groups']),1)
        group=feedback['failure_groups'][0]
        self.assertEqual((group['error'],group['count'],len(group['tests'])),('Error: same',7,5))
        self.assertEqual(group['tests'][0]['detail'],'Locator: 0')
        self.assertEqual(feedback['omitted_groups'],1)
        self.assertEqual(feedback['env_supplied'],['X_RUN_ID'])
        self.assertIn('log_tail',RuntimeCheckResult('build_failed','build',log_tail='tsc error').to_feedback())

class PreconditionAndConfigTests(TempCase):
    def test_unset_env_failures_are_harness_preconditions(self):
        write(self.dir/'helpers.ts',"export const a = process.env.ALPHA_BASE_URL; export const b = process.env.ALPHA_SECOND_BASE_URL; export const t = process.env.ALPHA_BASE_URL_TEMPLATE;")
        env=discover_test_env(self.dir,'http://127.0.0.1:1')
        unset=sorted(rc.test_env_names(self.dir)-set(env))
        self.assertEqual(unset,['ALPHA_BASE_URL_TEMPLATE','ALPHA_SECOND_BASE_URL'])
        failures=[RuntimeFailure('one','a.spec.ts','Error: ALPHA_SECOND_BASE_URL must be set by the evaluator\n    at x'),
                  RuntimeFailure('two','a.spec.ts','Error: expect(locator).toBeVisible() failed'),
                  RuntimeFailure('three','a.spec.ts','TypeError: process.env is not configured'),
                  RuntimeFailure('four','a.spec.ts','Error: ALPHA_BASE_URL_TEMPLATE_X is fine')]
        app,preconditions=rc.classify_failures(failures,unset)
        self.assertEqual([f.name for f in app],['two','four'])
        self.assertEqual([(f.name,f.kind) for f in preconditions],[('one','harness_precondition'),('three','harness_precondition')])
        result=RuntimeCheckResult('failed','tests',passed=1,failed=len(app),total=4,precondition=len(preconditions),
                                  failures=app,preconditions=preconditions,env_unset=unset)
        feedback=result.to_feedback()
        self.assertEqual(feedback['counts']['failed'],2)
        self.assertEqual(feedback['counts']['precondition'],2)
        self.assertEqual([g['error'] for g in feedback['failure_groups']],['Error: expect(locator).toBeVisible() failed','Error: ALPHA_BASE_URL_TEMPLATE_X is fine'])
        self.assertEqual([item['name'] for item in feedback['harness_preconditions']],['one','three'])
        self.assertEqual(feedback['env_unset'],unset)
        self.assertNotIn('ALPHA_SECOND_BASE_URL must be set',json.dumps(feedback['failure_groups']))
    def test_playwright_config_is_strict_and_utc(self):
        text=rc.playwright_config(self.dir/'tests','http://127.0.0.1:1',self.dir/'r.json',self.dir/'out',2.5)
        config=json.loads(text.removeprefix('module.exports = ').rstrip().rstrip(';'))
        self.assertEqual((config['forbidOnly'],config['workers'],config['retries'],config['timeout']),(True,1,0,2500))
        self.assertEqual(config['use']['timezoneId'],'UTC')
    def test_start_command_prefers_deploy_sh_under_a_real_bash(self):
        write(self.dir/'deploy.sh','exec npm run start\n')
        with patch.object(rc,'usable_bash',return_value='/usr/bin/bash'):
            self.assertEqual(rc.start_command(self.dir,'npm'),(['/usr/bin/bash','deploy.sh'],'deploy.sh'))
        with patch.object(rc,'usable_bash',return_value=None):
            self.assertEqual(rc.start_command(self.dir,'npm'),(['npm','run','start'],'npm start'))
        (self.dir/'deploy.sh').unlink()
        with patch.object(rc,'usable_bash',return_value='/usr/bin/bash'):
            self.assertEqual(rc.start_command(self.dir,'npm')[1],'npm start')
        with patch.object(rc,'WINDOWS',True),patch.object(rc.shutil,'which',return_value=r'C:\Windows\System32\bash.exe'):
            self.assertIsNone(rc.usable_bash())
        with patch.object(rc,'WINDOWS',True),patch.object(rc.shutil,'which',return_value=r'C:\Program Files\Git\bin\bash.exe'):
            self.assertEqual(rc.usable_bash(),r'C:\Program Files\Git\bin\bash.exe')
    def test_stage_tests_copies_minimal_parent_for_external_helpers(self):
        suite=self.dir/'pkg'/'tests'
        write(suite/'flow.spec.ts',"import { A } from '../shared/helpers';\nimport { B } from './local';\ntest('x', () => {});")
        write(suite/'local.ts','export const B = 1;')
        write(self.dir/'pkg'/'shared'/'helpers.ts','export const A = 1;')
        write(self.dir/'pkg'/'node_modules'/'dep'/'index.js','module.exports = 1;')
        write(self.dir/'pkg'/'playwright.config.ts','export default {};')
        self.assertEqual([p.name for p in rc.external_import_targets(suite)],['helpers.ts'])
        staged,error=rc.stage_tests(suite,self.dir/'harness')
        self.assertIsNone(error)
        self.assertEqual(staged,self.dir/'harness'/'suite'/'tests')
        self.assertTrue((self.dir/'harness'/'suite'/'shared'/'helpers.ts').is_file())
        self.assertFalse((self.dir/'harness'/'suite'/'node_modules').exists())
        self.assertFalse((self.dir/'harness'/'suite'/'playwright.config.ts').exists())
        plain=self.dir/'plain'
        write(plain/'a.spec.ts',"import './b'; test('y', () => {});")
        self.assertEqual(rc.stage_tests(plain,self.dir/'harness2'),(self.dir/'harness2'/'tests',None))
        deep=self.dir/'a'/'b'/'c'/'d'/'tests'
        write(deep/'far.spec.ts',"import { Z } from '../../../../../far_helper';")
        write(self.dir/'far_helper.ts','export const Z = 1;')
        staged,error=rc.stage_tests(deep,self.dir/'harness3')
        self.assertIsNone(staged)
        self.assertIn('cannot be staged safely',error)
    def test_safe_rmtree_does_not_follow_links(self):
        keep=self.dir/'keep'
        write(keep/'precious.txt','keep me')
        scratch=self.dir/'scratch'
        write(scratch/'inner'/'file.txt','x')
        link=scratch/'inner'/'linked'
        try:
            if rc.WINDOWS:
                import _winapi
                _winapi.CreateJunction(str(keep),str(link))
            else:
                os.symlink(keep,link,target_is_directory=True)
        except OSError as error:
            self.skipTest(f'cannot create directory link: {error}')
        self.assertTrue((link/'precious.txt').is_file())
        rc.safe_rmtree(scratch)
        self.assertFalse(scratch.exists())
        self.assertTrue((keep/'precious.txt').is_file())

class EnvironmentTests(unittest.TestCase):
    def test_secrets_not_forwarded(self):
        secret='sk-very-secret-value-123'
        fake=dict(PATH=os.environ.get('PATH',''),TEMP='C:/tmp',OPENAI_API_KEY=secret,OPENAI_BASE_URL='http://model',
                  GITHUB_TOKEN=secret,NPM_CONFIG__AUTH=secret,NPM_CONFIG_REGISTRY='https://registry.npmjs.org/',
                  MY_PASSWORD=secret,RANDOM_VAR='x')
        with patch.dict(os.environ,fake,clear=True):
            env=rc.clean_env(dict(DEMO_RUN_ID='abc'))
            self.assertEqual(rc.redact(f'log {secret} end'),'log *** end')
        for name in ('OPENAI_API_KEY','OPENAI_BASE_URL','GITHUB_TOKEN','NPM_CONFIG__AUTH','MY_PASSWORD','RANDOM_VAR'):
            self.assertNotIn(name,env)
        self.assertNotIn(secret,json.dumps(env))
        self.assertEqual(env['DEMO_RUN_ID'],'abc')
        self.assertIn('PATH',env); self.assertIn('TEMP',env); self.assertIn('NPM_CONFIG_REGISTRY',env)
    def test_missing_browsers_path_dropped(self):
        with patch.dict(os.environ,dict(PLAYWRIGHT_BROWSERS_PATH=str(Path(tempfile.gettempdir())/'does-not-exist-xyz'))):
            self.assertNotIn('PLAYWRIGHT_BROWSERS_PATH',rc.clean_env())
    def test_proxy_credentials_dropped_and_redacted(self):
        with patch.dict(os.environ,dict(HTTPS_PROXY='http://u:p@host:8080',HTTP_PROXY='http://plain-host:8080',
                                        DEMO_TOKEN='tok-abcdefgh12345'),clear=False):
            env=rc.clean_env()
            self.assertNotIn('HTTPS_PROXY',env)
            self.assertEqual(env.get('HTTP_PROXY'),'http://plain-host:8080')
            self.assertEqual(rc.redact('proxy http://u:p@host:8080 token tok-abcdefgh12345'),'proxy http://***host:8080 token ***')
            report=dict(suites=[dict(title='a.spec.ts',file='a.spec.ts',specs=[dict(title='leaks',file='a.spec.ts',tests=[dict(status='unexpected',
                results=[dict(status='failed',error=dict(message='Error: header tok-abcdefgh12345 via http://u:p@host:8080'))])])])])
            _,_,_,failures=rc.parse_playwright_report(report)
            self.assertEqual(failures[0].error,'Error: header *** via http://***host:8080')
    def test_child_environment_is_sandboxed(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp)/'home'; home.mkdir()
            rcfile=home/'.npmrc'; rcfile.write_text('',encoding='utf-8')
            env=rc.clean_env(home=home,npm_cache=Path(temp)/'cache',npm_userconfig=rcfile)
            self.assertEqual((env['HOME'],env['USERPROFILE']),(str(home),str(home)))
            self.assertEqual(env['npm_config_cache'],str(Path(temp)/'cache'))
            self.assertEqual(env['npm_config_userconfig'],str(rcfile))
            browsers=rc.default_browsers_path()
            if browsers is not None and 'PLAYWRIGHT_BROWSERS_PATH' not in os.environ:
                self.assertEqual(env['PLAYWRIGHT_BROWSERS_PATH'],str(browsers))

@unittest.skipUnless(HAS_NODE,'node/npm not available')
class ProcessTests(TempCase):
    def copy(self,name):
        target=self.dir/name
        shutil.copytree(FIXTURES/name,target)
        return target
    def test_process_tree_killed(self):
        app=self.copy('spawner')
        port=rc.free_port(); pid_file=self.dir/'pids.json'
        process=rc.spawn([rc.tool('npm'),'run','start'],app,rc.clean_env(dict(PORT=str(port),PID_FILE=str(pid_file))),self.dir/'start.log')
        self.addCleanup(rc.kill_process_tree,process)
        deadline=time.monotonic()+30
        while not (pid_file.exists() and rc.port_open(port)) and time.monotonic()<deadline: time.sleep(0.2)
        pids=json.loads(pid_file.read_text())
        for pid in pids.values(): self.addCleanup(force_kill,pid)
        self.assertTrue(rc.port_open(port))
        self.assertTrue(all(rc.pid_alive(pid) for pid in pids.values()))
        rc.kill_process_tree(process)
        rc.release_port(port)
        deadline=time.monotonic()+10
        while any(rc.pid_alive(pid) for pid in pids.values()) and time.monotonic()<deadline: time.sleep(0.2)
        self.assertFalse(any(rc.pid_alive(pid) for pid in pids.values()))
        self.assertFalse(rc.port_open(port))
    def test_release_port_only_kills_owned_processes(self):
        app=self.copy('spawner')
        port=rc.free_port(); pid_file=self.dir/'pids.json'
        process=rc.spawn([rc.tool('npm'),'run','start'],app,rc.clean_env(dict(PORT=str(port),PID_FILE=str(pid_file))),self.dir/'start.log')
        self.addCleanup(rc.kill_process_tree,process)
        deadline=time.monotonic()+30
        while not (pid_file.exists() and rc.port_open(port)) and time.monotonic()<deadline: time.sleep(0.2)
        pids=json.loads(pid_file.read_text())
        for pid in pids.values(): self.addCleanup(force_kill,pid)
        rc.release_port(port,timeout=1,allowed_pids=set())
        self.assertTrue(rc.port_open(port))
        self.assertTrue(all(rc.pid_alive(pid) for pid in pids.values()))
        owned=rc.kill_process_tree(process)
        self.assertIn(process.pid,owned)
        self.assertTrue(set(pids.values())<=owned or rc.WINDOWS and pids['child'] in owned)
        rc.release_port(port,allowed_pids=owned)
        self.assertFalse(rc.port_open(port))
    def test_missing_project_raises(self):
        with self.assertRaises(FileNotFoundError):
            run_runtime_check(self.dir/'missing',FIXTURES/'specs',work_dir=self.dir/'work')
    def test_failed_install_removes_partial_node_modules(self):
        app=self.copy('app')
        package=json.loads((app/'package.json').read_text(encoding='utf-8'))
        package['scripts']['preinstall']='node -e "process.exit(3)"'
        (app/'package.json').write_text(json.dumps(package),encoding='utf-8')
        result=run_runtime_check(app,FIXTURES/'specs',work_dir=self.dir/'work')
        self.assertEqual((result.status,result.stage),('install_failed','install'))
        self.assertFalse((app/'node_modules').exists())
    def test_shared_deadline_times_out_before_stages(self):
        app=self.copy('app')
        result=run_runtime_check(app,FIXTURES/'specs',work_dir=self.dir/'work',install=False,deadline=time.monotonic()-1)
        self.assertEqual((result.status,result.stage),('timeout','install'))
        result=run_runtime_check(app,FIXTURES/'specs',work_dir=self.dir/'work',install=False,timeouts=dict(build=0.01))
        self.assertEqual((result.status,result.stage),('timeout','build'))
    def test_build_failure_reported(self):
        app=self.copy('app')
        write(app/'build.js',"console.error('compile error: missing module'); process.exit(2);")
        result=run_runtime_check(app,FIXTURES/'specs',work_dir=self.dir/'work',install=False)
        self.assertEqual((result.status,result.stage),('build_failed','build'))
        self.assertIn('compile error: missing module',result.log_tail)
        self.assertFalse((app/'tests').exists())
    def test_start_failure_and_timeout(self):
        app=self.copy('app')
        write(app/'server.js',"console.error('cannot bind'); process.exit(1);")
        result=run_runtime_check(app,FIXTURES/'specs',work_dir=self.dir/'work',install=False,playwright_cache=PLAYWRIGHT_CACHE)
        if result.status=='unavailable': self.skipTest('Playwright unavailable')
        self.assertEqual((result.status,result.stage),('start_failed','start'))
        self.assertIn('cannot bind',result.log_tail)
        write(app/'server.js',"setInterval(() => {}, 1000);")
        result=run_runtime_check(app,FIXTURES/'specs',work_dir=self.dir/'work',install=False,
                                 timeouts=dict(start=3),playwright_cache=PLAYWRIGHT_CACHE)
        self.assertEqual((result.status,result.stage),('timeout','start'))

@unittest.skipUnless(HAS_NODE,'node/npm not available')
class EndToEndTests(TempCase):
    def test_fixture_app_one_pass_one_fail(self):
        app=self.dir/'app'
        shutil.copytree(FIXTURES/'app',app)
        result=run_runtime_check(app,FIXTURES/'specs',work_dir=self.dir/'work',playwright_cache=PLAYWRIGHT_CACHE)
        if result.status=='unavailable':
            self.skipTest('Playwright or Chromium unavailable: '+result.log_tail[-300:])
        self.assertEqual(result.status,'failed',result.log_tail)
        self.assertEqual((result.passed,result.failed,result.skipped,result.total),(1,1,0,2))
        self.assertEqual(result.failures[0].name,'intentionally fails')
        self.assertIn('Missing Heading',result.failures[0].error)
        self.assertEqual(set(result.env_supplied),{'DEMO_RUN_ID','DEMO_BASE_URL'})
        self.assertFalse(rc.port_open(int(result.env_supplied['DEMO_BASE_URL'].rsplit(':',1)[1])))
        self.assertFalse((app/'tests').exists())
        self.assertFalse((app/'playwright.config.cjs').exists())
        feedback=result.to_feedback()
        self.assertEqual(feedback['failure_groups'][0]['error'],'Error: expect(locator).toBeVisible() failed')
        self.assertTrue(any(item.get('path')=='/api/health' and item.get('ok') for item in feedback['api_probes']))
        self.assertEqual(result.launcher,'npm start')
    def test_precondition_failures_and_all_skipped_suites(self):
        app=self.dir/'app'
        shutil.copytree(FIXTURES/'app',app)
        specs=self.dir/'specs'
        write(specs/'pre.spec.ts','''import { test, expect } from '@playwright/test';
test.use({ baseURL: process.env.DEMO_BASE_URL });
test('needs a second instance', async ({ page }) => {
  if (!process.env.DEMO_SECOND_BASE_URL) throw new Error('DEMO_SECOND_BASE_URL must be set by the evaluator');
  await page.goto('/');
});
test('shows the heading', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Runtime Check Demo' })).toBeVisible();
});
''')
        result=run_runtime_check(app,specs,work_dir=self.dir/'work',playwright_cache=PLAYWRIGHT_CACHE)
        if result.status=='unavailable':
            self.skipTest('Playwright or Chromium unavailable: '+result.log_tail[-300:])
        self.assertEqual(result.status,'failed',result.log_tail)
        self.assertEqual((result.passed,result.failed,result.precondition,result.total),(1,0,1,2))
        self.assertEqual(result.env_unset,['DEMO_SECOND_BASE_URL'])
        self.assertEqual(result.preconditions[0].kind,'harness_precondition')
        self.assertFalse(result.failures)
        skipped=self.dir/'skipped'
        write(skipped/'skip.spec.ts','''import { test } from '@playwright/test';
test.skip('never runs', async () => {});
''')
        result=run_runtime_check(app,skipped,work_dir=self.dir/'work',install=False,playwright_cache=PLAYWRIGHT_CACHE)
        self.assertEqual((result.status,result.passed,result.failed,result.skipped),('test_harness_failed',0,0,1))
    def test_smoke_without_public_tests_reports_http(self):
        app=self.dir/'smoke'
        write(app/'package.json','{"scripts":{"start":"node server.js"}}\n')
        write(app/'server.js','''const http=require('node:http');
const port=Number(process.env.PORT||3000);
const host=process.env.HOST||'127.0.0.1';
http.createServer((req,res)=>{
  const path=(req.url||'/').split('?')[0];
  if(path==='/api/health'){res.writeHead(500);res.end('down');return;}
  res.writeHead(200,{'content-type':'text/html'});res.end('<html>ok</html>');
}).listen(port,host);
''')
        failed=run_runtime_check(app,None,work_dir=self.dir/'work-fail',install=False)
        self.assertEqual((failed.status,failed.stage),( 'failed','probe'),failed.log_tail)
        self.assertTrue(any(item.name=='GET /api/health' for item in failed.failures))
        self.assertTrue(any(item.get('path')=='/' and item.get('ok') for item in failed.api_probes))
        write(app/'server.js','''const http=require('node:http');
const port=Number(process.env.PORT||3000);
const host=process.env.HOST||'127.0.0.1';
http.createServer((req,res)=>{
  const path=(req.url||'/').split('?')[0];
  if(path==='/api/health'){res.writeHead(200,{'content-type':'application/json'});res.end('{"ok":true}');return;}
  res.writeHead(200,{'content-type':'text/html'});res.end('<html>ok</html>');
}).listen(port,host);
''')
        passed=run_runtime_check(app,None,work_dir=self.dir/'work-pass',install=False)
        self.assertEqual((passed.status,passed.passed,passed.failed),( 'passed',2,0),passed.log_tail)

if __name__=='__main__':
    unittest.main()

"""Revision matching, multi-suite signals, and diagnostics. No network and no browser."""
from pathlib import Path
import json, os, shutil, unittest
from unittest.mock import patch
root = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(root / 'platform-agent'))
from agent import platform_runtime_check as rc
from agent.platform_runtime_check import run_runtime_check

import tempfile

HAS_NODE = bool(shutil.which('node') and rc.tool('npm'))
SECRET = 'sk-planted-secret-xyz'


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def make_package(root: Path, revision: str, version: str, cli: bool) -> Path:
    """Layout: root/node_modules/@playwright/test and root/node_modules/playwright-core."""
    package = root / 'node_modules' / '@playwright' / 'test'
    package.mkdir(parents=True, exist_ok=True)
    if cli:
        (package / 'cli.js').write_text('// cli\n', encoding='utf-8')
    (package / 'package.json').write_text(json.dumps(dict(name='@playwright/test', version=version)), encoding='utf-8')
    core = package.parent.parent / 'playwright-core'
    core.mkdir(parents=True, exist_ok=True)
    browsers = dict(browsers=[dict(name='chromium', revision=revision)])
    (core / 'browsers.json').write_text(json.dumps(browsers), encoding='utf-8')
    (core / 'package.json').write_text(json.dumps(dict(name='playwright-core', version=version)), encoding='utf-8')
    return package


def browser_dir(root: Path, name: str) -> Path:
    path = root / 'browsers'
    (path / name).mkdir(parents=True, exist_ok=True)
    return path


class TempCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)


class PlaywrightResolutionTests(TempCase):
    def test_revision_match_selects_preinstalled_package(self):
        browsers = browser_dir(self.dir, 'chromium-100')
        pre = make_package(self.dir / 'pre', '100', '1.1.1', cli=True)
        project = make_package(self.dir / 'proj', '200', '2.2.2', cli=True)
        chosen, notes = rc.choose_playwright_package([pre], [project], [browsers])
        self.assertEqual(chosen, pre)
        self.assertEqual(notes['method'], 'preinstalled')
        self.assertEqual(notes['version'], '1.1.1')
        self.assertEqual(notes['chromium_revision'], '100')
        self.assertNotEqual(notes['package'], str(project))
        with patch.object(rc, '_install_playwright_package') as install, patch.object(rc, 'run_stage') as stage:
            resolved, how, resolved_notes = rc.resolve_playwright(
                self.dir / 'app', self.dir / 'cache', {}, self.dir / 'pw.log', 15,
                preinstalled=[pre], project=[project], browser_dirs=[browsers])
        install.assert_not_called()
        stage.assert_not_called()
        self.assertEqual(resolved, pre.resolve())
        self.assertTrue(how.startswith('preinstalled'))
        self.assertEqual(resolved_notes['chromium_revision'], '100')

    def test_project_package_with_different_revision_is_not_selected(self):
        browsers = browser_dir(self.dir, 'chromium-100')
        project = make_package(self.dir / 'proj', '200', '2.2.2', cli=True)
        chosen, notes = rc.choose_playwright_package([], [project], [browsers])
        self.assertIsNone(chosen)
        self.assertEqual(notes['method'], 'install-chromium')
        self.assertNotEqual(notes.get('package'), str(project))
        self.assertEqual(rc.read_chromium_revision(project), '200')

    def test_wrong_playwright_core_sibling_is_ignored(self):
        package = self.dir / 'node_modules' / '@playwright' / 'test'
        package.mkdir(parents=True)
        (package / 'cli.js').write_text('// cli\n', encoding='utf-8')
        (package / 'package.json').write_text('{"name":"@playwright/test","version":"3.3.3"}\n', encoding='utf-8')
        wrong = package.parent / 'playwright-core'
        wrong.mkdir()
        (wrong / 'browsers.json').write_text(
            json.dumps(dict(browsers=[dict(name='chromium', revision='100')])), encoding='utf-8')
        browsers = browser_dir(self.dir, 'chromium-100')
        chosen, notes = rc.choose_playwright_package([package], [], [browsers])
        self.assertIsNone(chosen)
        self.assertNotEqual(notes['method'], 'preinstalled')
        self.assertIsNone(rc.read_chromium_revision(package))

    def test_inference_returns_version_when_cli_is_absent(self):
        browsers = browser_dir(self.dir, 'chromium_headless_shell-100')
        source = make_package(self.dir / 'src', '100', '9.9.9', cli=False)
        project = make_package(self.dir / 'proj', '200', '2.2.2', cli=True)
        chosen, notes = rc.choose_playwright_package([source], [project], [browsers])
        self.assertIsNone(chosen)
        self.assertEqual(notes['method'], 'inferred')
        self.assertEqual(notes['version'], '9.9.9')
        self.assertEqual(notes['chromium_revision'], '100')
        self.assertNotEqual(notes['package'], str(project))

    def test_inferred_version_is_installed_without_chromium_download(self):
        browsers = browser_dir(self.dir, 'chromium_headless_shell-100')
        source = make_package(self.dir / 'src', '100', '9.9.9', cli=False)
        project = make_package(self.dir / 'proj', '200', '2.2.2', cli=True)
        cache = self.dir / 'cache'
        specs = []

        def fake_install(spec, cache_dir, env, log, timeout):
            specs.append((spec, timeout))
            make_package(cache_dir, '100', '9.9.9', cli=True)
            return 0

        with patch.object(rc, '_install_playwright_package', side_effect=fake_install), patch.object(rc, 'run_stage') as stage:
            resolved, how, notes = rc.resolve_playwright(
                self.dir / 'app', cache, {}, self.dir / 'pw.log', 19,
                preinstalled=[source], project=[project], browser_dirs=[browsers])
        stage.assert_not_called()
        self.assertEqual(specs, [('@playwright/test@9.9.9', 19)])
        self.assertEqual(resolved, (cache / 'node_modules' / '@playwright' / 'test').resolve())
        self.assertIn('9.9.9', how)
        self.assertEqual(notes['version'], '9.9.9')
        self.assertEqual(notes['chromium_revision'], '100')

    @unittest.skipUnless(shutil.which('node'), 'node not available')
    def test_chromium_install_runs_only_when_inference_fails(self):
        browsers = browser_dir(self.dir, 'chromium-222')
        launcher = make_package(self.dir / 'pre', '111', '1.0.0', cli=True)
        with patch.object(rc, '_install_playwright_package') as install, patch.object(rc, 'run_stage', return_value=7) as stage:
            resolved, how, notes = rc.resolve_playwright(
                self.dir / 'app', self.dir / 'cache', {}, self.dir / 'pw.log', 17,
                preinstalled=[launcher], project=[], browser_dirs=[browsers])
        install.assert_not_called()
        self.assertIsNone(resolved)
        self.assertIn('chromium install failed (7)', how)
        self.assertEqual(notes['install_exit'], 7)
        command = stage.call_args.args[0]
        self.assertEqual(command[-2:], ['install', 'chromium'])
        self.assertEqual(stage.call_args.args[4], 17)

    def test_package_beside_browsers_path_is_a_candidate(self):
        browsers = self.dir / 'cache' / 'ms-playwright'
        browsers.mkdir(parents=True)
        beside = self.dir / 'cache' / 'node_modules' / '@playwright' / 'test'
        beside.mkdir(parents=True)
        project = self.dir / 'app'
        project.mkdir()
        env = os.environ.copy()
        env['PLAYWRIGHT_BROWSERS_PATH'] = str(browsers)
        preinstalled, project_packages = rc.discover_playwright_packages(project, env)
        self.assertIn(beside, preinstalled)
        usr = Path('/usr/lib/node_modules/@playwright/test')
        self.assertLess(preinstalled.index(usr), preinstalled.index(beside))
        self.assertIn(Path('/ms-playwright').parent / 'node_modules' / '@playwright' / 'test', preinstalled)
        self.assertEqual(project_packages, [project / 'node_modules' / '@playwright' / 'test'])
        self.assertNotIn(project_packages[0], preinstalled)

    def test_clean_env_pins_real_browser_dir_before_replacing_home(self):
        cache = self.dir / 'real-browsers'
        (cache / 'chromium-55').mkdir(parents=True)
        home = self.dir / 'scratch-home'
        home.mkdir()
        with patch.dict(os.environ, {'PATH': os.environ.get('PATH', '')}, clear=True):
            with patch.object(rc, 'browser_cache_dirs', return_value=[cache]):
                env = rc.clean_env(home=home)
        self.assertEqual(env['PLAYWRIGHT_BROWSERS_PATH'], str(cache))
        self.assertEqual((env['HOME'], env['USERPROFILE']), (str(home), str(home)))
        self.assertNotEqual(env['HOME'], env['PLAYWRIGHT_BROWSERS_PATH'])

    def test_diagnostic_log_tail_is_redacted_and_truncated(self):
        blob = ('A' * 3000) + SECRET
        with patch.dict(os.environ, {'DEMO_API_TOKEN': SECRET}):
            tail = rc.diagnostic_log_tail(blob, 'unavailable')
            self.assertEqual(rc.diagnostic_log_tail(blob, 'passed'), '')
            self.assertEqual(rc.diagnostic_log_tail(blob, 'failed'), '')
        self.assertNotIn(SECRET, tail)
        self.assertIn('***', tail)
        self.assertEqual(len(tail), rc.DIAGNOSTIC_LOG_LIMIT)

    def test_suite_list_mapping(self):
        specs = self.dir / 'specs'
        other = self.dir / 'other'
        self.assertEqual(rc.normalize_suites(None, None), [])
        self.assertEqual(rc.normalize_suites(specs, []), [])
        self.assertEqual(rc.normalize_suites(specs, None), [('input', specs)])
        self.assertEqual(rc.normalize_suites(specs, [('compiled', other), ('authored', specs)]),
                         [('compiled', other), ('authored', specs)])


def server_app(root: Path) -> Path:
    app = root / 'app'
    write(app / 'package.json', '{"scripts":{"start":"node server.js"}}\n')
    write(app / 'server.js', '''const http=require('node:http');
const port=Number(process.env.PORT||3000);
const host=process.env.HOST||'127.0.0.1';
http.createServer((req,res)=>{
  const path=(req.url||'/').split('?')[0];
  if(path==='/api/health'){res.writeHead(200,{'content-type':'application/json'});res.end('{"ok":true}');return;}
  res.writeHead(200,{'content-type':'text/html'});res.end('<html>ok</html>');
}).listen(port,host);
''')
    return app


def spec_dir(root: Path, name: str) -> Path:
    path = root / name
    write(path / 'demo.spec.ts', "test('sample', () => {});\n")
    return path


@unittest.skipUnless(HAS_NODE, 'node/npm not available')
class SuiteAndDiagnosticTests(TempCase):
    def test_two_suites_record_separate_signals_on_one_server(self):
        app = server_app(self.dir)
        first = spec_dir(self.dir, 'input-specs')
        second = spec_dir(self.dir, 'compiled-specs')
        package = self.dir / 'pw-package'
        package.mkdir()
        (package / 'cli.js').write_text('// cli\n', encoding='utf-8')
        browsers = str(self.dir / 'browsers')
        order = []
        urls = []
        starts = []
        real_probe = rc.probe_routes
        real_spawn = rc.spawn

        def spy_probe(*args, **kwargs):
            order.append('probe')
            return real_probe(*args, **kwargs)

        def spy_resolve(*args, **kwargs):
            order.append('resolve')
            return (package, 'preinstalled test-package', dict(
                method='preinstalled', version='1.48.0', chromium_revision='1122', browser_dirs=[browsers]))

        def spy_suite(**kwargs):
            order.append(kwargs['signal_name'])
            urls.append(kwargs['base_url'])
            if kwargs['signal_name'] == 'input':
                failure = rc.RuntimeFailure('input broke', 'a.spec.ts', 'Error: input')
                return dict(status='failed', passed=1, failed=1, skipped=0, total=2, precondition=0,
                            failures=[failure], preconditions=[],
                            signal=rc.signal_counts(1, 1, 0, [failure]), signal_name='input')
            failure = rc.RuntimeFailure('compiled broke', 'b.spec.ts', 'Error: compiled')
            return dict(status='failed', passed=3, failed=2, skipped=1, total=6, precondition=0,
                        failures=[failure, failure], preconditions=[],
                        signal=rc.signal_counts(3, 2, 1, [failure, failure]), signal_name='compiled')

        def spy_spawn(command, cwd, env, log):
            starts.append(list(command))
            return real_spawn(command, cwd, env, log)

        with patch.object(rc, 'probe_routes', side_effect=spy_probe), \
             patch.object(rc, 'resolve_playwright', side_effect=spy_resolve), \
             patch.object(rc, 'run_playwright_suite', side_effect=spy_suite), \
             patch.object(rc, 'spawn', side_effect=spy_spawn):
            result = run_runtime_check(app, suites=[('input', first), ('compiled', second)],
                                       work_dir=self.dir / 'work', install=False)
        self.assertEqual(order, ['probe', 'resolve', 'input', 'compiled'])
        self.assertEqual(len(starts), 1)
        self.assertEqual(urls[0], urls[1])
        self.assertEqual(result.status, 'failed')
        self.assertEqual((result.passed, result.failed, result.skipped), (4, 3, 1))
        self.assertEqual(result.signals['input'], dict(passed=1, failed=1, skipped=0, failures=[
            dict(name='input broke', file='a.spec.ts', error='Error: input', kind='app')]))
        self.assertEqual(result.signals['compiled']['passed'], 3)
        self.assertEqual(result.signals['compiled']['failed'], 2)
        self.assertEqual(result.signals['compiled']['skipped'], 1)
        self.assertEqual(len(result.signals['compiled']['failures']), 2)
        self.assertGreaterEqual(result.signals['probes']['passed'], 1)
        self.assertTrue(any(item.get('path') == '/api/health' and item.get('ok') for item in result.api_probes))
        self.assertEqual(result.diagnostics['log_tail'], '')
        self.assertEqual(result.diagnostics['playwright']['version'], '1.48.0')
        self.assertEqual(result.diagnostics['playwright']['revision'], '1122')
        self.assertEqual(result.diagnostics['playwright']['browser_dirs'], [browsers])
        self.assertTrue(result.diagnostics['playwright']['how'].startswith('preinstalled'))
        feedback = result.to_feedback()
        json.dumps(feedback)
        self.assertEqual(feedback['signals']['input']['failed'], 1)
        self.assertEqual(feedback['diagnostics']['playwright']['revision'], '1122')

    def test_unavailable_browser_still_returns_probe_results(self):
        app = server_app(self.dir)
        specs = spec_dir(self.dir, 'specs')
        order = []
        real_probe = rc.probe_routes

        def spy_probe(*args, **kwargs):
            order.append('probe')
            return real_probe(*args, **kwargs)

        def spy_resolve(*args, **kwargs):
            order.append('resolve')
            return (None, 'no playwright package matched installed browsers', dict(
                method='install-chromium', version='', chromium_revision='', browser_dirs=[str(self.dir / 'browsers')]))

        with patch.object(rc, 'probe_routes', side_effect=spy_probe), \
             patch.object(rc, 'resolve_playwright', side_effect=spy_resolve), \
             patch.object(rc, 'run_playwright_suite') as suite:
            result = run_runtime_check(app, specs, work_dir=self.dir / 'work', install=False)
        suite.assert_not_called()
        self.assertEqual(order, ['probe', 'resolve'])
        self.assertEqual(result.status, 'unavailable')
        self.assertTrue(any(item.get('path') == '/api/health' and item.get('ok') for item in result.api_probes))
        self.assertGreaterEqual(result.signals['probes']['passed'], 1)
        self.assertNotIn('input', result.signals)
        self.assertEqual(result.diagnostics['playwright']['how'], 'no playwright package matched installed browsers')
        self.assertLessEqual(len(result.diagnostics['log_tail']), rc.DIAGNOSTIC_LOG_LIMIT)
        self.assertIn('api_probes', result.to_feedback())
        self.assertIn('signals', result.to_feedback())

    def test_empty_suites_probe_without_a_browser(self):
        app = server_app(self.dir)
        with patch.object(rc, 'resolve_playwright') as resolve:
            result = run_runtime_check(app, suites=[], work_dir=self.dir / 'work', install=False)
        resolve.assert_not_called()
        self.assertEqual(result.status, 'passed')
        self.assertIn('probes', result.signals)
        self.assertNotIn('input', result.signals)
        self.assertEqual(result.diagnostics['log_tail'], '')
        self.assertEqual(result.passed, result.signals['probes']['passed'])

    def test_result_log_tail_is_redacted_on_the_result(self):
        app = self.dir / 'app'
        write(app / 'package.json', '{"scripts":{"build":"node build.js","start":"node server.js"}}\n')
        write(app / 'build.js', "console.error('compile error: ' + 'A'.repeat(4000) + %r); process.exit(2);\n" % SECRET)
        with patch.dict(os.environ, {'DEMO_API_TOKEN': SECRET}):
            result = run_runtime_check(app, None, work_dir=self.dir / 'work', install=False)
        self.assertEqual(result.status, 'build_failed')
        self.assertNotIn(SECRET, result.diagnostics['log_tail'])
        self.assertNotIn(SECRET, result.log_tail)
        self.assertNotIn(SECRET, json.dumps(result.to_feedback()))
        self.assertIn('***', result.diagnostics['log_tail'])
        self.assertEqual(len(result.diagnostics['log_tail']), rc.DIAGNOSTIC_LOG_LIMIT)
        self.assertIn('compile error', result.log_tail)


if __name__ == '__main__':
    unittest.main()

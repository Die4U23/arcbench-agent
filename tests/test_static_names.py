
# Legacy fixtures exercise worker internals; host isolation is tested separately.
import unittest.mock as _sandbox_mock
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from agent.static_names import CHECK_NAME, verify_frontend_names
from agent.verify import verify_project


@unittest.skipUnless(shutil.which('node') and (shutil.which('npm') or shutil.which('npm.cmd')),
                     'Real Node/npm required')
@_sandbox_mock.patch("agent.sandbox_context._WORKER", True)
class StaticNameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'frontend/src'
        self.source.mkdir(parents=True)

    def write(self, name, source):
        path = self.source/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding='utf-8')
        return path

    def test_build_success_does_not_hide_unbound_event_handler(self):
        self.write('Search.jsx', '''import {useNavigate} from 'react-router-dom';
export default function Search(){
 async function submit(){ navigate('/results'); }
 return <button onClick={submit}>Search</button>;
}''')
        manifest = self.root/'frontend/package.json'
        manifest.write_text('{"scripts":{"build":"anything"}}', encoding='utf-8')
        from agent.verify import CheckResult
        with patch('agent.verify._run_npm_script', return_value=CheckResult(
                'frontend: npm run build', True, 0, 'compiled')):
            report = verify_project(self.root)
        self.assertFalse(report.passed)
        self.assertTrue(next(c for c in report.checks if c.name.endswith('npm run build')).passed)
        self.assertIn("'navigate' is not defined", report.checks[0].output)
        self.assertEqual(CHECK_NAME, report.checks[0].name)

    def test_scoped_hook_browser_globals_and_typeof_guard_pass_without_source_changes(self):
        source = self.write('Search.jsx', '''import {useNavigate} from 'react-router-dom';
// eslint-disable-next-line unrelated-rule
export default function Search(){
 const navigate=useNavigate();
 async function submit(){await fetch('/api'); window.localStorage.setItem('x','y'); navigate('/results');}
 if(typeof optionalFeature !== 'undefined') console.log('guard');
 return <button onClick={submit}>Search</button>;
}''')
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        report = verify_frontend_names(self.root)
        self.assertTrue(report.passed, report.output)
        self.assertEqual(before, hashlib.sha256(source.read_bytes()).hexdigest())

    def test_comments_cannot_suppress_genuine_undefined_reference(self):
        self.write('handler.js', '/* eslint-disable no-undef */\nexport const run=()=>missingService();')
        report = verify_frontend_names(self.root)
        self.assertFalse(report.passed)
        self.assertIn("'missingService' is not defined", report.output)

    def test_generated_tests_excluded_but_nested_business_source_checked(self):
        self.write('ok.test.jsx', 'describe("test",()=>unknownTestHelper());')
        self.write('tests/helper.js', 'undefinedFixture();')
        self.write('features/journey.js', 'export const go = () => unknownNavigator();')
        report = verify_frontend_names(self.root)
        self.assertFalse(report.passed)
        self.assertIn('features/journey.js', report.output)
        self.assertNotIn('unknownTestHelper', report.output)
        self.assertNotIn('undefinedFixture', report.output)

    def test_checker_unavailable_is_failure(self):
        self.write('index.js', 'export const x=1;')
        with patch('agent.static_names._tool_directory', side_effect=RuntimeError('offline')):
            result = verify_frontend_names(self.root)
        self.assertFalse(result.passed)
        self.assertIn('not a pass', result.output)

    def test_no_javascript_source_does_not_claim_a_check(self):
        self.write('index.ts', 'export const x:number=1;')
        self.assertIsNone(verify_frontend_names(self.root))


if __name__ == '__main__':
    unittest.main()

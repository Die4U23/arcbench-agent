import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'platform' / 'platform-agent'))
from agent.group_fixture_titles import fixture_title_derivation, runtime_setup, value_expr


class FixtureTitleDerivationTests(unittest.TestCase):
    def setUp(self):
        self.reference = 'Cobalt sample-a'
        self.binding = dict(field='Designation', role='article', examples={self.reference: 'Cobalt'}, actions={})
        self.step = dict(op='fill', target=dict(role='textbox', name='Designation'), value=self.reference)

    def test_literal_and_self_asserted_binding_cannot_claim_a_runtime_identity(self):
        case = dict(steps=[self.step])
        self.assertEqual(fixture_title_derivation(case, 0)['reason'], 'literal_title_has_no_runtime_identity')
        case['fixture_titles'] = self.binding
        self.assertEqual(fixture_title_derivation(case, 0)['reason'], 'untrusted_fixture_title_binding')
        actual = fixture_title_derivation(case, 0, controller_binding=self.binding, example='Cobalt')
        self.assertTrue(actual['planned'])
        self.assertEqual(actual['execution'], 'not_run')
        self.assertEqual(actual['reference'], self.reference)
        self.assertEqual(value_expr(self.step, self.binding), 'fixtureTitles[' + json.dumps(self.reference) + ']')
        mismatched = fixture_title_derivation(case, 0, controller_binding=self.binding, example='Zinc')
        self.assertFalse(mismatched['planned'])
        wrong_field = dict(case, steps=[dict(self.step, target=dict(role='textbox', name='Body'))])
        self.assertFalse(fixture_title_derivation(wrong_field, 0, controller_binding=self.binding)['planned'])

    def test_each_missing_runtime_dimension_fails_its_own_observation(self):
        source = '\n'.join(runtime_setup(self.binding))
        # Change only the construction of the actual titles, leaving all observers intact.
        definition, = [line for line in source.splitlines() if 'const fixtureTitle = ' in line]
        for missing in (None, 'run', 'worker', 'case'):
            with self.subTest(missing=missing):
                program = source
                if missing:
                    original = 'identity.' + missing
                    self.assertEqual(definition.count(original), 1)
                    program = source.replace(definition, definition.replace(original, '"constant"'))
                script = '''
const testInfo = {workerIndex:3,testId:'unit-case',attach:async (_,a)=>{receipt=JSON.parse(a.body)}};
let receipt, rejected=false;
(async () => { try {\n''' + program + '''\n} catch (error) {rejected=true;}
process.stdout.write(JSON.stringify({receipt,rejected})); })();'''
                output = subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True,
                                        env={**os.environ, 'ARC_FIXTURE_RUN_ID':'unit-run'})
                result = json.loads(output.stdout)
                row, = result['receipt']['derivations']
                self.assertEqual(result['rejected'], bool(missing))
                self.assertEqual(row['checks'], {key:key != missing for key in ('example','deterministic','run','worker','case')})
                if not missing:
                    self.assertEqual(row['title'], 'Cobalt unit-run-w3-unit-case-sample-a')


if __name__ == '__main__':
    unittest.main()

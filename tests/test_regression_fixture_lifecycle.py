from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent.candidate_checkpoint import PUBLIC_CHECK
from agent.native_verification import verify_public_acceptance
from agent.verify import CheckResult


class RegressionFixtureLifecycleTests(unittest.TestCase):
    def setUp(self):
        worker = patch('agent.sandbox_context.in_worker', return_value=True)
        worker.start()
        self.addCleanup(worker.stop)

    def test_every_run_executes_fresh_fixture_and_preserves_complete_results(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / 'frontend/.arc'
            protected.mkdir(parents=True)
            fixture = protected / 'public-case-reconstruction.test.tsx'
            evidence = protected / 'public-case-reconstruction.fixture.txt'
            statuses = tuple((f'case {n}', 'failed' if n == 0 else 'passed') for n in range(32))
            expected = (CheckResult(PUBLIC_CHECK, False, 1, '31/32', (), statuses),)
            def run_original(*args, **kwargs):
                self.assertFalse(fixture.exists())
                fixture.write_bytes(b'unchanged public assertions\r\n')
                return expected
            with patch('agent.native_verification.original_public_acceptance', side_effect=run_original) as runner:
                for _ in range(2):
                    self.assertEqual(verify_public_acceptance(root, {}), expected)
                    self.assertFalse(fixture.exists())
                    self.assertEqual(evidence.read_bytes(), b'unchanged public assertions\r\n')
                self.assertEqual(runner.call_count, 2)

    def test_runner_exception_still_archives_fixture_without_suppressing_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / 'frontend/.arc'
            protected.mkdir(parents=True)
            fixture = protected / 'public-case-reconstruction.test.tsx'
            fixture.write_text('incomplete runner evidence', encoding='utf-8')
            with patch('agent.native_verification.original_public_acceptance', side_effect=RuntimeError('runner failed')):
                with self.assertRaisesRegex(RuntimeError, 'runner failed'):
                    verify_public_acceptance(root, {})
            self.assertFalse(fixture.exists())
            self.assertEqual((protected / 'public-case-reconstruction.fixture.txt').read_text(),
                             'incomplete runner evidence')

    def test_startup_only_does_not_require_or_create_a_fixture(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checks = (CheckResult('web runtime startup', True, 0, 'served'),)
            with patch('agent.native_verification.original_public_acceptance', return_value=checks) as runner:
                self.assertEqual(verify_public_acceptance(root, {}, startup_only=True), checks)
                runner.assert_called_once_with(root, {}, startup_only=True)
            self.assertFalse((root / 'frontend/.arc').exists())


if __name__ == '__main__':
    unittest.main()

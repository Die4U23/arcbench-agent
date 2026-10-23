import sys
import unittest

from agent.sandbox_process import SandboxProcessError, run_bounded


class BoundedProcessTests(unittest.TestCase):
    def run_python(self, source, **kwargs):
        return run_bounded(
            [sys.executable, "-c", source],
            timeout=kwargs.get("timeout", 3),
            output_limit=kwargs.get("output_limit", 1024),
        )

    def test_collects_separate_streams_and_exit_status(self):
        result = self.run_python(
            'import sys;print("out");print("err",file=sys.stderr);sys.exit(3)'
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout.strip(), b"out")
        self.assertEqual(result.stderr.strip(), b"err")

    def test_stdout_and_stderr_are_each_bounded(self):
        for stream in ("stdout", "stderr"):
            with (
                self.subTest(stream=stream),
                self.assertRaisesRegex(SandboxProcessError, "output limit"),
            ):
                self.run_python(
                    f'import sys;sys.{stream}.write("x"*100000)', output_limit=100
                )

    def test_deadline_is_enforced(self):
        with self.assertRaisesRegex(SandboxProcessError, "deadline"):
            self.run_python("import time;time.sleep(10)", timeout=0.1)

    def test_invalid_limits_do_not_launch(self):
        for timeout, limit in ((0, 10), (1, 0), (-1, 10)):
            with self.assertRaises(ValueError):
                run_bounded(["does-not-exist"], timeout=timeout, output_limit=limit)


if __name__ == "__main__":
    unittest.main()

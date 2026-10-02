from __future__ import annotations

import json
from pathlib import Path
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, call, patch

from agent.contract_checks import verify_public_acceptance


def tree():
    return {"id":"ROOT", "name":"Railway Ticket Booking Demo", "children":[
        {"id":node} for node in ("REQ-1.1","REQ-1.2","REQ-2.1","REQ-2.2","REQ-3.1","REQ-3.2")]}


class ContractCheckTests(unittest.TestCase):
    def setUp(self):
        # Popen is mocked in these tests. Never signal a real process group
        # using the fake process PID when running on a Unix CI worker.
        cleanup = patch("agent.contract_checks.os.killpg", create=True)
        self.killpg = cleanup.start()
        self.addCleanup(cleanup.stop)

    def test_unix_cleanup_terminates_owned_group_and_kills_after_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            server = MagicMock()
            server.pid = 4242
            server.poll.return_value = None
            server.wait.side_effect = [subprocess.TimeoutExpired("backend", 5), None]
            response = MagicMock()
            response.__enter__.return_value.status = 200
            kill_signal = getattr(signal, "SIGKILL", 9)
            with patch("agent.contract_checks.shutil.which", side_effect=lambda name: name), \
                 patch("agent.contract_checks.socket.socket"), \
                 patch("agent.contract_checks.urlopen", return_value=response), \
                 patch("agent.contract_checks.subprocess.Popen", return_value=server), \
                 patch("agent.contract_checks.signal.SIGKILL", kill_signal, create=True), \
                 patch("agent.contract_checks.os.name", "posix"):
                result = verify_public_acceptance(root, {}, startup_only=True)
            self.assertTrue(result[0].passed)
            self.assertEqual(self.killpg.call_args_list, [
                call(4242, signal.SIGTERM), call(4242, kill_signal),
            ])
            self.assertEqual(server.wait.call_args_list, [call(timeout=5), call(timeout=5)])

    def test_delivery_startup_skips_behavior_tests_and_preserves_their_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = root / "backend"
            backend.mkdir()
            (backend / "package.json").write_text(json.dumps({"scripts": {"start": "node server.js"}}), encoding="utf-8")
            protected = root / "frontend/.arc"
            protected.mkdir(parents=True)
            report = protected / "public-regression.json"
            report.write_text('{"numPassedTests":27}', encoding="utf-8")
            server = MagicMock()
            server.poll.return_value = None
            response = MagicMock()
            response.__enter__.return_value.status = 200
            with patch("agent.contract_checks.shutil.which", side_effect=lambda name: name), \
                 patch("agent.contract_checks.socket.socket"), \
                 patch("agent.contract_checks.urlopen", return_value=response), \
                 patch("agent.contract_checks.subprocess.Popen", return_value=server), \
                 patch("agent.contract_checks.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
                result = verify_public_acceptance(root, {}, startup_only=True)
            self.assertTrue(result[0].passed)
            self.assertEqual(result[0].name, "web runtime startup")
            self.assertEqual(report.read_text(encoding="utf-8"), '{"numPassedTests":27}')
            self.assertFalse((protected / "public-case-reconstruction.test.tsx").exists())
            self.assertTrue(all(call.args[0][0] == "taskkill" for call in run.call_args_list))
            server.wait.assert_called_once()

    def prepare(self, root):
        files = {"frontend/src/App.jsx":"export default function App() {}",
                 "backend/package.json":json.dumps({"scripts":{"start":"node src/server.js"}}),
                 "frontend/node_modules/vitest/vitest.mjs":""}
        for package in ("react","react-router-dom","jsdom","@testing-library/react","@testing-library/dom"):
            files[f"frontend/node_modules/{package}/package.json"] = "{}"
        for name, content in files.items():
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content,encoding="utf-8")

    def test_other_tasks_do_not_launch_a_probe(self):
        with patch("agent.contract_checks.subprocess.Popen") as popen:
            self.assertEqual(verify_public_acceptance(Path("unused"), {"id":"ROOT"}), ())
            popen.assert_not_called()

    def test_missing_probe_prerequisites_fail_instead_of_silently_passing(self):
        with tempfile.TemporaryDirectory() as directory:
            result=verify_public_acceptance(Path(directory),tree())
        self.assertFalse(result[0].passed)
        self.assertIn("dependencies",result[0].output)

    def test_occupied_port_does_not_test_an_unrelated_server(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.prepare(root)
            with patch("agent.contract_checks.shutil.which", side_effect=lambda name:name), \
                 patch("agent.contract_checks.socket.socket") as socket_mock, \
                 patch("agent.contract_checks.subprocess.Popen") as popen:
                socket_mock.return_value.__enter__.return_value.bind.side_effect=OSError("busy")
                result=verify_public_acceptance(root,tree())
                self.assertFalse(result[0].passed)
                self.assertIn("already occupied",result[0].output)
                popen.assert_not_called()

    def test_workspace_hoisted_dependencies_are_not_reported_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            (root / "frontend/node_modules").rename(root / "node_modules")
            with patch("agent.contract_checks.shutil.which", side_effect=lambda name: name), \
                 patch("agent.contract_checks.socket.socket") as probe, \
                 patch("agent.contract_checks.subprocess.Popen") as popen:
                probe.return_value.__enter__.return_value.bind.side_effect = OSError("busy")
                result = verify_public_acceptance(root, tree())
            self.assertIn("already occupied", result[0].output)
            self.assertNotIn("requires the real React", result[0].output)
            popen.assert_not_called()

    def test_unix_probe_reuses_closed_addresses_but_windows_does_not(self):
        for platform in ("posix", "nt"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.prepare(root)
                with patch("agent.contract_checks.shutil.which", side_effect=lambda name: name), \
                     patch("agent.contract_checks.socket.socket") as socket_mock, \
                     patch("agent.contract_checks.os.name", platform), \
                     patch("agent.contract_checks.subprocess.Popen") as popen:
                    probe = socket_mock.return_value.__enter__.return_value
                    probe.bind.side_effect = OSError("live listener")
                    result = verify_public_acceptance(root, tree())
                    self.assertFalse(result[0].passed)
                    self.assertEqual(probe.setsockopt.call_count, 1 if platform == "posix" else 0)
                    popen.assert_not_called()

    def test_startup_failure_preserves_backend_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            server = MagicMock()
            server.poll.return_value = 1
            server.returncode = 1
            def start(*args, **kwargs):
                kwargs['stdout'].write('Error: Cannot find module ./src/app\n')
                kwargs['stdout'].flush()
                return server
            with patch("agent.contract_checks.shutil.which", side_effect=lambda name: name), \
                 patch("agent.contract_checks.socket.socket"), \
                 patch("agent.contract_checks.subprocess.Popen", side_effect=start), \
                 patch("agent.contract_checks.subprocess.run", return_value=SimpleNamespace(returncode=0)):
                result = verify_public_acceptance(root, tree())
            self.assertFalse(result[0].passed)
            self.assertIn('exited with code 1', result[0].output)
            self.assertIn('Cannot find module ./src/app', result[0].output)

    def test_large_dom_failures_preserve_later_case_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            def run(command, **kwargs):
                if command[0] == 'node':
                    assertions = [{"title": f"case {i}", "status": "failed",
                                   "failureMessages": [f"assertion {i}\nIgnored nodes:\n"+'<div>'*2000]}
                                  for i in range(30)]
                    (root/'frontend/.arc/public-regression.json').write_text(json.dumps({
                        "numTotalTests": 30, "numPassedTests": 0,
                        "testResults": [{"assertionResults": assertions}]}), encoding='utf-8')
                return SimpleNamespace(returncode=1 if command[0] == 'node' else 0, stdout='', stderr='')
            server = MagicMock()
            server.poll.return_value = None
            response = MagicMock()
            response.__enter__.return_value.status = 200
            with patch("agent.contract_checks.shutil.which", side_effect=lambda name: name), \
                 patch("agent.contract_checks.socket.socket"), \
                 patch("agent.contract_checks.urlopen", return_value=response), \
                 patch("agent.contract_checks.subprocess.Popen", return_value=server), \
                 patch("agent.contract_checks.subprocess.run", side_effect=run):
                result = verify_public_acceptance(root, tree())
            self.assertIn('0/30 passed', result[0].output)
            self.assertIn('case 29: assertion 29', result[0].output)
            self.assertNotIn('<div>', result[0].output)

    def test_exit_zero_with_wrong_case_count_is_rejected_and_exact_thirty_passes(self):
        for count, expected in ((0,False),(29,False),(30,True),(31,False)):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);self.prepare(root)
                def run(command, **kwargs):
                    if command[0] == "node":
                        (root/"frontend/.arc/public-regression.json").write_text(json.dumps({
                            "numTotalTests":count,"numPassedTests":count,"testResults":[]}),encoding="utf-8")
                    return SimpleNamespace(returncode=0,stdout="",stderr="")
                server=MagicMock();server.poll.return_value=None
                response=MagicMock();response.__enter__.return_value.status=200
                with patch("agent.contract_checks.shutil.which",side_effect=lambda name:name), \
                     patch("agent.contract_checks.socket.socket"), \
                     patch("agent.contract_checks.urlopen",return_value=response), \
                     patch("agent.contract_checks.subprocess.Popen",return_value=server), \
                     patch("agent.contract_checks.subprocess.run",side_effect=run):
                    result=verify_public_acceptance(root,tree())
                self.assertEqual(result[0].passed,expected)
                fixture=(root/"frontend/.arc/public-case-reconstruction.test.tsx").read_text(encoding="utf-8")
                self.assertEqual(fixture.count("\ntest("),30)
                self.assertIn("NOT",result[0].name.upper())

    def test_denied_process_cleanup_is_a_failed_environment_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            server = MagicMock()
            server.pid = 123
            server.poll.return_value = None
            response = MagicMock()
            response.__enter__.return_value.status = 200
            with patch("agent.contract_checks.shutil.which", side_effect=lambda name: name), \
                 patch("agent.contract_checks.socket.socket"), \
                 patch("agent.contract_checks.urlopen", return_value=response), \
                 patch("agent.contract_checks.subprocess.Popen", return_value=server), \
                 patch("agent.contract_checks.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="spawn denied", stderr="")), \
                 patch("agent.contract_checks.subprocess.CREATE_NEW_PROCESS_GROUP", 0x200, create=True), \
                 patch("agent.contract_checks.os.name", "nt"):
                result = verify_public_acceptance(root, tree())
            self.assertFalse(result[0].passed)
            self.assertIn("cleanup failed for owned PID 123", result[0].output)

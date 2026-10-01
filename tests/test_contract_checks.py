from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agent.contract_checks import verify_public_acceptance


def tree():
    return {"id":"ROOT", "name":"Railway Ticket Booking Demo", "children":[
        {"id":node} for node in ("REQ-1.1","REQ-1.2","REQ-2.1","REQ-2.2","REQ-3.1","REQ-3.2")]}


class ContractCheckTests(unittest.TestCase):
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
                 patch("agent.contract_checks.os.name", "nt"):
                result = verify_public_acceptance(root, tree())
            self.assertFalse(result[0].passed)
            self.assertIn("cleanup failed for owned PID 123", result[0].output)

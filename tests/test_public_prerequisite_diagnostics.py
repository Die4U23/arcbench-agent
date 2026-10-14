"""Missing prerequisites stay failed and tell the implementer what blocked execution."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agent.contract_checks import verify_public_acceptance
from tests import test_contract_checks as fixtures


class PublicPrerequisiteDiagnosticsTests(unittest.TestCase):
    def prepare(self, root):
        fixtures.ContractCheckTests.prepare(self, root)

    def failed_check(self, root, commands=None, *, startup_only=False):
        commands = commands if commands is not None else {"node": "/tools/node", "npm": "/tools/npm"}
        with patch("agent.contract_checks.shutil.which", side_effect=commands.get), \
             patch("agent.contract_checks.socket.socket") as probe, \
             patch("agent.contract_checks.subprocess.Popen") as popen, \
             patch("agent.contract_checks.subprocess.run") as run:
            result = verify_public_acceptance(root, fixtures.tree(), startup_only=startup_only)
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].passed)
        self.assertEqual(result[0].exit_code, 1)
        self.assertEqual(result[0].case_statuses, ())
        self.assertEqual(result[0].requirement_ids, ())
        probe.assert_not_called()
        popen.assert_not_called()
        run.assert_not_called()
        return result[0]

    def test_each_missing_dependency_is_identified_before_cases_start(self):
        dependencies = ("vitest/vitest.mjs", "react/package.json", "react-router-dom/package.json",
                        "jsdom/package.json", "@testing-library/react/package.json",
                        "@testing-library/dom/package.json", "@testing-library/jest-dom/package.json")
        for missing in dependencies:
            with self.subTest(missing=missing), TemporaryDirectory() as directory:
                root = Path(directory)
                self.prepare(root)
                (root / "frontend/node_modules" / missing).unlink()
                result = self.failed_check(root)
                self.assertEqual(result.name, "public interaction regression (reconstructed jsdom, not Stage 3)")
                self.assertIn("Public case execution did not start.", result.output)
                self.assertIn("Failed prerequisites: " + missing + "\n", result.output)
                self.assertIn("Browser entry adapter configured", result.output)
                self.assertIn("node=/tools/node; npm/npm.cmd=/tools/npm", result.output)
                self.assertIn("MISSING " + missing + "; searched: " +
                              str(root / "frontend/node_modules" / missing) + " | " +
                              str(root / "node_modules" / missing), result.output)
                self.assertIn("react/package.json", result.output)
                self.assertIn("react-router-dom/package.json", result.output)
                self.assertNotIn("requires the real browser entry", result.output)
                self.assertFalse((root / "frontend/.arc/public-case-reconstruction.test.tsx").exists())

    def test_missing_node_and_npm_are_distinct_from_missing_packages(self):
        scenarios = (({"npm": "/tools/npm"}, "node (PATH)", "node=NOT FOUND; npm/npm.cmd=/tools/npm"),
                     ({"node": "/tools/node"}, "npm/npm.cmd (PATH)", "node=/tools/node; npm/npm.cmd=NOT FOUND"),
                     ({}, "npm/npm.cmd (PATH), node (PATH)", "node=NOT FOUND; npm/npm.cmd=NOT FOUND"))
        for commands, failure, resolution in scenarios:
            with self.subTest(commands=commands), TemporaryDirectory() as directory:
                root = Path(directory)
                self.prepare(root)
                result = self.failed_check(root, commands)
                self.assertIn("Failed prerequisites: " + failure + "\n", result.output)
                self.assertIn(resolution, result.output)
                self.assertNotIn("MISSING ", result.output)

    def test_multiple_failures_are_reported_together_and_keep_prior_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            for name in ("react-router-dom", "@testing-library/jest-dom"):
                (root / "frontend/node_modules" / name / "package.json").unlink()
            prior = root / "frontend/.arc/public-regression.json"
            prior.parent.mkdir()
            prior.write_text('{"previous_snapshot":true}', encoding="utf-8")
            result = self.failed_check(root, {"npm": "/tools/npm"})
            self.assertIn("Failed prerequisites: node (PATH), react-router-dom/package.json, "
                          "@testing-library/jest-dom/package.json\n", result.output)
            self.assertEqual(prior.read_text(encoding="utf-8"), '{"previous_snapshot":true}')
            self.assertEqual(result.case_statuses, ())

    def test_backend_installation_does_not_expand_frontend_dependency_search(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            relative = "@testing-library/jest-dom/package.json"
            (root / "frontend/node_modules" / relative).unlink()
            backend_file = root / "backend/node_modules" / relative
            backend_file.parent.mkdir(parents=True)
            backend_file.write_text("{}", encoding="utf-8")
            result = self.failed_check(root)
            self.assertIn("Failed prerequisites: " + relative + "\n", result.output)
            self.assertNotIn(str(root / "backend/node_modules"), result.output)

    def test_hoisted_dependencies_and_npm_cmd_fallback_still_reach_next_check(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            (root / "frontend/node_modules").rename(root / "node_modules")
            commands = {"node": "/tools/node", "npm.cmd": "/tools/npm.cmd"}
            with patch("agent.contract_checks.shutil.which", side_effect=commands.get), \
                 patch("agent.contract_checks.socket.socket") as probe, \
                 patch("agent.contract_checks.subprocess.Popen") as popen:
                probe.return_value.__enter__.return_value.bind.side_effect = OSError("owned test barrier")
                result = verify_public_acceptance(root, fixtures.tree())[0]
            self.assertFalse(result.passed)
            self.assertIsNone(result.exit_code)
            self.assertIn("port 3000 is already occupied", result.output)
            self.assertNotIn("Failed prerequisites", result.output)
            popen.assert_not_called()

    def test_startup_only_does_not_require_node_or_frontend_test_packages(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            backend = root / "backend"
            backend.mkdir()
            (backend / "package.json").write_text(json.dumps({"scripts": {"start": "node server.js"}}), encoding="utf-8")
            commands = {"npm": "/tools/npm"}
            server = MagicMock()
            server.poll.return_value = None
            response = MagicMock()
            response.__enter__.return_value.status = 200
            with patch("agent.contract_checks.shutil.which", side_effect=commands.get), \
                 patch("agent.contract_checks.socket.socket"), \
                 patch("agent.contract_checks.urlopen", return_value=response), \
                 patch("agent.contract_checks.subprocess.Popen", return_value=server), \
                 patch("agent.contract_checks.subprocess.run", return_value=SimpleNamespace(returncode=0)), \
                 patch("agent.contract_checks.os.killpg", create=True):
                result = verify_public_acceptance(root, {}, startup_only=True)[0]
            self.assertTrue(result.passed)
            self.assertEqual(result.name, "web runtime startup")
            self.assertEqual(result.case_statuses, ())

    def test_startup_missing_npm_does_not_claim_an_entry_or_dependency_failure(self):
        with TemporaryDirectory() as directory:
            result = self.failed_check(Path(directory), {}, startup_only=True)
            self.assertEqual(result.name, "web runtime startup")
            self.assertIn("Runtime startup did not start.", result.output)
            self.assertIn("Failed prerequisites: npm/npm.cmd (PATH)\n", result.output)
            self.assertNotIn("MISSING ", result.output)
            self.assertNotIn("Browser entry", result.output)
            self.assertNotIn("node (PATH)", result.output)
            self.assertNotIn("test dependencies", result.output)


if __name__ == "__main__":
    unittest.main()

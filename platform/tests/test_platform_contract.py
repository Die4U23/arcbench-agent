"""Contract draft, static checks, and HTTP probes. No model API."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "platform-agent"))

from agent.platform_contract import contract_errors, draft_contract, write_contract
from agent.platform_runtime_check import api_failures, probe_routes


REQUIREMENTS = """\
id: ROOT
name: Sample
data:
  - id: DATA-ACCOUNT
    lifecycle: SEED
    entity: verified account
    properties:
      email: one@example.com
      password: secret
  - id: DATA-NOTE
    lifecycle: CREATED
    entity: note
    properties:
      title: ignored
"""

SPEC = """\
import { test, expect } from '@playwright/test';
test('home', async ({ page }) => {
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
  await expect(page.getByLabel('Email')).toBeVisible();
});
"""


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = self.root / "task"
        (self.task / "tests").mkdir(parents=True)
        (self.task / "requirements.yaml").write_text(REQUIREMENTS, encoding="utf-8")
        (self.task / "tests" / "home.spec.ts").write_text(SPEC, encoding="utf-8")

    def test_draft_lists_seed_entities_and_names_without_created_records(self):
        document = draft_contract(self.task / "requirements.yaml", self.task / "tests")
        self.assertEqual([item["lifecycle"] for item in document["seeds"]], ["SEED"])
        self.assertEqual(document["seeds"][0]["file"], "seed/verified-account.json")
        self.assertEqual(document["seeds"][0]["fields"], ["email", "password"])
        self.assertEqual([item["lifecycle"] for item in document["entities"]], ["SEED", "CREATED"])
        self.assertEqual(document["accessible_names"], ["Overview", "Email"])
        self.assertEqual(document["routes"][0], dict(method="GET", path="/api/health", probe="read", expect=200))

    def test_missing_seed_file_and_duplicate_route_fail(self):
        app = self.root / "app"
        document = draft_contract(self.task / "requirements.yaml", self.task / "tests")
        document["routes"].append(dict(method="POST", path="/api/items", probe="read"))
        document["routes"].append(dict(method="GET", path="/api/health", probe="read", expect=200))
        write_contract(app, document)
        errors = {item["error"] for item in contract_errors(app)}
        self.assertIn("Seed file listed in docs/contract.json does not exist", errors)
        self.assertTrue(any("cannot use probe read" in error for error in errors))
        self.assertTrue(any(error.startswith("Duplicate route GET /api/health") for error in errors))
        seed = app / "seed" / "verified-account.json"
        seed.parent.mkdir(parents=True)
        seed.write_text('{"collection":"verified-account","records":[]}\n', encoding="utf-8")
        document["routes"] = [dict(method="GET", path="/api/health", probe="read", expect=200),
                              dict(method="POST", path="/api/items", probe="skip")]
        write_contract(app, document)
        self.assertEqual(contract_errors(app), [])

    def test_read_probes_skip_writes_and_keep_auth_separate_from_missing_routes(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/health":
                    body = b'{"ok":true}'
                    status = 200
                elif path == "/private":
                    body = b"login"
                    status = 401
                else:
                    body = b"missing"
                    status = 404
                self.send_response(status)
                self.send_header("content-type", "text/plain")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        probes = probe_routes(base, [
            dict(method="GET", path="/api/health", probe="read", expect=200),
            dict(method="GET", path="/private", probe="read"),
            dict(method="GET", path="/missing", probe="read"),
            dict(method="POST", path="/api/items", probe="skip"),
            dict(method="GET", path="/api/items/:id", probe="read"),
        ])
        by_path = {item["path"]: item for item in probes}
        self.assertTrue(by_path["/api/health"]["ok"])
        self.assertEqual(by_path["/private"]["note"], "auth_required")
        self.assertFalse(by_path["/missing"]["ok"])
        self.assertIn("skipped", by_path["/api/items"])
        self.assertIn("skipped", by_path["/api/items/:id"])
        self.assertEqual([item.name for item in api_failures(probes)], ["GET /missing"])
        json.dumps(probes)


if __name__ == "__main__":
    unittest.main()

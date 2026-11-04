from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import urlopen

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "platform-agent"))
sys.path.insert(0, str(root / "platform-agent/arcbench-agent-runtime/src"))

from agent.platform_scaffold import (
    DEPLOY,
    SCAFFOLD_FILES,
    SCAFFOLD_GUIDE,
    is_scaffold,
    prepare_scaffold,
)
from agent.platform_seed import (
    collect_seed_evidence,
    seed_instructions,
    validate_seeds,
)
from agent.platform_static import validate_source


FORBIDDEN_BUSINESS_WORDS = (
    "bookstack",
    "12306",
    "prestashop",
    "ctrip",
    "stackoverflow",
    "stack overflow",
    "shelf",
    "shelves",
    "booking",
    "train",
    "trains",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _kill_process(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
            else:
                proc.terminate()
                proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass


class PlatformScaffoldAndSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_scaffold_created_on_empty_dir_and_passes_static_validation(self):
        app = self.base / "empty_app"
        self.assertTrue(prepare_scaffold(app))
        self.assertTrue(is_scaffold(app))
        self.assertEqual((app / "deploy.sh").read_text(encoding="utf-8"), DEPLOY)

        for rel_path in SCAFFOLD_FILES:
            self.assertTrue((app / rel_path).is_file(), f"Missing scaffold file: {rel_path}")

        pkg = json.loads((app / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(pkg["scripts"], {"build": "vite build", "start": "node server/index.js"})
        self.assertEqual(
            pkg["dependencies"],
            {"express": "4.19.2", "react": "18.3.1", "react-dom": "18.3.1"},
        )

        # Second call on already scaffolded project must leave it untouched and return False.
        self.assertFalse(prepare_scaffold(app))

        docs = app / "docs"
        docs.mkdir()
        (docs / "task-ir.json").write_text('{"work_items":[],"ui_elements":[],"scenarios":[]}\n', encoding="utf-8")
        (docs / "coverage.json").write_text('{"items":[]}\n', encoding="utf-8")
        static_check = validate_source(app)
        self.assertTrue(static_check["passed"], static_check)

    def test_scaffold_untouched_on_non_empty_dirs(self):
        with_pkg = self.base / "with_pkg"
        with_pkg.mkdir()
        (with_pkg / "package.json").write_text('{"name":"existing-app"}\n', encoding="utf-8")
        self.assertFalse(prepare_scaffold(with_pkg))
        self.assertFalse(is_scaffold(with_pkg))
        self.assertFalse((with_pkg / "deploy.sh").exists())
        self.assertEqual(
            json.loads((with_pkg / "package.json").read_text(encoding="utf-8"))["name"],
            "existing-app",
        )

        nested_pkg = self.base / "nested_pkg"
        (nested_pkg / "frontend").mkdir(parents=True)
        (nested_pkg / "frontend" / "package.json").write_text("{}", encoding="utf-8")
        self.assertFalse(prepare_scaffold(nested_pkg))
        self.assertFalse((nested_pkg / "package.json").exists())

        with_deploy = self.base / "with_deploy"
        with_deploy.mkdir()
        (with_deploy / "deploy.sh").write_text("exec node main.js\n", encoding="utf-8")
        self.assertFalse(prepare_scaffold(with_deploy))
        self.assertFalse((with_deploy / "package.json").exists())

        # Metadata, dotfiles and README notes do not suppress the full scaffold.
        with_notes = self.base / "with_notes"
        (with_notes / ".arc").mkdir(parents=True)
        (with_notes / ".arc" / "state.json").write_text("{}", encoding="utf-8")
        (with_notes / "README.txt").write_text("custom content", encoding="utf-8")
        (with_notes / ".gitignore").write_text("node_modules\n", encoding="utf-8")
        self.assertTrue(prepare_scaffold(with_notes))
        for rel_path in SCAFFOLD_FILES:
            self.assertTrue((with_notes / rel_path).is_file(), rel_path)
        self.assertEqual((with_notes / "README.txt").read_text(encoding="utf-8"), "custom content")

        # Substantive files without a launcher get only package.json + deploy.sh (earlier campaign rule).
        with_sources = self.base / "with_sources"
        (with_sources / "src").mkdir(parents=True)
        (with_sources / "src" / "main.js").write_text("console.log('existing')\n", encoding="utf-8")
        self.assertTrue(prepare_scaffold(with_sources))
        self.assertTrue(is_scaffold(with_sources))
        self.assertTrue((with_sources / "package.json").is_file())
        self.assertFalse((with_sources / "server").exists())
        self.assertFalse((with_sources / "src" / "App.jsx").exists())
        self.assertFalse(prepare_scaffold(with_sources))

    def test_scaffold_and_modules_have_no_business_words(self):
        app = self.base / "neutral_app"
        self.assertTrue(prepare_scaffold(app))
        texts_to_scan = {
            "SCAFFOLD_GUIDE": SCAFFOLD_GUIDE,
            "seed_instructions": seed_instructions(),
            "platform_scaffold.py": (root / "platform-agent/agent/platform_scaffold.py").read_text(
                encoding="utf-8"
            ),
            "platform_seed.py": (root / "platform-agent/agent/platform_seed.py").read_text(
                encoding="utf-8"
            ),
        }
        for rel_path in SCAFFOLD_FILES:
            texts_to_scan[f"scaffold:{rel_path}"] = (app / rel_path).read_text(encoding="utf-8")

        for label, content in texts_to_scan.items():
            lowered = content.lower()
            for word in FORBIDDEN_BUSINESS_WORDS:
                pattern = r"\b" + re.escape(word) + r"\b"
                self.assertIsNone(
                    re.search(pattern, lowered),
                    f"Forbidden domain word '{word}' found in {label}",
                )

    def test_collect_seed_evidence_synthetic_yaml_and_helpers(self):
        req_dir = self.base / "reqs"
        req_dir.mkdir()
        yaml_text = """\
id: ROOT
name: Synthetic Portal
children:
  - id: REQ-1
    name: Session access
    scenarios:
      - id: SCN-1
        name: Sign in with existing member
        steps:
          - id: STEP-1
            Given: The store contains existing account member_alpha@example.org.
            When: Sign in with member_alpha@example.org.
            Then: Dashboard is shown.
          - id: STEP-2
            Given: The store contains existing account member_alpha@example.org.
            When: Refresh the dashboard.
            Then: Dashboard stays visible.
      - id: SCN-2
        name: View workspace container
        steps:
          - id: STEP-3
            Given: Workspace Container Alpha-01 is available.
            When: Open the workspace list.
            Then: Workspace Container Alpha-01 is listed.
    children: []
"""
        (req_dir / "requirements.yaml").write_text(yaml_text, encoding="utf-8")

        tests_dir = self.base / "tests"
        (tests_dir / "fixtures").mkdir(parents=True)
        (tests_dir / "helpers.ts").write_text(
            """\
// Seeded by the scenario environment
export const PRIMARY_ACCOUNT: Record<string, string> = {
  id: 'usr-100',
  email: 'member_alpha@example.org',
  password: 'SyntheticPassword!42',
};

export const WORKSPACE_NAME = 'Workspace Container Alpha-01';

export const DRAFT_INPUT_TITLE = 'Created during UI scenario execution';

export const formatLabel = (prefix: string, id: string) => `${prefix}-${id}`;
""",
            encoding="utf-8",
        )
        (tests_dir / "fixtures" / "data.ts").write_text(
            """\
export const BOOTSTRAP_TAGS = ['general', 'urgent'] as const;
""",
            encoding="utf-8",
        )
        (tests_dir / "REQ-1.spec.ts").write_text(
            """\
export const SPEC_LOCAL_CONST = 'should-not-be-collected';
""",
            encoding="utf-8",
        )

        evidence = collect_seed_evidence(req_dir, tests_dir)
        self.assertEqual(len(evidence["given_steps"]), 2)
        self.assertEqual(
            evidence["given_steps"][0],
            {
                "requirement_id": "REQ-1",
                "scenario_id": "SCN-1",
                "given": "The store contains existing account member_alpha@example.org.",
            },
        )
        self.assertEqual(
            evidence["given_steps"][1],
            {
                "requirement_id": "REQ-1",
                "scenario_id": "SCN-2",
                "given": "Workspace Container Alpha-01 is available.",
            },
        )

        by_name = {item["name"]: item for item in evidence["test_constants"]}
        self.assertNotIn("SPEC_LOCAL_CONST", by_name)
        self.assertNotIn("formatLabel", by_name)
        self.assertIn("PRIMARY_ACCOUNT", by_name)
        self.assertIn("WORKSPACE_NAME", by_name)
        self.assertIn("DRAFT_INPUT_TITLE", by_name)
        self.assertIn("BOOTSTRAP_TAGS", by_name)

        self.assertTrue(by_name["PRIMARY_ACCOUNT"]["seed_hint"])
        self.assertIn("member_alpha@example.org", by_name["PRIMARY_ACCOUNT"]["source"])
        self.assertTrue(by_name["WORKSPACE_NAME"]["seed_hint"])
        self.assertTrue(by_name["BOOTSTRAP_TAGS"]["seed_hint"])
        self.assertFalse(by_name["DRAFT_INPUT_TITLE"]["seed_hint"])

        guide = seed_instructions()
        self.assertIn("seed/<collection>.json", guide)
        self.assertIn("password", guide)

        # Also verify dict input and tests_dir=None work cleanly.
        dict_evidence = collect_seed_evidence(
            {
                "id": "ROOT",
                "scenarios": [
                    {
                        "id": "SCN-ROOT",
                        "steps": [{"Given": "A registered user exists."}],
                    }
                ],
                "children": [],
            },
            None,
        )
        self.assertEqual(
            dict_evidence,
            {
                "given_steps": [
                    {
                        "requirement_id": "ROOT",
                        "scenario_id": "SCN-ROOT",
                        "given": "A registered user exists.",
                    }
                ],
                "test_constants": [],
            },
        )

    def test_store_crud_and_atomic_persistence_in_node(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")
        app = self.base / "store_app"
        self.assertTrue(prepare_scaffold(app))
        data_dir = app / "store_data"
        script = app / "_test_store.mjs"
        script.write_text(
            """\
import {
  initStore,
  all,
  get,
  find,
  findOne,
  insert,
  update,
  remove,
  hashPassword,
  verifyPassword,
} from './server/store.js';

await initStore();
const created = insert('records', { label: 'first' });
const explicit = insert('records', { id: 'rec-2', label: 'second' });
const updated = update('records', 'rec-2', { label: 'second-updated' });
const matched = find('records', (item) => item.label.startsWith('second'));
const one = findOne('records', { id: 'rec-2' });
const fetched = get('records', created.id);
const deleted = remove('records', created.id);
await initStore();
const remaining = all('records');
const pwHash = hashPassword('MySecret99');
console.log(JSON.stringify({
  hasGeneratedId: Boolean(created.id),
  explicitId: explicit.id,
  updatedLabel: updated.label,
  matchedCount: matched.length,
  oneLabel: one.label,
  fetchedLabel: fetched.label,
  deleted,
  remainingCount: remaining.length,
  remainingId: remaining[0].id,
  pwValid: verifyPassword('MySecret99', pwHash),
  pwInvalid: verifyPassword('Other', pwHash),
}));
""",
            encoding="utf-8",
        )
        run = subprocess.run(
            [node, str(script)],
            cwd=str(app),
            env={**os.environ, "DATA_DIR": str(data_dir)},
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        out = json.loads(run.stdout.strip())
        self.assertTrue(out["hasGeneratedId"])
        self.assertEqual(out["explicitId"], "rec-2")
        self.assertEqual(out["updatedLabel"], "second-updated")
        self.assertEqual(out["matchedCount"], 1)
        self.assertEqual(out["oneLabel"], "second-updated")
        self.assertEqual(out["fetchedLabel"], "first")
        self.assertTrue(out["deleted"])
        self.assertEqual(out["remainingCount"], 1)
        self.assertEqual(out["remainingId"], "rec-2")
        self.assertTrue(out["pwValid"])
        self.assertFalse(out["pwInvalid"])

    def test_store_get_meta_guards_missing_keys(self):
        from agent.platform_scaffold import SERVER_INDEX_JS, SERVER_STORE_JS
        # Static: getMeta never clones a missing value (fresh store on first boot).
        get_meta = SERVER_STORE_JS.split("export function getMeta(key) {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("hasOwnProperty.call(current._meta, key)", get_meta)
        self.assertIn("return undefined", get_meta)
        self.assertNotIn("return clone(current._meta[key])", get_meta)
        # Static: the entry check compares real paths case-insensitively on win32.
        self.assertIn("fs.realpathSync", SERVER_INDEX_JS)
        self.assertIn("process.platform === 'win32'", SERVER_INDEX_JS)
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")
        app = self.base / "fresh_meta"
        self.assertTrue(prepare_scaffold(app))
        script = app / "_meta.mjs"
        script.write_text(
            "import { initStore, getMeta, setMeta } from './server/store.js';\n"
            "await initStore();\n"
            "const missing = getMeta('seedsApplied');\n"
            "setMeta('seedsApplied', true);\n"
            "console.log(JSON.stringify({ missing: missing === undefined, present: getMeta('seedsApplied') }));\n",
            encoding="utf-8",
        )
        run = subprocess.run([node, str(script)], cwd=str(app), env={**os.environ, "DATA_DIR": str(app / "fresh_data")},
                             capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout.strip()), {"missing": True, "present": True})

    def test_validate_seeds_detects_errors_and_accepts_valid(self):
        app = self.base / "seed_validation"
        prepare_scaffold(app)
        self.assertEqual(validate_seeds(app), [])

        seed_dir = app / "seed"
        (seed_dir / "valid.json").write_text(
            json.dumps(
                {
                    "collection": "users",
                    "records": [
                        {"id": "u-1", "username": "alpha", "password": "secret"},
                        {"id": "u-2", "username": "beta"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(validate_seeds(app), [])

        (seed_dir / "bad_json.json").write_text("{not valid json", encoding="utf-8")
        (seed_dir / "missing_fields.json").write_text(
            json.dumps({"collection": ""}), encoding="utf-8"
        )
        (seed_dir / "non_object_record.json").write_text(
            json.dumps({"collection": "items", "records": ["not-an-object"]}),
            encoding="utf-8",
        )
        (seed_dir / "duplicate_ids.json").write_text(
            json.dumps(
                {
                    "collection": "items",
                    "records": [{"id": "dup-1", "name": "a"}, {"id": "dup-1", "name": "b"}],
                }
            ),
            encoding="utf-8",
        )

        errors = validate_seeds(app)
        by_path = {}
        for err in errors:
            by_path.setdefault(err["path"], []).append(err["error"])

        self.assertNotIn("seed/valid.json", by_path)
        self.assertIn("seed/bad_json.json", by_path)
        self.assertTrue(any("Invalid JSON" in msg for msg in by_path["seed/bad_json.json"]))
        self.assertIn("seed/missing_fields.json", by_path)
        self.assertIn("seed/non_object_record.json", by_path)
        self.assertIn("seed/duplicate_ids.json", by_path)
        self.assertTrue(any("dup-1" in msg for msg in by_path["seed/duplicate_ids.json"]))

    def test_node_smoke_server_health_and_seed_idempotency(self):
        node = shutil.which("node")
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or shutil.which("npm")
        if not node:
            self.skipTest("node is not available")

        app = self.base / "smoke_app"
        self.assertTrue(prepare_scaffold(app))
        self.assertFalse((app / "dist").exists())

        if not (app / "node_modules" / "express").exists():
            if not npm:
                self.skipTest("express not preinstalled and npm is unavailable")
            install = subprocess.run(
                [npm, "install", "--omit=dev", "--no-audit", "--no-fund", "express@4.19.2"],
                cwd=str(app),
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if install.returncode != 0 or not (app / "node_modules" / "express").exists():
                self.skipTest(f"express install unavailable in test environment: {install.stderr[:300]}")

        seed_payload = {
            "collection": "users",
            "records": [
                {
                    "id": "u-1",
                    "username": "smoke_member",
                    "password": "PlainPassword!2026",
                }
            ],
        }
        (app / "seed" / "users.json").write_text(
            json.dumps(seed_payload, indent=2), encoding="utf-8"
        )
        self.assertEqual(validate_seeds(app), [])

        data_dir = app / "runtime_data"
        port1 = _free_port()
        env1 = {
            **os.environ,
            "HOST": "127.0.0.1",
            "PORT": str(port1),
            "DATA_DIR": str(data_dir),
        }

        proc1 = None
        proc2 = None
        try:
            proc1 = subprocess.Popen(
                [node, "server/index.js"],
                cwd=str(app),
                env=env1,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            health = None
            deadline = time.time() + 15.0
            while time.time() < deadline:
                if proc1.poll() is not None:
                    stderr = proc1.stderr.read() if proc1.stderr else ""
                    self.fail(f"Server exited early on first start: {stderr}")
                try:
                    with urlopen(f"http://127.0.0.1:{port1}/api/health", timeout=1.0) as resp:
                        if resp.status == 200:
                            health = json.loads(resp.read().decode("utf-8"))
                            break
                except (URLError, OSError):
                    time.sleep(0.1)

            self.assertEqual(health, {"ok": True})

            # Missing dist/ must still allow non-/api GET fallback without crashing.
            with urlopen(f"http://127.0.0.1:{port1}/some/client/route", timeout=2.0) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("root", resp.read().decode("utf-8"))

            store_file = data_dir / "store.json"
            self.assertTrue(store_file.is_file())
            stored1 = json.loads(store_file.read_text(encoding="utf-8"))
            users1 = stored1["collections"]["users"]
            self.assertEqual(len(users1), 1)
            user1 = users1[0]
            self.assertEqual(user1["id"], "u-1")
            self.assertEqual(user1["username"], "smoke_member")
            self.assertNotIn("password", user1)
            self.assertTrue(user1.get("passwordHash", "").startswith("scrypt$"))
            first_hash = user1["passwordHash"]

            # Verify password helper round-trip with the stored hash.
            verify_script = app / "_verify_pw.mjs"
            verify_script.write_text(
                "import { verifyPassword } from './server/store.js';\n"
                f"const ok = verifyPassword('PlainPassword!2026', {json.dumps(first_hash)});\n"
                f"const bad = verifyPassword('WrongPassword', {json.dumps(first_hash)});\n"
                "console.log(JSON.stringify({ ok, bad }));\n",
                encoding="utf-8",
            )
            verify_run = subprocess.run(
                [node, str(verify_script)],
                cwd=str(app),
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            self.assertEqual(json.loads(verify_run.stdout.strip()), {"ok": True, "bad": False})

            _kill_process(proc1)
            proc1 = None

            # Mutate the seed file before restart 2 to prove seeds are only applied once per fresh store.
            seed_payload["records"].append(
                {"id": "u-2", "username": "second_boot_only", "password": "AnotherPassword!1"}
            )
            (app / "seed" / "users.json").write_text(
                json.dumps(seed_payload, indent=2), encoding="utf-8"
            )

            port2 = _free_port()
            env2 = {
                **os.environ,
                "HOST": "127.0.0.1",
                "PORT": str(port2),
                "DATA_DIR": str(data_dir),
            }
            proc2 = subprocess.Popen(
                [node, "server/index.js"],
                cwd=str(app),
                env=env2,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            health2 = None
            deadline2 = time.time() + 15.0
            while time.time() < deadline2:
                if proc2.poll() is not None:
                    stderr = proc2.stderr.read() if proc2.stderr else ""
                    self.fail(f"Server exited early on second start: {stderr}")
                try:
                    with urlopen(f"http://127.0.0.1:{port2}/api/health", timeout=1.0) as resp:
                        if resp.status == 200:
                            health2 = json.loads(resp.read().decode("utf-8"))
                            break
                except (URLError, OSError):
                    time.sleep(0.1)

            self.assertEqual(health2, {"ok": True})
            stored2 = json.loads(store_file.read_text(encoding="utf-8"))
            users2 = stored2["collections"]["users"]
            self.assertEqual(len(users2), 1)
            self.assertEqual(users2[0]["passwordHash"], first_hash)
        finally:
            _kill_process(proc1)
            _kill_process(proc2)


if __name__ == "__main__":
    unittest.main()

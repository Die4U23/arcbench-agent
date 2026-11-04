"""Playwright acceptance compiler. Invented kiln fixtures only."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform-agent"))

from agent.platform_acceptance import author_tests, compile_checks, triage, verify_frozen
from agent.platform_task_ir import build_task_ir

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ir"
PAGE = Path(__file__).resolve().parent / "fixtures" / "acceptance" / "kiln-board.html"
_BROWSER_MISSING = re.compile(
    r"Executable doesn't exist|browserType\.launch: .*(not found|missing)|"
    r"Please run the following command to download new browsers",
    re.I,
)

AUTH = {
    "login_entry": {"role": "link", "name": "Sign in"},
    "fields": [{"label": "Email"}, {"label": "Password"}],
    "submit": {"role": "button", "name": "Sign in"},
}
POTTER = {
    "id": "DATA-POTTER",
    "name": "Potter account",
    "fields": ["email", "password"],
    "lifecycle": "SEED",
    "seed_records": [{
        "id": "potter-ada",
        "email": "ada@kiln.example",
        "password": "clay-secret",
    }],
    "source": "parsed",
}


def _element(role, name, *, group=None, multiple=False, persistent=False, work_item="REQ-BOARD",
             source="parsed", evidence=None):
    return {
        "role": role,
        "name": name,
        "page": "Firing board",
        "work_item": work_item,
        "group": group,
        "persistent": persistent,
        "multiple": multiple,
        "source": source,
        "evidence": evidence if evidence is not None else f"{name} is on the board.",
    }


def _step(action, role, name, text, *, value=None, kind=None):
    step = {
        "target": {"role": role, "name": name},
        "value": value,
        "text": text,
    }
    if kind is None:
        step["action"] = action
    else:
        step["kind"] = kind
    return step


def _scenario(scenario_id, work_item, name, *, session="anonymous", account=None, when=None, then=None,
              source="parsed"):
    return {
        "id": scenario_id,
        "work_item": work_item,
        "name": name,
        "given": {"session": session, "account": account, "data": [], "text": session},
        "when": when or [],
        "then": then or [],
        "source": source,
    }


def _ir(elements, scenarios, *, auth=None, entities=None):
    return {
        "version": 1,
        "task": {"slug": "kiln-board", "title": "Kiln board"},
        "work_items": [],
        "entities": entities or [],
        "pages": [],
        "ui_elements": elements,
        "scenarios": scenarios,
        "auth": auth,
        "runtime": {"layout": "empty"},
        "rejected": [],
    }


def _board_elements():
    return [
        _element("link", "North kiln", group="kiln-nav", persistent=True,
                 evidence="North kiln stays in the board navigation."),
        _element("link", "Glaze notes", group="kiln-nav", persistent=True,
                 evidence="Glaze notes stays in the board navigation."),
        _element("heading", "Firing board", evidence="Firing board is the page heading."),
        _element("listitem", "Shelf chip", multiple=True, evidence="Shelf chip can show more than once."),
        _element("button", "INFERRED_PIN", source="inferred", evidence="pin the note"),
    ]


def _passing_ir():
    return _ir(
        _board_elements(),
        [
            _scenario(
                "SCN-READ",
                "REQ-BOARD",
                "Anonymous neighbor reads the board",
                when=[_step("click", "link", "Public hours", 'click "Public hours"(role=link)')],
                then=[_step("visible", "link", "North kiln", 'the board shows "North kiln"', kind="text")],
            ),
            _scenario("SCN-INFERRED", "REQ-BOARD", "Inferred glaze pin", source="inferred"),
        ],
    )


def _failing_ir():
    return _ir(
        [_element("heading", "Missing kiln mark", evidence="Missing kiln mark is required on the board.")],
        [_scenario("SCN-MISS", "REQ-BOARD", "Missing mark is visible")],
    )


def _spec_text(out, result, scenario_id):
    matches = [rel for rel in result["generated"] if rel.endswith(f"__{scenario_id}.spec.ts")]
    if len(matches) != 1:
        raise AssertionError(result)
    return (out / matches[0]).read_text(encoding="utf-8")


def _playwright_cli():
    candidates = [
        Path(tempfile.gettempdir()) / "arc-runtime-check-playwright-cache" / "node_modules" / "@playwright" / "test" / "cli.js",
    ]
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidates.append(parent / "node_modules" / "@playwright" / "test" / "cli.js")
        if parent.name == "arc_agent":
            break
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "node_modules" / "@playwright" / "test" / "cli.js")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port, proc, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited {proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"server did not listen on {port}")


def _stop_server(proc, log_handle):
    if proc is not None and proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if log_handle is not None and not log_handle.closed:
        log_handle.close()


def _link_playwright(cli, spec_dir):
    """Make `@playwright/test` resolvable from a compiled spec in a scratch directory."""
    package = Path(cli).resolve().parent
    link = spec_dir / "node_modules" / "@playwright" / "test"
    if link.exists():
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(package), str(link))
        return
    os.symlink(package, link, target_is_directory=True)


def _write_playwright_config(path, test_dir, report, output):
    config = {
        "testDir": str(test_dir),
        "outputDir": str(output),
        "timeout": 30000,
        "expect": {"timeout": 5000},
        "workers": 1,
        "retries": 0,
        "fullyParallel": False,
        "forbidOnly": True,
        "reporter": [["json", {"outputFile": str(report)}], ["line"]],
        "use": {
            "headless": True,
            "trace": "off",
            "screenshot": "off",
            "video": "off",
            "timezoneId": "UTC",
        },
        "projects": [{"name": "chromium", "use": {"browserName": "chromium"}}],
    }
    path.write_text("module.exports = " + json.dumps(config, indent=2) + ";\n", encoding="utf-8", newline="\n")


class PlatformAcceptanceTests(unittest.TestCase):
    def test_compiled_spec_uses_exact_count_and_navigation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            ir = _passing_ir()
            result = compile_checks(ir, out)
            self.assertEqual(result["generated"], ["REQ-BOARD__SCN-READ.spec.ts"])
            self.assertEqual(result["skipped"], [])
            self.assertTrue(all(not Path(rel).is_absolute() and "\\" not in rel for rel in result["generated"]))
            text = _spec_text(out, result, "SCN-READ")
            first = (out / result["generated"][0]).read_bytes()
            again = compile_checks(ir, out)
            self.assertEqual(again["generated"], result["generated"])
            self.assertEqual((out / result["generated"][0]).read_bytes(), first)
            self.assertIn("REQ-BOARD", text)
            self.assertIn("SCN-READ", text)
            self.assertIn("exact: true", text)
            self.assertEqual(text.count("getByRole('navigation')"), 1)
            self.assertNotIn("INFERRED_PIN", text)
            self.assertNotIn("SCN-INFERRED", text)
            self.assertNotIn("session sign-in", text)
            heading = [line for line in text.splitlines() if "Firing board" in line and "toHaveCount(1)" in line]
            self.assertTrue(heading)
            self.assertTrue(all("page.getByRole" in line and "navigation.getByRole" not in line for line in heading))
            self.assertIn("exact: true", heading[0])
            for name in ("North kiln", "Glaze notes"):
                scoped = [line for line in text.splitlines() if name in line and "navigation.getByRole" in line]
                self.assertTrue(scoped, name)
                self.assertTrue(any("toHaveCount(1)" in line for line in scoped))
            chip = [line for line in text.splitlines() if "Shelf chip" in line]
            self.assertTrue(chip)
            self.assertTrue(all("toHaveCount" not in line for line in chip))
            self.assertTrue(any("toBeVisible" in line and ".first()" in line for line in chip))
            self.assertTrue(any("Public hours" in line and ".click()" in line for line in text.splitlines()))
            self.assertIn('getByText("North kiln"', text)

    def test_kiln_schedule_stops_at_unknown_and_signs_in_only_with_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            out = Path(tmp) / "specs"
            ir = build_task_ir(FIXTURES / "kiln-schedule.yaml", project)
            result = compile_checks(ir, out)
            self.assertEqual(result["skipped"], [])
            anon = _spec_text(out, result, "SCN-ANON")
            auth = _spec_text(out, result, "SCN-AUTH")
            self.assertIn("REQ-BOARD", anon)
            self.assertIn("SCN-ANON", anon)
            self.assertIn("exact: true", anon)
            self.assertIn("toHaveCount(1)", anon)
            self.assertEqual(anon.count("getByRole('navigation')"), 1)
            self.assertNotIn("session sign-in", anon)
            self.assertNotIn("Sign in", anon)
            self.assertNotIn("ada@kiln.example", anon)
            hours = [line for line in anon.splitlines() if "Public hours" in line and "toHaveCount(1)" in line]
            self.assertTrue(hours)
            self.assertTrue(all("page.getByRole" in line and "navigation.getByRole" not in line for line in hours))
            self.assertTrue(any("North kiln" in line and "navigation.getByRole" in line for line in anon.splitlines()))
            self.assertTrue(any("Glaze notes" in line and "navigation.getByRole" in line for line in anon.splitlines()))
            self.assertIn("session sign-in", auth)
            self.assertIn("ada@kiln.example", auth)
            self.assertIn("stoneware", auth)
            self.assertIn("Reserve shelf", auth)
            self.assertIn("REQ-SIGNIN", auth)
            self.assertIn("SCN-AUTH", auth)
            self.assertNotIn("Shelf reserved", auth)
            self.assertNotIn("Shelf chip", auth)
            self.assertNotIn("/reserve", auth)

    def test_no_parsed_elements_produce_no_assertions(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            empty = _ir(
                [],
                [_scenario(
                    "SCN-EMPTY",
                    "REQ-NOTE",
                    "Save a note",
                    when=[_step("unknown", None, None, "They write a note about the damp rack.")],
                )],
            )
            result = compile_checks(empty, out)
            self.assertEqual(result, {"generated": [], "skipped": []})
            self.assertFalse(out.exists())

            inferred = _ir(
                [_element("button", "INFERRED_PIN", source="inferred")],
                [_scenario("SCN-INFERRED", "REQ-BOARD", "Inferred glaze pin", source="inferred")],
            )
            inferred_out = Path(tmp) / "inferred"
            inferred_result = compile_checks(inferred, inferred_out)
            self.assertEqual(inferred_result, {"generated": [], "skipped": []})
            self.assertFalse(inferred_out.exists())

            project = Path(tmp) / "project"
            project.mkdir()
            drying = build_task_ir(FIXTURES / "drying-notes.yaml", project)
            self.assertEqual(drying["ui_elements"], [])
            dried = compile_checks(drying, Path(tmp) / "dried")
            self.assertEqual(dried["generated"], [])
            self.assertEqual(dried["skipped"], [{
                "scenario": "SCN-NOTE",
                "reason": "authenticated scenario skipped: auth is missing",
            }])

    def test_authenticated_scenarios_without_auth_or_seed_are_skipped(self):
        elements = _board_elements()
        anon = _scenario("SCN-ANON", "REQ-BOARD", "Anonymous neighbor reads the board")
        signed = _scenario(
            "SCN-AUTH",
            "REQ-SIGNIN",
            "Potter reserves a shelf",
            session="authenticated",
            account="DATA-POTTER:potter-ada",
            when=[_step("click", "button", "Reserve shelf", 'click "Reserve shelf"(role=button)')],
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ready"
            ready = compile_checks(_ir(elements, [anon, signed], auth=AUTH, entities=[POTTER]), out)
            self.assertEqual(ready["skipped"], [])
            anonymous = _spec_text(out, ready, "SCN-ANON")
            authenticated = _spec_text(out, ready, "SCN-AUTH")
            self.assertNotIn("session sign-in", anonymous)
            self.assertNotIn("Sign in", anonymous)
            self.assertNotIn("clay-secret", anonymous)
            self.assertNotIn("ada@kiln.example", anonymous)
            self.assertIn("session sign-in", authenticated)
            self.assertIn("ada@kiln.example", authenticated)
            self.assertIn("clay-secret", authenticated)
            self.assertIn('name: "Sign in", exact: true', authenticated)

            missing_auth = compile_checks(
                _ir(elements, [signed], auth=None, entities=[POTTER]),
                Path(tmp) / "no-auth",
            )
            self.assertEqual(missing_auth["generated"], [])
            self.assertEqual(missing_auth["skipped"], [{
                "scenario": "SCN-AUTH",
                "reason": "authenticated scenario skipped: auth is missing",
            }])

            missing_account = compile_checks(
                _ir(
                    elements,
                    [_scenario("SCN-AUTH", "REQ-SIGNIN", "Potter reserves a shelf", session="authenticated")],
                    auth=AUTH,
                    entities=[POTTER],
                ),
                Path(tmp) / "no-account",
            )
            self.assertEqual(missing_account["generated"], [])
            self.assertEqual(missing_account["skipped"], [{
                "scenario": "SCN-AUTH",
                "reason": "authenticated scenario skipped: account is missing",
            }])

            missing_seed = compile_checks(
                _ir(
                    elements,
                    [_scenario(
                        "SCN-AUTH",
                        "REQ-SIGNIN",
                        "Potter reserves a shelf",
                        session="authenticated",
                        account="DATA-POTTER:missing-potter",
                    )],
                    auth=AUTH,
                    entities=[POTTER],
                ),
                Path(tmp) / "no-seed",
            )
            self.assertEqual(missing_seed["generated"], [])
            self.assertEqual(missing_seed["skipped"], [{
                "scenario": "SCN-AUTH",
                "reason": "authenticated scenario skipped: seed record not found",
            }])

    def test_unknown_and_select_stop_before_that_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            ir = _ir(
                [
                    _element("button", "READY_LAMP", work_item="REQ-STOP", evidence="READY_LAMP is on the bench."),
                    _element("button", "PICK_LAMP", work_item="REQ-PICK", evidence="PICK_LAMP is on the bench."),
                ],
                [
                    _scenario(
                        "SCN-UNKNOWN",
                        "REQ-STOP",
                        "Stop at an unknown action",
                        when=[
                            _step("click", "button", "WARM_KILN", 'click "WARM_KILN"(role=button)'),
                            _step("unknown", "listitem", "UNKNOWN_QUEUE", "the queue does something else"),
                            _step("click", "button", "AFTER_UNKNOWN", 'click "AFTER_UNKNOWN"(role=button)'),
                        ],
                        then=[_step("visible", "status", "AFTER_THEN_QUOTE", 'the board shows "AFTER_THEN_QUOTE"', kind="text")],
                    ),
                    _scenario(
                        "SCN-SELECT",
                        "REQ-PICK",
                        "Stop at select",
                        when=[
                            _step("fill", "textbox", "Clay body", 'fill "Clay body"(role=textbox)', value="stoneware"),
                            _step("check", "checkbox", "Bisque", 'check "Bisque"(role=checkbox)'),
                            _step("submit", "button", "Reserve shelf", 'submit "Reserve shelf"(role=button)'),
                            _step("reload", "button", "Refresh board", 'reload "Refresh board"(role=button)'),
                            _step("select", "listbox", "SELECT_CONE", "select a cone"),
                            _step("click", "button", "AFTER_SELECT", 'click "AFTER_SELECT"(role=button)'),
                        ],
                        then=[_step("visible", "status", "AFTER_THEN_QUOTE", 'the board shows "AFTER_THEN_QUOTE"', kind="text")],
                    ),
                ],
            )
            result = compile_checks(ir, out)
            unknown = _spec_text(out, result, "SCN-UNKNOWN")
            selected = _spec_text(out, result, "SCN-SELECT")
            self.assertIn("REQ-STOP", unknown)
            self.assertIn("SCN-UNKNOWN", unknown)
            self.assertLess(unknown.index("READY_LAMP"), unknown.index("WARM_KILN"))
            self.assertIn("toHaveCount(1)", unknown)
            self.assertNotIn("UNKNOWN_QUEUE", unknown)
            self.assertNotIn("AFTER_UNKNOWN", unknown)
            self.assertNotIn("AFTER_THEN_QUOTE", unknown)
            self.assertIn('.fill("stoneware")', selected)
            self.assertIn(".check();", selected)
            self.assertIn("Reserve shelf", selected)
            self.assertIn("page.reload()", selected)
            self.assertIn("Refresh board", selected)
            self.assertNotIn("SELECT_CONE", selected)
            self.assertNotIn("AFTER_SELECT", selected)
            self.assertNotIn("AFTER_THEN_QUOTE", selected)
            self.assertNotIn("session sign-in", unknown)
            self.assertNotIn("session sign-in", selected)

    def test_compound_submit_stops_and_absence_is_not_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            ir = _ir(
                [_element("button", "Sign in", work_item="REQ-GATE", evidence="Sign in submits the form.")],
                [
                    _scenario(
                        "SCN-COMPOUND",
                        "REQ-GATE",
                        "Compound submit is not a bare click",
                        when=[_step(
                            "submit", "button", "Sign in",
                            'The visitor submits Email ada@kiln.example and Password secret-value with "Sign in"(role=button).',
                        )],
                        then=[_step("visible", None, None, 'the header shows "Welcome Ada"', kind="text")],
                    ),
                    _scenario(
                        "SCN-PROSE",
                        "REQ-GATE",
                        "Ordinary prose click stays reliable",
                        when=[_step("click", "link", "Overview", 'The visitor clicks "Overview"(role=link).')],
                        then=[_step("visible", None, None, 'the page shows "Overview"', kind="text")],
                    ),
                    _scenario(
                        "SCN-ABSENT",
                        "REQ-GATE",
                        "A rejected result stays hidden",
                        when=[],
                        then=[_step(
                            "text", None, None,
                            '"Welcome Ada" is visible, and "Signed in as Mara" is not shown.',
                            kind="text",
                        )],
                    ),
                ],
            )
            result = compile_checks(ir, out)
            self.assertFalse(any(name.endswith("__SCN-COMPOUND.spec.ts") for name in result["generated"]))
            prose = _spec_text(out, result, "SCN-PROSE")
            self.assertIn('getByRole("link", { name: "Overview", exact: true }).click()', prose)
            absent = _spec_text(out, result, "SCN-ABSENT")
            self.assertIn('getByText("Welcome Ada", { exact: true })).toBeVisible()', absent)
            self.assertIn('getByText("Signed in as Mara", { exact: true })).toHaveCount(0)', absent)

    def test_elements_without_scenarios_still_compile(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            result = compile_checks(_ir(_board_elements(), []), out)
            self.assertEqual(result["generated"], ["REQ-BOARD.elements.spec.ts"])
            self.assertEqual(result["skipped"], [])
            text = (out / result["generated"][0]).read_text(encoding="utf-8")
            self.assertIn("REQ-BOARD elements", text)
            self.assertIn("exact: true", text)
            self.assertIn("toHaveCount(1)", text)
            self.assertIn("getByRole('navigation')", text)
            self.assertNotIn("INFERRED_PIN", text)
            self.assertIn("toBeVisible", text)

    def test_author_tests_freeze_hashes_and_omit_source_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src" / "server.js"
            source.parent.mkdir()
            sentinel = "SENTINEL_APP_SOURCE_9f3c"
            source.write_text(f"function boot() {{ return '{sentinel}'; }}\n", encoding="utf-8")
            ir = _passing_ir()
            seen = []

            def model(prompt):
                seen.append(prompt)
                return (
                    "import { test, expect } from '@playwright/test';\n\n"
                    "test('REQ-BOARD SCN-ANON board', async ({ page }) => {\n"
                    "  await expect(page.getByRole('link', { name: 'North kiln' })).toBeVisible();\n"
                    "});\n"
                )

            out = root / "authored"
            result = author_tests(ir, out, model)
            self.assertEqual(len(seen), 1)
            prompt = seen[0]
            self.assertIn("Write one Playwright test per scenario", prompt)
            self.assertIn("role, name, and label locators", prompt)
            self.assertNotIn(sentinel, prompt)
            self.assertNotIn("server.js", prompt)
            self.assertNotIn(str(source), prompt)
            self.assertEqual(json.loads(prompt[prompt.index("{"):]), ir)
            spec = out / result["generated"][0]
            self.assertEqual(result["generated"], ["authored.spec.ts"])
            self.assertTrue(spec.is_file())
            digest = hashlib.sha256(spec.read_bytes()).hexdigest()
            manifest = json.loads((out / result["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["algorithm"], "sha256")
            self.assertEqual(manifest["files"]["authored.spec.ts"], digest)
            self.assertTrue(verify_frozen(out))
            blob = bytearray(spec.read_bytes())
            blob[0] ^= 1
            spec.write_bytes(blob)
            self.assertFalse(verify_frozen(out))

    def test_triage_separates_defect_sources(self):
        self.assertEqual(triage(None)["groups"], [])
        self.assertEqual(triage({})["groups"], [])
        self.assertEqual(triage({"probes": {"passed": 1, "failed": 0}})["groups"], [])
        self.assertEqual(
            triage({})["confidence_order"],
            ["input", "compiled", "probes", "authored"],
        )
        result = triage({
            "input": {"passed": 1, "failed": 1, "failures": [{
                "name": "input-case", "file": "input.spec.ts", "error": "input broke",
            }]},
            "compiled": {"passed": 0, "failed": 2, "failures": [
                {"name": "compiled-case", "file": "compiled.spec.ts", "error": "compiled broke"},
                {"name": "shared-case", "file": "compiled.spec.ts", "error": "shared"},
            ]},
            "authored": {"passed": 0, "failed": 2, "failures": [
                {"name": "shared-case", "file": "authored.spec.ts", "error": "shared authored"},
                {"name": "authored-only", "file": "authored.spec.ts", "error": "maybe the test"},
            ]},
            "probes": {"passed": 1, "failed": 1, "failures": [{
                "name": "GET /health", "file": "docs/contract.json", "error": "GET /health HTTP 500",
            }]},
        })
        self.assertEqual([group["source"] for group in result["groups"]], ["input", "compiled", "probes", "authored"])
        self.assertEqual([group["confidence"] for group in result["groups"]], [4, 3, 2, 1])
        self.assertEqual(
            [group["kind"] for group in result["groups"]],
            ["application_defect", "application_defect", "interface_defect", "needs_judgment"],
        )

        def names(source):
            group = next(item for item in result["groups"] if item["source"] == source)
            return [failure["name"] for failure in group["failures"]]

        self.assertEqual(names("input"), ["input-case"])
        self.assertEqual(names("compiled"), ["compiled-case", "shared-case"])
        self.assertEqual(names("probes"), ["GET /health"])
        self.assertEqual(names("authored"), ["authored-only"])
        authored = next(item for item in result["groups"] if item["source"] == "authored")
        self.assertIs(authored["change_app"], False)
        self.assertNotIn("change the app", json.dumps(authored).lower())
        self.assertNotIn("fix the application", json.dumps(authored).lower())
        for group in result["groups"]:
            if group["source"] != "authored":
                self.assertNotIn("change_app", group)

    def test_playwright_runs_compiled_spec_against_local_page(self):
        node = shutil.which("node")
        cli = _playwright_cli()
        if not node or cli is None:
            self.skipTest("Chromium cannot launch: Playwright runner not found")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            passing_dir = root / "pass"
            failing_dir = root / "fail"
            passing = compile_checks(_passing_ir(), passing_dir)
            failing = compile_checks(_failing_ir(), failing_dir)
            passing_text = _spec_text(passing_dir, passing, "SCN-READ")
            failing_text = _spec_text(failing_dir, failing, "SCN-MISS")
            self.assertIn("exact: true", passing_text)
            self.assertIn("getByRole('navigation')", passing_text)
            self.assertIn("Missing kiln mark", failing_text)
            self.assertIn("toBeVisible", failing_text)

            port = _free_port()
            server_js = root / "server.js"
            server_js.write_text(
                "const http = require('node:http');\n"
                "const fs = require('node:fs');\n"
                "const page = fs.readFileSync(process.env.PAGE_FILE);\n"
                "const port = Number(process.env.PORT);\n"
                "http.createServer((req, res) => {\n"
                "  res.writeHead(200, {'content-type': 'text/html; charset=utf-8'});\n"
                "  res.end(page);\n"
                "}).listen(port, '127.0.0.1');\n",
                encoding="utf-8",
                newline="\n",
            )
            log_path = root / "server.log"
            log_handle = open(log_path, "w", encoding="utf-8")
            server_env = os.environ.copy()
            server_env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
            server_env["PORT"] = str(port)
            server_env["PAGE_FILE"] = str(PAGE)
            proc = subprocess.Popen(
                [node, str(server_js)],
                cwd=str(root),
                env=server_env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
            try:
                try:
                    _wait_for_port(port, proc)
                except Exception as exc:
                    log_handle.flush()
                    detail = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
                    self.fail(f"{exc}\n{detail}")
                base_url = f"http://127.0.0.1:{port}/"
                self._run_playwright(node, cli, passing_dir, passing["generated"][0], root / "pass-report.json", base_url, expect_pass=True)
                self._run_playwright(node, cli, failing_dir, failing["generated"][0], root / "fail-report.json", base_url, expect_pass=False)
            finally:
                _stop_server(proc, log_handle)

    def _run_playwright(self, node, cli, spec_dir, spec_name, report, base_url, *, expect_pass):
        _link_playwright(cli, spec_dir)
        output_dir = spec_dir / "playwright-output"
        config = spec_dir / "playwright.config.cjs"
        _write_playwright_config(config, spec_dir, report, output_dir)
        env = os.environ.copy()
        env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        env["ACCEPTANCE_BASE_URL"] = base_url
        completed = subprocess.run(
            [node, str(cli), "test", "--config", str(config), spec_name],
            cwd=str(spec_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
        output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        if _BROWSER_MISSING.search(output):
            line = next((item.strip() for item in output.splitlines() if _BROWSER_MISSING.search(item)), "Chromium executable is missing")
            self.skipTest(f"Chromium cannot launch: {line[:300]}")
        spec = (spec_dir / spec_name).read_text(encoding="utf-8")
        if expect_pass:
            self.assertEqual(completed.returncode, 0, output[-4000:] + "\n" + spec)
            self.assertNotIn("SyntaxError", output)
            stats = json.loads(report.read_text(encoding="utf-8"))["stats"]
            self.assertGreaterEqual(stats["expected"], 1)
            self.assertEqual(stats["unexpected"], 0)
            return
        self.assertNotEqual(completed.returncode, 0, output[-4000:])
        self.assertNotIn("SyntaxError", output)
        blob = output
        if report.is_file():
            blob += "\n" + report.read_text(encoding="utf-8", errors="replace")
            stats = json.loads(report.read_text(encoding="utf-8"))["stats"]
            self.assertGreaterEqual(stats["unexpected"], 1)
        self.assertIn("Missing kiln mark", blob)


if __name__ == "__main__":
    unittest.main()

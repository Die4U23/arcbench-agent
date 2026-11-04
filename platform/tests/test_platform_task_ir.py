"""Task IR parser and runtime layout detector. Invented fixtures only."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform-agent"))

from agent.platform_task_ir import build_task_ir, detect_runtime, write_task_ir

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ir"
ACTIONS = {"open", "click", "fill", "check", "select", "submit", "reload", "unknown"}
KINDS = {"visible", "hidden", "text", "count", "persisted", "url", "unknown"}
SESSIONS = {"anonymous", "authenticated", "unknown"}


class PlatformTaskIrTests(unittest.TestCase):
    def test_full_parse_of_annotated_kiln_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._assert_kiln_schedule(Path(tmp))

    def _assert_kiln_schedule(self, project: Path):
        ir = build_task_ir(FIXTURES / "kiln-schedule.yaml", project)

        self.assertEqual(ir["version"], 1)
        self.assertEqual(ir["task"], {
            "slug": "community-kiln-schedule",
            "title": "Community kiln schedule",
        })
        self.assertEqual(ir["rejected"], [])
        self.assertEqual(ir["runtime"]["layout"], "empty")

        items = {item["id"]: item for item in ir["work_items"]}
        self.assertEqual(set(items), {"ROOT", "REQ-BOARD", "REQ-SIGNIN", "REQ-QUEUE"})
        self.assertIsNone(items["ROOT"]["parent"])
        self.assertEqual(items["REQ-BOARD"]["parent"], "ROOT")
        self.assertEqual(items["REQ-SIGNIN"]["parent"], "ROOT")
        self.assertEqual(items["REQ-QUEUE"]["parent"], "ROOT")
        self.assertEqual(items["REQ-BOARD"]["depends_on"], [])
        self.assertEqual(items["REQ-SIGNIN"]["depends_on"], ["REQ-BOARD"])
        self.assertEqual(items["REQ-QUEUE"]["depends_on"], ["REQ-BOARD"])

        entities = {entity["id"]: entity for entity in ir["entities"]}
        self.assertEqual(entities["DATA-POTTER"]["lifecycle"], "SEED")
        self.assertEqual(entities["DATA-POTTER"]["name"], "Potter account")
        self.assertEqual(entities["DATA-POTTER"]["fields"], ["display_name", "email"])
        self.assertEqual(entities["DATA-POTTER"]["seed_records"][0]["email"], "ada@kiln.example")
        self.assertEqual(entities["DATA-POTTER"]["source"], "parsed")
        self.assertEqual(entities["DATA-KILN"]["lifecycle"], "SEED")
        self.assertEqual(entities["DATA-KILN"]["seed_records"][0]["label"], "North kiln")
        self.assertEqual(entities["DATA-SHELF"]["lifecycle"], "RUNTIME")
        self.assertEqual(entities["DATA-SHELF"]["fields"], ["clay_body", "window"])
        self.assertEqual(entities["DATA-SHELF"]["seed_records"], [])

        self.assertEqual(ir["pages"], [
            {"name": "Firing board", "path": "/board", "reference": "refs/firing-board.png"},
            {"name": "Shelf form", "path": "/reserve", "reference": "refs/shelf-form.png"},
        ])

        grouped = [element for element in ir["ui_elements"] if element["persistent"]]
        self.assertEqual(
            [element["name"] for element in grouped],
            ["Firing board", "North kiln", "Glaze notes"],
        )
        self.assertEqual({element["role"] for element in grouped}, {"heading", "link"})
        self.assertEqual(len({element["group"] for element in grouped}), 1)
        self.assertTrue(all(element["multiple"] is False for element in grouped))
        self.assertTrue(all(element["source"] == "parsed" for element in grouped))
        self.assertIn("remain available", grouped[0]["evidence"])

        hours = next(element for element in ir["ui_elements"] if element["name"] == "Public hours")
        self.assertEqual(hours["role"], "link")
        self.assertFalse(hours["persistent"])
        self.assertIsNone(hours["group"])
        chip = next(element for element in ir["ui_elements"] if element["name"] == "Shelf chip")
        self.assertEqual(chip["role"], "listitem")
        self.assertTrue(chip["multiple"])
        self.assertFalse(chip["persistent"])
        self.assertTrue(any(
            element["name"] == "窑次" and element["role"] == "button"
            for element in ir["ui_elements"]
        ))
        self.assertTrue(any(
            element["name"] == "釉料" and element["role"] == "link"
            for element in ir["ui_elements"]
        ))
        self.assertEqual(len(ir["ui_elements"]), 18)

        self.assertEqual(ir["auth"], {
            "login_entry": {"role": "link", "name": "Sign in"},
            "fields": [{"label": "Email"}, {"label": "Password"}],
            "submit": {"role": "button", "name": "Sign in"},
        })

        scenarios = {scenario["id"]: scenario for scenario in ir["scenarios"]}
        anon = scenarios["SCN-ANON"]
        self.assertEqual(anon["work_item"], "REQ-BOARD")
        self.assertEqual(anon["source"], "parsed")
        self.assertEqual(anon["given"]["session"], "anonymous")
        self.assertIsNone(anon["given"]["account"])
        self.assertIn("DATA-KILN:kiln-north", anon["given"]["data"])
        self.assertEqual(anon["when"][0]["action"], "open")
        self.assertEqual(anon["when"][0]["target"], {"role": "link", "name": "Firing board"})
        self.assertIsNone(anon["when"][0]["value"])
        self.assertEqual(anon["then"][0]["kind"], "visible")
        self.assertEqual(anon["then"][0]["target"], {"role": "link", "name": "North kiln"})

        signed_in = scenarios["SCN-AUTH"]
        self.assertEqual(signed_in["given"]["session"], "authenticated")
        self.assertEqual(signed_in["given"]["account"], "DATA-POTTER:potter-ada")
        self.assertIn("DATA-POTTER:potter-ada", signed_in["given"]["data"])
        self.assertEqual(signed_in["when"][0]["action"], "fill")
        self.assertEqual(signed_in["when"][0]["target"], {"role": "textbox", "name": "Clay body"})
        self.assertEqual(signed_in["when"][0]["value"], "stoneware")
        self.assertEqual(signed_in["when"][1]["action"], "click")
        self.assertEqual(signed_in["when"][1]["target"], {"role": "button", "name": "Reserve shelf"})
        self.assertEqual(signed_in["when"][2]["action"], "unknown")
        self.assertEqual(signed_in["when"][2]["target"], {"role": "listitem", "name": "Shelf chip"})
        self.assertEqual(signed_in["then"][0]["kind"], "visible")
        self.assertEqual(signed_in["then"][0]["target"], {"role": "status", "name": "Shelf reserved"})
        self.assertEqual(signed_in["then"][1]["kind"], "url")
        self.assertEqual(signed_in["then"][1]["value"], "/reserve")

        chinese = scenarios["SCN-CN"]
        self.assertEqual(chinese["given"]["session"], "anonymous")
        self.assertIsNone(chinese["given"]["account"])
        self.assertEqual(chinese["when"][0]["action"], "click")
        self.assertEqual(chinese["when"][0]["target"], {"role": "link", "name": "釉料"})
        self.assertEqual(chinese["then"][0]["kind"], "visible")
        self.assertEqual(chinese["then"][0]["target"], {"role": "heading", "name": "窑表"})

    def test_natural_language_without_roles_degrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._assert_natural_language(Path(tmp))

    def _assert_natural_language(self, project: Path):
        ir = build_task_ir(FIXTURES / "drying-notes.yaml", project)
        self.assertEqual(ir["task"]["slug"], "drying-rack-notes")
        self.assertEqual(ir["ui_elements"], [])
        self.assertIsNone(ir["auth"])
        self.assertEqual(ir["entities"], [])
        self.assertEqual(ir["pages"], [])
        self.assertEqual(len(ir["scenarios"]), 1)
        scenario = ir["scenarios"][0]
        self.assertEqual(scenario["id"], "SCN-NOTE")
        self.assertEqual(scenario["source"], "parsed")
        self.assertEqual(scenario["given"]["session"], "authenticated")
        self.assertIsNone(scenario["given"]["account"])
        self.assertEqual(scenario["when"][0]["action"], "unknown")
        self.assertEqual(scenario["when"][0]["target"], {"role": None, "name": None})
        self.assertIn("damp rack", scenario["when"][0]["text"])
        self.assertEqual(scenario["then"][0]["kind"], "visible")
        self.assertIn("shown", scenario["then"][0]["text"])

    def test_missing_and_renamed_fields_do_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._assert_odd_fields(Path(tmp))

    def _assert_odd_fields(self, project: Path):
        ir = build_task_ir(FIXTURES / "quiet-shelf.yaml", project)
        self.assertEqual(ir["version"], 1)
        self.assertEqual(ir["task"]["title"], "Quiet shelf hours")
        self.assertIsNone(ir["auth"])
        self.assertEqual(ir["pages"], [{
            "name": "Quiet shelf",
            "path": None,
            "reference": "refs/quiet-shelf.png",
        }])
        self.assertEqual(len(ir["entities"]), 1)
        self.assertEqual(ir["entities"][0]["name"], "Clay batch")
        self.assertIsNone(ir["entities"][0]["lifecycle"])
        self.assertEqual(ir["entities"][0]["fields"], [])
        self.assertEqual(ir["entities"][0]["seed_records"], [])
        self.assertEqual(ir["entities"][0]["source"], "parsed")
        self.assertGreaterEqual(len(ir["work_items"]), 1)
        for item in ir["work_items"]:
            self.assertIsInstance(item["depends_on"], list)
            self.assertIsInstance(item["id"], str)
        for scenario in ir["scenarios"]:
            self.assertIn(scenario["given"]["session"], SESSIONS)
            for step in scenario["when"]:
                self.assertIn(step["action"], ACTIONS)
            for step in scenario["then"]:
                self.assertIn(step["kind"], KINDS)
        self.assertTrue(any(
            step["action"] == "unknown" and "looks around" in step["text"]
            for scenario in ir["scenarios"] for step in scenario["when"]
        ))
        self.assertTrue(any(
            step["kind"] == "visible" and "shown" in step["text"]
            for scenario in ir["scenarios"] for step in scenario["then"]
        ))

        listed = project / "list.yaml"
        listed.write_text("- just a string\n", encoding="utf-8")
        odd = build_task_ir(listed, project)
        self.assertEqual(odd["work_items"], [])
        self.assertEqual(odd["scenarios"], [])
        self.assertIn("requirements root is not a mapping", odd["rejected"])

    def test_model_fill_garbage_is_rejected_without_changing_parsed_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._assert_model_fill(Path(tmp))

    def _assert_model_fill(self, project: Path):
        plain = build_task_ir(FIXTURES / "drying-notes.yaml", project)
        captured = []

        def garbage(payload):
            captured.append(payload)
            return {
                "ui_elements": [
                    "nope",
                    {
                        "role": "button",
                        "name": "Pin shelf",
                        "work_item": "REQ-NOTE",
                        "evidence": "pin the note",
                    },
                    {"role": "button", "name": "Missing shelf", "work_item": "missing"},
                ],
                "scenarios": {"id": "bad"},
                "entities": [123],
                "pages": [{"name": ""}],
                "auth": {"login_entry": {"role": "link", "name": "Sign in"}},
                "bonus": 1,
            }

        filled = build_task_ir(FIXTURES / "drying-notes.yaml", project, model_fill=garbage)
        self.assertEqual(len(captured), 1)
        self.assertIsInstance(captured[0]["fragments"], list)
        self.assertIsInstance(captured[0]["schema"], str)
        self.assertIn("ui_elements", captured[0]["schema"])
        self.assertTrue(any("damp rack" in item["text"] for item in captured[0]["fragments"]))
        self.assertEqual(
            [element for element in filled["ui_elements"] if element["source"] == "parsed"],
            plain["ui_elements"],
        )
        self.assertEqual(
            [scenario for scenario in filled["scenarios"] if scenario["source"] == "parsed"],
            plain["scenarios"],
        )
        self.assertEqual(
            [entity for entity in filled["entities"] if entity["source"] == "parsed"],
            plain["entities"],
        )
        inferred = [element for element in filled["ui_elements"] if element["source"] == "inferred"]
        self.assertEqual(len(inferred), 1)
        self.assertEqual(inferred[0]["name"], "Pin shelf")
        self.assertEqual(inferred[0]["role"], "button")
        self.assertFalse(inferred[0]["persistent"])
        self.assertIsNone(filled["auth"])
        self.assertTrue(any("ui_elements[0]" in item for item in filled["rejected"]))
        self.assertTrue(any("unknown work_item" in item for item in filled["rejected"]))
        self.assertIn("scenarios is not a list", filled["rejected"])
        self.assertTrue(any(item.startswith("entities[0]") for item in filled["rejected"]))
        self.assertTrue(any(item.startswith("pages[0]") for item in filled["rejected"]))
        self.assertIn("ignored auth", filled["rejected"])
        self.assertIn("ignored bonus", filled["rejected"])

        def boom(_payload):
            raise RuntimeError("down")

        raised = build_task_ir(FIXTURES / "drying-notes.yaml", project, model_fill=boom)
        self.assertTrue(any("RuntimeError" in item for item in raised["rejected"]))
        self.assertEqual(raised["scenarios"], plain["scenarios"])
        self.assertEqual(raised["ui_elements"], plain["ui_elements"])

        def returns_list(_payload):
            return ["nope"]

        listed = build_task_ir(FIXTURES / "drying-notes.yaml", project, model_fill=returns_list)
        self.assertIn("model_fill returned list", listed["rejected"])
        self.assertEqual(listed["scenarios"], plain["scenarios"])

    def test_detect_runtime_empty_single_package_and_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = detect_runtime(root)
            self.assertEqual(empty["layout"], "empty")
            self.assertIsNone(empty["install"])
            self.assertIsNone(empty["build"])
            self.assertIsNone(empty["start"])
            self.assertIsNone(empty["port"])
            self.assertIsNone(empty["health"])
            self.assertFalse(empty["deploy_sh"])

            missing = detect_runtime(root / "missing-board")
            self.assertEqual(missing["layout"], "empty")
            self.assertIsNone(missing["port"])
            self.assertFalse(missing["deploy_sh"])

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "package.json").write_text(
                json.dumps({
                    "name": "kiln-board",
                    "scripts": {
                        "build": "node build-board.js",
                        "start": "node server.js",
                    },
                }),
                encoding="utf-8",
                newline="\n",
            )
            (project / "deploy.sh").write_text(
                "#!/bin/sh\n"
                "npm install\n"
                "npm run build\n"
                "npm start\n"
                "# ready at /api/health\n"
                "PORT=4173\n",
                encoding="utf-8",
                newline="\n",
            )
            info = detect_runtime(project)
            self.assertEqual(info["layout"], "single-package")
            self.assertTrue(info["deploy_sh"])
            self.assertEqual(info["install"], "npm install")
            self.assertEqual(info["build"], "npm run build")
            self.assertEqual(info["start"], "npm start")
            self.assertEqual(info["port"], 4173)
            self.assertEqual(info["health"], "/api/health")
            ir = build_task_ir(FIXTURES / "drying-notes.yaml", project)
            self.assertEqual(ir["runtime"], info)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            frontend = project / "frontend"
            backend = project / "backend"
            frontend.mkdir()
            backend.mkdir()
            (frontend / "package.json").write_text(
                json.dumps({"scripts": {"build": "node build-frontend.js", "start": "node frontend.js"}}),
                encoding="utf-8",
                newline="\n",
            )
            (backend / "package.json").write_text(
                json.dumps({
                    "scripts": {
                        "build": "node build-backend.js",
                        "start": "node backend.js --port 8088",
                    },
                }),
                encoding="utf-8",
                newline="\n",
            )
            info = detect_runtime(project)
            self.assertEqual(info["layout"], "split")
            self.assertFalse(info["deploy_sh"])
            self.assertIsNone(info["install"])
            self.assertIsNone(info["health"])
            self.assertEqual(info["build"], "node build-backend.js")
            self.assertEqual(info["start"], "node backend.js --port 8088")
            self.assertEqual(info["port"], 8088)
            self.assertTrue(any("frontend/package.json" in note for note in info["notes"]))

    def test_write_task_ir_uses_utf8_and_lf(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ir = build_task_ir(FIXTURES / "kiln-schedule.yaml", project)
            write_task_ir(project, ir)
            raw = (project / "docs" / "task-ir.json").read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            self.assertNotIn(b"\r", raw)
            text = raw.decode("utf-8")
            self.assertIn("窑次", text)
            loaded = json.loads(text)
            self.assertEqual(loaded["task"]["slug"], "community-kiln-schedule")
            self.assertEqual(loaded["auth"]["login_entry"]["role"], "link")


if __name__ == "__main__":
    unittest.main()

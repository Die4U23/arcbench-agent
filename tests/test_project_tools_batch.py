from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.tools import MAX_BATCH_FILES, MAX_BATCH_OUTPUT_CHARS, MAX_FILE_CHARS, ProjectTools, TOOL_SCHEMAS


class ProjectToolsBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_dir = Path(self.temp_dir.name)
        self.tools = ProjectTools(self.project_dir)

    def test_model_file_io_schema_exposes_batch_tools_only(self) -> None:
        names = {item["function"]["name"] for item in TOOL_SCHEMAS}

        self.assertIn("read_files", names)
        self.assertIn("write_files", names)
        self.assertIn("replace_text", names)
        self.assertIn("run_project_scripts", names)
        self.assertNotIn("read_file", names)
        self.assertNotIn("write_file", names)

    def test_replace_text_changes_only_one_exact_span(self) -> None:
        source = self.project_dir / "large.js"
        source.write_text("header\n" + ("x" * 25_000) + "\nconst value = 1;\n", encoding="utf-8")
        result = json.loads(self.tools.call("replace_text", {
            "path": "large.js", "old_text": "const value = 1;", "new_text": "const value = 2;",
        }))
        self.assertEqual(result["path"], "large.js")
        self.assertTrue(source.read_text(encoding="utf-8").endswith("const value = 2;\n"))
        self.assertEqual(self.tools.written_paths, ["large.js"])
        with self.assertRaisesRegex(ValueError, "found 0"):
            self.tools.replace_text("large.js", "const value = 1;", "unused")

    def test_replace_text_rejects_ambiguous_and_unsafe_edits(self) -> None:
        source = self.project_dir / "repeated.js"
        source.write_text("same\nsame\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "found 2"):
            self.tools.replace_text("repeated.js", "same", "changed")
        self.assertEqual(source.read_text(encoding="utf-8"), "same\nsame\n")
        with self.assertRaisesRegex(ValueError, "credential"):
            self.tools.replace_text(".env", "KEY", "VALUE")

    def test_read_files_returns_multiple_files_in_one_call(self) -> None:
        (self.project_dir / "a.js").write_text("alpha", encoding="utf-8")
        (self.project_dir / "b.js").write_text("beta", encoding="utf-8")

        result = json.loads(self.tools.call("read_files", {"paths": ["a.js", "b.js"]}))

        self.assertFalse(result["truncated"])
        self.assertEqual(result["files"], [
            {"path": "a.js", "content": "alpha"},
            {"path": "b.js", "content": "beta"},
        ])

    def test_read_files_keeps_context_above_previous_twenty_thousand_character_cap(self) -> None:
        content = "x" * 25_000
        (self.project_dir / "large.js").write_text(content, encoding="utf-8")

        result = json.loads(self.tools.call("read_files", {"paths": ["large.js"]}))

        self.assertFalse(result["truncated"])
        self.assertEqual(result["files"][0]["content"], content)

    def test_run_project_scripts_returns_all_results_in_one_call(self) -> None:
        scripts = [
            {"directory": "frontend", "script": "build"},
            {"directory": "frontend", "script": "test"},
        ]
        with patch.object(self.tools, "run_project_script", side_effect=[
            json.dumps({"directory": "frontend", "command": "npm run build", "exit_code": 0, "output": "built"}),
            json.dumps({"directory": "frontend", "command": "npm run test", "exit_code": 1, "output": "failed"}),
        ]) as run_script:
            result = json.loads(self.tools.call("run_project_scripts", {"scripts": scripts}))

        self.assertEqual([item["exit_code"] for item in result["results"]], [0, 1])
        self.assertEqual(run_script.call_count, 2)

    def test_run_project_scripts_validates_batch_size(self) -> None:
        scripts = [{"directory": ".", "script": "test"}] * 4

        with self.assertRaisesRegex(ValueError, "between 1 and"):
            self.tools.run_project_scripts(scripts)

    def test_root_scripts_fail_fast_when_required_web_files_are_missing(self) -> None:
        (self.project_dir / "frontend").mkdir()
        (self.project_dir / "package.json").write_text(
            json.dumps({"scripts": {"build": "npm --prefix frontend run build", "test": "node --test backend/tests"}}),
            encoding="utf-8",
        )
        with patch("agent.tools.subprocess.run") as run:
            with self.assertRaisesRegex(FileNotFoundError, "frontend/package.json"):
                self.tools.run_project_script(".", "build")
            (self.project_dir / "frontend" / "package.json").write_text(
                json.dumps({"scripts": {"build": "node build.js"}}), encoding="utf-8",
            )
            with self.assertRaisesRegex(FileNotFoundError, "backend/tests"):
                self.tools.run_project_script(".", "test")
            (self.project_dir / "backend" / "tests").mkdir(parents=True)
            with self.assertRaisesRegex(FileNotFoundError, "executable JavaScript tests"):
                self.tools.run_project_script(".", "test")
            run.assert_not_called()

    def test_read_files_rejects_unsafe_paths_before_returning_content(self) -> None:
        (self.project_dir / "safe.js").write_text("safe", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "credential"):
            self.tools.read_files(["safe.js", ".env"])

    def test_read_files_enforces_file_count_and_output_limit(self) -> None:
        for index in range(MAX_BATCH_FILES + 1):
            (self.project_dir / f"{index}.txt").write_text("x" * MAX_FILE_CHARS, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "between 1 and"):
            self.tools.read_files([f"{index}.txt" for index in range(MAX_BATCH_FILES + 1)])

        encoded = self.tools.read_files([f"{index}.txt" for index in range(MAX_BATCH_FILES)])
        result = json.loads(encoded)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(sum(len(item["content"]) for item in result["files"]), 40_000)
        self.assertLessEqual(len(encoded), MAX_BATCH_OUTPUT_CHARS)

    def test_write_files_validates_the_whole_batch_before_writing(self) -> None:
        with self.assertRaisesRegex(ValueError, "credential"):
            self.tools.write_files([
                {"path": "frontend/app.js", "content": "app"},
                {"path": ".env", "content": "secret"},
            ])

        self.assertFalse((self.project_dir / "frontend" / "app.js").exists())
        self.assertEqual(self.tools.written_paths, [])

    def test_write_files_enforces_file_and_batch_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceeds"):
            self.tools.write_files([{"path": "large.txt", "content": "x" * (MAX_FILE_CHARS + 1)}])
        with self.assertRaisesRegex(ValueError, "between 1 and"):
            self.tools.write_files([
                {"path": f"{index}.txt", "content": "x"}
                for index in range(MAX_BATCH_FILES + 1)
            ])
        with self.assertRaisesRegex(ValueError, "batch exceeds"):
            self.tools.write_files([
                {"path": "a.txt", "content": "x" * MAX_FILE_CHARS},
                {"path": "b.txt", "content": "y" * MAX_FILE_CHARS},
                {"path": "c.txt", "content": "z" * MAX_FILE_CHARS},
                {"path": "d.txt", "content": "w" * MAX_FILE_CHARS},
                {"path": "e.txt", "content": "v" * MAX_FILE_CHARS},
            ])
        self.assertEqual(self.tools.written_paths, [])

    def test_write_files_writes_many_files_and_tracks_each_path(self) -> None:
        result = json.loads(self.tools.call("write_files", {"files": [
            {"path": "frontend/app.js", "content": "app"},
            {"path": "frontend/style.css", "content": "body {}"},
        ]}))

        self.assertEqual([item["path"] for item in result["written"]], [
            "frontend/app.js", "frontend/style.css"
        ])
        self.assertEqual((self.project_dir / "frontend" / "app.js").read_text(encoding="utf-8"), "app")
        self.assertEqual((self.project_dir / "frontend" / "style.css").read_text(encoding="utf-8"), "body {}")
        self.assertEqual(self.tools.written_paths, ["frontend/app.js", "frontend/style.css"])


if __name__ == "__main__":
    unittest.main()

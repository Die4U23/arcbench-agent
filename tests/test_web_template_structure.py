from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent.verify import prepare_web_template_structure, verify_web_template_structure


class WebTemplateStructureTests(unittest.TestCase):
    def test_preparation_creates_required_directories_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "project"

            prepare_web_template_structure(project_dir)
            prepare_web_template_structure(project_dir)

            self.assertTrue((project_dir / "frontend").is_dir())
            self.assertTrue((project_dir / "backend").is_dir())
            result = verify_web_template_structure(project_dir)
            self.assertFalse(result.passed)
            self.assertIn("frontend/package.json is missing", result.summary())

    def test_preparation_fails_if_required_directory_path_is_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "frontend").write_text("conflict", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "frontend/"):
                prepare_web_template_structure(project_dir)

    def test_fails_when_required_directories_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = verify_web_template_structure(Path(temp_dir))

        self.assertFalse(result.passed)
        self.assertEqual(len(result.checks), 1)
        self.assertIn("frontend/", result.summary())
        self.assertIn("backend/", result.summary())

    def test_passes_when_required_directories_are_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "frontend").mkdir()
            (project_dir / "backend").mkdir()
            (project_dir / "frontend" / "package.json").write_text(
                json.dumps({"scripts": {"build": "node build.js"}}),
                encoding="utf-8",
            )

            result = verify_web_template_structure(project_dir)

        self.assertTrue(result.passed)
        self.assertEqual(result.checks[0].exit_code, 0)

    def test_fails_when_frontend_build_script_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "frontend").mkdir()
            (project_dir / "backend").mkdir()
            (project_dir / "frontend" / "package.json").write_text(
                json.dumps({"scripts": {"test": "node test.js"}}),
                encoding="utf-8",
            )

            result = verify_web_template_structure(project_dir)

        self.assertFalse(result.passed)
        self.assertIn("scripts.build", result.summary())

    def test_fails_when_frontend_manifest_is_not_a_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "frontend").mkdir()
            (project_dir / "backend").mkdir()
            (project_dir / "frontend" / "package.json").write_text("[]", encoding="utf-8")

            result = verify_web_template_structure(project_dir)

        self.assertFalse(result.passed)
        self.assertIn("scripts.build", result.summary())

    def test_fails_when_frontend_manifest_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "frontend").mkdir()
            (project_dir / "backend").mkdir()

            result = verify_web_template_structure(project_dir)

        self.assertFalse(result.passed)
        self.assertIn("frontend/package.json is missing", result.summary())

    def test_reports_only_the_directory_that_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "frontend").mkdir()
            (project_dir / "frontend" / "package.json").write_text(
                json.dumps({"scripts": {"build": "node build.js"}}),
                encoding="utf-8",
            )

            result = verify_web_template_structure(project_dir)

        self.assertFalse(result.passed)
        self.assertIn("backend/", result.summary())
        self.assertNotIn("frontend/package.json is missing", result.summary())
        self.assertNotIn("scripts.build", result.summary())


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
import hashlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "platform-agent"))
sys.path.insert(0, str(root / "platform-agent/arcbench-agent-runtime/src"))

from agent.platform_inputs import bundle_kwargs, resolve_task_inputs
from agent.task_bundle import load_task_bundle


REQUIREMENTS = """\
id: ROOT
name: Café Widgets
type: FOLDER
description: A synthetic application.
environment: {}
data: []
scenarios:
  - id: ROOT-SCENARIO
    name: Root behavior
    steps:
      - Given: A visitor exists.
        When: The page opens.
        Then: The page is visible.
children:
  - id: REQ-1
    name: Child behavior
    type: ATOMIC
    description: Show a child page.
    scenarios:
      - id: CHILD-SCENARIO
        name: Child behavior
        steps:
          - Given: The visitor is ready.
            When: An action occurs.
            Then: A result is visible.
    children: []
"""


class PlatformInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def write_requirements(self, directory: Path, text: str = REQUIREMENTS) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "requirements.yaml").write_text(text, encoding="utf-8")
        return directory

    def resolve(self, requirement, **kwargs):
        kwargs.setdefault("registry", [])
        kwargs.setdefault("packaged_root", self.path / "absent-package")
        return resolve_task_inputs(requirement, **kwargs)

    def test_unknown_task_with_input_tests_and_slug_id(self):
        task = self.write_requirements(self.path / "input")
        tests = task / "tests"
        tests.mkdir()
        (tests / "flow.spec.ts").write_text(
            "test('one', async () => {});\ntest('two', async () => {});\n",
            encoding="utf-8",
        )
        inputs = self.resolve(task)
        self.assertIsNone(inputs.registry_entry)
        self.assertEqual(inputs.task_id, "cafe-widgets")
        self.assertEqual(inputs.task_name, "Café Widgets")
        self.assertEqual(inputs.tests_source, "input")
        self.assertEqual(inputs.tests_dir, tests.resolve())
        self.assertEqual(inputs.declared_tests, 2)
        self.assertEqual(inputs.scenario_count, 2)
        self.assertEqual(inputs.work_item_count, 1)
        self.assertEqual(inputs.entity_count, 0)
        self.assertEqual(inputs.notes, ())

    def test_registry_and_packaged_suite_are_ignored(self):
        task = self.write_requirements(self.path / "input")
        digest = hashlib.sha256((task / "requirements.yaml").read_bytes()).hexdigest()
        packaged = self.path / "packaged" / "not-the-slug" / "tests"
        packaged.mkdir(parents=True)
        (packaged / "hidden.spec.ts").write_text("test('hidden', () => {});\n", encoding="utf-8")
        registry = [{
            "yaml_sha256": digest,
            "task_name": "Café Widgets",
            "task_id": "not-the-slug",
            "token_limit": 999,
            "declared": 40,
        }]
        inputs = resolve_task_inputs(task, registry=registry, packaged_root=self.path / "packaged")
        self.assertIsNone(inputs.registry_entry)
        self.assertEqual(inputs.task_id, "cafe-widgets")
        self.assertEqual(inputs.tests_source, "none")
        self.assertIsNone(inputs.tests_dir)
        self.assertIsNone(inputs.declared_tests)
        self.assertEqual(inputs.token_limit, 1_570_000)

    def test_small_input_suite_is_used(self):
        task = self.write_requirements(self.path / "input")
        tests = task / "tests"
        tests.mkdir()
        (tests / "changed.spec.ts").write_text("test('changed', () => {});", encoding="utf-8")
        inputs = self.resolve(task, registry=[{"task_id": "not-the-slug", "declared": 100}])
        self.assertEqual(inputs.tests_source, "input")
        self.assertEqual(inputs.tests_dir, tests.resolve())
        self.assertEqual(inputs.declared_tests, 1)

    def test_parent_tests_directory_is_used(self):
        task = self.write_requirements(self.path / "bundle" / "requirements")
        tests = self.path / "bundle" / "tests"
        tests.mkdir()
        (tests / "flow.spec.ts").write_text("test('parent', () => {});\n", encoding="utf-8")
        inputs = self.resolve(task)
        self.assertEqual(inputs.tests_source, "input")
        self.assertEqual(inputs.tests_dir, tests.resolve())
        self.assertEqual(inputs.declared_tests, 1)

    def test_requirement_tests_win_over_parent(self):
        task = self.write_requirements(self.path / "bundle" / "requirements")
        parent_tests = self.path / "bundle" / "tests"
        parent_tests.mkdir()
        (parent_tests / "parent.spec.ts").write_text("test('parent', () => {});\n", encoding="utf-8")
        own = task / "tests"
        own.mkdir()
        (own / "own.spec.ts").write_text("test('own', () => {});\n", encoding="utf-8")
        inputs = self.resolve(task)
        self.assertEqual(inputs.tests_dir, own.resolve())
        self.assertEqual(inputs.tests_source, "input")

    def test_nested_tests_outside_the_two_locations_are_ignored(self):
        task = self.write_requirements(self.path / "input")
        nested = task / "extra" / "tests"
        nested.mkdir(parents=True)
        (nested / "flow.spec.ts").write_text("test('nested', () => {});\n", encoding="utf-8")
        inputs = self.resolve(task)
        self.assertEqual(inputs.tests_source, "none")
        self.assertIsNone(inputs.tests_dir)

    def test_empty_tests_directory_is_input_without_declared_calls(self):
        task = self.write_requirements(self.path / "input")
        tests = task / "tests"
        tests.mkdir()
        inputs = self.resolve(task)
        self.assertEqual(inputs.tests_source, "input")
        self.assertEqual(inputs.tests_dir, tests.resolve())
        self.assertIsNone(inputs.declared_tests)

    def test_spec_suffixes_are_unified(self):
        task = self.write_requirements(self.path / "input")
        tests = task / "tests"
        tests.mkdir()
        (tests / "flow.test.ts").write_text("test('a', () => {});\ntest('b', () => {});", encoding="utf-8")
        (tests / "more.spec.mjs").write_text("test('c', () => {});", encoding="utf-8")
        inputs = self.resolve(task)
        self.assertEqual(inputs.tests_source, "input")
        self.assertEqual(inputs.declared_tests, 3)

    def test_no_tests_anywhere(self):
        inputs = self.resolve(self.write_requirements(self.path / "input"))
        self.assertEqual(inputs.tests_source, "none")
        self.assertIsNone(inputs.tests_dir)
        self.assertIsNone(inputs.declared_tests)

    def test_missing_name_uses_generic_task_id(self):
        text = REQUIREMENTS.replace("name: Café Widgets\n", "", 1)
        inputs = self.resolve(self.write_requirements(self.path / "input", text))
        self.assertIsNone(inputs.task_name)
        self.assertEqual(inputs.task_id, "task")

    def test_ambiguous_requirements_raises(self):
        self.write_requirements(self.path / "a")
        self.write_requirements(self.path / "b")
        with self.assertRaisesRegex(FileNotFoundError, "2 matches"):
            self.resolve(self.path)

    def test_token_limit_grows_with_size_and_caps(self):
        small = self.resolve(self.write_requirements(self.path / "small"))
        self.assertEqual(
            (small.scenario_count, small.work_item_count, small.entity_count),
            (2, 1, 0),
        )
        self.assertEqual(small.token_limit, 1_570_000)

        medium_text = REQUIREMENTS.replace(
            "data: []\n",
            "data:\n  - id: DATA-1\n    entity: sample record\n",
            1,
        ).replace(
            "    children: []\n",
            "    scenarios:\n"
            "      - id: CHILD-SCENARIO\n"
            "        name: Child behavior\n"
            "        steps: []\n"
            "      - id: CHILD-EXTRA\n"
            "        name: Extra behavior\n"
            "        steps: []\n"
            "    children: []\n",
            1,
        )
        medium = self.resolve(self.write_requirements(self.path / "medium", medium_text))
        self.assertEqual(medium.entity_count, 1)
        self.assertGreater(medium.scenario_count, small.scenario_count)
        self.assertGreater(medium.token_limit, small.token_limit)
        self.assertLess(medium.token_limit, 6_000_000)

        children = "\n".join(
            f"  - id: REQ-{index}\n    name: Item {index}\n    type: ATOMIC\n    children: []"
            for index in range(500)
        )
        huge_text = (
            "id: ROOT\nname: Wide Catalog\ntype: FOLDER\nchildren:\n" + children + "\n"
        )
        huge = self.resolve(self.write_requirements(self.path / "huge", huge_text))
        self.assertEqual(huge.work_item_count, 500)
        self.assertEqual(huge.token_limit, 6_000_000)
        self.assertGreater(huge.token_limit, medium.token_limit)

    def test_explicit_default_overrides_derived_cap_not_environment(self):
        task = self.write_requirements(self.path / "input")
        self.assertEqual(self.resolve(task, default_token_limit=1_765_432).token_limit, 1_765_432)
        with patch.dict(os.environ, {"ARC_TOKEN_LIMIT": "2000000"}):
            self.assertEqual(self.resolve(task).token_limit, 1_570_000)

    def test_load_task_bundle_accepts_unknown_input_tests(self):
        task = self.write_requirements(self.path / "input")
        tests = task / "tests"
        tests.mkdir()
        (tests / "flow.spec.ts").write_text("test('one', () => {});", encoding="utf-8")
        inputs = self.resolve(task)
        bundle = load_task_bundle(**bundle_kwargs(inputs))
        self.assertEqual(bundle.task_id, "cafe-widgets")
        self.assertTrue(any(record.relative_path == "tests/flow.spec.ts" for record in bundle.files))

    def test_load_task_bundle_accepts_no_tests(self):
        inputs = self.resolve(self.write_requirements(self.path / "input"))
        bundle = load_task_bundle(**bundle_kwargs(inputs))
        self.assertEqual(bundle.import_graph, {})
        self.assertTrue(any("TEST_SUITE_MISSING" in item for item in bundle.diagnostics))
        bundle.assert_integrity()


if __name__ == "__main__":
    unittest.main()

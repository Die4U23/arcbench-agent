"""Cache-friendly context ordering without changing tool or acceptance semantics."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent.prompt_context import requirement_context
from agent.tools import ProjectTools
from scripts.benchmark_cache_sessions import common_prefix, legacy_requirement_context, summarize
from tests.test_llm_budget import _FakeMessage, _make_client, _write_call


def tree_fixture():
    return {"id": "ROOT", "name": "An ordinary project", "children": [
        {"id": f"REQ-{index}", "description": f"Maintain state transition {index}.",
         "children": [{"id": f"REQ-{index}.1", "description": f"Observe output {index} after save."}]}
        for index in range(1, 4)]}


class PromptCacheTests(unittest.TestCase):
    def test_shared_project_precedes_scope_and_is_serialized_once(self):
        tree = tree_fixture()
        untouched = deepcopy(tree)
        contexts = [requirement_context("web", dict(node, project_requirements=tree)) for node in tree["children"]]
        delimiter = "\n\nRequirement subtree (current implementation scope):\n"
        prefixes = [value.split(delimiter)[0] for value in contexts]
        self.assertTrue(all(value == prefixes[0] for value in prefixes))
        self.assertEqual(tree, untouched)
        self.assertEqual(contexts[0].count('"id": "ROOT"'), 1)
        project = json.loads(prefixes[0].split("Project requirements (shared context):\n")[1])
        scope = json.loads(contexts[0].split(delimiter)[1])
        self.assertEqual(project, tree)
        self.assertEqual(scope, tree["children"][0])

    def test_key_insertion_order_does_not_reset_shared_prefix(self):
        tree = tree_fixture()
        reordered = dict(reversed(list(tree.items())))
        first = requirement_context("web", dict(tree["children"][0], project_requirements=tree))
        second = requirement_context("web", dict(tree["children"][0], project_requirements=reordered))
        self.assertEqual(first, second)

    def test_root_scope_keeps_every_requirement_without_duplicate_tree(self):
        tree = tree_fixture()
        result = requirement_context("web", tree)
        self.assertEqual(result.count('"name": "An ordinary project"'), 1)
        for node in tree["children"]:
            self.assertEqual(result.count(node["description"]), 1)
        self.assertIn('"id": "ROOT"', result)

    def test_unknown_standalone_scope_is_unchanged(self):
        subtree = {"id": "REQ-X", "description": "Standalone task"}
        self.assertEqual(requirement_context("web", subtree), legacy_requirement_context("web", subtree))

    def test_shared_context_is_not_stale_after_project_change(self):
        tree = tree_fixture()
        scoped = dict(tree["children"][0], project_requirements=tree)
        before = requirement_context("web", scoped)
        tree["children"][1]["description"] = "A revised explicit requirement"
        after = requirement_context("web", scoped)
        self.assertNotEqual(before, after)
        self.assertIn("A revised explicit requirement", after)

    def test_scope_transition_improves_prefix_without_padding(self):
        tree = tree_fixture()
        scopes = [dict(node, project_requirements=tree) for node in tree["children"]]
        before = [legacy_requirement_context("web", node) for node in scopes]
        after = [requirement_context("web", node) for node in scopes]
        self.assertGreater(common_prefix(after[0], after[1]), common_prefix(before[0], before[1]))
        self.assertLess(sum(map(len, after)), sum(map(len, before)))

    def test_planning_and_implementation_receive_same_stable_project_order(self):
        tree = tree_fixture()
        scope = dict(tree["children"][0], project_requirements=tree)
        model = _make_client(None, None, [_FakeMessage(None, "plan"), _FakeMessage(None, "done")])
        model.plan("web", scope)
        with TemporaryDirectory() as directory:
            model.implement(task_type="web", subtree=scope, plan="one bounded plan", project_tools=ProjectTools(Path(directory)))
        planning, implementation = model.client.completions.requests
        expected = requirement_context("web", scope)
        self.assertTrue(planning["messages"][1]["content"].startswith(expected))
        self.assertTrue(implementation["messages"][1]["content"].startswith(expected))
        # The first request in both stages knows the actual image's package versions.
        catalog = json.loads((Path(__file__).resolve().parents[1] /
            "sandbox/toolchain/package.json").read_text())["dependencies"]
        for request in (planning, implementation):
            guidance = request["messages"][0]["content"]
            for name, version in catalog.items():
                self.assertIn(json.dumps(name) + ": " + json.dumps(version), guidance)

    def test_retry_retains_prefix_and_schema_while_explicitly_changing_mode(self):
        model = _make_client(None, None, [
            _FakeMessage([_write_call("partial.js")], finish_reason="length"),
            _FakeMessage([_write_call("complete.js")]), _FakeMessage(None, "done")],
            {"MODEL": "deepseek-flash", "OPENAI_BASE_URL": "https://api.deepseek.com"})
        with TemporaryDirectory() as directory:
            tools = ProjectTools(Path(directory))
            model.implement(task_type="web", subtree=tree_fixture(), plan="preserve prefix", project_tools=tools)
            self.assertFalse((Path(directory) / "partial.js").exists())
            self.assertTrue((Path(directory) / "complete.js").exists())
        first, retry, continued = model.client.completions.requests
        self.assertEqual(first["messages"], retry["messages"][:-1])
        self.assertEqual(first["tools"], retry["tools"])
        self.assertEqual(first["tools"], continued["tools"])
        self.assertEqual(first["tool_choice"], "auto")
        self.assertIsInstance(retry["tool_choice"], dict)
        self.assertEqual(first["extra_body"]["thinking"]["type"], "enabled")
        self.assertEqual(retry["extra_body"]["thinking"]["type"], "disabled")

    def test_proxy_reports_absolute_chars_and_separates_mode_assumptions(self):
        messages = [{"role": "user", "content": "fixed task"}]
        sample = {"model": "deepseek-flash", "messages": messages, "tools": [], "tool_choice": "auto"}
        result = summarize([sample, dict(sample, tool_choice={"type": "function", "function": {"name": "edit"}})])
        self.assertGreater(result["best_prior_shared_prefix_chars"], 0)
        self.assertEqual(result["same_mode_shared_prefix_chars"], 0)
        self.assertEqual(result["input_chars"], result["same_mode_fresh_chars"])
        self.assertEqual(result["tool_schema_variants"], 1)


if __name__ == "__main__":
    unittest.main()

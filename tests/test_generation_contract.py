from __future__ import annotations

from copy import deepcopy
import unittest

from agent.generation_contract import generation_contract_context


def ticket_tree() -> dict:
    return {"id": "ROOT", "name": "Railway Ticket Booking Demo", "children": [
        {"id": requirement, "name": requirement}
        for requirement in ("REQ-1.1", "REQ-1.2", "REQ-2.1", "REQ-2.2", "REQ-3.1", "REQ-3.2")
    ]}


class GenerationContractTests(unittest.TestCase):
    def context(self, scope: str) -> str:
        return generation_contract_context({"id": scope, "project_requirements": ticket_tree()})

    def test_other_or_incomplete_projects_and_unknown_scopes_are_untouched(self):
        other = ticket_tree()
        other["name"] = "Other ticket application"
        incomplete = ticket_tree()
        incomplete["children"].pop()
        for tree in ({}, {"project_requirements": None}, other, incomplete):
            with self.subTest(tree=tree):
                self.assertEqual(generation_contract_context(tree), "")
        for scope in ("REQ-1", "REQ-1.1", "REQ-9", "REQ-2.10"):
            with self.subTest(scope=scope):
                self.assertEqual(self.context(scope), "")

    def test_search_scope_has_location_rules_without_booking_states(self):
        context = self.context("REQ-2.1")
        self.assertIn("each departure/arrival location in its own visible text node", context)
        self.assertIn("do not repeat the city", context)
        self.assertNotIn("signed out", context)
        self.assertNotIn("Invalid Place order", context)
        self.assertNotIn("Please confirm", context)

    def test_guest_scope_preserves_read_only_content_and_protects_submission(self):
        for scope in ("REQ-2.2", "REQ-3.1"):
            with self.subTest(scope=scope):
                context = self.context(scope)
                self.assertIn("Train Information", context)
                self.assertIn("no working booking submission", context)
                self.assertIn("read-only summary", context)
                self.assertNotIn("Please confirm the following information", context)
                self.assertNotIn("Invalid Place order", context)

    def test_booking_scope_distinguishes_review_and_validation_states(self):
        context = self.context("REQ-3.2")
        for evidence in (
            "one selected-journey summary",
            "Please confirm the following information",
            "Only Confirm creates the booking",
            "Back retains entered values",
            "missing name/nationality (required)",
            "short name (at least 2)",
            "short ID (at least 6)",
            "original upper bounds, format and terms rules",
        ):
            with self.subTest(evidence=evidence):
                self.assertIn(evidence, context)
        self.assertNotIn("Search results:", context)
        self.assertNotIn("signed out:", context)

    def test_folder_scope_includes_only_its_descendants(self):
        context = self.context("REQ-2")
        self.assertIn("Search results:", context)
        self.assertIn("Selected journey / signed out:", context)
        self.assertNotIn("Invalid Place order:", context)
        self.assertNotIn("Entry / review / success:", context)

    def test_root_is_compact_without_test_bodies_or_input_mutation(self):
        tree = ticket_tree()
        before = deepcopy(tree)
        context = generation_contract_context(tree)
        self.assertEqual(tree, before)
        self.assertEqual(context.count("\n- "), 5)
        self.assertLess(len(context), 2300)
        self.assertNotIn("test(", context)
        self.assertIn("does not replace the original requirements", context)
        self.assertEqual(context.count("one selected-journey summary"), 1)


if __name__ == "__main__":
    unittest.main()

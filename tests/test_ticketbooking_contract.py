from __future__ import annotations

import unittest

from agent.ticketbooking_contract import PUBLIC_CASES, public_acceptance_context


def ticket_tree() -> dict:
    return {"id": "ROOT", "name": "Railway Ticket Booking Demo", "children": [
        {"id": requirement, "name": requirement, "description": "contract"}
        for requirement in ("REQ-1.1", "REQ-1.2", "REQ-2.1", "REQ-2.2", "REQ-3.1", "REQ-3.2")
    ]}


class PublicAcceptanceContextTests(unittest.TestCase):
    def test_full_project_includes_all_thirty_public_bodies_and_shared_operations(self):
        context = public_acceptance_context(ticket_tree())
        self.assertEqual(context.count("\ntest("), 30)
        self.assertIn("page.getByLabel(/^name$/i).fill", context)
        self.assertIn("page.getByLabel(/^date$/i).fill", context)
        self.assertIn("page.getByRole('link', { name: /sign out/i })", context)
        self.assertIn("NOT the full official executable suite", context)

    def test_module_context_selects_its_cases_without_mutating_input(self):
        tree = ticket_tree()
        module = {"id": "REQ-1", "project_requirements": tree}
        before = repr(module)
        context = public_acceptance_context(module)
        self.assertEqual(context.count("\ntest("), 12)
        self.assertNotIn("test('REQ-2", context)
        self.assertEqual(repr(module), before)

    def test_atomic_context_selects_only_its_cases(self):
        context = public_acceptance_context({"id": "REQ-3.2", "project_requirements": ticket_tree()})
        self.assertEqual(context.count("\ntest("), 6)
        self.assertIn("getByRole('textbox', { name: /^name$/i })", context)

    def test_contract_is_not_attached_to_other_tasks_or_incomplete_same_name(self):
        tree = ticket_tree()
        tree["name"] = "Other ticket application"
        self.assertEqual(public_acceptance_context(tree), "")
        tree = ticket_tree()
        tree["children"].pop()
        self.assertEqual(public_acceptance_context(tree), "")
        self.assertEqual(public_acceptance_context({"project_requirements": None}), "")

    def test_published_case_set_is_complete_and_unique(self):
        self.assertEqual(len(PUBLIC_CASES), 30)
        self.assertEqual(len({source for _, source in PUBLIC_CASES}), 30)


if __name__ == "__main__":
    unittest.main()

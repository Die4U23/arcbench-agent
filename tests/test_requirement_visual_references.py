from __future__ import annotations

import unittest
from pathlib import Path

from agent.requirements import load_requirement_tree
from agent.visual_acceptance import collect_visual_references
from agent.llm import ModelClient


class RequirementVisualReferenceTests(unittest.TestCase):
    def test_final_yaml_task_exposes_embedded_reference_images(self) -> None:
        task_dir = Path(__file__).resolve().parents[1] / "template" / "requirements"
        tree = load_requirement_tree(task_dir)

        self.assertEqual(collect_visual_references(tree), [])
        references = [reference for _, reference in ModelClient._visual_references(tree)]

        self.assertEqual(
            references,
            [
                "./reference/register.png",
                "./reference/login.png",
                "./reference/search-form.png",
                "./reference/search-results.png",
                "./reference/booking-page.png",
            ],
        )
        self.assertTrue(all((task_dir / reference).is_file() for reference in references))


if __name__ == "__main__":
    unittest.main()

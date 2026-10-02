from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from agent.requirements import load_requirement_tree
from agent.visual_acceptance import collect_visual_references
from agent.llm import ModelClient


class RequirementVisualReferenceTests(unittest.TestCase):
    def test_yaml_task_exposes_embedded_optional_reference_images(self) -> None:
        expected = ["./reference/" + name + ".png" for name in (
            "register", "login", "search-form", "search-results", "booking-page",
        )]
        # The downloaded platform template is ignored and absent in a fresh
        # checkout. Exercise the same YAML image-reference contract locally.
        with tempfile.TemporaryDirectory() as directory:
            task_dir = Path(directory)
            payload = {"id": "ROOT", "children": [
                {"id": f"REQ-{index}",
                 "description": f"Optional visual reference: ![image]({reference})"}
                for index, reference in enumerate(expected, 1)
            ]}
            (task_dir / "requirements.yaml").write_text(json.dumps(payload), encoding="utf-8")
            for reference in expected:
                image = task_dir / reference
                image.parent.mkdir(exist_ok=True)
                image.write_bytes(b"image fixture")
            tree = load_requirement_tree(task_dir)
            self.assertEqual(collect_visual_references(tree), [])
            references = [reference for _, reference in ModelClient._visual_references(tree)]
            self.assertEqual(references, expected)
            self.assertTrue(all((task_dir / reference).is_file() for reference in references))


if __name__ == "__main__":
    unittest.main()

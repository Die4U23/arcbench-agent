from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class RequirementModule:
    node_id: str
    name: str
    subtree: dict[str, Any]


def load_requirement_tree(requirement_dir: Path) -> dict[str, Any]:
    path = requirement_dir / "requirements.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"requirements.yaml not found: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or str(payload.get("id", "")).strip() != "ROOT":
        raise ValueError("requirements.yaml must contain a ROOT mapping")
    if not isinstance(payload.get("children"), list):
        raise ValueError("ROOT must contain a children list")
    if not payload["children"]:
        raise ValueError("ROOT must contain at least one child requirement")
    return payload


def root_modules(tree: dict[str, Any]) -> list[RequirementModule]:
    modules: list[RequirementModule] = []
    for index, child in enumerate(tree["children"], start=1):
        if not isinstance(child, dict):
            raise ValueError(f"ROOT child {index} must be a mapping")
        node_id = str(child.get("id") or child.get("req_id") or "").strip()
        if not node_id:
            raise ValueError(f"ROOT child {index} is missing id")
        modules.append(
            RequirementModule(
                node_id=node_id,
                name=str(child.get("name") or node_id).strip(),
                subtree=child,
            )
        )
    return modules


def walk_requirement_ids(node: dict[str, Any]) -> list[str]:
    result: list[str] = []

    def visit(current: Any) -> None:
        if not isinstance(current, dict):
            return
        node_id = str(current.get("id") or current.get("req_id") or "").strip()
        if node_id:
            result.append(node_id)
        children = current.get("children", [])
        if isinstance(children, list):
            for child in children:
                visit(child)

    visit(node)
    return result

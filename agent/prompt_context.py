"""Put immutable project requirements before changing module and repair scope."""
from __future__ import annotations

import json
from typing import Any


def requirement_context(task_type: str, subtree: dict[str, Any]) -> str:
    project = subtree.get("project_requirements")
    scope = {key: value for key, value in subtree.items() if key != "project_requirements"}
    if not isinstance(project, dict):
        if subtree.get("id") != "ROOT" or not isinstance(subtree.get("children"), list):
            # Standalone scopes have no shared project evidence to hoist.
            return (f"Task type: {task_type}\nRequirement subtree:\n"
                    + json.dumps(subtree, ensure_ascii=False, indent=2))
        project = subtree
    if scope == project:
        # The complete tree is already present once; retain an explicit scope.
        scope = {"id": project.get("id", "ROOT")}
    return (
        f"Task type: {task_type}\nProject requirements (shared context):\n"
        + json.dumps(project, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n\nRequirement subtree (current implementation scope):\n"
        + json.dumps(scope, ensure_ascii=False, indent=2, sort_keys=True)
    )

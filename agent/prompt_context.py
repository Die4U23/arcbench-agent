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
    else:
        scope_id = scope.get("id")
        pending, matches = [project], []
        while pending:
            node = pending.pop()
            if node.get("id") == scope_id:
                matches.append(node)
            children = node.get("children", [])
            if isinstance(children, list):
                pending.extend(child for child in children if isinstance(child, dict))
        # Refer only to a unique, byte-equivalent data structure. A partial or
        # augmented repair scope must keep its extra constraints verbatim.
        if isinstance(scope_id, str) and scope_id and len(matches) == 1 and matches[0] == scope:
            scope = {"id": scope_id}
    return (
        f"Task type: {task_type}\nProject requirements (shared context):\n"
        + json.dumps(project, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n\nAn id-only scope selects that complete shared subtree, including all descendants."
        + "\n\nRequirement subtree (current implementation scope):\n"
        + json.dumps(scope, ensure_ascii=False, indent=2, sort_keys=True)
    )

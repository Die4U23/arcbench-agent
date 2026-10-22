"""Put immutable project requirements before changing module and repair scope."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def offline_dependency_context(task_type: str) -> str:
    if task_type == "web":
        # Controller-owned catalog, also used to build the offline worker image.
        # Supply it before generation, rather than only after npm has failed.
        catalog = Path(__file__).resolve().parent.parent / "sandbox/toolchain/package.json"
        dependencies = json.loads(catalog.read_text(encoding="utf-8"))["dependencies"]
        return (
            "\n\n"
            "Execution uses an offline npm cache. For all package.json dependencies and "
            "devDependencies, choose only the following packages and pin their exact versions "
            "(no ^, ~, latest, or remembered older versions). Use Node built-ins for other "
            "needs. Preserve required behavior and real tests; do not remove tests to avoid "
            "a dependency. Keep existing manifests/lockfiles consistent with these versions "
            "before the first build. Do not assume additional packages can be downloaded.\n"
            + json.dumps(dependencies, sort_keys=True) + "\n\n"
        )
    return ""


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

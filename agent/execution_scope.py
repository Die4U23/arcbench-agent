"""Choose a bounded implementation scope without dropping requirements."""
from __future__ import annotations

import json
from .requirements import RequirementModule, walk_requirement_ids


def execution_modules(tree: dict, modules: list[RequirementModule]) -> list[RequirementModule]:
    """Small complete contracts share one planning/source context.

    Larger or underspecified trees keep the existing module allocation. This
    only changes scheduling; the original tree and all requirement IDs survive.
    """
    if len(modules) < 2 or len(json.dumps(tree, ensure_ascii=False)) > 24_000:
        return modules
    leaves = []
    def visit(node):
        children = node.get('children', [])
        if children:
            for child in children:
                if isinstance(child, dict):
                    visit(child)
        else:
            leaves.append(node)
    visit(tree)
    if not 2 <= len(leaves) <= 4 or any(not node.get('description') for node in leaves):
        return modules
    scheduled = {key for module in modules for key in walk_requirement_ids(module.subtree)}
    required = set(walk_requirement_ids(tree)) - {str(tree.get('id', 'ROOT'))}
    if scheduled != required:
        return modules
    return [RequirementModule(str(tree.get('id', 'ROOT')), str(tree.get('name', 'Application')), tree)]

"""Resolve platform-supplied requirements and tests without executing them.

Every task, including one this package has never seen, uses the same path.
``registry`` and ``packaged_root`` stay in the signature for existing callers
and are ignored. Identity is the slug of the requirement name. Tests come only
from a ``tests/`` directory on the requirement directory or its parent.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


# Default run cap from requirement size. Generation applies ARC_TOKEN_LIMIT
# later through settings.token_limit and does not read it here.
#
# clamp(1_500_000 + 30_000*scenarios + 10_000*work_items + 5_000*entities,
#       1_500_000, 6_000_000)
#
# scenarios: number of entries in every ``scenarios`` list.
# work_items: atomic requirements, using the task-bundle rule — type ATOMIC,
#   or a node whose id is not ROOT, whose type is omitted, and which has no
#   children. Only the requirement tree (the root and ``children``) is walked.
# entities: mappings stored in every ``data`` list.
TOKEN_FLOOR = 1_500_000
TOKEN_CAP = 6_000_000
TOKEN_PER_SCENARIO = 30_000
TOKEN_PER_WORK_ITEM = 10_000
TOKEN_PER_ENTITY = 5_000

SPEC_SUFFIXES = (".spec.ts", ".spec.js", ".spec.mjs", ".spec.cjs", ".test.ts")


@dataclass(frozen=True)
class TaskInputs:
    requirement_dir: Path
    requirements_path: Path
    tests_dir: Path | None
    tests_source: str
    task_id: str
    task_name: str | None
    registry_entry: dict | None
    token_limit: int
    declared_tests: int | None
    scenario_count: int
    notes: tuple[str, ...]
    work_item_count: int = 0
    entity_count: int = 0


def _requirement_dir(argument: str | Path) -> Path:
    path = Path(argument)
    if path.is_file():
        if path.name != "requirements.yaml":
            raise FileNotFoundError(f"requirements.yaml not found under {path}")
        return path.parent.resolve()
    direct = path / "requirements.yaml"
    if direct.is_file():
        return path.resolve()
    found = [
        candidate
        for candidate in path.rglob("requirements.yaml")
        if candidate.is_file() and "__MACOSX" not in candidate.parts
    ]
    if len(found) != 1:
        raise FileNotFoundError(
            f"requirements.yaml not found uniquely under {path} ({len(found)} matches)"
        )
    return found[0].parent.resolve()


def _is_spec(path: Path) -> bool:
    return path.is_file() and path.name.endswith(SPEC_SUFFIXES) and "node_modules" not in path.parts


def _spec_files(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return [file for file in sorted(path.rglob("*")) if _is_spec(file)]


def _scenario_count(value: Any) -> int:
    if isinstance(value, dict):
        count = 0
        for key, child in value.items():
            if key == "scenarios" and isinstance(child, list):
                count += len(child)
            count += _scenario_count(child)
        return count
    if isinstance(value, list):
        return sum(_scenario_count(child) for child in value)
    return 0


def _work_item_count(node: Any) -> int:
    """Count atomic requirements in the root node and its ``children`` tree."""
    if not isinstance(node, dict):
        return 0
    children = node.get("children")
    if not isinstance(children, list):
        children = []
    kind = node.get("type")
    node_id = node.get("id", node.get("req_id"))
    node_id = node_id.strip() if isinstance(node_id, str) else None
    count = 0
    if kind == "ATOMIC" or (kind is None and not children and node_id not in (None, "ROOT")):
        count = 1
    return count + sum(_work_item_count(child) for child in children)


def _entity_count(value: Any) -> int:
    """Count data-registry records: each mapping in a ``data`` list."""
    if isinstance(value, dict):
        count = 0
        for key, child in value.items():
            if key == "data" and isinstance(child, list):
                count += sum(1 for item in child if isinstance(item, dict))
            count += _entity_count(child)
        return count
    if isinstance(value, list):
        return sum(_entity_count(child) for child in value)
    return 0


def derived_token_limit(scenario_count: int, work_item_count: int, entity_count: int = 0) -> int:
    """Return the size-based default token cap. See the module formula."""
    raw = (
        TOKEN_FLOOR
        + TOKEN_PER_SCENARIO * max(0, int(scenario_count))
        + TOKEN_PER_WORK_ITEM * max(0, int(work_item_count))
        + TOKEN_PER_ENTITY * max(0, int(entity_count))
    )
    return min(TOKEN_CAP, max(TOKEN_FLOOR, raw))


def _slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", ascii_name.lower())).strip("-")


def _input_tests(requirement_dir: Path) -> Path | None:
    """Use ``tests/`` on the requirement directory, otherwise on its parent."""
    for candidate in (requirement_dir / "tests", requirement_dir.parent / "tests"):
        if candidate.is_dir():
            return candidate.resolve()
    return None


def _test_call_count(tests_dir: Path | None) -> int | None:
    if tests_dir is None:
        return None
    files = _spec_files(Path(tests_dir))
    if not files:
        return None
    return sum(
        len(re.findall(r"\btest\s*\(", file.read_text(encoding="utf-8-sig")))
        for file in files
    )


def resolve_task_inputs(
    requirements_arg,
    *,
    registry: list[dict] | None = None,
    packaged_root: Path | None = None,
    default_token_limit: int | None = None,
) -> TaskInputs:
    """Resolve generic platform inputs. Registry and packaged tests are ignored."""
    del registry, packaged_root
    requirement_dir = _requirement_dir(requirements_arg)
    requirements_path = requirement_dir / "requirements.yaml"
    loaded = yaml.safe_load(requirements_path.read_text(encoding="utf-8-sig"))
    task_name = loaded.get("name") if isinstance(loaded, dict) else None
    task_name = task_name.strip() if isinstance(task_name, str) and task_name.strip() else None
    task_id = _slug(task_name or "") or "task"

    tests_dir = _input_tests(requirement_dir)
    tests_source = "input" if tests_dir is not None else "none"
    scenario_count = _scenario_count(loaded)
    work_item_count = _work_item_count(loaded)
    entity_count = _entity_count(loaded)
    token_limit = (
        int(default_token_limit)
        if default_token_limit is not None
        else derived_token_limit(scenario_count, work_item_count, entity_count)
    )
    return TaskInputs(
        requirement_dir=requirement_dir,
        requirements_path=requirements_path,
        tests_dir=tests_dir,
        tests_source=tests_source,
        task_id=task_id,
        task_name=task_name,
        registry_entry=None,
        token_limit=token_limit,
        declared_tests=_test_call_count(tests_dir),
        scenario_count=scenario_count,
        notes=(),
        work_item_count=work_item_count,
        entity_count=entity_count,
    )


def bundle_kwargs(inputs: TaskInputs) -> dict:
    """Return keyword arguments accepted by ``load_task_bundle``."""
    metadata = inputs.registry_entry.get("source_metadata") if inputs.registry_entry else None
    return {
        "requirement_dir": inputs.requirement_dir,
        "tests_dir": inputs.tests_dir,
        "task_id": inputs.task_id,
        "published_test_count": inputs.declared_tests,
        "source_metadata": metadata,
    }

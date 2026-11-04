"""Frozen task inputs and lossless, source-addressed requirement contracts.

This module detects changed bytes; filesystem/process isolation belongs to the
controller. Import inspection covers literal static ESM/CommonJS imports only.
It is deliberately not a JavaScript parser or evidence of test collection.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

import yaml

from .requirements import _MARKDOWN_FILES, load_requirement_tree


class TaskBundleError(ValueError):
    """Malformed, incomplete, unsafe, or changed task input."""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return sorted((_thaw(item) for item in value), key=repr)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"yaml_binary_hex": value.hex()}
    return value


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _root(path: Path) -> Path:
    path = path.absolute()
    if any(_linked(part) for part in (path, *path.parents)):
        raise TaskBundleError(f"PATH_ESCAPE: linked input root {path}")
    if not path.is_dir():
        raise TaskBundleError(f"INPUT_MISSING: input directory {path}")
    return path.resolve()


def _inventory(root: Path, *, include_tests=True) -> dict[str, Path]:
    result: dict[str, Path] = {}
    folded: set[str] = set()
    pending = [root]
    paths: list[Path] = []
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            if not include_tests and is_test_path(path.relative_to(root).as_posix()):
                continue
            # Inspect directory links before recursing; rglob can traverse a
            # Windows junction before its caller gets to reject that junction.
            if _linked(path) or not path.resolve().is_relative_to(root):
                raise TaskBundleError(f"PATH_ESCAPE: {path}")
            if path.is_dir():
                pending.append(path)
            else:
                paths.append(path)
    for path in sorted(paths):
        if _linked(path) or not path.resolve().is_relative_to(root):
            raise TaskBundleError(f"PATH_ESCAPE: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise TaskBundleError(f"UNSUPPORTED_INPUT_FILE: {path}")
        relative = path.relative_to(root).as_posix()
        if relative.casefold() in folded:
            raise TaskBundleError(f"DUPLICATE_FILE_IDENTITY: {relative}")
        folded.add(relative.casefold())
        result[relative] = path
    return result


@dataclass(frozen=True)
class FileRecord:
    relative_path: str
    sha256: str
    size_bytes: int
    raw_bytes: bytes = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {"relative_path": self.relative_path, "sha256": self.sha256, "size_bytes": self.size_bytes}


@dataclass(frozen=True)
class ContractIndex:
    task_id: str
    nodes: Mapping[str, Any]
    atomic_ids: tuple[str, ...]
    scenarios: tuple[Mapping[str, Any], ...]
    environment: Mapping[str, Any]
    data_registry: tuple[Mapping[str, Any], ...]
    data_by_lifecycle: Mapping[str, Any]
    dependencies: Mapping[str, tuple[str, ...]]
    obligations: tuple[Mapping[str, Any], ...]
    diagnostics: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {name: _thaw(getattr(self, name)) for name in self.__dataclass_fields__}


def _yaml_locations(raw: str) -> dict[str, int]:
    locations: dict[str, int] = {}
    active: set[int] = set()

    def walk(node: yaml.Node, pointer: str) -> None:
        if id(node) in active:
            raise TaskBundleError(f"CYCLIC_YAML_ALIAS: {pointer}")
        active.add(id(node))
        locations[pointer] = node.start_mark.line + 1
        if isinstance(node, yaml.MappingNode):
            keys: set[str] = set()
            for key, value in node.value:
                if not isinstance(key, yaml.ScalarNode):
                    raise TaskBundleError(f"INVALID_MAPPING_KEY: {pointer}")
                if key.value in keys:
                    raise TaskBundleError(f"DUPLICATE_YAML_KEY: {pointer}/{key.value}")
                keys.add(key.value)
                walk(value, f"{pointer}/{key.value}")
        elif isinstance(node, yaml.SequenceNode):
            for index, value in enumerate(node.value):
                walk(value, f"{pointer}/{index}")
        active.remove(id(node))

    node = yaml.compose(raw)
    if node is not None:
        walk(node, "")
    return locations


def build_contract_index(tree: Mapping[str, Any], *, task_id: str,
                         source_path: str = "requirements/requirements.yaml",
                         source_text: str = "") -> ContractIndex:
    """Retain all raw fields, sharing ancestor constraints by requirement ID."""
    locations = _yaml_locations(source_text) if source_path.endswith((".yaml", ".yml")) else {}
    diagnostics: list[str] = []
    nodes: dict[str, Any] = {}
    dependencies: dict[str, tuple[str, ...]] = {}
    atomic: list[str] = []
    scenarios: list[Any] = []
    obligations: list[Any] = []
    scenario_ids: set[str] = set()
    known = {"id", "req_id", "name", "description", "type", "children", "dependencies",
             "environment", "data", "pages", "scenarios", "visual_reference", "optional_visual_reference"}

    def source(pointer: str) -> dict[str, Any]:
        return {"path": source_path, "pointer": pointer, "line": locations.get(pointer)}

    def visit(node: Any, pointer: str, ancestors: tuple[str, ...]) -> None:
        if not isinstance(node, Mapping):
            raise TaskBundleError(f"INVALID_REQUIREMENT: {pointer} must be a mapping")
        node_id = node.get("id", node.get("req_id"))
        if not isinstance(node_id, str) or not node_id.strip():
            raise TaskBundleError(f"MISSING_REQUIREMENT_ID: {pointer}")
        if node_id in nodes:
            raise TaskBundleError(f"DUPLICATE_REQUIREMENT_ID: {node_id}")
        if set(node) - known:
            diagnostics.append(f"UNKNOWN_FIELDS_PRESERVED: {node_id}: {sorted(set(node) - known)}")
        deps = node.get("dependencies", [])
        if not isinstance(deps, (list, tuple)) or any(not isinstance(dep, str) or not dep for dep in deps):
            raise TaskBundleError(f"INVALID_DEPENDENCIES: {node_id}")
        dependencies[node_id] = tuple(deps)
        children = node.get("children", [])
        if not isinstance(children, (list, tuple)):
            raise TaskBundleError(f"INVALID_CHILDREN: {node_id}")
        kind = node.get("type")
        if kind == "ATOMIC" and children:
            raise TaskBundleError(f"ATOMIC_HAS_CHILDREN: {node_id}")
        if kind == "ATOMIC" or (kind is None and not children and node_id != "ROOT"):
            atomic.append(node_id)
        if kind not in (None, "ATOMIC", "FOLDER"):
            diagnostics.append(f"UNKNOWN_REQUIREMENT_TYPE_PRESERVED: {node_id}: {kind}")
        nodes[node_id] = {"identity": [task_id, node_id], "raw": dict(node),
                          "source": source(pointer), "ancestor_ids": ancestors,
                          "constraint_sources": (*ancestors, node_id)}
        if node.get("description"):
            obligations.append({"identity": [task_id, node_id, "description"], "requirement_id": node_id,
                                "kind": "DESCRIPTION", "source": source(pointer + "/description"),
                                "text": node["description"], "ancestor_ids": ancestors,
                                "applicability": "See original requirement and ancestor constraints",
                                "known_public_tests": [], "supplemental_verification": None,
                                "status": "UNVERIFIED_REQUIREMENT"})
        node_scenarios = node.get("scenarios", [])
        if not isinstance(node_scenarios, (list, tuple)):
            raise TaskBundleError(f"INVALID_SCENARIOS: {node_id}")
        for ordinal, scenario in enumerate(node_scenarios):
            scenario_pointer = f"{pointer}/scenarios/{ordinal}"
            if not isinstance(scenario, Mapping):
                raise TaskBundleError(f"INVALID_SCENARIO: {scenario_pointer}")
            unknown_scenario_fields = set(scenario) - {"id", "name", "steps"}
            if unknown_scenario_fields:
                diagnostics.append(f"UNKNOWN_SCENARIO_FIELDS_PRESERVED: {node_id}/{ordinal}: {sorted(unknown_scenario_fields)}")
            scenario_id = scenario.get("id", f"{node_id}::scenario-{ordinal + 1}")
            if not isinstance(scenario_id, str) or not scenario_id or scenario_id in scenario_ids:
                raise TaskBundleError(f"DUPLICATE_OR_INVALID_SCENARIO_ID: {scenario_id}")
            scenario_ids.add(scenario_id)
            steps = scenario.get("steps", [])
            if not isinstance(steps, (list, tuple)):
                raise TaskBundleError(f"INVALID_STEPS: {scenario_id}")
            normalized_steps = []
            for step_index, step in enumerate(steps):
                if not isinstance(step, Mapping):
                    raise TaskBundleError(f"INVALID_STEP: {scenario_id}/{step_index}")
                clause_keys = [key for key in step if isinstance(key, str) and key.casefold() in {"given", "when", "then"}]
                unknown_step_fields = set(step) - {"keyword", "type", "content", "id", "references", *clause_keys}
                if unknown_step_fields:
                    diagnostics.append(f"UNKNOWN_STEP_FIELDS_PRESERVED: {scenario_id}/{step_index}: {sorted(unknown_step_fields)}")
                keyword = step.get("keyword", step.get("type"))
                clauses = [(key, step[key]) for key in clause_keys if isinstance(step.get(key), str)]
                if isinstance(keyword, str) and isinstance(step.get("content"), str):
                    normalized_steps.append({"keyword": keyword, "raw": dict(step),
                                             "source": source(f"{scenario_pointer}/steps/{step_index}")})
                elif clauses:
                    for clause, _text in clauses:
                        normalized_steps.append({"keyword": clause.upper(), "raw": dict(step),
                                                 "source": source(f"{scenario_pointer}/steps/{step_index}")})
                else:
                    raise TaskBundleError(f"INVALID_STEP: {scenario_id}/{step_index}: keyword/type and content required")
            record = {"identity": [task_id, scenario_id], "id": scenario_id, "requirement_id": node_id,
                      "raw": dict(scenario), "steps": normalized_steps, "source": source(scenario_pointer)}
            scenarios.append(record)
            obligations.append({"identity": [task_id, node_id, scenario_id], "requirement_id": node_id,
                                "kind": "SCENARIO", "source": source(scenario_pointer),
                                "applicability": "See original GIVEN/WHEN steps", "known_public_tests": [],
                                "supplemental_verification": None, "status": "UNVERIFIED_REQUIREMENT"})
        for index, child in enumerate(children):
            visit(child, f"{pointer}/children/{index}", (*ancestors, node_id))

    visit(tree, "", ())
    for node_id, deps in dependencies.items():
        for dependency in deps:
            if dependency not in nodes:
                raise TaskBundleError(f"UNKNOWN_DEPENDENCY: {node_id} -> {dependency}")
    complete: set[str] = set()
    active: set[str] = set()

    def check_cycle(node_id: str) -> None:
        if node_id in active:
            raise TaskBundleError(f"CYCLIC_DEPENDENCY: {node_id}")
        if node_id in complete:
            return
        active.add(node_id)
        for dependency in dependencies[node_id]:
            check_cycle(dependency)
        active.remove(node_id)
        complete.add(node_id)

    for node_id in nodes:
        check_cycle(node_id)
    environment = tree.get("environment", {})
    data = tree.get("data", [])
    if not isinstance(environment, Mapping) or not isinstance(data, (list, tuple)):
        raise TaskBundleError("INVALID_ROOT_ENVIRONMENT_OR_DATA")
    data_ids: set[str] = set()
    by_lifecycle: dict[str, list[str]] = {kind: [] for kind in ("SEED", "CREATED", "DERIVED")}
    for item in data:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str) or not item["id"]:
            raise TaskBundleError("INVALID_DATA_REGISTRY_ENTRY")
        if item["id"] in data_ids:
            raise TaskBundleError(f"DUPLICATE_DATA_ID: {item['id']}")
        data_ids.add(item["id"])
        lifecycle = item.get("lifecycle")
        if lifecycle not in by_lifecycle:
            raise TaskBundleError(f"UNKNOWN_DATA_LIFECYCLE: {item['id']}: {lifecycle}")
        by_lifecycle[lifecycle].append(item["id"])
    for node_id, record in nodes.items():
        node_data = record["raw"].get("data", {}) if node_id != "ROOT" else {}
        if node_data and not isinstance(node_data, Mapping):
            raise TaskBundleError(f"INVALID_NODE_DATA: {node_id}")
        required_data = node_data.get("requires", [])
        if not isinstance(required_data, (list, tuple)) or any(not isinstance(item, str) for item in required_data):
            raise TaskBundleError(f"INVALID_NODE_DATA_REQUIRES: {node_id}")
        for registry_id in required_data:
            if registry_id not in data_ids:
                raise TaskBundleError(f"UNKNOWN_DATA_DEPENDENCY: {node_id} -> {registry_id}")
    if not source_path.endswith((".yaml", ".yml")):
        diagnostics.append("MARKDOWN_ENVIRONMENT_DATA_UNSTRUCTURED: source text retained; root environment/data extraction unsupported")
    return ContractIndex(task_id, _freeze(nodes), tuple(atomic), _freeze(scenarios), _freeze(environment),
                         _freeze(data), _freeze(by_lifecycle), _freeze(dependencies),
                         _freeze(obligations), tuple(diagnostics))


def _import_literals(content: str, relative: str) -> list[str]:
    """Tokenize comments/strings before inspecting literal module references.

    This small lexer does not implement JavaScript regex/template expression
    grammar, TypeScript module resolution, or parsing. Exotic import syntax is
    rejected and full runner collection remains mandatory.
    """
    tokens: list[tuple[str, str]] = []
    offset = 0
    while offset < len(content):
        char = content[offset]
        if char.isspace():
            offset += 1
            continue
        if content.startswith("//", offset):
            end = content.find("\n", offset + 2)
            offset = end if end >= 0 else len(content)
            continue
        if content.startswith("/*", offset):
            end = content.find("*/", offset + 2)
            if end < 0:
                raise TaskBundleError(f"UNRESOLVED_IMPORT_SYNTAX: {relative}: unterminated comment")
            offset = end + 2
            continue
        if char in "'\"`":
            start, quote = offset + 1, char
            offset += 1
            while offset < len(content) and content[offset] != quote:
                offset += 2 if content[offset] == "\\" else 1
            if offset >= len(content):
                raise TaskBundleError(f"UNRESOLVED_IMPORT_SYNTAX: {relative}: unterminated literal")
            body = content[start:offset]
            if quote == "`" and re.search(r"\b(?:import|require)\s*\(", body):
                raise TaskBundleError(f"UNRESOLVED_IMPORT_SYNTAX: {relative}: module reference inside template expression")
            tokens.append(("template" if quote == "`" else "string", body))
            offset += 1
            continue
        if char.isalpha() or char in "_$":
            start = offset
            offset += 1
            while offset < len(content) and (content[offset].isalnum() or content[offset] in "_$"):
                offset += 1
            tokens.append(("id", content[start:offset]))
            continue
        tokens.append(("symbol", char))
        offset += 1

    targets: list[str] = []

    def literal(index: int) -> str:
        if index >= len(tokens) or tokens[index][0] != "string" or "\\" in tokens[index][1]:
            raise TaskBundleError(f"UNRESOLVED_IMPORT_SYNTAX: {relative}: literal unescaped module string required")
        return tokens[index][1]

    for index, (kind, token) in enumerate(tokens):
        if kind != "id" or token not in ("import", "require", "export"):
            continue
        if index and tokens[index - 1][1] == ".":
            continue
        following = tokens[index + 1][1] if index + 1 < len(tokens) else ""
        if token == "import" and following == ".":  # import.meta
            continue
        if token in ("import", "require") and following == "(":
            target = literal(index + 2)
            if index + 3 >= len(tokens) or tokens[index + 3][1] != ")":
                raise TaskBundleError(f"UNRESOLVED_IMPORT_SYNTAX: {relative}: computed or attributed module import")
            targets.append(target)
        elif token == "require":
            continue
        elif token == "import" and index + 1 < len(tokens) and tokens[index + 1][0] == "string":
            targets.append(literal(index + 1))
        else:
            cursor = index + 1
            if token == "export":
                if cursor < len(tokens) and tokens[cursor][1] == "type":
                    cursor += 1
                if cursor >= len(tokens) or tokens[cursor][1] not in ("*", "{"):
                    continue
                if tokens[cursor][1] == "{":
                    while cursor < len(tokens) and tokens[cursor][1] != "}":
                        cursor += 1
                    cursor += 1
                    if cursor >= len(tokens) or tokens[cursor][1] != "from":
                        continue  # local export {value}
            while cursor < len(tokens) and tokens[cursor][1] not in ("from", ";", "="):
                cursor += 1
            if cursor < len(tokens) and tokens[cursor][1] == "from":
                targets.append(literal(cursor + 1))
            elif token == "import":
                raise TaskBundleError(f"UNRESOLVED_IMPORT_SYNTAX: {relative}: unsupported import declaration")
    return targets


def inspect_test_imports(files: Mapping[str, bytes]) -> tuple[Mapping[str, tuple[str, ...]], tuple[str, ...]]:
    """Resolve literal relative imports; unsupported constructs remain blocked.

    Lexical inspection is an offline preflight. Only real runner collection can
    validate TypeScript parsing, package exports, aliases, and execution config.
    """
    graph: dict[str, tuple[str, ...]] = {}
    diagnostics = ["IMPORT_ANALYSIS_LIMITED: lexical literal imports only; runner collection required"]
    for relative, raw in files.items():
        if not relative.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
            continue
        content = raw.decode("utf-8-sig")
        targets = _import_literals(content, relative)
        resolved: list[str] = []
        for target in targets:
            if target.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", target):
                raise TaskBundleError(f"PATH_ESCAPE: {relative} imports {target}")
            if not target.startswith("."):
                diagnostics.append(f"EXTERNAL_IMPORT_REQUIRES_TOOLCHAIN: {relative}: {target}")
                continue
            # Resolve purely lexically without consulting paths outside bundle.
            parts: list[str] = list(Path(relative).parent.parts)
            for part in target.replace("\\", "/").split("/"):
                if part == "..":
                    if not parts:
                        raise TaskBundleError(f"PATH_ESCAPE: {relative} imports {target}")
                    parts.pop()
                elif part not in ("", "."):
                    parts.append(part)
            base = "/".join(parts)
            candidates = [base] + [base + extension for extension in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json")]
            candidates += [base + "/index" + extension for extension in (".ts", ".tsx", ".js", ".jsx")]
            if base.endswith(".js"):
                candidates += [base[:-3] + ".ts", base[:-3] + ".tsx"]
            matches = [candidate for candidate in candidates if candidate in files]
            if not matches:
                raise TaskBundleError(f"MISSING_TEST_IMPORT: {relative} -> {target}")
            resolved.append(matches[0])
        graph[relative] = tuple(dict.fromkeys(resolved))
    return _freeze(graph), tuple(dict.fromkeys(diagnostics))


@dataclass(frozen=True)
class TaskBundle:
    task_id: str
    requirements: Mapping[str, Any]
    index: ContractIndex
    files: tuple[FileRecord, ...]
    source_hash: str
    suite_hash: str
    published_test_count: int | None
    import_graph: Mapping[str, tuple[str, ...]]
    provenance: Mapping[str, Any]
    diagnostics: tuple[str, ...]
    _requirement_dir: Path = field(repr=False)
    _tests_dir: Path | None = field(repr=False)
    _include_tests: bool = field(default=True, repr=False)

    def assert_integrity(self) -> None:
        """Invalidate prior evidence on added/deleted/changed or linked input."""
        current = _read_files(self._requirement_dir, self._tests_dir, include_tests=self._include_tests)
        expected = {record.relative_path: record.sha256 for record in self.files}
        actual = {record.relative_path: record.sha256 for record in current}
        if expected != actual:
            changed = sorted(key for key in expected.keys() | actual.keys() if expected.get(key) != actual.get(key))
            raise TaskBundleError(f"SOURCE_INTEGRITY_CHANGED: {changed}")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, "task_id": self.task_id, "source_hash": self.source_hash,
                "suite_hash": self.suite_hash, "published_test_count": self.published_test_count,
                "files": [record.to_dict() for record in self.files], "requirements": _thaw(self.requirements),
                "index": self.index.to_dict(), "import_graph": _thaw(self.import_graph),
                "provenance": _thaw(self.provenance), "diagnostics": list(self.diagnostics),
                "integrity_scope": "byte and file-set mutation detection; does not provide OS write isolation"}


def is_test_path(relative: str) -> bool:
    path = Path(relative)
    return (any(part.casefold() in {'tests', '__tests__'} for part in path.parts)
            or bool(re.search(r'\.(spec|test)\.[^.]+$', path.name, re.I)))


def _read_files(requirement_dir: Path, tests_dir: Path | None, *, include_tests=True) -> tuple[FileRecord, ...]:
    records = []
    roots = [("requirements", _root(requirement_dir))]
    if tests_dir is not None and include_tests:
        roots.append(("tests", _root(tests_dir)))
    for prefix, root in roots:
        for relative, path in _inventory(root, include_tests=include_tests).items():
            if not include_tests and is_test_path(relative):
                continue
            if prefix == "requirements" and tests_dir is not None and path.is_relative_to(tests_dir):
                continue
            raw = path.read_bytes()
            records.append(FileRecord(f"{prefix}/{relative}", _sha(raw), len(raw), raw))
    return tuple(records)


def load_task_bundle(requirement_dir: Path, tests_dir: Path | None, *, task_id: str,
                     published_test_count: int | None = None,
                     expected_hashes: dict[str, str] | None = None,
                     source_metadata: Mapping[str, Any] | None = None,
                     include_tests: bool = True) -> TaskBundle:
    """Load generic input directories without writing or running task code.

    expected_hashes pins supplied canonical requirements/* or tests/* paths;
    all other files are still frozen against the load-time integrity baseline.
    source_metadata carries caller-supplied URLs, retrieval time, toolchain and
    known missing inputs. It is preserved rather than inferred from a task name.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise TaskBundleError("INVALID_TASK_ID")
    if published_test_count is not None and (isinstance(published_test_count, bool) or not isinstance(published_test_count, int) or published_test_count < 0):
        raise TaskBundleError("INVALID_PUBLISHED_TEST_COUNT")
    requirement_dir = _root(requirement_dir)
    tests_dir = _root(tests_dir) if tests_dir is not None else None
    if tests_dir is not None and (
        requirement_dir == tests_dir or requirement_dir.is_relative_to(tests_dir)
    ):
        raise TaskBundleError("OVERLAPPING_INPUT_ROOTS")
    files = _read_files(requirement_dir, tests_dir, include_tests=include_tests)
    file_map = {record.relative_path: record for record in files}
    for relative, expected in (expected_hashes or {}).items():
        if relative not in file_map or file_map[relative].sha256 != expected:
            raise TaskBundleError(f"SOURCE_HASH_MISMATCH: {relative}")
    # Use the parser's format priority, never lexicographic inventory order.
    # In particular requirement.md sorts before requirements.md but is the
    # parser's lower-priority fallback.
    source = next((file_map[f"requirements/{filename}"]
                   for filename in ("requirements.yaml", *_MARKDOWN_FILES)
                   if f"requirements/{filename}" in file_map), None)
    if source is None:
        raise TaskBundleError("REQUIREMENT_SOURCE_MISSING")
    if source.relative_path.endswith(".yaml"):
        # Validate aliases and duplicate mapping keys before the legacy parser
        # recursively visits the decoded tree.
        _yaml_locations(source.raw_bytes.decode("utf-8-sig"))
    tree = load_requirement_tree(requirement_dir)
    index = build_contract_index(tree, task_id=task_id, source_path=source.relative_path,
                                 source_text=source.raw_bytes.decode("utf-8-sig"))
    diagnostics = list(index.diagnostics)
    for record in index.nodes.values():
        node = record["raw"]
        references: list[str] = []
        for key in ("visual_reference", "optional_visual_reference"):
            values = node.get(key, [])
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, (list, tuple)):
                raise TaskBundleError(f"INVALID_VISUAL_REFERENCE: {key}")
            references.extend(values)
        for page in node.get("pages", []):
            if isinstance(page, Mapping) and page.get("reference"):
                references.append(page["reference"])
        for reference in references:
            if not isinstance(reference, str):
                raise TaskBundleError("INVALID_VISUAL_REFERENCE")
            parsed = urlsplit(reference)
            if parsed.scheme or parsed.netloc:
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    raise TaskBundleError(f"PATH_ESCAPE: unsupported visual reference {reference}")
                diagnostics.append(f"REMOTE_REFERENCE_NOT_SNAPSHOTTED: {reference}")
                continue
            path = unquote(parsed.path).replace("\\", "/")
            if path.startswith("/") or ".." in path.split("/"):
                raise TaskBundleError(f"PATH_ESCAPE: visual reference {reference}")
            canonical = "requirements/" + path.removeprefix("./")
            if canonical not in file_map:
                raise TaskBundleError(f"MISSING_VISUAL_REFERENCE: {reference}")
    test_files = {record.relative_path.removeprefix("tests/"): record.raw_bytes for record in files if record.relative_path.startswith("tests/")}
    if test_files:
        graph, import_diagnostics = inspect_test_imports(test_files)
        diagnostics.extend(import_diagnostics)
    else:
        graph = _freeze({})
        diagnostics.append("TEST_SUITE_MISSING: no public tests were supplied" if include_tests else
                           "TEST_SUITE_WITHHELD: evaluation is outside generation")
    if published_test_count is not None and published_test_count != len(index.scenarios):
        diagnostics.append(f"CATALOG_MISMATCH: published={published_test_count}, requirement_scenarios={len(index.scenarios)}; cause unknown")
    metadata = dict(source_metadata or {})
    if not metadata.get("sources") or not (metadata.get("retrieved_at") or metadata.get("retrieved_date")):
        diagnostics.append("SOURCE_PROVENANCE_INCOMPLETE: caller must supply source URLs and retrieval time")

    def fingerprint(selected: tuple[FileRecord, ...]) -> str:
        return _sha(json.dumps([(record.relative_path, record.sha256) for record in selected], separators=(",", ":")).encode())

    result = TaskBundle(task_id, _freeze(tree), index, files, fingerprint(files),
                        fingerprint(tuple(record for record in files if record.relative_path.startswith("tests/"))),
                        published_test_count, graph, _freeze(metadata), tuple(diagnostics), requirement_dir, tests_dir,
                        include_tests)
    # Catch changes between first byte capture and parsed-tree construction.
    result.assert_integrity()
    return result

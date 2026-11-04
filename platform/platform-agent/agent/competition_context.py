"""Source-bound planning, bounded context, and conservative partition coverage.

This module never executes tests or models. Helper imports are reading evidence,
not implementation dependencies. A source review is not behavioral acceptance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
from collections.abc import Iterator, Sequence
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .task_bundle import TaskBundle, TaskBundleError, _freeze, _thaw
from .sandbox_files import MAX_FILE_BYTES, UnsafeSandboxFile, _safe_target, snapshot_project, validate_relative_path


STABLE_PREFIX = """Implement the current task from its immutable source contracts.
Requirement identities are task scoped. Preserve ancestor constraints and data
lifecycles: only declared SEED facts may be initialized; CREATED facts require
their prescribed user flows; DERIVED facts follow the declared rules.
Public helpers describe test setup, not universal implementation prerequisites.
Read omitted exact source through its chunk references before making claims.
Source review, behavioral tests, and formal acceptance are separate evidence.
Never certify unread source or infer success from a model completion message.
"""

GLOBAL_CONTRACT_FIELDS = (
    "entities_and_relationships", "identity_and_ownership", "routes", "interfaces",
    "state_transitions", "seed_initialization", "persistence", "clock",
    "shared_components",
)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _identity(prefix: str, *values: str) -> str:
    return prefix + ":" + _digest(json.dumps(values, ensure_ascii=False).encode())


def global_contract_schema(bundle: TaskBundle) -> dict[str, Any]:
    """A source-addressed design worksheet; empty decisions are explicitly open."""
    return {"schema_version": 1, "task_id": bundle.task_id,
            "source_hash": bundle.source_hash,
            "sections": {key: {"status": "UNDECIDED", "decisions": [], "source_refs": []}
                         for key in GLOBAL_CONTRACT_FIELDS}}


@dataclass(frozen=True)
class WorkItem:
    id: str
    task_id: str
    requirement_ids: tuple[str, ...]
    dependency_ids: tuple[str, ...]
    ancestor_ids: tuple[str, ...]
    related_data_ids: tuple[str, ...]
    scenario_ids: tuple[str, ...]
    test_paths: tuple[str, ...]
    helper_paths: tuple[str, ...]
    weight: int
    shared_data_requirement_ids: tuple[str, ...] = ()
    modification_scope: str = "Application files implementing this flow and shared callers"
    completion_evidence: str = "Source partition review plus independent selected public cases; initially UNRUN"

    def to_dict(self) -> dict[str, Any]:
        return {name: _thaw(getattr(self, name)) for name in self.__dataclass_fields__}


def _test_paths(bundle: TaskBundle, requirement_ids: Iterable[str]) -> tuple[str, ...]:
    # Filename stems are explicit task-package identities, not inferred titles.
    identifiers = set(requirement_ids)
    return tuple(record.relative_path for record in bundle.files
                 if record.relative_path.startswith("tests/") and
                 any(Path(record.relative_path).name == identifier + suffix
                     for identifier in identifiers
                     for suffix in (".spec.ts", ".test.ts", ".spec.js", ".test.js")))


def _helper_paths(bundle: TaskBundle, test_paths: Iterable[str]) -> tuple[str, ...]:
    found: set[str] = set()
    pending = [path.removeprefix("tests/") for path in test_paths]
    roots = set(pending)
    while pending:
        for path in bundle.import_graph.get(pending.pop(), ()):
            if path not in found:
                found.add(path)
                pending.append(path)
    return tuple("tests/" + path for path in sorted(found - roots))


def build_work_plan(bundle: TaskBundle) -> tuple[WorkItem, ...]:
    """Topological atomic flows with inherited and cross-folder dependencies.

    Atomic scenarios stay together as one vertical flow. Data relationships and
    exact helper source are attached for planning; neither creates hidden edges.
    Effective dependency cycles introduced by folder expansion are rejected.
    """
    bundle.assert_integrity()
    index = bundle.index
    atomics = set(index.atomic_ids)
    expanded = {node: tuple(leaf for leaf in index.atomic_ids
                           if node == leaf or node in index.nodes[leaf]["ancestor_ids"])
                for node in index.nodes}
    edges: dict[str, tuple[str, ...]] = {}
    for leaf in index.atomic_ids:
        chain = (*index.nodes[leaf]["ancestor_ids"], leaf)
        edges[leaf] = tuple(dict.fromkeys(target for node in chain
                            for dependency in index.dependencies[node]
                            for target in expanded[dependency] if target != leaf))
        for node in chain:
            for dependency in index.dependencies[node]:
                if not expanded[dependency]:
                    raise TaskBundleError(f"DEPENDENCY_HAS_NO_ATOMIC_FLOW: {node} -> {dependency}")
    ordered: list[str] = []
    active: set[str] = set()
    complete: set[str] = set()

    def visit(leaf: str) -> None:
        if leaf in active:
            raise TaskBundleError(f"CYCLIC_EFFECTIVE_DEPENDENCY: {leaf}")
        if leaf in complete:
            return
        active.add(leaf)
        for dependency in edges[leaf]:
            if dependency not in atomics:
                raise TaskBundleError(f"UNKNOWN_ATOMIC_DEPENDENCY: {dependency}")
            visit(dependency)
        active.remove(leaf)
        complete.add(leaf)
        ordered.append(leaf)

    for leaf in index.atomic_ids:
        visit(leaf)
    result = []
    data_requirements = {
        leaf: tuple(dict.fromkeys(data_id for node in (*index.nodes[leaf]["ancestor_ids"], leaf)
                                 if index.nodes[node]["ancestor_ids"]
                                 for data_id in index.nodes[node]["raw"].get("data", {}).get("requires", ())))
        for leaf in index.atomic_ids}
    for leaf in ordered:
        ancestors = tuple(index.nodes[leaf]["ancestor_ids"])
        chain = (*ancestors, leaf)
        data = data_requirements[leaf]
        shared = tuple(other for other in index.atomic_ids
                       if other != leaf and set(data).intersection(data_requirements[other]))
        scenarios = tuple(record["id"] for record in index.scenarios if record["requirement_id"] == leaf)
        tests = _test_paths(bundle, (leaf,))
        helpers = _helper_paths(bundle, tests)
        size = sum(record.size_bytes for record in bundle.files if record.relative_path in (*tests, *helpers))
        fanout = sum(leaf in targets for targets in edges.values())
        weight = 1 + len(scenarios) * 2 + len(data) + len(shared) + len(edges[leaf]) + fanout + size // 4096
        result.append(WorkItem(_identity("work", bundle.task_id, leaf), bundle.task_id, (leaf,),
                      tuple(_identity("work", bundle.task_id, dep) for dep in edges[leaf]),
                      ancestors, data, scenarios, tests, helpers, weight, shared))
    return tuple(result)


@dataclass(frozen=True)
class SourceChunk:
    id: str
    path: str
    sha256: str
    start: int
    end: int
    text: str
    kind: str
    owners: tuple[str, ...] = ()

    def reference(self) -> dict[str, Any]:
        return {name: _thaw(getattr(self, name)) for name in self.__dataclass_fields__ if name != "text"}


def _yaml_nodes(text: str) -> dict[str, yaml.Node]:
    result: dict[str, yaml.Node] = {}

    def walk(node: yaml.Node, pointer: str) -> None:
        result[pointer] = node
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                walk(value, pointer + "/" + key.value)
        elif isinstance(node, yaml.SequenceNode):
            for ordinal, value in enumerate(node.value):
                walk(value, pointer + "/" + str(ordinal))

    root = yaml.compose(text)
    if root is not None:
        walk(root, "")
    return result


class ContextRepository:
    """Frozen exact text retrieval; production reads reject links and traversal.

    Character offsets address UTF-8 decoded text without newline normalization.
    File hashes address original bytes. Binary inputs remain in the bundle file
    inventory and are explicitly unavailable as text, never silently decoded.
    """
    def __init__(self, bundle: TaskBundle, application_dir: Path | None = None, *, chunk_chars: int = 1200):
        if chunk_chars < 1:
            raise ValueError("INVALID_CHUNK_SIZE")
        bundle.assert_integrity()
        self.bundle = bundle
        self.chunk_chars = chunk_chars
        self._files = {record.relative_path: (record.sha256, record.raw_bytes) for record in bundle.files}
        self._chunks: dict[str, SourceChunk] = {}
        self._observed_ranges: dict[tuple[str, str], list[tuple[int, int]]] = {}
        self._obligation_refs: Mapping[str, Any] = _freeze({})
        if application_dir:
            application_dir = Path(application_dir).absolute()
            if any(part.is_symlink() or bool(getattr(part, "is_junction", lambda: False)())
                   for part in (application_dir, *application_dir.parents)):
                raise TaskBundleError(f"PATH_ESCAPE: linked application root {application_dir}")
            if not application_dir.is_dir():
                raise TaskBundleError(f"APPLICATION_SOURCE_MISSING: {application_dir}")
        self._application_dir = application_dir.resolve() if application_dir else None
        self._yaml_cache: dict[str, dict[str, yaml.Node]] = {}

    def _file(self, path: str) -> tuple[str, str]:
        if path not in self._files and path.startswith("application/") and self._application_dir:
            raw = self._application_bytes(path)
            self._files[path] = (_digest(raw), raw)
        if path not in self._files:
            raise TaskBundleError(f"UNKNOWN_CONTEXT_SOURCE: {path}")
        sha, raw = self._files[path]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise TaskBundleError(f"NON_TEXT_CONTEXT_SOURCE: {path}") from error
        if "\x00" in text:
            raise TaskBundleError(f"NON_TEXT_CONTEXT_SOURCE: {path}")
        return sha, text

    def _application_bytes(self, path: str) -> bytes:
        try:
            relative = path.removeprefix("application/")
            validate_relative_path(relative)
            target = _safe_target(self._application_dir, relative)
            info = target.lstat()
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            with os.fdopen(os.open(target, flags), "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or opened.st_nlink != 1:
                    raise UnsafeSandboxFile("application input changed while opening")
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise UnsafeSandboxFile("application context source exceeds file limit")
            return raw
        except (OSError, UnsafeSandboxFile) as error:
            raise TaskBundleError(f"PATH_ESCAPE_OR_UNSAFE_APPLICATION_SOURCE: {path}: {error}") from error

    def add_evidence(self, resource: str, text: str) -> dict[str, Any]:
        """Register controller-owned immutable text, never a model write tool."""
        try:
            validate_relative_path(resource)
        except UnsafeSandboxFile as error:
            raise TaskBundleError(f"INVALID_EVIDENCE_RESOURCE: {resource}") from error
        if not resource.startswith("evidence/") or not isinstance(text, str):
            raise TaskBundleError("INVALID_EVIDENCE_RESOURCE")
        folded = resource.casefold()
        if any(folded == path.casefold() or folded.startswith(path.casefold() + "/") or
               path.casefold().startswith(folded + "/") for path in self._files):
            raise TaskBundleError(f"EVIDENCE_RESOURCE_COLLISION: {resource}")
        raw = text.encode("utf-8")
        self._files[resource] = (_digest(raw), raw)
        return {"resource": resource, "sha256": _digest(raw), "size_bytes": len(raw)}

    def _range(self, path: str, start: int, end: int, kind: str, owners: tuple[str, ...]) -> tuple[SourceChunk, ...]:
        sha, text = self._file(path)
        chunks = []
        for offset in range(start, end, self.chunk_chars):
            stop = min(end, offset + self.chunk_chars)
            chunk = SourceChunk(_identity("chunk", self.bundle.task_id, path, sha, str(offset), str(stop)),
                                path, sha, offset, stop, text[offset:stop], kind, owners)
            self._chunks[chunk.id] = chunk
            chunks.append(chunk)
        return tuple(chunks)

    def file_chunks(self, path: str) -> tuple[SourceChunk, ...]:
        _, text = self._file(path)
        kind = "application" if path.startswith("application/") else "test" if path.startswith("tests/") else "evidence" if path.startswith("evidence/") else "requirement"
        return self._range(path, 0, len(text), kind, ())

    def pointer_chunks(self, source: Mapping[str, Any], *, owner: str = "") -> tuple[SourceChunk, ...]:
        path, pointer = source["path"], source["pointer"]
        _, text = self._file(path)
        if not path.endswith((".yaml", ".yml")):
            # Markdown has no validated AST source spans. Retain all exact text.
            return self._range(path, 0, len(text), "requirement", (owner,) if owner else ())
        if path not in self._yaml_cache:
            self._yaml_cache[path] = _yaml_nodes(text)
        node = self._yaml_cache[path].get(pointer)
        if node is None:
            raise TaskBundleError(f"UNKNOWN_SOURCE_POINTER: {path}#{pointer}")
        return self._range(path, node.start_mark.index, node.end_mark.index, "requirement", (owner,) if owner else ())

    def requirement_chunks(self, requirement_id: str) -> tuple[SourceChunk, ...]:
        record = self.bundle.index.nodes.get(requirement_id)
        if record is None:
            raise TaskBundleError(f"UNKNOWN_REQUIREMENT: {requirement_id}")
        source = record["source"]
        path = source["path"]
        if not path.endswith((".yaml", ".yml")):
            return self.pointer_chunks(source, owner=requirement_id)
        _, text = self._file(path)
        if path not in self._yaml_cache:
            self._yaml_cache[path] = _yaml_nodes(text)
        node = self._yaml_cache[path][source["pointer"]]
        result = []
        for key, value in node.value:
            if key.value == "children" or (not record["ancestor_ids"] and key.value in ("environment", "data")):
                continue
            result.extend(self._range(path, key.start_mark.index, value.end_mark.index,
                                      "requirement", (requirement_id,)))
        return tuple(result)

    def get_chunk(self, chunk_id: str) -> SourceChunk:
        try:
            return self._chunks[chunk_id]
        except KeyError as error:
            raise TaskBundleError(f"UNKNOWN_CONTEXT_CHUNK: {chunk_id}") from error

    def list_resources(self) -> list[dict[str, Any]]:
        return [{"resource": path, "sha256": sha, "size_bytes": len(raw)}
                for path, (sha, raw) in sorted(self._files.items())] + [
                    {"resource": chunk.id, **chunk.reference()} for chunk in self._chunks.values()]

    def read_context(self, resource: str, offset: int = 0, limit: int = 8000) -> dict[str, Any]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 32000:
            raise TaskBundleError("INVALID_CONTEXT_RANGE")
        if resource.startswith("chunk:"):
            chunk = self.get_chunk(resource)
            path, sha, text, base = chunk.path, chunk.sha256, chunk.text, chunk.start
        else:
            path, base = resource, 0
            sha, text = self._file(path)
        if offset > len(text):
            raise TaskBundleError("CONTEXT_OFFSET_OUT_OF_RANGE")
        end = min(len(text), offset + limit)
        current_application_read = path.startswith('application/')
        if current_application_read and _digest(self._application_bytes(path)) != sha:
            raise TaskBundleError(f"APPLICATION_SOURCE_CHANGED: {path}")
        self._observed_ranges[(path, sha)] = self._merge_ranges([
            *self._observed_ranges.get((path, sha), ()), (base + offset, base + end)])
        return {"resource": resource, "path": path, "sha256": sha,
                "start": base + offset, "end": base + end, "total_chars": len(text),
                "content": text[offset:end], "has_more": end < len(text),
                "current_application_read": current_application_read}

    @staticmethod
    def _merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
        merged: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if end <= start:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def read_ranges(self) -> tuple[Mapping[str, Any], ...]:
        """Only spans actually returned by the public retrieval tool."""
        return tuple(_freeze({"path": path, "sha256": sha, "start": start, "end": end})
                     for (path, sha), ranges in self._observed_ranges.items() for start, end in ranges)

    def reset_read_receipts(self) -> None:
        """A new model invocation cannot inherit another partition's reads."""
        self._observed_ranges.clear()

    def obligation_chunks(self, obligation_id: str) -> tuple[Mapping[str, Any], ...]:
        if obligation_id not in self._obligation_refs:
            raise TaskBundleError(f"UNKNOWN_OBLIGATION_SOURCE: {obligation_id}")
        return self._obligation_refs[obligation_id]

    def planned_obligation_ids(self) -> tuple[str, ...]:
        return tuple(self._obligation_refs)

    def read_obligation_ids(self, included_chunks: Iterable[SourceChunk] = ()) -> tuple[str, ...]:
        ranges = {identity: list(spans) for identity, spans in self._observed_ranges.items()}
        for chunk in included_chunks:
            ranges.setdefault((chunk.path, chunk.sha256), []).append((chunk.start, chunk.end))
        ranges = {identity: self._merge_ranges(spans) for identity, spans in ranges.items()}
        return tuple(oid for oid, refs in self._obligation_refs.items() if refs and all(
            any(start <= ref["start"] and end >= ref["end"]
                for start, end in ranges.get((ref["path"], ref["sha256"]), ()))
            for ref in refs))

    def assert_current(self) -> None:
        self.bundle.assert_integrity()
        if self._application_dir:
            for path, (sha, _) in self._files.items():
                if path.startswith("application/"):
                    if _digest(self._application_bytes(path)) != sha:
                        raise TaskBundleError(f"APPLICATION_SOURCE_CHANGED: {path}")


@dataclass(frozen=True)
class ContextPacket:
    text: str
    manifest: Mapping[str, Any]
    chunks: tuple[SourceChunk, ...]
    repository: ContextRepository = field(repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "manifest": _thaw(self.manifest)}


def _chunk_block(chunk: SourceChunk) -> str:
    return f"\n--- {chunk.id} {chunk.path} sha256={chunk.sha256} chars={chunk.start}:{chunk.end} ---\n{chunk.text}\n"


def assemble_context(bundle: TaskBundle, item_or_ids: WorkItem | Iterable[str],
                     application_dir: Path | None = None, max_chars: int = 32000, *,
                     global_contract: Mapping[str, Any] | None = None,
                     source_paths: Iterable[str] = ()) -> ContextPacket:
    """Build an exact-source packet with explicit retrievable omissions.

    The manifest is controller evidence outside the prompt budget. Prompt text,
    including headings and delimiters, is bounded. No chunk is partially shown.
    A small budget may contain no source and can never authorize audit claims.
    source_paths names production files relative to application_dir; selection
    itself does not establish that all relevant implementation files were read.
    """
    if isinstance(item_or_ids, WorkItem):
        if item_or_ids.task_id != bundle.task_id:
            raise TaskBundleError("CONTEXT_TASK_ID_MISMATCH")
        selected = item_or_ids.requirement_ids
    elif isinstance(item_or_ids, str):
        selected = (item_or_ids,)
    else:
        selected = tuple(item_or_ids)
    if not selected or any(node not in bundle.index.nodes for node in selected):
        raise TaskBundleError("UNKNOWN_OR_EMPTY_CONTEXT_REQUIREMENTS")
    repository = ContextRepository(bundle, application_dir)
    nodes = tuple(dict.fromkeys(node for leaf in selected
                  for node in (*bundle.index.nodes[leaf]["ancestor_ids"], leaf)))
    # The dependency closure supplies contracts, not assumptions about auth/setup.
    pending = list(nodes)
    related = list(nodes)
    while pending:
        node = pending.pop()
        for dep in bundle.index.dependencies[node]:
            descendants = tuple(leaf for leaf in bundle.index.atomic_ids
                                if dep == leaf or dep in bundle.index.nodes[leaf]["ancestor_ids"])
            candidates_for_dep = tuple(candidate for leaf in descendants
                                       for candidate in (*bundle.index.nodes[leaf]["ancestor_ids"], leaf))
            for candidate in (*bundle.index.nodes[dep]["ancestor_ids"], dep, *candidates_for_dep):
                if candidate not in related:
                    related.append(candidate)
                    pending.append(candidate)
    data_ids = tuple(dict.fromkeys(data_id for node in related if bundle.index.nodes[node]["ancestor_ids"]
                     for data_id in bundle.index.nodes[node]["raw"].get("data", {}).get("requires", ())))
    source = bundle.index.nodes[next(iter(bundle.index.nodes))]["source"]
    candidates: list[SourceChunk] = []
    for node in related:
        candidates.extend(repository.requirement_chunks(node))
    if bundle.index.environment:
        candidates.extend(repository.pointer_chunks({"path": source["path"], "pointer": "/environment"}, owner="environment"))
    for ordinal, data in enumerate(bundle.index.data_registry):
        if data["id"] in data_ids:
            candidates.extend(repository.pointer_chunks({"path": source["path"], "pointer": f"/data/{ordinal}"}, owner=data["id"]))
    tests = _test_paths(bundle, related)
    helpers = _helper_paths(bundle, tests)
    for path in (*tests, *helpers):
        candidates.extend(repository.file_chunks(path))
    application_paths = tuple(dict.fromkeys("application/" + Path(path).as_posix() for path in source_paths))
    for path in application_paths:
        candidates.extend(repository.file_chunks(path))
    candidates = list({chunk.id: chunk for chunk in candidates}.values())
    contract = global_contract_schema(bundle) if global_contract is None else _thaw(global_contract)
    if contract.get("task_id") != bundle.task_id or contract.get("source_hash") != bundle.source_hash:
        raise TaskBundleError("GLOBAL_CONTRACT_IDENTITY_MISMATCH")
    header = STABLE_PREFIX + "\nGlobal design contract:\n" + json.dumps(contract, ensure_ascii=False, sort_keys=True)
    header += "\nInput identity: " + json.dumps({"task_id": bundle.task_id, "source_hash": bundle.source_hash,
                                              "suite_hash": bundle.suite_hash, "requirement_ids": selected})
    footer = "\nContext manifest: omitted chunks require retrieval; this packet does not establish full application coverage.\n"
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or len(header) + len(footer) > max_chars:
        raise TaskBundleError("CONTEXT_BUDGET_TOO_SMALL_FOR_METADATA")
    text, included, omitted = header, [], []
    for chunk in candidates:
        block = _chunk_block(chunk)
        if len(text) + len(block) + len(footer) <= max_chars:
            text += block
            included.append(chunk)
        else:
            omitted.append(chunk)
    text += footer
    # Every obligation's exact span is independently addressable. Only full
    # containment by displayed chunks counts as read, even for multiline YAML.
    covered_obligations = []
    obligation_refs = {}
    for obligation in bundle.index.obligations:
        if obligation["requirement_id"] not in related:
            continue
        oid = _identity("obligation", *obligation["identity"])
        chunks = repository.pointer_chunks(obligation["source"], owner=obligation["requirement_id"])
        obligation_refs[oid] = [chunk.reference() for chunk in chunks]
        def fully_read(chunk: SourceChunk) -> bool:
            cursor = chunk.start
            for read in sorted((read for read in included if read.path == chunk.path and read.sha256 == chunk.sha256), key=lambda read: read.start):
                if read.start > cursor:
                    break
                if read.end > cursor:
                    cursor = read.end
                if cursor >= chunk.end:
                    return True
            return False

        if chunks and all(fully_read(chunk) for chunk in chunks):
            covered_obligations.append(oid)
    manifest = {"schema_version": 1, "task_id": bundle.task_id, "source_hash": bundle.source_hash,
                "suite_hash": bundle.suite_hash, "requirement_ids": list(selected),
                "constraint_requirement_ids": related, "data_ids": list(data_ids),
                "unselected_data_ids": [record["id"] for record in bundle.index.data_registry if record["id"] not in data_ids],
                "test_paths": list(tests), "helper_setup_paths": list(helpers),
                "unselected_test_paths": [record.relative_path for record in bundle.files
                                          if record.relative_path.startswith("tests/") and record.relative_path not in (*tests, *helpers)],
                "helper_setup_is_implementation_dependency": False,
                "source_paths": list(application_paths), "character_count": len(text), "max_chars": max_chars,
                "included_chunks": [chunk.reference() for chunk in included],
                "omitted_chunks": [chunk.reference() for chunk in omitted],
                "obligation_chunks": obligation_refs, "read_obligation_ids": covered_obligations,
                "application_scope_complete": False,
                "input_files": [record.to_dict() for record in bundle.files],
                "claim_scope": "Partition source review only; behavioral and formal status remain UNVERIFIED"}
    frozen_manifest = _freeze(manifest)
    repository._obligation_refs = frozen_manifest["obligation_chunks"]
    return ContextPacket(text, frozen_manifest, tuple(included), repository)


class AuditPackets(Sequence[ContextPacket]):
    """Lazy packet views over one frozen application/obligation audit plan.

    Iterating materializes only the current packet. The full plan is registered
    once as paginated read-only evidence and is never embedded in each manifest.
    """
    def __init__(self, base: ContextPacket, application_chunks: tuple[SourceChunk, ...],
                 groups: tuple[tuple[int, int], ...], plan: Mapping[str, Any],
                 plan_digest: str, max_chars: int):
        self._base = base
        self._application_chunks = application_chunks
        self._groups = groups
        self.plan = plan
        self.plan_id = plan["plan_id"]
        self.plan_digest = plan_digest
        self.repository = base.repository
        self._max_chars = max_chars
        self._read_obligation_refs = _freeze({oid: base.manifest["obligation_chunks"][oid]
                                            for oid in base.manifest["read_obligation_ids"]})

    def __len__(self) -> int:
        return len(self._groups)

    def summary(self) -> dict[str, Any]:
        return {"plan_id": self.plan_id, "plan_digest": self.plan_digest,
                "plan_resource": "evidence/audit-plan.json", "partition_count": len(self),
                "application_chunk_count": len(self._application_chunks),
                "planned_obligation_count": len(self.plan["obligation_chunks"])}

    def __getitem__(self, index: int | slice) -> ContextPacket | tuple[ContextPacket, ...]:
        if isinstance(index, slice):
            return tuple(self[ordinal] for ordinal in range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        start, end = self._groups[index]
        group = self._application_chunks[start:end]
        text = self._base.text + "".join(_chunk_block(chunk) for chunk in group)
        references = tuple(_freeze(chunk.reference()) for chunk in group)
        # All global inventory/omission details live in the single plan resource.
        manifest = {"schema_version": 2, "task_id": self.plan["task_id"],
                    "source_hash": self.plan["source_hash"], "suite_hash": self.plan["suite_hash"],
                    **self.summary(), "packet_kind": "AUDIT_PARTITION", "partition_index": index,
                    "max_chars": self._max_chars, "character_count": len(text),
                    "source_paths": tuple(dict.fromkeys(chunk.path for chunk in group)),
                    "partition_application_chunks": references,
                    "included_chunks": (*self._base.manifest["included_chunks"], *references),
                    "omitted_chunks": (), "omitted_chunk_count": len(self._base.manifest["omitted_chunks"]),
                    "unread_obligation_count": len(self.plan["obligation_chunks"]) - len(self._read_obligation_refs),
                    "obligation_chunks": self._read_obligation_refs,
                    "read_obligation_ids": self._base.manifest["read_obligation_ids"],
                    "application_scope_complete": False,
                    "claim_scope": "This source partition only; retrieve unread exact obligation spans from the audit plan"}
        # These mappings are already immutable; do not recursively copy/freeze
        # the shared obligation table or included-prefix metadata per packet.
        from types import MappingProxyType
        return ContextPacket(text, MappingProxyType(manifest), (*self._base.chunks, *group), self.repository)

    def __iter__(self) -> Iterator[ContextPacket]:
        for index in range(len(self)):
            yield self[index]


def build_audit_packets(bundle: TaskBundle, item_or_ids: WorkItem | Iterable[str],
                        application_dir: Path, max_chars: int = 32000, *,
                        global_contract: Mapping[str, Any] | None = None,
                        source_paths: Iterable[str] = ()) -> AuditPackets:
    """Partition exact application source instead of clipping an oversized audit.

    Call once per source revision with all atomic IDs for a global plan. Half
    of each packet is reserved for implementation source. Every selected
    text source character belongs to exactly one planned application partition.
    Requirement text may itself exceed its share: those obligations remain
    explicit planned obligations, unread and unclaimable until separately read.
    Nontext assets retain hashes and cannot be represented as a source review.
    """
    repository = ContextRepository(bundle, application_dir)
    selected_paths = tuple(source_paths)
    if not selected_paths:
        # Shared sandbox policy excludes credentials, metadata, and linked files.
        snapshot = snapshot_project(repository._application_dir)
        for path, raw in snapshot.items():
            repository._files["application/" + path] = (_digest(raw), raw)
        selected_paths = tuple(sorted(snapshot))
    application_chunks = []
    nontext = []
    for path in selected_paths:
        canonical = "application/" + path
        try:
            application_chunks.extend(repository.file_chunks(canonical))
        except TaskBundleError as error:
            if not str(error).startswith("NON_TEXT_CONTEXT_SOURCE:"):
                raise
            sha, raw = repository._files[canonical]
            nontext.append({"path": canonical, "sha256": sha, "size_bytes": len(raw), "status": "NON_TEXT_SOURCE_UNREVIEWED"})
    application_chunks = list({chunk.id: chunk for chunk in application_chunks}.values())
    largest = max((len(_chunk_block(chunk)) for chunk in application_chunks), default=0)
    reserved = max(largest, max_chars // 2)
    base = assemble_context(bundle, item_or_ids, max_chars=max_chars - reserved, global_contract=global_contract)
    # Use the base repository for requirement retrieval and copy frozen sources
    # into it so tool reads and later hash checks address the same snapshot.
    base.repository._files.update({path: value for path, value in repository._files.items() if path.startswith("application/")})
    base.repository._application_dir = repository._application_dir
    base.repository._chunks.update(repository._chunks)
    groups: list[tuple[int, int]] = []
    group_start = 0
    used = len(base.text)
    for ordinal, chunk in enumerate(application_chunks):
        size = len(_chunk_block(chunk))
        if used + size > max_chars and ordinal > group_start:
            groups.append((group_start, ordinal))
            group_start = ordinal
            used = len(base.text)
        if used + size > max_chars:
            raise TaskBundleError("AUDIT_BUDGET_TOO_SMALL_FOR_SOURCE_CHUNK")
        used += size
    groups.append((group_start, len(application_chunks)))
    plan = {"schema_version": 2, "task_id": bundle.task_id, "source_hash": bundle.source_hash,
            "suite_hash": bundle.suite_hash, "requirement_ids": base.manifest["requirement_ids"],
            "constraint_requirement_ids": base.manifest["constraint_requirement_ids"],
            "obligation_chunks": base.manifest["obligation_chunks"],
            "planned_obligation_ids": tuple(base.manifest["obligation_chunks"]),
            "planned_application_chunks": [chunk.reference() for chunk in application_chunks],
            "planned_application_files": [{"path": path, "sha256": sha, "size_bytes": len(raw)}
                                          for path, (sha, raw) in repository._files.items() if path.startswith("application/")],
            "partition_ranges": groups, "non_text_application_files": nontext,
            "input_files": base.manifest["input_files"],
            "omitted_chunks": base.manifest["omitted_chunks"],
            "application_scope_complete": False}
    seed = json.dumps(_thaw(plan), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    plan["plan_id"] = "audit-plan:" + _digest(seed.encode("utf-8"))
    plan_text = json.dumps(_thaw(plan), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    metadata = base.repository.add_evidence("evidence/audit-plan.json", plan_text)
    return AuditPackets(base, tuple(application_chunks), tuple(groups), _freeze(plan), metadata["sha256"], max_chars)


class CoverageLedger:
    """All input obligations remain visible; partition reviews never certify them.

    Records bind exact task/input/application hashes and full read obligation
    spans. Even PASS means LOCAL_SOURCE_REVIEW_PASS, never formal verification.
    """
    def __init__(self, bundle: TaskBundle):
        self.bundle = bundle
        self._entries = {_identity("obligation", *item["identity"]):
                         {**_thaw(item), "status": "UNVERIFIED_REQUIREMENT", "partitions": []}
                         for item in bundle.index.obligations}
        if len(self._entries) != len(bundle.index.obligations):
            raise TaskBundleError("DUPLICATE_OBLIGATION_IDENTITY")
        self._partitions: list[dict[str, Any]] = []

    def record_partition(self, packet: ContextPacket, obligation_ids: Iterable[str], *,
                         outcome: str, evidence: str) -> dict[str, Any]:
        packet.repository.assert_current()
        manifest = packet.manifest
        if (manifest["task_id"], manifest["source_hash"], manifest["suite_hash"]) != (
                self.bundle.task_id, self.bundle.source_hash, self.bundle.suite_hash):
            raise TaskBundleError("PARTITION_IDENTITY_MISMATCH")
        ids = tuple(dict.fromkeys(obligation_ids))
        if not ids or any(oid not in self._entries for oid in ids):
            raise TaskBundleError("UNKNOWN_OR_EMPTY_OBLIGATION_IDS")
        read_ids = set(packet.repository.read_obligation_ids(packet.chunks))
        if any(oid not in read_ids for oid in ids):
            raise TaskBundleError("UNREAD_OBLIGATION_SOURCE")
        application_chunks = [chunk for chunk in packet.chunks if chunk.kind == "application"]
        if not application_chunks or any(chunk["kind"] == "application" for chunk in manifest["omitted_chunks"]):
            raise TaskBundleError("INCOMPLETE_PARTITION_APPLICATION_SOURCE")
        if outcome not in ("PASS", "FAIL", "INCONCLUSIVE") or not isinstance(evidence, str) or not evidence.strip():
            raise TaskBundleError("INVALID_PARTITION_EVIDENCE")
        partition = {"id": f"partition-{len(self._partitions) + 1}", "task_id": self.bundle.task_id,
                     "source_hash": self.bundle.source_hash, "suite_hash": self.bundle.suite_hash,
                     "obligation_ids": list(ids), "outcome": "LOCAL_SOURCE_REVIEW_" + outcome,
                     "evidence": evidence, "application_chunks": [chunk.reference() for chunk in application_chunks],
                     "requirement_chunks": {oid: _thaw(packet.repository.obligation_chunks(oid)) for oid in ids},
                     "claim_scope": "Read source partition only; no behavioral or formal acceptance"}
        self._partitions.append(partition)
        for oid in ids:
            self._entries[oid]["partitions"].append(partition["id"])
        return deepcopy(partition)

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.bundle.task_id, "source_hash": self.bundle.source_hash,
                "obligations": deepcopy(list(self._entries.values())),
                "obligation_ids": list(self._entries), "partitions": deepcopy(self._partitions),
                "unreviewed_obligation_ids": [oid for oid, entry in self._entries.items() if not entry["partitions"]],
                "unverified_requirement_count": len(self._entries), "formal_status": "UNVERIFIED"}

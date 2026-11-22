"""Seed evidence extraction, model instructions, and static seed file validation."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .requirements import load_requirement_tree

MAX_GIVEN_STEPS = 200
MAX_TEST_CONSTANTS = 100
MAX_CONSTANT_SOURCE_CHARS = 4000

_SEED_KEYWORD_RE = re.compile(
    r"(?i)(?:seed(?:ed|ing|s)?|pre-?existing|existing|prerequisite(?:s)?|registered|bootstrap(?:ped)?)"
)
_SPEC_FILE_RE = re.compile(r"\.(?:spec|test)\.[cm]?[jt]sx?$", re.IGNORECASE)
_EXPORT_CONST_RE = re.compile(r"(?m)^[ \t]*export[ \t]+const[ \t]+([A-Za-z_$][A-Za-z0-9_$]*)")
_STRING_LITERAL_RE = re.compile(
    r"""'([^'\\]*(?:\\.[^'\\]*)*)'|"([^"\\]*(?:\\.[^"\\]*)*)"|`([^`$\\]*(?:\\.[^`$\\]*)*)`"""
)
_OBJECT_KEY_RE = re.compile(r"(?:^|[{,])\s*(?:['\"]([A-Za-z0-9_$-]+)['\"]|([A-Za-z_$][A-Za-z0-9_$]*))\s*:")
_SEED_HEADER_RE = re.compile(r"(?i)^\s*Seed prerequisites(?:\s*\([^\r\n]*\))?\s*:\s*$")
_SEED_BULLET_RE = re.compile(r"^(?P<indent>[ \t]*)[-*][ \t]+(?P<body>.*)$")
_GIVEN_PREFIX_RE = re.compile(r"(?is)^\s*given\b\s*:?\s*(.*)$")


def _load_requirements(requirements: Mapping[str, Any] | Path | str) -> Mapping[str, Any]:
    if isinstance(requirements, Mapping):
        return requirements
    path = Path(requirements)
    if path.is_file():
        suffix = path.suffix.lower()
        if suffix in {".yaml", ".yml"}:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
            return loaded if isinstance(loaded, Mapping) else {}
        if suffix == ".json":
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            return loaded if isinstance(loaded, Mapping) else {}
        return load_requirement_tree(path.parent)
    if path.is_dir():
        yaml_file = path / "requirements.yaml"
        if yaml_file.is_file():
            loaded = yaml.safe_load(yaml_file.read_text(encoding="utf-8-sig"))
            return loaded if isinstance(loaded, Mapping) else {}
        return load_requirement_tree(path)
    raise FileNotFoundError(f"Requirements source not found: {requirements}")


def _strict_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("non-finite JSON constant: " + value)


def parse_seed_prerequisites(
    text: str, *, requirement_id: str = "", scenario_id: str = "",
    step_id: str = "", source_path: str = "",
) -> dict[str, Any]:
    """Preserve explicit public seed entries without inferring or executing setup.

    ``parsed`` means JSON syntax only. Nested prose remains in ``value`` and
    ``raw``; it does not grant an executable record mapping or a ready session.
    Unsupported entries remain visible instead of becoming empty prerequisites.
    Line locations are relative to the original Given field, not the YAML file.
    """
    lines = text.splitlines(keepends=True)
    headers = [i for i, line in enumerate(lines) if _SEED_HEADER_RE.fullmatch(line)]
    result = {"status": "absent", "entries": [], "remaining_text": text, "issues": []}
    if not headers:
        return result
    owner = {"requirement_id": requirement_id, "scenario_id": scenario_id,
             "step_id": step_id, "source_path": source_path}
    result["remaining_text"] = "".join(lines[:headers[0]]).strip()
    for block_index, header in enumerate(headers):
        end = headers[block_index + 1] if block_index + 1 < len(headers) else len(lines)
        starts = []
        bullet_indent = None
        for pos in range(header + 1, end):
            match = _SEED_BULLET_RE.match(lines[pos].rstrip("\r\n"))
            if match and (bullet_indent is None or len(match["indent"]) <= bullet_indent):
                bullet_indent = len(match["indent"]) if bullet_indent is None else bullet_indent
                starts.append(pos)
        prefix_end = starts[0] if starts else end
        prefix = "".join(lines[header + 1:prefix_end])
        if prefix.strip() or not starts:
            result["issues"].append({
                "reason": "unparsed_seed_text" if prefix.strip() else "missing_seed_entries",
                "raw": prefix, "source": dict(owner, line_start=header + 1, line_end=prefix_end),
            })
        for ordinal, start in enumerate(starts):
            stop = starts[ordinal + 1] if ordinal + 1 < len(starts) else end
            raw = "".join(lines[start:stop])
            body = _SEED_BULLET_RE.match(lines[start].rstrip("\r\n"))["body"]
            label, separator, first_value = body.partition(":")
            payload = first_value + "\n" + "".join(lines[start + 1:stop])
            value, reason = None, None
            if not separator or not label.strip():
                reason = "missing_seed_label_or_value_separator"
            else:
                try:
                    value = json.loads(payload, object_pairs_hook=_strict_json_object,
                                       parse_constant=_reject_json_constant)
                    if not isinstance(value, (dict, list)):
                        reason = "seed_value_is_not_an_object_or_array"
                        value = None
                except (ValueError, RecursionError) as exc:
                    reason = "invalid_seed_json: " + str(exc)
            result["entries"].append({
                "id": (f"{scenario_id or requirement_id or 'given'}::"
                       f"{source_path or step_id or 'Given'}::seed-{block_index + 1}-{ordinal + 1}"),
                "label": label.strip() if separator else "", "raw": raw,
                "value": value, "parse_status": "unparsed" if reason else "parsed",
                "reason": reason,
                "source": dict(owner, line_start=start + 1, line_end=stop),
            })
    parsed = sum(entry["parse_status"] == "parsed" for entry in result["entries"])
    if parsed and parsed == len(result["entries"]) and not result["issues"]:
        result["status"] = "parsed"
    else:
        result["status"] = "partial" if parsed else "unparsed"
    return result


def public_scenario_id(work_id: str, scenario: Any, index: int) -> str:
    raw = scenario.get("id") if isinstance(scenario, Mapping) else None
    if isinstance(raw, (str, int)) and not isinstance(raw, bool) and str(raw).strip():
        return str(raw).strip()
    return f"{work_id}::scenario-{index + 1}"


def scenario_given_sources(
    scenario: Any, *, requirement_id: str, scenario_id: str, source_path: str,
) -> list[dict[str, Any]]:
    """Return Given occurrences with owners; identical text is not shared state."""
    rows = []

    def append(text, step_id, path):
        if not isinstance(text, str) or not text.strip():
            return
        rows.append({
            "requirement_id": requirement_id, "scenario_id": scenario_id,
            "step_id": step_id, "source_path": path, "given": text,
            "seed_prerequisites": parse_seed_prerequisites(
                text, requirement_id=requirement_id, scenario_id=scenario_id,
                step_id=step_id, source_path=path),
        })

    def consume(step, index, path, *, allow_typed=True):
        fallback = f"{scenario_id}::step-{index + 1}"
        if isinstance(step, str):
            match = _GIVEN_PREFIX_RE.match(step)
            if match:
                append(match[1], fallback, path)
            return
        if not isinstance(step, Mapping):
            return
        step_id = str(step.get("id") or fallback)
        phase_keys = [key for key in step if str(key).lower() in {"given", "when", "then"}]
        if phase_keys:
            for key in phase_keys:
                if str(key).lower() == "given":
                    append(step[key], step_id, path + "." + str(key))
            return
        if (not allow_typed or set(step) - {"id", "references", "keyword", "type", "content", "text"}
                or ("keyword" in step and "type" in step) or ("content" in step and "text" in step)):
            return
        keyword = step.get("keyword", step.get("type"))
        if isinstance(keyword, str) and keyword.strip().lower() == "given":
            field = "content" if "content" in step else "text"
            append(step.get(field), step_id, path + "." + field)

    if isinstance(scenario, str):
        append(scenario, f"{scenario_id}::step-1", source_path)
    elif isinstance(scenario, Mapping):
        steps = scenario.get("steps")
        if isinstance(steps, list):
            for index, step in enumerate(steps):
                consume(step, index, f"{source_path}.steps[{index}]")
        elif isinstance(steps, str) and steps.strip():
            consume(steps, 0, source_path + ".steps")
        else:
            consume(scenario, 0, source_path, allow_typed=False)
    return rows


def _given_evidence(tree: Mapping[str, Any], limit: int | None = MAX_GIVEN_STEPS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # The same traversal identities address both the full IR and scoped retrieval.
    from .platform_task_ir import _work_id
    given_steps = []
    total = 0
    seen = set()

    def visit(node: Any, path: str) -> None:
        nonlocal total
        if not isinstance(node, Mapping):
            return
        req_id = _work_id(node, seen, len(seen))
        seen.add(req_id)
        scenarios = node.get("scenarios")
        if isinstance(scenarios, list):
            for index, scenario in enumerate(scenarios):
                scenario_id = public_scenario_id(req_id, scenario, index)
                rows = scenario_given_sources(scenario, requirement_id=req_id, scenario_id=scenario_id,
                                              source_path=f"{path}.scenarios[{index}]")
                total += len(rows)
                given_steps.extend(rows if limit is None else rows[:max(0, limit - len(given_steps))])
        children = node.get("children")
        if isinstance(children, list):
            for index, child in enumerate(children):
                visit(child, f"{path}.children[{index}]")

    visit(tree, "requirements")
    return given_steps, {"total": total, "returned": len(given_steps),
                         "omitted": total - len(given_steps), "truncated": total > len(given_steps),
                         "limit": limit}


def _extract_given_steps(tree: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _given_evidence(tree)[0]


def _skip_string_or_comment(text: str, index: int) -> int:
    n = len(text)
    if text.startswith("//", index):
        nl = text.find("\n", index + 2)
        return n if nl == -1 else nl + 1
    if text.startswith("/*", index):
        end = text.find("*/", index + 2)
        return n if end == -1 else end + 2
    quote = text[index]
    if quote in {"'", '"', "`"}:
        index += 1
        while index < n:
            ch = text[index]
            if ch == "\\":
                index += 2
                continue
            if ch == quote:
                return index + 1
            index += 1
        return n
    return index + 1


def _find_assignment_equals(text: str, pos: int) -> int:
    """Advance past optional TypeScript type annotation `: ...` to the `=` token."""
    n = len(text)
    while pos < n and text[pos].isspace():
        pos += 1
    if pos < n and text[pos] == ":":
        pos += 1
        depth_angle = 0
        depth_brace = 0
        depth_paren = 0
        depth_bracket = 0
        while pos < n:
            ch = text[pos]
            if ch in {"'", '"', "`"} or text.startswith("//", pos) or text.startswith("/*", pos):
                pos = _skip_string_or_comment(text, pos)
                continue
            if ch == "<":
                depth_angle += 1
            elif ch == ">" and pos > 0 and text[pos - 1] != "=":
                depth_angle = max(0, depth_angle - 1)
            elif ch == "{":
                depth_brace += 1
            elif ch == "}":
                depth_brace = max(0, depth_brace - 1)
            elif ch == "(":
                depth_paren += 1
            elif ch == ")":
                depth_paren = max(0, depth_paren - 1)
            elif ch == "[":
                depth_bracket += 1
            elif ch == "]":
                depth_bracket = max(0, depth_bracket - 1)
            elif (
                ch == "="
                and depth_angle == 0
                and depth_brace == 0
                and depth_paren == 0
                and depth_bracket == 0
                and (pos + 1 >= n or text[pos + 1] != ">")
            ):
                return pos
            pos += 1
        return -1
    if pos < n and text[pos] == "=" and (pos + 1 >= n or text[pos + 1] != ">"):
        return pos
    return -1


def _extract_literal_expression(text: str, start: int) -> str | None:
    """Extract an object, array, or string literal starting at `start`."""
    n = len(text)
    pos = start
    while pos < n and text[pos].isspace():
        pos += 1
    if pos >= n:
        return None
    first = text[pos]
    expr_start = pos

    if first in {"{", "["}:
        closing = "}" if first == "{" else "]"
        depth = 0
        while pos < n:
            ch = text[pos]
            if ch in {"'", '"', "`"} or text.startswith("//", pos) or text.startswith("/*", pos):
                pos = _skip_string_or_comment(text, pos)
                continue
            if ch == first:
                depth += 1
            elif ch == closing:
                depth -= 1
                if depth == 0:
                    pos += 1
                    break
            pos += 1
        if depth != 0:
            return None
        return text[expr_start:pos].strip()

    if first in {"'", '"', "`"}:
        end = _skip_string_or_comment(text, pos)
        literal = text[expr_start:end].strip()
        return literal

    return None


def _nearby_comment(text: str, decl_start: int) -> str:
    """Collect contiguous comment lines immediately preceding `decl_start` and same-line comments."""
    prefix = text[:decl_start]
    lines = prefix.splitlines()
    collected: list[str] = []
    in_block = False
    for line in reversed(lines[-8:]):
        stripped = line.strip()
        if not stripped:
            if collected:
                break
            continue
        if in_block:
            collected.append(stripped)
            if "/*" in stripped:
                in_block = False
            continue
        if stripped.startswith("//") or (stripped.startswith("/*") and stripped.endswith("*/")):
            collected.append(stripped)
            continue
        if stripped.endswith("*/"):
            collected.append(stripped)
            if "/*" not in stripped:
                in_block = True
            continue
        break
    line_end = text.find("\n", decl_start)
    same_line = text[decl_start : line_end if line_end != -1 else len(text)]
    if "//" in same_line:
        collected.append(same_line[same_line.find("//") :])
    return "\n".join(reversed(collected))


def _name_tokens(name: str, source: str) -> set[str]:
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower().split("_")
    tokens = {p for p in parts if len(p) >= 3}
    for match in _OBJECT_KEY_RE.finditer(source):
        key = (match.group(1) or match.group(2) or "").lower()
        if len(key) >= 3:
            tokens.add(key)
    return tokens


def _extract_string_values(source: str) -> list[str]:
    values: list[str] = []
    for match in _STRING_LITERAL_RE.finditer(source):
        val = next((g for g in match.groups() if g is not None), "").strip()
        if len(val) >= 2:
            values.append(val)
    return values


def _compute_seed_hint(
    name: str,
    source: str,
    nearby_comment: str,
    given_texts: list[str],
) -> bool:
    if _SEED_KEYWORD_RE.search(nearby_comment) or _SEED_KEYWORD_RE.search(name):
        return True
    if _SEED_KEYWORD_RE.search(source):
        return True
    given_lower = [g.lower() for g in given_texts]
    for value in _extract_string_values(source):
        val_lower = value.lower()
        if any(val_lower in g for g in given_lower):
            return True
    tokens = _name_tokens(name, source)
    for g_raw, g_low in zip(given_texts, given_lower):
        if _SEED_KEYWORD_RE.search(g_raw):
            for tok in tokens:
                if re.search(r"\b" + re.escape(tok) + r"s?\b", g_low):
                    return True
    return False


def _extract_test_constants(tests_dir: Path | None, given_texts: list[str]) -> list[dict[str, Any]]:
    if tests_dir is None:
        return []
    tests_dir = Path(tests_dir)
    if not tests_dir.is_dir():
        return []

    constants: list[dict[str, Any]] = []
    for path in sorted(tests_dir.rglob("*")):
        if len(constants) >= MAX_TEST_CONSTANTS:
            break
        if not path.is_file():
            continue
        if "node_modules" in path.parts:
            continue
        if path.suffix.lower() not in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
            continue
        if path.name.lower().endswith(".d.ts") or _SPEC_FILE_RE.search(path.name):
            continue
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            continue

        rel_file = path.relative_to(tests_dir).as_posix()
        for match in _EXPORT_CONST_RE.finditer(content):
            if len(constants) >= MAX_TEST_CONSTANTS:
                break
            name = match.group(1)
            eq_pos = _find_assignment_equals(content, match.end())
            if eq_pos == -1:
                continue
            literal = _extract_literal_expression(content, eq_pos + 1)
            if literal is None:
                continue
            bounded_source = literal[:MAX_CONSTANT_SOURCE_CHARS]
            comment = _nearby_comment(content, match.start())
            hint = _compute_seed_hint(name, bounded_source, comment, given_texts)
            constants.append(
                {
                    "file": rel_file,
                    "name": name,
                    "source": bounded_source,
                    "source_truncated": len(literal) > MAX_CONSTANT_SOURCE_CHARS,
                    "source_char_count": len(literal),
                    "seed_hint": hint,
                }
            )
    return constants


def collect_seed_evidence(
    requirements: Mapping[str, Any] | Path | str,
    tests_dir: Path | str | None = None, *, given_limit: int | None = MAX_GIVEN_STEPS,
) -> dict[str, Any]:
    """Collect owned public Given evidence; legacy helper input remains optional."""
    tree = _load_requirements(requirements)
    given_steps, given_summary = _given_evidence(tree, given_limit)
    given_texts = [item["given"] for item in given_steps]
    test_constants = _extract_test_constants(
        Path(tests_dir) if tests_dir is not None else None,
        given_texts,
    )
    return {
        "given_steps": given_steps,
        "given_summary": given_summary,
        "test_constants": test_constants,
        "test_constants_limit_reached": len(test_constants) >= MAX_TEST_CONSTANTS,
    }


def seed_instructions() -> str:
    """Return application-neutral instructions for creating startup seed files."""
    return (
        "Seed Data Rules:\n"
        "1. Create ONLY records explicitly declared as public initial/seed data, as JSON "
        "files under `seed/<collection>.json` with shape `{\"collection\": \"<name>\", \"records\": [...]}`.\n"
        "2. A Given condition is not permission to seed it. Records declared as UI-created and runtime "
        "states such as signed-in sessions must be established through the stated operations, including "
        "operations described in Given. Separate declared initial records from scenario-created records.\n"
        "3. Reuse the exact identifiers, usernames, emails, credentials, and field values from requirement "
        "Given steps. Parsed seed JSON is input evidence, not proof of an executable setup or satisfied "
        "preconditions. Preserve unresolved entries and nested prose; do not invent missing values or "
        "merge differently scoped records by label. Optional helper constants do not override public "
        "initial-state rules.\n"
        "4. Specify plaintext passwords in seed records using a field named exactly `password`; "
        "`server/seed.js` automatically replaces `password` with `passwordHash` via `hashPassword` "
        "(scrypt) once per fresh storage on server startup."
    )


def validate_seeds(project_dir: Path | str) -> list[dict[str, str]]:
    """Validate `seed/*.json` files for JSON syntax, structure, object records, and unique ids."""
    project_path = Path(project_dir)
    seed_dir = project_path / "seed"
    if not seed_dir.is_dir():
        return []

    errors: list[dict[str, str]] = []
    for path in sorted(seed_dir.glob("*.json")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(project_path).as_posix()
        try:
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append({"path": rel_path, "error": f"Invalid JSON: {exc}"})
            continue

        if not isinstance(parsed, dict):
            errors.append(
                {
                    "path": rel_path,
                    "error": "Seed file must contain a JSON object with 'collection' and 'records'",
                }
            )
            continue

        collection = parsed.get("collection")
        records = parsed.get("records")
        missing_fields: list[str] = []
        if not isinstance(collection, str) or not collection.strip():
            missing_fields.append("collection")
        if not isinstance(records, list):
            missing_fields.append("records")
        if missing_fields:
            errors.append(
                {
                    "path": rel_path,
                    "error": f"Missing or invalid required field(s): {', '.join(missing_fields)}",
                }
            )
            if not isinstance(records, list):
                continue

        seen_ids: set[str] = set()
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                errors.append(
                    {
                        "path": rel_path,
                        "error": f"Record at index {index} must be a JSON object",
                    }
                )
                continue
            if "id" in record and record["id"] is not None and record["id"] != "":
                rec_id = str(record["id"])
                if rec_id in seen_ids:
                    errors.append(
                        {
                            "path": rel_path,
                            "error": f"Duplicate record id '{rec_id}' within {rel_path}",
                        }
                    )
                else:
                    seen_ids.add(rec_id)

    return errors

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


_MARKDOWN_FILES = ("README.md", "requirements.md", "requirement.md")
_HEADING_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+?)\s*#*\s*$")
_REQUIREMENT_HEADING_RE = re.compile(
    r"^(?P<id>REQ-[A-Za-z0-9][A-Za-z0-9._-]*)\s*[:：-]?\s+(?P<name>.+?)\s*$",
    re.IGNORECASE,
)
_STEP_RE = re.compile(r"^(GIVEN|WHEN|THEN|AND)\s*:\s*(.*)$", re.IGNORECASE)
_IMAGE_EXTENSION_RE = re.compile(r"\.(?:png|jpe?g|gif|webp)(?:[?#].*)?$", re.IGNORECASE)


@dataclass(frozen=True)
class RequirementModule:
    node_id: str
    name: str
    subtree: dict[str, Any]


def load_requirement_tree(requirement_dir: Path) -> dict[str, Any]:
    yaml_path = requirement_dir / "requirements.yaml"
    if yaml_path.is_file():
        payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        return _validate_requirement_tree(payload, yaml_path)

    for filename in _MARKDOWN_FILES:
        markdown_path = requirement_dir / filename
        if markdown_path.is_file():
            return _parse_markdown_requirements(markdown_path)

    expected = ", ".join(("requirements.yaml", *_MARKDOWN_FILES))
    raise FileNotFoundError(
        f"No supported task requirements found in {requirement_dir}. "
        f"Expected one of: {expected}."
    )


def _validate_requirement_tree(payload: Any, source_path: Path) -> dict[str, Any]:
    if not isinstance(payload, dict) or str(payload.get("id", "")).strip() != "ROOT":
        raise ValueError(f"{source_path.name} must contain a ROOT mapping")
    if not isinstance(payload.get("children"), list):
        raise ValueError(f"{source_path.name}: ROOT must contain a children list")
    if not payload["children"]:
        raise ValueError(f"{source_path.name}: ROOT must contain at least one child requirement")
    return payload


def _clean_markdown(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*(.*?)\*\*|__(.*?)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*", r"\1", text)
    text = re.sub(r"(?m)^\s*>\s?", "", text)
    return text.strip()


def _extract_scenarios(section: str, req_id: str) -> tuple[list[dict[str, Any]], str]:
    match = re.search(r"(?im)^\s*(?:\*\*)?Scenarios:(?:\*\*)?\s*$", section)
    if not match:
        return [], section

    scenario_text = section[match.end():]
    remaining = section[:match.start()]
    scenarios: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def start_scenario(name: str) -> dict[str, Any]:
        scenario = {
            "id": f"SCN-{req_id}-{len(scenarios) + 1}",
            "name": _clean_markdown(name).strip(" :-"),
            "steps": [],
        }
        scenarios.append(scenario)
        return scenario

    for raw_line in scenario_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        item = re.match(r"^(?P<indent>\s*)(?:[-*+]\s+|\d+[.)]\s+)(?P<body>.+)$", raw_line)
        content = item.group("body").strip() if item else stripped
        content = _clean_markdown(content)
        step_match = _STEP_RE.match(content)

        if step_match:
            if current is None:
                current = start_scenario("Scenario")
            current["steps"].append(
                {"type": step_match.group(1).upper(), "content": step_match.group(2).strip()}
            )
        elif item or re.match(r"(?i)^Scenario\s*:", content):
            name = re.sub(r"(?i)^Scenario\s*:\s*", "", content).strip()
            current = start_scenario(name)
        elif current is not None and current["steps"] and current["steps"][-1]["type"] != "THEN":
            last_step = current["steps"][-1]
            last_step["content"] = f"{last_step['content']} {content}".strip()
        else:
            current = start_scenario(content)

    return scenarios, remaining


def _parse_requirement_section(req_id: str, name: str, section: str) -> dict[str, Any]:
    scenarios, section_without_scenarios = _extract_scenarios(section, req_id)
    normalized_section = _clean_markdown(section)
    type_match = re.search(r"\bType\s*:\s*(FOLDER|ATOMIC)\b", normalized_section, re.IGNORECASE)
    dependencies_match = re.search(
        r"\bDependencies\s*:\s*(.*?)(?=\s+(?:Type|Function|Required\s+system\s+data|Scenarios)\s*:|$)",
        normalized_section,
        re.IGNORECASE | re.DOTALL,
    )
    dependencies: list[str] = []
    if dependencies_match:
        raw_dependencies = dependencies_match.group(1).strip()
        if raw_dependencies and raw_dependencies.lower() != "none":
            dependencies = [
                value.strip()
                for value in re.split(r"[,;\n]", raw_dependencies)
                if value.strip()
            ]

    references = _extract_visual_references(section)
    description = _clean_markdown(section_without_scenarios)
    description = re.sub(r"\bType\s*:\s*(?:FOLDER|ATOMIC)\b", "", description, flags=re.IGNORECASE)
    description = re.sub(
        r"\bDependencies\s*:\s*(?:None|REQ-[A-Za-z0-9._-]+(?:\s*[,;]\s*REQ-[A-Za-z0-9._-]+)*)",
        "",
        description,
        flags=re.IGNORECASE,
    )
    description = re.sub(r"\bFunction\s*:\s*", "", description, flags=re.IGNORECASE)
    description = re.sub(r"\bRequired\s+system\s+data\s*:\s*", "Required system data: ", description, flags=re.IGNORECASE)
    description = re.sub(r"(?i)Optional visual reference\s*:?", "", description)
    description = re.sub(r"\s+", " ", description).strip()

    node: dict[str, Any] = {
        "id": req_id,
        "name": _clean_markdown(name),
        "description": description,
        "dependencies": dependencies,
        "scenarios": scenarios,
        "children": [],
    }
    if type_match:
        node["type"] = type_match.group(1).upper()
    if references:
        node["visual_reference"] = list(dict.fromkeys(references))
    return node


def _extract_visual_references(section: str) -> list[str]:
    references: list[str] = []
    patterns = (
        r"!\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))[^)]*\)",
        r"\[Open\]\(\s*(?:<([^>]+)>|([^\s)]+))[^)]*\)",
        r"<img\b[^>]*\bsrc\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))[^>]*>",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, section, re.IGNORECASE):
            reference = next((group for group in match.groups() if group), "").strip()
            if reference and _IMAGE_EXTENSION_RE.search(reference) and reference not in references:
                references.append(reference)
    return references


def _parse_markdown_requirements(path: Path) -> dict[str, Any]:
    markdown = path.read_text(encoding="utf-8-sig")
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    headings: list[tuple[int, int, str, str | None, str | None]] = []
    fence_marker: str | None = None
    for index, line in enumerate(lines):
        fence = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)[0]
            if fence_marker is None:
                fence_marker = marker
            elif marker == fence_marker:
                fence_marker = None
            continue
        if fence_marker is not None:
            continue
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            level = len(heading_match.group("level"))
            title = _clean_markdown(heading_match.group("title"))
        else:
            title = _clean_markdown(line.strip())
            plain_requirement_match = _REQUIREMENT_HEADING_RE.match(title)
            if not plain_requirement_match:
                continue
            level = 2 + plain_requirement_match.group("id").count(".")
        requirement_match = _REQUIREMENT_HEADING_RE.match(title)
        headings.append(
            (
                index,
                level,
                title,
                requirement_match.group("id") if requirement_match else None,
                requirement_match.group("name") if requirement_match else None,
            )
        )

    if not headings:
        raise ValueError(f"{path.name} has no Markdown headings; expected a title and REQ-* headings")

    title_heading = next((heading for heading in headings if heading[3] is None), None)
    first_requirement_index = next((i for i, heading in enumerate(headings) if heading[3]), None)
    intro_end = headings[first_requirement_index][0] if first_requirement_index is not None else len(lines)
    first_content_line = next((i for i, line in enumerate(lines[:intro_end]) if line.strip()), None)
    if title_heading:
        title = title_heading[2]
        intro_start = title_heading[0] + 1
    elif first_content_line is not None:
        title = _clean_markdown(lines[first_content_line])
        intro_start = first_content_line + 1
    else:
        title = path.stem
        intro_start = 0
    intro = _clean_markdown("\n".join(lines[intro_start:intro_end]))

    requirement_headings = [heading for heading in headings if heading[3]]
    if not requirement_headings:
        raise ValueError(f"{path.name} has no REQ-* headings to build a requirement tree")

    nodes_by_id: dict[str, dict[str, Any]] = {}
    level_by_id: dict[str, int] = {}
    ordered_ids: list[str] = []
    for position, heading in enumerate(requirement_headings):
        line_index, level, _, req_id, req_name = heading
        assert req_id is not None and req_name is not None
        if req_id in nodes_by_id:
            raise ValueError(f"{path.name} contains duplicate requirement id {req_id}")
        next_requirement_line = (
            requirement_headings[position + 1][0]
            if position + 1 < len(requirement_headings)
            else len(lines)
        )
        section = "\n".join(lines[line_index + 1:next_requirement_line])
        nodes_by_id[req_id] = _parse_requirement_section(req_id, req_name, section)
        level_by_id[req_id] = level
        ordered_ids.append(req_id)

    root: dict[str, Any] = {
        "id": "ROOT",
        "name": title,
        "description": intro,
        "children": [],
    }
    root_references = _extract_visual_references(intro)
    if root_references:
        root["visual_reference"] = root_references
    for req_id in ordered_ids:
        node = nodes_by_id[req_id]
        parent_id = next(
            (
                candidate
                for candidate in reversed(ordered_ids[:ordered_ids.index(req_id)])
                if req_id.startswith(f"{candidate}.")
            ),
            None,
        )
        if parent_id:
            nodes_by_id[parent_id]["children"].append(node)
            continue

        level = level_by_id[req_id]
        heading_parent = next(
            (
                candidate
                for candidate in reversed(ordered_ids[:ordered_ids.index(req_id)])
                if level_by_id[candidate] < level
            ),
            None,
        )
        if heading_parent:
            nodes_by_id[heading_parent]["children"].append(node)
        else:
            root["children"].append(node)

    return _validate_requirement_tree(root, path)


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

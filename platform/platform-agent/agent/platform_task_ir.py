"""Deterministic task intermediate representation.

The parser follows the requirements mapping already used by the platform:
a tree of children, scenario steps in either keyword/content form or
Given/When/Then mappings, and data entities with properties and lifecycle.
Accessible names are taken only from explicit role annotations. Runtime
layout is read from deploy.sh and package.json. model_fill is optional and
is never replaced with a real model call.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

SCHEMA_DESCRIPTION = (
    "Return a mapping with any of these extra fields. "
    "ui_elements: list of objects with role (ARIA or Playwright token), name, "
    "page (string or null), work_item (an existing work item id or null), "
    "group (string or null), persistent (boolean), multiple (boolean), evidence (string). "
    "scenarios: list of objects with id, work_item, name, "
    "given {session: anonymous|authenticated|unknown, account: string or null, data: list of strings, text: string}, "
    "when: list of {action: open|click|fill|check|select|submit|reload|unknown, "
    "target: {role: string or null, name: string or null}, value: string or null, text: string}, "
    "then: list of {kind: visible|hidden|text|count|persisted|url|unknown, target, value, text}. "
    "entities: list of {id, name, fields: list of strings, lifecycle: SEED|RUNTIME|null, seed_records: list of objects}. "
    "pages: list of {name, path: string or null, reference: string or null}. "
    "Auth is not accepted from this callback."
)

# "Name"(role=link), 'Name'(role="textbox"), “名称”(role=button), 「名称」(role='link').
_ANNOTATION = re.compile(
    r"(?:"
    r'"(?P<dn>(?:\\.|[^"\\])*)"'
    r"|'(?P<sn>(?:\\.|[^'\\])*)'"
    r"|“(?P<cn>[^”]*)”"
    r"|「(?P<jn>[^」]*)」"
    r")"
    r"\s*\(\s*role\s*=\s*"
    r"(?:"
    r'"(?P<rd>[^"]+)"'
    r"|'(?P<rs>[^']+)'"
    r"|(?P<rb>[A-Za-z][A-Za-z0-9_-]*)"
    r")"
    r"\s*\)"
)
_QUOTED = re.compile(
    r'"(?P<dn>(?:\\.|[^"\\])*)"'
    r"|'(?P<sn>(?:\\.|[^'\\])*)'"
    r"|“(?P<cn>[^”]*)”"
    r"|「(?P<jn>[^」]*)」"
)

# Generic session wording. Anonymous phrases are removed before the
# authenticated check so a negated phrase is not also read as signed in.
_ANONYMOUS_PHRASES = (
    "no session",
    "without a session",
    "not signed in",
    "not logged in",
    "signed out",
    "logged out",
    "unauthenticated",
    "anonymous",
    "未登录",
    "未登入",
    "没有会话",
    "无会话",
    "未认证",
    "匿名",
)
_AUTHENTICATED_PHRASES = (
    "already signed in",
    "already logged in",
    "signed in",
    "logged in",
    "authenticated",
    "已登录",
    "已登入",
    "已经登录",
    "已认证",
)

# One action per step. Two obvious verbs in the same step stay unknown.
_ACTION_PATTERNS = (
    ("reload", re.compile(r"(?i)\b(?:reload(?:s|ed|ing)?|refresh(?:es|ed|ing)?)\b|刷新|重新加载")),
    ("submit", re.compile(r"(?i)\bsubmit(?:s|ted|ting)?\b|提交")),
    ("check", re.compile(r"(?i)\b(?:uncheck(?:s|ed|ing)?|check(?:s|ed|ing)?|tick(?:s|ed|ing)?)\b|勾选")),
    ("select", re.compile(r"(?i)\b(?:select(?:s|ed|ing)?|choose(?:s|d)?|chosen)\b|选择")),
    ("fill", re.compile(r"(?i)\bfill(?:s|ed|ing)?\b|填写|输入")),
    ("click", re.compile(r"(?i)\b(?:click(?:s|ed|ing)?|tap(?:s|ped|ping)?)\b|点击|单击")),
    ("open", re.compile(r"(?i)\b(?:open(?:s|ed|ing)?|visit(?:s|ed|ing)?|navigate(?:s|d)?)\b|\bgo to\b|打开|访问|进入")),
)
_KIND_PATTERNS = (
    ("hidden", re.compile(r"(?i)\b(?:hidden|disappears|disappeared)\b|\bnot visible\b|\bnot shown\b|不可见|不显示|隐藏")),
    ("url", re.compile(r"(?i)\burl\b|地址")),
    ("count", re.compile(r"(?i)\bcount\b|\bnumber of\b|数量")),
    ("persisted", re.compile(r"(?i)\bpersist(?:s|ed|ing)?\b|\bsaved\b|已保存|被保存")),
    ("text", re.compile(r"(?i)\btext\b|\breads\b|文本|内容为")),
    ("visible", re.compile(r"(?i)\b(?:visible|displayed|shown|appears)\b|可见|显示")),
)
_PERSIST = re.compile(
    r"(?i)remain available|always available|stay available|stays available|一直可用|始终可用|一直保持可用|始终保持可用"
)
_MULTIPLE = re.compile(
    r"(?i)more than once|more than one|multiple times|不止一次|多次出现|可以出现多次|可出现多次"
)
_ENTRY_NAME = re.compile(r"(?i)^(?:sign[\s-]?in|log[\s-]?in|登录)$")
_SUBMIT_NAME = re.compile(r"(?i)^(?:sign[\s-]?in|log[\s-]?in|submit|登录|提交)$")
_ACCOUNT_ENTITY = re.compile(r"(?i)account|user|member|credential")
_ROLE_TOKEN = re.compile(r"^[a-z][a-z0-9_-]*$")
_ACTIONS = {"open", "click", "fill", "check", "select", "submit", "reload", "unknown"}
_KINDS = {"visible", "hidden", "text", "count", "persisted", "url", "unknown"}
_SESSIONS = {"anonymous", "authenticated", "unknown"}
_STEP_PREFIX = re.compile(r"(?is)^(given|when|then)\b\s*:?\s*(.*)$")

_INSTALL_CMD = re.compile(
    r"(?im)(?:^|[;&]\s*)((?:npm|pnpm|yarn|bun)\s+(?:ci|install)\b[^;\n\r#]*)"
)
_BUILD_CMD = re.compile(
    r"(?im)(?:^|[;&]\s*)((?:npm|pnpm|yarn|bun)\s+(?:run[ \t]+)?build\b[^;\n\r#]*)"
)
_START_CMD = re.compile(
    r"(?im)(?:^|[;&]\s*)((?:(?:npm|pnpm|yarn|bun)\s+(?:run[ \t]+)?(?:start|dev)\b|(?:node|python3?)\s+\S)[^;\n\r#]*)"
)
_PORT_PATTERNS = (
    re.compile(r"(?i)\bPORT\s*=\s*(\d{2,5})\b"),
    re.compile(r"(?i)--port(?:=|\s+)(\d{2,5})\b"),
    re.compile(r"(?i)\bport\s*[:=]\s*(\d{2,5})\b"),
    re.compile(r"(?i)\blisten(?:s|ing)?\s*\(?\s*(\d{2,5})\b"),
    re.compile(r"(?i)(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{2,5})\b"),
)
_HEALTH_PATH = re.compile(r"(?i)(/[A-Za-z0-9_./-]*health[A-Za-z0-9_./-]*)")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


def build_task_ir(requirements_path, project_dir, *, model_fill=None) -> dict:
    """Parse requirements and attach detect_runtime(project_dir)."""
    tree, rejected = _load_requirements(requirements_path)
    entities = _entities(tree)
    pages = _pages(tree)
    work_items, scenarios, blobs = _walk(tree, entities)
    elements = _collect_elements(blobs)
    auth = _auth(elements)
    fragments = _fragments(work_items, scenarios, elements)
    work_ids = {item["id"] for item in work_items}
    rejected.extend(_apply_model_fill(
        model_fill,
        fragments,
        elements,
        scenarios,
        entities,
        pages,
        work_ids,
    ))
    return {
        "version": 1,
        "task": _task(tree),
        "work_items": work_items,
        "entities": entities,
        "pages": pages,
        "ui_elements": elements,
        "scenarios": scenarios,
        "auth": auth,
        "runtime": detect_runtime(project_dir),
        "rejected": rejected,
    }


def write_task_ir(project_dir, ir) -> None:
    """Write docs/task-ir.json as UTF-8 with LF newlines."""
    path = Path(project_dir) / "docs" / "task-ir.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(ir, indent=2, ensure_ascii=False) + "\n"
    path.write_text(payload, encoding="utf-8", newline="\n")


def detect_runtime(project_dir) -> dict:
    """Read deploy.sh, root package.json, and one level of child package.json files."""
    root = Path(project_dir)
    result = {
        "layout": "empty",
        "install": None,
        "build": None,
        "start": None,
        "port": None,
        "health": None,
        "deploy_sh": False,
        "notes": [],
    }
    if not root.exists() or not root.is_dir():
        result["notes"].append("project directory is missing")
        return result
    try:
        entries = list(root.iterdir())
    except OSError:
        result["notes"].append("project directory is unreadable")
        return result
    if not entries:
        result["notes"].append("project directory is empty")
        return result

    deploy = root / "deploy.sh"
    deploy_text = ""
    if deploy.is_file():
        result["deploy_sh"] = True
        try:
            deploy_text = deploy.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            result["notes"].append("deploy.sh is unreadable")
    packages = _package_files(root)
    frontend = (root / "frontend" / "package.json").is_file()
    backend = (root / "backend" / "package.json").is_file()
    if frontend and backend:
        result["layout"] = "split"
    elif len(packages) == 1:
        result["layout"] = "single-package"
    elif len(packages) > 1:
        result["layout"] = "multi-package"
    else:
        result["layout"] = "directory"

    loaded = _load_packages(root, packages, result["notes"])
    install = _shell_command(deploy_text, _INSTALL_CMD)
    build = _shell_command(deploy_text, _BUILD_CMD)
    start = _shell_command(deploy_text, _START_CMD)
    if install is None:
        install, notes = _pick_script(loaded, "install")
        result["notes"].extend(notes)
    if build is None:
        build, notes = _pick_script(loaded, "build")
        result["notes"].extend(notes)
    if start is None:
        start, notes = _pick_script(loaded, "start")
        result["notes"].extend(notes)
    result["install"] = install
    result["build"] = build
    result["start"] = start

    port_texts = [deploy_text]
    if isinstance(start, str):
        port_texts.append(start)
    for _rel, commands, _text in loaded:
        if commands.get("start"):
            port_texts.append(commands["start"])
    result["port"] = _find_port(port_texts)
    result["health"] = _find_health([deploy_text, *[text for _rel, _commands, text in loaded]])
    return result


def _load_requirements(requirements_path) -> tuple[dict[str, Any], list[str]]:
    path = Path(requirements_path)
    notes: list[str] = []
    if path.is_dir():
        candidate = path / "requirements.yaml"
        if not candidate.is_file():
            return {}, ["requirements file is missing"]
        path = candidate
    if not path.is_file():
        raise FileNotFoundError(f"Requirements source not found: {requirements_path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}, ["requirements file is unreadable"]
    if not text.strip():
        return {}, notes
    try:
        if path.suffix.lower() == ".json":
            loaded = json.loads(text)
        else:
            loaded = yaml.safe_load(text)
    except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError):
        return {}, ["requirements file is not valid YAML or JSON"]
    if not isinstance(loaded, dict):
        return {}, ["requirements root is not a mapping"]
    return loaded, notes


def _task(tree: dict[str, Any]) -> dict[str, str]:
    raw_title = tree.get("title") or tree.get("name") or tree.get("id") or "task"
    title = str(raw_title)
    slug = tree.get("slug")
    if isinstance(slug, str) and slug.strip():
        slug = slug.strip()
    else:
        slug = _slug(title, str(tree.get("id") or "task"))
    return {"slug": slug, "title": title}


def _slug(text: str, fallback: str = "item") -> str:
    def norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:48].strip("-")

    return norm(text) or norm(fallback) or "item"


def _entities(tree: dict[str, Any]) -> list[dict[str, Any]]:
    items = tree.get("data")
    if not isinstance(items, list):
        return []
    entities = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        entity_id = _entity_id(item, index)
        name = item.get("entity") or item.get("name") or item.get("title") or entity_id
        entities.append({
            "id": entity_id,
            "name": str(name),
            "fields": _fields(item.get("properties")),
            "lifecycle": _lifecycle(item.get("lifecycle")),
            "seed_records": _seed_records(item),
            "source": "parsed",
        })
    return entities


def _entity_id(item: dict[str, Any], index: int) -> str:
    raw = item.get("id")
    if isinstance(raw, (str, int)) and not isinstance(raw, bool) and str(raw).strip():
        return str(raw).strip()
    label = item.get("entity") or item.get("name") or item.get("title") or ""
    return _slug(str(label), f"entity-{index + 1}")


def _fields(properties: Any) -> list[str]:
    if isinstance(properties, dict):
        return sorted(str(key) for key in properties)
    if isinstance(properties, list):
        names = []
        for prop in properties:
            if isinstance(prop, str) and prop.strip():
                names.append(prop.strip())
            elif isinstance(prop, dict):
                label = prop.get("name") or prop.get("id")
                if isinstance(label, str) and label.strip():
                    names.append(label.strip())
        return names
    return []


def _lifecycle(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip().casefold()
    if token == "seed":
        return "SEED"
    if token == "runtime":
        return "RUNTIME"
    return None


def _seed_records(item: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("records", "seed_records", "seeds", "seed"):
        if key not in item:
            continue
        raw = item[key]
        if isinstance(raw, list):
            return [_jsonish(rec) for rec in raw if isinstance(rec, dict)]
        if isinstance(raw, dict):
            inner = raw.get("records")
            if isinstance(inner, list):
                return [_jsonish(rec) for rec in inner if isinstance(rec, dict)]
            return [_jsonish(raw)]
        return []
    return []


def _pages(tree: dict[str, Any]) -> list[dict[str, Any]]:
    raw = None
    for key in ("pages", "references", "images"):
        candidate = tree.get(key)
        if isinstance(candidate, list):
            raw = candidate
            break
    if raw is None:
        return []
    pages = []
    for item in raw:
        page = _page(item)
        if page is not None:
            pages.append(page)
    return pages


def _page(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str) and item.strip():
        reference = item.strip() if _looks_like_reference(item) else None
        name = Path(item.strip()).stem if reference else item.strip()
        return {"name": name, "path": None, "reference": reference}
    if not isinstance(item, dict):
        return None
    reference = _optional_str(item.get("reference") or item.get("image") or item.get("src") or item.get("file"))
    path = _optional_str(item.get("path") or item.get("route") or item.get("url"))
    name = _optional_str(item.get("name") or item.get("title") or item.get("page"))
    if name is None and reference:
        name = Path(reference).stem or None
    if not name:
        return None
    return {"name": name, "path": path, "reference": reference}


def _looks_like_reference(value: str) -> bool:
    text = value.strip()
    return "/" in text or "\\" in text or Path(text).suffix.casefold() in _IMAGE_SUFFIXES


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _walk(tree: dict[str, Any], entities: list[dict[str, Any]]):
    work_items: list[dict[str, Any]] = []
    scenarios: list[dict[str, Any]] = []
    blobs: list[tuple[str, str, str | None]] = []
    seen: set[str] = set()

    def visit(node: Any, parent: str | None) -> None:
        if not isinstance(node, dict):
            return
        if parent is None and not _looks_like_work_item(node):
            return
        item_id = _work_id(node, seen, len(work_items))
        seen.add(item_id)
        name = node.get("name") or node.get("title") or item_id
        text = node.get("description")
        if isinstance(text, dict):
            text = text.get("text")
        if not isinstance(text, str):
            text = node.get("text") if isinstance(node.get("text"), str) else ""
        text = text.strip()
        page = node.get("page") if isinstance(node.get("page"), str) and node.get("page").strip() else None
        if isinstance(page, str):
            page = page.strip()
        work_items.append({
            "id": item_id,
            "name": str(name),
            "parent": parent,
            "depends_on": _depends(node),
            "text": text,
        })
        if text:
            blobs.append((item_id, text, page))
        raw_scenarios = node.get("scenarios")
        if isinstance(raw_scenarios, list):
            for index, raw in enumerate(raw_scenarios):
                scenario, texts = _scenario(item_id, raw, index, entities)
                if scenario is None:
                    continue
                scenarios.append(scenario)
                for piece in texts:
                    if piece:
                        blobs.append((item_id, piece, page))
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                visit(child, item_id)

    visit(tree, None)
    return work_items, scenarios, blobs


def _looks_like_work_item(node: dict[str, Any]) -> bool:
    return any(key in node for key in (
        "id", "req_id", "name", "title", "children", "scenarios", "description", "text", "data",
    ))


def _work_id(node: dict[str, Any], seen: set[str], index: int) -> str:
    raw = node.get("id") if node.get("id") not in (None, "") else node.get("req_id")
    if isinstance(raw, (str, int)) and not isinstance(raw, bool) and str(raw).strip():
        item_id = str(raw).strip()
    else:
        item_id = _slug(str(node.get("name") or node.get("title") or ""), f"item-{index + 1}")
    if item_id in seen:
        item_id = f"{item_id}-{index + 1}"
    return item_id


def _depends(node: dict[str, Any]) -> list[str]:
    raw = node.get("dependencies")
    if raw is None:
        raw = node.get("depends_on")
    if not isinstance(raw, list):
        return []
    ids = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            ids.append(item.strip())
        elif isinstance(item, int) and not isinstance(item, bool):
            ids.append(str(item))
        elif isinstance(item, dict):
            child = item.get("id") or item.get("req_id")
            if isinstance(child, (str, int)) and not isinstance(child, bool) and str(child).strip():
                ids.append(str(child).strip())
    return ids


def _scenario(work_id: str, raw: Any, index: int, entities: list[dict[str, Any]]):
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        return {
            "id": f"{work_id}::scenario-{index + 1}",
            "work_item": work_id,
            "name": text[:80],
            "given": _given([text], entities),
            "when": [],
            "then": [],
            "source": "parsed",
        }, [text]
    if not isinstance(raw, dict):
        return None, []
    sid = raw.get("id")
    if isinstance(sid, (str, int)) and not isinstance(sid, bool) and str(sid).strip():
        scenario_id = str(sid).strip()
    else:
        scenario_id = f"{work_id}::scenario-{index + 1}"
    name = raw.get("name") if isinstance(raw.get("name"), str) and raw.get("name").strip() else None
    if name is None and isinstance(raw.get("title"), str) and raw.get("title").strip():
        name = raw.get("title").strip()
    givens: list[str] = []
    whens: list[dict[str, Any]] = []
    thens: list[dict[str, Any]] = []
    steps = raw.get("steps")
    if isinstance(steps, list):
        for step in steps:
            _consume_step(step, givens, whens, thens)
    elif isinstance(steps, str) and steps.strip():
        _consume_step(steps, givens, whens, thens)
    else:
        _consume_step(
            {key: raw[key] for key in raw if str(key).lower() in {"given", "when", "then"}},
            givens,
            whens,
            thens,
        )
    texts = [*givens, *[step["text"] for step in whens], *[step["text"] for step in thens]]
    return {
        "id": scenario_id,
        "work_item": work_id,
        "name": name or scenario_id,
        "given": _given(givens, entities),
        "when": whens,
        "then": thens,
        "source": "parsed",
    }, texts


def _consume_step(step: Any, givens: list[str], whens: list[dict[str, Any]], thens: list[dict[str, Any]]) -> None:
    if isinstance(step, str):
        text = step.strip()
        if not text:
            return
        match = _STEP_PREFIX.match(text)
        if match and match.group(2).strip():
            _put(match.group(1).lower(), match.group(2).strip(), givens, whens, thens)
        elif match:
            return
        else:
            whens.append(_when_step(text))
        return
    if not isinstance(step, dict):
        return
    placed = False
    for key, kind in (
        ("Given", "given"),
        ("given", "given"),
        ("When", "when"),
        ("when", "when"),
        ("Then", "then"),
        ("then", "then"),
    ):
        value = step.get(key)
        if isinstance(value, str) and value.strip():
            _put(kind, value.strip(), givens, whens, thens)
            placed = True
    if placed:
        return
    keyword = step.get("keyword")
    if not isinstance(keyword, str) or not keyword.strip():
        keyword = step.get("type")
    content = step.get("content")
    if not isinstance(content, str) and isinstance(step.get("text"), str):
        content = step.get("text")
    if isinstance(keyword, str) and keyword.strip().upper() in {"GIVEN", "WHEN", "THEN"} and isinstance(content, str) and content.strip():
        _put(keyword.strip().lower(), content.strip(), givens, whens, thens)
        return
    if isinstance(content, str) and content.strip():
        whens.append(_when_step(content.strip()))


def _put(kind: str, text: str, givens: list[str], whens: list[dict[str, Any]], thens: list[dict[str, Any]]) -> None:
    if kind == "given":
        givens.append(text)
    elif kind == "when":
        whens.append(_when_step(text))
    else:
        thens.append(_then_step(text))


def _when_step(text: str) -> dict[str, Any]:
    action = _one_match(text, _ACTION_PATTERNS) or "unknown"
    name, role = _first_target(text)
    return {
        "action": action,
        "target": {"role": role, "name": name},
        "value": _action_value(text, action),
        "text": text,
    }


def _then_step(text: str) -> dict[str, Any]:
    kind = _kind(text)
    name, role = _first_target(text)
    return {
        "kind": kind,
        "target": {"role": role, "name": name},
        "value": _then_value(text, kind),
        "text": text,
    }


def _one_match(text: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]) -> str | None:
    matched = [name for name, pattern in patterns if pattern.search(text)]
    if len(matched) == 1:
        return matched[0]
    return None


def _kind(text: str) -> str:
    matched = [name for name, pattern in _KIND_PATTERNS if pattern.search(text)]
    if "hidden" in matched:
        matched = [name for name in matched if name != "visible"]
    if len(matched) == 1:
        return matched[0]
    return "unknown"


def _annotations(text: str) -> list[tuple[str, str]]:
    found = []
    for match in _ANNOTATION.finditer(text):
        name = next((match.group(group) for group in ("dn", "sn", "cn", "jn") if match.group(group) is not None), "")
        role = next((match.group(group) for group in ("rd", "rs", "rb") if match.group(group) is not None), "")
        name = name.strip()
        role = role.strip().casefold()
        if name and role:
            found.append((name, role))
    return found


def _first_target(text: str) -> tuple[str | None, str | None]:
    found = _annotations(text)
    if not found:
        return None, None
    name, role = found[0]
    return name, role


def _loose_quotes(text: str) -> list[str]:
    masked = _ANNOTATION.sub(lambda match: " " * (match.end() - match.start()), text)
    values = []
    for match in _QUOTED.finditer(masked):
        raw = next((match.group(group) for group in ("dn", "sn", "cn", "jn") if match.group(group) is not None), "")
        raw = raw.strip()
        if raw:
            values.append(raw)
    return values


def _action_value(text: str, action: str) -> str | None:
    if action not in {"fill", "select"}:
        return None
    quotes = _loose_quotes(text)
    return quotes[-1] if quotes else None


def _then_value(text: str, kind: str) -> str | None:
    quotes = _loose_quotes(text)
    if kind == "url":
        for quote in quotes:
            if re.match(r"(?i)https?://|/", quote):
                return quote
        match = re.search(r"(?i)https?://\S+|(/[A-Za-z0-9_./~-]*)", text)
        if not match:
            return None
        return (match.group(1) or match.group(0)).rstrip(".,)")
    if kind == "count":
        match = re.search(r"\b(\d+)\b", text)
        return match.group(1) if match else None
    if kind == "text":
        return quotes[0] if quotes else None
    return None


def _given(parts: list[str], entities: list[dict[str, Any]]) -> dict[str, Any]:
    text = "\n".join(parts)
    session = _session(text)
    refs = _mentioned_records(text, entities)
    account = _account_ref(refs, entities) if session == "authenticated" else None
    return {"session": session, "account": account, "data": refs, "text": text}


def _session(text: str) -> str:
    if not text:
        return "unknown"
    lowered = text.casefold()
    anonymous = any(phrase.casefold() in lowered for phrase in _ANONYMOUS_PHRASES)
    masked = lowered
    for phrase in _ANONYMOUS_PHRASES:
        masked = masked.replace(phrase.casefold(), " ")
    authenticated = any(phrase.casefold() in masked for phrase in _AUTHENTICATED_PHRASES)
    if anonymous and authenticated:
        return "unknown"
    if anonymous:
        return "anonymous"
    if authenticated:
        return "authenticated"
    return "unknown"


def _mentioned_records(text: str, entities: list[dict[str, Any]]) -> list[str]:
    if not text:
        return []
    refs = []
    for entity in entities:
        records = entity.get("seed_records") or []
        for index, record in enumerate(records):
            if isinstance(record, dict) and _record_mentioned(text, record):
                refs.append(_record_ref(str(entity["id"]), record, index))
    return refs


def _record_ref(entity_id: str, record: dict[str, Any], index: int) -> str:
    rec_id = record.get("id")
    if isinstance(rec_id, (str, int)) and not isinstance(rec_id, bool) and str(rec_id).strip():
        return f"{entity_id}:{rec_id}"
    return f"{entity_id}#{index + 1}"


def _record_mentioned(text: str, record: dict[str, Any]) -> bool:
    rec_id = record.get("id")
    if isinstance(rec_id, (str, int)) and not isinstance(rec_id, bool):
        token = str(rec_id).strip()
        if len(token) >= 2 and _contains_token(text, token):
            return True
    for key, value in record.items():
        if key == "id" or not isinstance(value, str):
            continue
        token = value.strip()
        if len(token) >= 4 and _contains_token(text, token):
            return True
    return False


def _contains_token(text: str, token: str) -> bool:
    """Match an id or email without letting a sentence period swallow it.

    The character before the token may not continue an id or a dotted name.
    The character after it may be a period, so prose can end on the value.
    """
    if re.fullmatch(r"[A-Za-z0-9_@.+-]+", token):
        return re.search(
            r"(?<![A-Za-z0-9_@.+-])" + re.escape(token) + r"(?![A-Za-z0-9_@+-])",
            text,
        ) is not None
    return token in text


def _account_ref(refs: list[str], entities: list[dict[str, Any]]) -> str | None:
    if len(refs) == 1:
        return refs[0]
    if not refs:
        return None
    by_id = {str(entity["id"]): entity for entity in entities}
    preferred = []
    for ref in refs:
        entity_id = ref.split(":", 1)[0].split("#", 1)[0]
        entity = by_id.get(entity_id)
        if entity and _ACCOUNT_ENTITY.search(f"{entity.get('name', '')} {entity.get('id', '')}"):
            preferred.append(ref)
    if len(preferred) == 1:
        return preferred[0]
    return None


def _sentences(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    parts = re.split(r"\n+|(?<=[。！？])\s*|(?<=[.!?])\s+", stripped)
    return [part.strip() for part in parts if part.strip()]


def _collect_elements(blobs: list[tuple[str, str, str | None]]) -> list[dict[str, Any]]:
    elements = []
    seen: set[tuple[str, str, str, str]] = set()
    for blob_index, (work_item, text, page) in enumerate(blobs):
        for sentence_index, sentence in enumerate(_sentences(text)):
            found = _annotations(sentence)
            if not found:
                continue
            persistent = len(found) >= 2 and _PERSIST.search(sentence) is not None
            group = f"persistent:{work_item}:{blob_index}:{sentence_index}" if persistent else None
            multiple = _MULTIPLE.search(sentence) is not None
            for name, role in found:
                identity = (role, name, work_item, sentence)
                if identity in seen:
                    continue
                seen.add(identity)
                elements.append({
                    "role": role,
                    "name": name,
                    "page": page,
                    "work_item": work_item,
                    "group": group,
                    "persistent": persistent,
                    "multiple": multiple,
                    "source": "parsed",
                    "evidence": sentence,
                })
    return elements


def _auth(elements: list[dict[str, Any]]) -> dict[str, Any] | None:
    parsed = [element for element in elements if element.get("source") == "parsed"]
    links = [
        element for element in parsed
        if element["role"] == "link" and _ENTRY_NAME.fullmatch(element["name"].strip())
    ]
    buttons = [
        element for element in parsed
        if element["role"] == "button" and _SUBMIT_NAME.fullmatch(element["name"].strip())
    ]
    entry = links[0] if links else None
    if entry is None:
        entry_buttons = [
            element for element in parsed
            if element["role"] == "button" and _ENTRY_NAME.fullmatch(element["name"].strip())
        ]
        entry = entry_buttons[0] if entry_buttons else None
    submit = _distinct_submit(entry, buttons)
    if entry is None or submit is None:
        return None
    evidences = {entry["evidence"], submit["evidence"]}
    fields = []
    seen: set[str] = set()
    for element in parsed:
        if element["role"] != "textbox" or element["evidence"] not in evidences:
            continue
        label = element["name"].strip()
        if not label or label in seen:
            continue
        seen.add(label)
        fields.append({"label": label})
    if not fields:
        return None
    return {
        "login_entry": {"role": entry["role"], "name": entry["name"]},
        "fields": fields,
        "submit": {"role": submit["role"], "name": submit["name"]},
    }


def _distinct_submit(entry: dict[str, Any] | None, buttons: list[dict[str, Any]]) -> dict[str, Any] | None:
    if entry is None:
        return None
    for button in buttons:
        if button is entry:
            continue
        same = (
            button["role"] == entry["role"]
            and button["name"] == entry["name"]
            and button["evidence"] == entry["evidence"]
        )
        if not same:
            return button
    return None


def _fragments(work_items, scenarios, elements) -> list[dict[str, str]]:
    covered = {element["evidence"] for element in elements}
    fragments = []
    for item in work_items:
        for sentence in _sentences(item["text"]):
            if sentence in covered or _annotations(sentence):
                continue
            fragments.append({"work_item": item["id"], "where": "text", "text": sentence})
    for scenario in scenarios:
        given = scenario["given"]["text"]
        if given and scenario["given"]["session"] == "unknown" and not scenario["given"]["data"]:
            fragments.append({
                "work_item": scenario["work_item"],
                "scenario": scenario["id"],
                "where": "given",
                "text": given,
            })
        for step in scenario["when"]:
            if step["action"] == "unknown":
                fragments.append({
                    "work_item": scenario["work_item"],
                    "scenario": scenario["id"],
                    "where": "when",
                    "text": step["text"],
                })
        for step in scenario["then"]:
            if step["kind"] == "unknown":
                fragments.append({
                    "work_item": scenario["work_item"],
                    "scenario": scenario["id"],
                    "where": "then",
                    "text": step["text"],
                })
    return fragments


def _apply_model_fill(model_fill, fragments, elements, scenarios, entities, pages, work_ids) -> list[str]:
    if model_fill is None:
        return []
    if not callable(model_fill):
        return ["model_fill is not callable"]
    try:
        returned = model_fill({"fragments": fragments, "schema": SCHEMA_DESCRIPTION})
    except Exception as exc:
        return [f"model_fill raised {type(exc).__name__}"]
    if not isinstance(returned, dict):
        return [f"model_fill returned {type(returned).__name__}"]
    rejected = []
    for key in returned:
        if key not in {"ui_elements", "scenarios", "entities", "pages"}:
            rejected.append(f"ignored {key}")
    _merge_elements(returned.get("ui_elements"), elements, work_ids, rejected)
    _merge_scenarios(returned.get("scenarios"), scenarios, work_ids, rejected)
    _merge_entities(returned.get("entities"), entities, rejected)
    _merge_pages(returned.get("pages"), pages, rejected)
    return rejected


def _merge_elements(items, elements, work_ids, rejected) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        rejected.append("ui_elements is not a list")
        return
    existing = {(element["role"], element["name"], element["work_item"], element["evidence"]) for element in elements}
    for index, item in enumerate(items):
        element, reason = _valid_element(item, work_ids)
        if element is None:
            rejected.append(f"ui_elements[{index}] {reason}")
            continue
        identity = (element["role"], element["name"], element["work_item"], element["evidence"])
        if identity in existing:
            rejected.append(f"ui_elements[{index}] duplicates a parsed item")
            continue
        existing.add(identity)
        elements.append(element)


def _valid_element(item: Any, work_ids: set[str]):
    if not isinstance(item, dict):
        return None, "is not an object"
    role = item.get("role")
    name = item.get("name")
    if not isinstance(role, str) or not _ROLE_TOKEN.fullmatch(role.strip().casefold()):
        return None, "invalid role"
    if not isinstance(name, str) or not name.strip():
        return None, "invalid name"
    work_item = item.get("work_item", None)
    if work_item is not None:
        if not isinstance(work_item, str) or work_item not in work_ids:
            return None, "unknown work_item"
    for key in ("persistent", "multiple"):
        if key in item and not isinstance(item[key], bool):
            return None, f"invalid {key}"
    for key in ("page", "group", "evidence"):
        if key in item and item[key] is not None and not isinstance(item[key], str):
            return None, f"invalid {key}"
    page = item.get("page")
    group = item.get("group")
    evidence = item.get("evidence", "")
    return {
        "role": role.strip().casefold(),
        "name": name.strip(),
        "page": page.strip() if isinstance(page, str) and page.strip() else None,
        "work_item": work_item,
        "group": group.strip() if isinstance(group, str) and group.strip() else None,
        "persistent": item.get("persistent", False) is True,
        "multiple": item.get("multiple", False) is True,
        "source": "inferred",
        "evidence": evidence if isinstance(evidence, str) else "",
    }, None


def _merge_scenarios(items, scenarios, work_ids, rejected) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        rejected.append("scenarios is not a list")
        return
    used = {scenario["id"] for scenario in scenarios}
    for index, item in enumerate(items):
        scenario, reason = _valid_scenario(item, work_ids, used)
        if scenario is None:
            rejected.append(f"scenarios[{index}] {reason}")
            continue
        used.add(scenario["id"])
        scenarios.append(scenario)


def _valid_scenario(item: Any, work_ids: set[str], used_ids: set[str]):
    if not isinstance(item, dict):
        return None, "is not an object"
    sid = item.get("id")
    work_item = item.get("work_item")
    name = item.get("name")
    if not isinstance(sid, str) or not sid.strip():
        return None, "invalid id"
    if sid in used_ids:
        return None, "duplicate id"
    if not isinstance(work_item, str) or work_item not in work_ids:
        return None, "unknown work_item"
    if not isinstance(name, str) or not name.strip():
        return None, "invalid name"
    given = item.get("given")
    if not isinstance(given, dict):
        return None, "invalid given"
    session = given.get("session")
    if session not in _SESSIONS:
        return None, "invalid session"
    account = given.get("account", None)
    if account is not None and not isinstance(account, str):
        return None, "invalid account"
    data = given.get("data", [])
    if not isinstance(data, list) or not all(isinstance(part, str) for part in data):
        return None, "invalid data"
    text = given.get("text", "")
    if not isinstance(text, str):
        return None, "invalid given text"
    when = _valid_steps(item.get("when"), "action", _ACTIONS)
    if when is None:
        return None, "invalid when"
    then = _valid_steps(item.get("then"), "kind", _KINDS)
    if then is None:
        return None, "invalid then"
    return {
        "id": sid.strip(),
        "work_item": work_item,
        "name": name.strip(),
        "given": {
            "session": session,
            "account": account,
            "data": list(data),
            "text": text,
        },
        "when": when,
        "then": then,
        "source": "inferred",
    }, None


def _valid_steps(steps: Any, field: str, allowed: set[str]):
    if not isinstance(steps, list):
        return None
    parsed = []
    for step in steps:
        if not isinstance(step, dict) or step.get(field) not in allowed:
            return None
        target = _target(step.get("target"))
        if target is None:
            return None
        value = step.get("value", None)
        if value is not None and not isinstance(value, str):
            return None
        text = step.get("text", "")
        if not isinstance(text, str):
            return None
        parsed.append({field: step[field], "target": target, "value": value, "text": text})
    return parsed


def _target(value: Any):
    if value is None:
        return {"role": None, "name": None}
    if not isinstance(value, dict):
        return None
    role = value.get("role")
    name = value.get("name")
    if role is not None and not isinstance(role, str):
        return None
    if name is not None and not isinstance(name, str):
        return None
    return {
        "role": role.strip().casefold() if isinstance(role, str) and role.strip() else None,
        "name": name.strip() if isinstance(name, str) and name.strip() else None,
    }


def _merge_entities(items, entities, rejected) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        rejected.append("entities is not a list")
        return
    used = {entity["id"] for entity in entities}
    for index, item in enumerate(items):
        entity, reason = _valid_entity(item, used)
        if entity is None:
            rejected.append(f"entities[{index}] {reason}")
            continue
        used.add(entity["id"])
        entities.append(entity)


def _valid_entity(item: Any, used_ids: set[str]):
    if not isinstance(item, dict):
        return None, "is not an object"
    entity_id = item.get("id")
    name = item.get("name")
    if not isinstance(entity_id, str) or not entity_id.strip():
        return None, "invalid id"
    if entity_id in used_ids:
        return None, "duplicate id"
    if not isinstance(name, str) or not name.strip():
        return None, "invalid name"
    fields = item.get("fields", [])
    if not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
        return None, "invalid fields"
    lifecycle = item.get("lifecycle", None)
    if lifecycle is not None:
        if not isinstance(lifecycle, str):
            return None, "invalid lifecycle"
        lifecycle = lifecycle.strip().upper()
        if lifecycle not in {"SEED", "RUNTIME"}:
            return None, "invalid lifecycle"
    records = item.get("seed_records", [])
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        return None, "invalid seed_records"
    return {
        "id": entity_id.strip(),
        "name": name.strip(),
        "fields": list(fields),
        "lifecycle": lifecycle,
        "seed_records": [_jsonish(record) for record in records],
        "source": "inferred",
    }, None


def _merge_pages(items, pages, rejected) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        rejected.append("pages is not a list")
        return
    used = {page["name"] for page in pages}
    for index, item in enumerate(items):
        page, reason = _valid_page(item, used)
        if page is None:
            rejected.append(f"pages[{index}] {reason}")
            continue
        used.add(page["name"])
        pages.append(page)


def _valid_page(item: Any, used_names: set[str]):
    if not isinstance(item, dict):
        return None, "is not an object"
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, "invalid name"
    if name.strip() in used_names:
        return None, "duplicate name"
    path = item.get("path", None)
    reference = item.get("reference", None)
    if path is not None and not isinstance(path, str):
        return None, "invalid path"
    if reference is not None and not isinstance(reference, str):
        return None, "invalid reference"
    return {
        "name": name.strip(),
        "path": path.strip() if isinstance(path, str) and path.strip() else None,
        "reference": reference.strip() if isinstance(reference, str) and reference.strip() else None,
    }, None


def _jsonish(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_jsonish(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonish(item) for key, item in value.items()}
    return str(value)


def _package_files(root: Path) -> list[Path]:
    files = []
    root_package = root / "package.json"
    if root_package.is_file():
        files.append(root_package)
    try:
        children = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name)
    except OSError:
        children = []
    for child in children:
        if child.name.startswith(".") or child.name == "node_modules":
            continue
        package = child / "package.json"
        if package.is_file():
            files.append(package)
    return files


def _load_packages(root: Path, paths: list[Path], notes: list[str]):
    loaded = []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            notes.append(f"unreadable {rel}")
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            notes.append(f"invalid JSON: {rel}")
            continue
        if not isinstance(data, dict):
            notes.append(f"invalid JSON: {rel}")
            continue
        scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
        commands = {}
        for key in ("install", "build", "start"):
            value = scripts.get(key)
            if isinstance(value, str) and value.strip():
                commands[key] = value.strip()
        loaded.append((rel, commands, text))
    return loaded


def _pick_script(loaded, key: str):
    found = [(rel, commands[key]) for rel, commands, _text in loaded if key in commands]
    if not found:
        return None, []
    _rel, command = found[0]
    notes = [f"also found {key} in {other}" for other, _command in found[1:]]
    return command, notes


def _shell_command(text: str, pattern: re.Pattern[str]) -> str | None:
    if not text:
        return None
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1).strip()


def _find_port(texts: list[str]) -> int | None:
    for text in texts:
        if not text:
            continue
        for pattern in _PORT_PATTERNS:
            for match in pattern.finditer(text):
                port = int(match.group(1))
                if 1 <= port <= 65535:
                    return port
    return None


def _find_health(texts: list[str]) -> str | None:
    for text in texts:
        if not text:
            continue
        match = _HEALTH_PATH.search(text)
        if match:
            return match.group(1)
    return None

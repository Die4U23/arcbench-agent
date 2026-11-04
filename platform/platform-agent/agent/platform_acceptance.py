"""Compile parsed task IR into Playwright checks and freeze authored specs.

compile_checks is deterministic and never calls a model. author_tests asks a
caller-supplied callable for one spec and records hashes. triage folds runtime
signals into a compact repair payload.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_RELIABLE_ACTIONS = {"open", "click", "fill", "check", "submit", "reload"}
_SPEC_SUFFIXES = (".spec.ts", ".spec.js", ".spec.mjs", ".spec.cjs", ".spec.tsx", ".spec.jsx")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_QUOTED = re.compile(
    r'"(?P<dn>(?:\\.|[^"\\])*)"'
    r"|'(?P<sn>(?:\\.|[^'\\])*)'"
    r"|“(?P<cn>[^”]*)”"
    r"|「(?P<jn>[^」]*)」"
)
_AUTHOR_INSTRUCTION = (
    "Write one Playwright test per scenario. "
    "Use only role, name, and label locators.\n"
)
_CONFIDENCE = {"input": 4, "compiled": 3, "probes": 2, "authored": 1}
_KIND = {
    "input": "application_defect",
    "compiled": "application_defect",
    "probes": "interface_defect",
    "authored": "needs_judgment",
}
_SOURCE_ORDER = ("input", "compiled", "probes", "authored")
_ABSENCE = re.compile(
    r"(?i)\b(?:does not show|do not show|is not shown|not shown|is not visible|not visible|does not display)\b"
    r"|不显示|不会显示|不可见"
)
_LABEL_VALUE = re.compile(r"\b[A-Z][A-Za-z]{2,}\s+\S*(?:[\d@]|-)+\S*")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\b")


def compile_checks(ir: dict, out_dir) -> dict:
    """Write Playwright specs for parsed elements and scenarios.

    Returns {"generated": [relative paths], "skipped": [{"scenario", "reason"}]}.
    Inferred records are ignored. An authenticated scenario is skipped unless
    auth is present and given.account names an existing seed record.
    """
    document = ir if isinstance(ir, dict) else {}
    elements = [
        element for element in _items(document.get("ui_elements"))
        if isinstance(element, dict) and element.get("source") == "parsed"
    ]
    scenarios = [
        scenario for scenario in _items(document.get("scenarios"))
        if isinstance(scenario, dict) and scenario.get("source") == "parsed"
    ]
    if not elements and not scenarios:
        return {"generated": [], "skipped": []}
    root = Path(out_dir)
    if not scenarios:
        return _write_element_specs(root, elements)

    allow_assertions = any(_usable(element) for element in elements)
    skipped = []
    pending = []
    for scenario in scenarios:
        session, account = _session_account(scenario)
        if session == "authenticated":
            reason = _skip_reason(document, account)
            if reason:
                skipped.append({"scenario": _scenario_label(scenario), "reason": reason})
                continue
        body = _scenario_body(document, scenario, elements, session, allow_assertions)
        if _meaningful(body):
            pending.append((scenario, body))
    if not pending:
        return {"generated": [], "skipped": skipped}

    root.mkdir(parents=True, exist_ok=True)
    generated = []
    used: set[str] = set()
    for scenario, body in pending:
        filename = _scenario_filename(scenario.get("work_item") or "item", scenario.get("id") or "scenario", used)
        (root / filename).write_text(_spec_text(_title(scenario), body), encoding="utf-8", newline="\n")
        generated.append(filename)
    return {"generated": generated, "skipped": skipped}


def author_tests(ir: dict, out_dir, model) -> dict:
    """Ask model(prompt) for a Playwright spec and freeze its hash.

    The prompt is the instruction plus the IR JSON. It does not read application
    source files. model is not called by this module except as the given callable.
    """
    prompt = _AUTHOR_INSTRUCTION + json.dumps(ir, ensure_ascii=False, indent=2, sort_keys=True)
    source = model(prompt)
    if not isinstance(source, str):
        source = "" if source is None else str(source)
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "authored.spec.ts").write_bytes(source.encode("utf-8"))
    _write_manifest(root)
    return {"generated": ["authored.spec.ts"], "manifest": "manifest.json"}


def verify_frozen(out_dir) -> bool:
    """Return False when a spec byte, spec set, or manifest hash does not match."""
    root = Path(out_dir)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    recorded = manifest.get("files")
    if not isinstance(recorded, dict):
        return False
    current = {name: digest for name, digest in _spec_digests(root).items()}
    if set(current) != set(recorded):
        return False
    return all(isinstance(recorded.get(name), str) and recorded[name] == digest for name, digest in current.items())


def triage(signals) -> dict:
    """Classify runtime signals for a repair prompt.

    Confidence is input, then compiled, then probes, then authored. A failure
    is kept on the highest source that contains its name. Compiled failures are
    application defects. Probe-only failures are interface defects. Authored-only
    failures need judgment and must not be treated as an application change.
    Missing keys are empty.
    """
    document = signals if isinstance(signals, dict) else {}
    buckets = {key: _signal_failures(document.get(key)) for key in _SOURCE_ORDER}
    owners: dict[str, str] = {}
    for key in _SOURCE_ORDER:
        for failure in buckets[key]:
            owners.setdefault(_failure_id(failure), key)
    groups = []
    for key in _SOURCE_ORDER:
        seen: set[str] = set()
        compact = []
        for failure in buckets[key]:
            identity = _failure_id(failure)
            if owners.get(identity) != key or identity in seen:
                continue
            seen.add(identity)
            compact.append(_compact_failure(failure))
        if not compact:
            continue
        group = {
            "source": key,
            "confidence": _CONFIDENCE[key],
            "kind": _KIND[key],
            "failures": compact,
        }
        if key == "authored":
            group["change_app"] = False
            group["note"] = "needs judgment; the test may be wrong"
        groups.append(group)
    return {"confidence_order": list(_SOURCE_ORDER), "groups": groups}


def _write_element_specs(root: Path, elements: list[dict]) -> dict:
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for element in elements:
        if not _usable(element):
            continue
        key = str(element.get("work_item") or "elements")
        if key not in grouped:
            order.append(key)
            grouped[key] = []
        grouped[key].append(element)
    pending = []
    for key in order:
        chosen = _dedupe(grouped[key])
        body = [_goto(), *_assert_elements(chosen)]
        if _meaningful(body):
            pending.append((key, body))
    if not pending:
        return {"generated": [], "skipped": []}
    root.mkdir(parents=True, exist_ok=True)
    generated = []
    used: set[str] = set()
    for key, body in pending:
        filename = _unique_name(f"{key}.elements.spec.ts", used)
        (root / filename).write_text(_spec_text(f"{key} elements", body), encoding="utf-8", newline="\n")
        generated.append(filename)
    return {"generated": generated, "skipped": []}


def _scenario_body(ir: dict, scenario: dict, elements: list[dict], session: str, allow_assertions: bool) -> list[str]:
    body = [_goto()]
    if session == "authenticated":
        body.extend(_sign_in_lines(ir, elements, _session_account(scenario)[1]))
    if allow_assertions:
        body.extend(_assert_elements(_start_elements(scenario, elements)))
    stopped = False
    fills = False
    for step in _items(scenario.get("when")):
        if not _reliable(step) or _unparsed_click(step, fills):
            stopped = True
            break
        body.extend(_action_lines(step, elements))
        if step.get("action") == "fill":
            fills = True
    if allow_assertions and not stopped:
        for step in _items(scenario.get("then")):
            body.extend(_then_lines(step))
    return body


def _start_elements(scenario: dict, elements: list[dict]) -> list[dict]:
    targets, texts = _step_mentions(scenario)
    chosen = []
    for element in elements:
        if not _usable(element):
            continue
        role = element["role"].strip()
        name = element["name"].strip()
        evidence = element.get("evidence")
        evidence = evidence.strip() if isinstance(evidence, str) else ""
        grouped = _group(element) is not None
        persistent = element.get("persistent") is True
        same_item = element.get("work_item") == scenario.get("work_item")
        from_step_text = bool(evidence) and evidence in texts
        from_step_target = (role, name) in targets and same_item and not grouped and not persistent
        if from_step_text or from_step_target:
            continue
        if same_item or persistent or grouped:
            chosen.append(element)
    return _dedupe(chosen)


def _step_mentions(scenario: dict) -> tuple[set[tuple[str, str]], set[str]]:
    targets: set[tuple[str, str]] = set()
    texts: set[str] = set()
    for step in [*_items(scenario.get("when")), *_items(scenario.get("then"))]:
        if not isinstance(step, dict):
            continue
        text = step.get("text")
        if isinstance(text, str) and text.strip():
            texts.add(text.strip())
        role, name = _role_name(step.get("target"))
        if role and name:
            targets.add((role, name))
    return targets, texts


def _assert_elements(elements: list[dict]) -> list[str]:
    lines = []
    if any(_group(element) for element in elements):
        lines.append("  const navigation = page.getByRole('navigation');")
    for element in elements:
        scope = "navigation" if _group(element) else "page"
        multiple = element.get("multiple") is True
        locator = _locator(scope, element["role"].strip(), element["name"].strip(), first=multiple)
        lines.append(f"  await expect({locator}).toBeVisible();")
        if not multiple:
            lines.append(f"  await expect({locator}).toHaveCount(1);")
    return lines


def _action_lines(step: dict, elements: list[dict]) -> list[str]:
    action = step.get("action")
    role, name = _role_name(step.get("target"))
    if not role or not name:
        return []
    first = any(
        _usable(element)
        and element.get("multiple") is True
        and element["role"].strip() == role
        and element["name"].strip() == name
        for element in elements
    )
    locator = _locator("page", role, name, first=first)
    if action in {"open", "click", "submit"}:
        return [f"  await {locator}.click();"]
    if action == "fill":
        return [f"  await {locator}.fill({_js(step.get('value'))});"]
    if action == "check":
        return [f"  await {locator}.check();"]
    if action == "reload":
        return [f"  await {locator}.waitFor();", "  await page.reload();"]
    return []


def _unparsed_click(step: dict, fills: bool) -> bool:
    """A click that still names unlabeled values is not the whole step.

    Basis: one When sentence that pairs field values with a control is a single
    action. Emitting only the control would check a different action.
    """
    if fills or not isinstance(step, dict) or step.get("action") not in {"click", "submit"}:
        return False
    text = step.get("text")
    if not isinstance(text, str):
        return False
    stripped = _QUOTED.sub(" ", text)
    return bool(_EMAIL.search(stripped) or _LABEL_VALUE.search(stripped))


def _then_lines(step: dict) -> list[str]:
    if not isinstance(step, dict) or not isinstance(step.get("text"), str):
        return []
    text = step["text"]
    kind = step.get("kind") if isinstance(step.get("kind"), str) else ""
    lines = []
    seen: set[str] = set()
    for name in _quoted(text):
        if name in seen:
            continue
        seen.add(name)
        locator = f"page.getByText({_js(name)}, {{ exact: true }})"
        if kind == "hidden" or _quote_absent(text, name):
            lines.append(f"  await expect({locator}).toHaveCount(0);")
        else:
            lines.append(f"  await expect({locator}).toBeVisible();")
    return lines


def _quote_absent(text: str, quote: str) -> bool:
    for clause in re.split(r"[,;]|\band\b", text):
        if quote not in clause:
            continue
        outside = clause
        for wrapper in (f'"{quote}"', f"'{quote}'", f"“{quote}”", f"「{quote}」"):
            outside = outside.replace(wrapper, " ")
        if _ABSENCE.search(outside):
            return True
    return False


def _sign_in_lines(ir: dict, elements: list[dict], account: str | None) -> list[str]:
    auth = ir.get("auth") if isinstance(ir.get("auth"), dict) else {}
    record = _seed_record(ir, account)
    if not isinstance(record, dict):
        return []
    lines = ["  // session sign-in"]
    entry_role, entry_name = _role_name(auth.get("login_entry"))
    if entry_role and entry_name:
        lines.append(f"  await {_locator('page', entry_role, entry_name)}.click();")
    for field in _items(auth.get("fields")):
        if not isinstance(field, dict) or not isinstance(field.get("label"), str) or not field["label"].strip():
            continue
        value = _field_value(record, field["label"])
        if value is None:
            continue
        label = field["label"].strip()
        lines.append(f"  await {_locator('page', _role_for_label(elements, label), label)}.fill({_js(value)});")
    submit_role, submit_name = _role_name(auth.get("submit"))
    if submit_role and submit_name:
        lines.append(f"  await {_locator('page', submit_role, submit_name)}.click();")
    return lines


def _skip_reason(ir: dict, account: str | None) -> str | None:
    auth = ir.get("auth")
    if not isinstance(auth, dict) or not auth:
        return "authenticated scenario skipped: auth is missing"
    if account is None:
        return "authenticated scenario skipped: account is missing"
    if _seed_record(ir, account) is None:
        return "authenticated scenario skipped: seed record not found"
    return None


def _seed_record(ir: dict, account: str | None) -> dict | None:
    if not isinstance(account, str) or not account.strip():
        return None
    account = account.strip()
    entities = [entity for entity in _items(ir.get("entities")) if isinstance(entity, dict)]

    def entity_by_id(entity_id: str) -> dict | None:
        return next((entity for entity in entities if str(entity.get("id")) == entity_id), None)

    def records(entity: dict | None) -> list[dict]:
        if not isinstance(entity, dict):
            return []
        return [record for record in _items(entity.get("seed_records")) if isinstance(record, dict)]

    if ":" in account:
        entity_id, record_id = account.split(":", 1)
        for record in records(entity_by_id(entity_id)):
            if str(record.get("id")) == record_id:
                return record
        return None
    if "#" in account:
        entity_id, index_text = account.split("#", 1)
        if not index_text.isdigit():
            return None
        found = records(entity_by_id(entity_id))
        index = int(index_text)
        if 1 <= index <= len(found):
            return found[index - 1]
    return None


def _field_value(record: dict, label: str) -> str | None:
    want = _norm(label)
    if not want:
        return None
    for key, value in record.items():
        if str(key) == "id" or value is None or isinstance(value, bool):
            continue
        if isinstance(value, (str, int, float)) and _norm(str(key)) == want:
            return str(value)
    return None


def _role_for_label(elements: list[dict], label: str) -> str:
    for element in elements:
        if _usable(element) and element["name"].strip() == label:
            return element["role"].strip()
    return "textbox"


def _reliable(step: dict) -> bool:
    if not isinstance(step, dict) or step.get("action") not in _RELIABLE_ACTIONS:
        return False
    role, name = _role_name(step.get("target"))
    if not role or not name:
        return False
    if step.get("action") == "fill" and not isinstance(step.get("value"), str):
        return False
    if step.get("action") == "fill" and step.get("value") == "":
        return False
    return True


def _session_account(scenario: dict) -> tuple[str, str | None]:
    given = scenario.get("given") if isinstance(scenario.get("given"), dict) else {}
    session = given.get("session")
    if session not in {"anonymous", "authenticated", "unknown"}:
        session = "unknown"
    account = given.get("account")
    if not isinstance(account, str) or not account.strip():
        return session, None
    return session, account.strip()


def _usable(element: dict) -> bool:
    return (
        isinstance(element, dict)
        and element.get("source") == "parsed"
        and isinstance(element.get("role"), str)
        and bool(element["role"].strip())
        and isinstance(element.get("name"), str)
        and bool(element["name"].strip())
    )


def _group(element: dict) -> str | None:
    group = element.get("group")
    if isinstance(group, str) and group.strip():
        return group.strip()
    return None


def _dedupe(elements: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    chosen = []
    for element in elements:
        if not _usable(element):
            continue
        identity = (element["role"].strip(), element["name"].strip(), _group(element) or "", element.get("work_item"))
        if identity in seen:
            continue
        seen.add(identity)
        chosen.append(element)
    return chosen


def _quoted(text: str) -> list[str]:
    found = []
    for match in _QUOTED.finditer(text):
        raw = next((match.group(group) for group in ("dn", "sn", "cn", "jn") if match.group(group) is not None), "")
        raw = raw.strip()
        if raw:
            found.append(raw)
    return found


def _goto() -> str:
    return f"  await page.goto(process.env.ACCEPTANCE_BASE_URL || {_js('http://127.0.0.1:3000/')});"


def _locator(scope: str, role: str, name: str, *, first: bool = False) -> str:
    # multiple elements are not unique, so visibility uses the first match.
    locator = f"{scope}.getByRole({_js(role)}, {{ name: {_js(name)}, exact: true }})"
    if first:
        return locator + ".first()"
    return locator


def _spec_text(title: str, body: list[str]) -> str:
    lines = [
        "import { test, expect } from '@playwright/test';",
        "",
        f"test({_js(title)}, async ({{ page }}) => {{",
        *body,
        "});",
        "",
    ]
    return "\n".join(lines)


def _title(scenario: dict) -> str:
    parts = [scenario.get("work_item"), scenario.get("id"), scenario.get("name")]
    return " ".join(str(part).strip() for part in parts if isinstance(part, str) and part.strip())


def _scenario_label(scenario: dict) -> str:
    scenario_id = scenario.get("id")
    if isinstance(scenario_id, str) and scenario_id.strip():
        return scenario_id.strip()
    return "" if scenario_id is None else str(scenario_id)


def _meaningful(lines: list[str]) -> bool:
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("const navigation"):
            continue
        if "page.goto(" in stripped:
            continue
        return True
    return False


def _scenario_filename(work_item, scenario_id, used: set[str]) -> str:
    return _unique_name(f"{work_item}__{scenario_id}.spec.ts", used)


def _unique_name(raw: str, used: set[str]) -> str:
    safe = _UNSAFE_NAME.sub("_", str(raw)).strip("._") or "scenario.spec.ts"
    if not safe.endswith(".spec.ts"):
        safe += ".spec.ts"
    candidate = safe
    index = 2
    while candidate in used:
        candidate = f"{safe[:-len('.spec.ts')]}-{index}.spec.ts"
        index += 1
    used.add(candidate)
    return candidate


def _write_manifest(root: Path) -> None:
    payload = {"algorithm": "sha256", "files": _spec_digests(root)}
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    (root / "manifest.json").write_text(text, encoding="utf-8", newline="\n")


def _spec_digests(root: Path) -> dict[str, str]:
    digests = {}
    for path in _spec_paths(root):
        digests[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def _spec_paths(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    paths = [
        path for path in root.rglob("*")
        if path.is_file() and path.name.endswith(_SPEC_SUFFIXES) and "node_modules" not in path.parts
    ]
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def _signal_failures(block) -> list:
    if not isinstance(block, dict):
        return []
    failures = block.get("failures")
    if not isinstance(failures, list):
        return []
    return list(failures)


def _failure_id(failure) -> str:
    if isinstance(failure, dict):
        name = failure.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        error = failure.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip().splitlines()[0][:300]
    if isinstance(failure, str) and failure.strip():
        return failure.strip()
    return json.dumps(failure, ensure_ascii=False, sort_keys=True, default=str)


def _compact_failure(failure) -> dict:
    if isinstance(failure, str):
        return {"name": failure, "file": None, "error": failure[:300]}
    if not isinstance(failure, dict):
        text = str(failure)
        return {"name": text, "file": None, "error": text[:300]}
    error = failure.get("error")
    name = failure.get("name")
    file = failure.get("file")
    return {
        "name": name if isinstance(name, str) else None,
        "file": file if isinstance(file, str) else None,
        "error": error[:300] if isinstance(error, str) else "",
    }


def _items(value) -> list:
    return value if isinstance(value, list) else []


def _role_name(target) -> tuple[str | None, str | None]:
    if not isinstance(target, dict):
        return None, None
    role = target.get("role")
    name = target.get("name")
    if not isinstance(role, str) or not isinstance(name, str):
        return None, None
    role, name = role.strip(), name.strip()
    if not role or not name:
        return None, None
    return role, name


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _js(value) -> str:
    return json.dumps(value, ensure_ascii=False)

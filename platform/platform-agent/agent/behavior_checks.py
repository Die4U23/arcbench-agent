"""Bounded, requirement-bound browser diagnostics; never official acceptance.

The model returns data only. This compiler owns all executable code, and the
frozen suite lives outside the editable application throughout repair rounds.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAX_CASES = 12
MAX_STEPS = 24
MAX_TOTAL_STEPS = 192
AUTHOR_REQUESTS = 4
CASE_TIMEOUT_MS = 60_000

INSTRUCTIONS = '''Design a small set of executable browser journeys from PUBLIC REQUIREMENTS and current source.
You have read-only tools. Do not implement or edit application files. Do not read scoring tests or feedback.
Return exactly {"cases": [...]} with at most 12 cases, 24 steps per case, 192 total steps.
Each case has exactly: name (unique, <=120 chars), requirement_id (a supplied work item ID),
requirement_quote (8..500 exact characters copied from one quote_sources entry in that ID's
PUBLIC REQUIREMENT BINDINGS), steps (array). These bindings include the requirement description
and its explicitly parsed public scenarios; do not quote another requirement or inferred behavior.
Derive expected behavior from the quoted requirement, not from what the implementation currently does.
Use source only to locate controls and establish actual routes; prefer accessible names and labels.
Select distinct end-to-end transitions across shared modules, including state persistence when required.
Avoid redundant presence-only checks. Use unique synthetic input values compatible with public data shapes;
reuse an account only if explicitly provided by the public requirements. Each case has a new browser context
but shares the scratch application database: make fixtures independent and avoid destructive seed changes.
Every case starts with exactly one goto; goto is forbidden at all later positions.
Navigate thereafter through source-observed UI controls using click or other supported interactions.
Use reload to check persistence at the current URL. Each case must include an interaction followed by
a positive assertion of its result; a url step asserts the current location and does not navigate.
No assumptions about unspecified business behavior, no API shortcut for UI actions, no external URLs.
An unsupported flow should be omitted rather than inventing an oracle. These are fallible development
diagnostics, NOT formal acceptance or complete requirement coverage.

Step schemas (no extra fields):
{"op":"goto","value":"/relative/path"}, {"op":"reload"},
{"op":"click|check|uncheck","target":TARGET},
{"op":"fill|select|press","target":TARGET,"value":"string"},
{"op":"visible","target":TARGET},
{"op":"text|value","target":TARGET,"value":"expected string"},
{"op":"count","target":TARGET,"value":positive_integer},
{"op":"url","value":"/exact/relative/path"}.
text uses contains-text; value uses exact input value; url checks exact relative path including query/hash.
press supports only Enter, Tab, Escape, ArrowDown, ArrowUp, Space.
TARGET is exactly one of {"role":"role","name":"exact accessible name"}, {"label":"exact label"},
{"text":"exact text"}, {"placeholder":"exact placeholder"}, {"css":"source-observed CSS selector"}.
TARGET may additionally contain "within": TARGET for one enclosing region (no deeper nesting).
Do not use arbitrary JavaScript, retries, sleeps, catches, conditional assertions or broad fallback locators.
The controller freezes this plan before executing it; checks cannot be rewritten after failures.
'''


def _text(value, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValueError('expected nonempty bounded string')
    return value


def _fields(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError('unexpected or missing fields: expected ' + ','.join(sorted(keys)))


def _path(value):
    _text(value)
    # Decode repeatedly to reject browser-normalized cross-origin forms too.
    decoded = value
    for _ in range(4):
        parts = urlsplit(decoded)
        if (not decoded.startswith('/') or decoded.startswith('//') or '\\' in decoded
                or parts.scheme or parts.netloc or any(ord(c) < 32 for c in decoded)):
            raise ValueError('navigation must be a same-origin relative path')
        changed = unquote(decoded)
        if changed == decoded:
            return value
        decoded = changed
    raise ValueError('excessively encoded navigation')


def _target(value, depth=0):
    if not isinstance(value, dict):
        raise ValueError('target must be an object')
    keys = set(value) - {'within'}
    if keys not in ({'role', 'name'}, {'label'}, {'text'}, {'placeholder'}, {'css'}):
        raise ValueError('target must use exactly one locator')
    for key in keys:
        _text(value[key], 500)
    if 'within' in value:
        if depth:
            raise ValueError('target nesting exceeds one enclosing region')
        _target(value['within'], depth + 1)


def requirement_bindings(ir):
    """Exact public citation sources, grouped by their existing requirement identity."""
    bindings = {}
    for item in ir.get('work_items') or []:
        if isinstance(item, dict) and isinstance(item.get('id'), str):
            bindings[item['id']] = dict(id=item['id'], name=item.get('name'),
                quote_sources=[item[key] for key in ('name', 'text')
                               if isinstance(item.get(key), str) and item[key].strip()])
    for scenario in ir.get('scenarios') or []:
        if not isinstance(scenario, dict) or scenario.get('source') != 'parsed':
            continue
        ref = scenario.get('work_item')
        binding = bindings.get(ref) if isinstance(ref, str) else None
        if binding is None:
            continue
        # Scenario names can be generated IDs; only parsed statements are quotes.
        pieces = []
        given = scenario.get('given')
        if isinstance(given, dict):
            pieces.append(given.get('text'))
        for phase in ('when', 'then'):
            steps = scenario.get(phase)
            if isinstance(steps, list):
                pieces.extend(step.get('text') for step in steps if isinstance(step, dict))
        binding['quote_sources'].extend(piece for piece in pieces
            if isinstance(piece, str) and piece.strip())
    for binding in bindings.values():
        binding['quote_sources'] = list(dict.fromkeys(binding['quote_sources']))
    return list(bindings.values())


def validate_plan(value, ir):
    _fields(value, {'cases'})
    cases = value['cases']
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise ValueError('cases count outside bounds')
    items = {row['id']: row for row in requirement_bindings(ir)}
    names, total = set(), 0
    actions = {'click', 'fill', 'select', 'check', 'uncheck', 'press'}
    assertions = {'visible', 'text', 'value', 'count', 'url'}
    for case in cases:
        _fields(case, {'name', 'requirement_id', 'requirement_quote', 'steps'})
        name = _text(case['name'], 120)
        if name in names:
            raise ValueError('duplicate case name')
        names.add(name)
        ref = _text(case['requirement_id'], 200)
        quote = _text(case['requirement_quote'], 500)
        item = items.get(ref, {})
        if len(quote) < 8 or not any(quote in source for source in item.get('quote_sources', [])):
            raise ValueError('quote is not bound to the referenced public requirement')
        steps = case['steps']
        if not isinstance(steps, list) or not 3 <= len(steps) <= MAX_STEPS:
            raise ValueError('step count outside bounds')
        total += len(steps)
        last_action, last_assertion = -1, -1
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or not isinstance(step.get('op'), str):
                raise ValueError('invalid step')
            op = step['op']
            keys = {'op'}
            if op in actions | (assertions - {'url'}):
                keys.add('target')
            if op in {'goto', 'url', 'fill', 'select', 'press', 'text', 'value', 'count'}:
                keys.add('value')
            if op not in actions | assertions | {'goto', 'reload'}:
                raise ValueError('unsupported browser operation')
            _fields(step, keys)
            if index == 0 and op != 'goto':
                raise ValueError('journey must start with goto')
            if op == 'goto' and index:
                raise ValueError('navigate through controls after the initial goto')
            if 'target' in keys:
                _target(step['target'])
            if op in {'goto', 'url'}:
                _path(step['value'])
            elif op == 'count':
                if type(step['value']) is not int or not 1 <= step['value'] <= 10000:
                    raise ValueError('count assertion must be a positive bounded integer')
            elif 'value' in keys:
                _text(step['value'])
            if op == 'press' and step['value'] not in {'Enter', 'Tab', 'Escape', 'ArrowDown', 'ArrowUp', 'Space'}:
                raise ValueError('unsupported key')
            if op in actions:
                last_action = index
            if op in assertions:
                last_assertion = index
        if last_action < 0 or last_assertion <= last_action:
            raise ValueError('interaction must have a subsequent positive assertion')
    if total > MAX_TOTAL_STEPS:
        raise ValueError('total steps outside bounds')
    return value


def _locator(target):
    base = _locator(target['within']) if 'within' in target else 'page'
    js = json.dumps
    if 'role' in target:
        return f'{base}.getByRole({js(target["role"])}, {{name:{js(target["name"])},exact:true}})'
    for key, method in (('label', 'getByLabel'), ('text', 'getByText'), ('placeholder', 'getByPlaceholder')):
        if key in target:
            return f'{base}.{method}({js(target[key])}, {{exact:true}})'
    return f'{base}.locator({js(target["css"])})'


def compile_plan(plan, ir):
    validate_plan(plan, ir)
    js = json.dumps
    lines = ["const {test,expect}=require('@playwright/test');", 'test.describe.configure({retries:0});']
    for case in plan['cases']:
        title = case['requirement_id'] + ': ' + case['name']
        lines += [f'test({js(title)},async ({{page,baseURL}})=>{{', f'test.setTimeout({CASE_TIMEOUT_MS});',
                  # Block cross-origin redirects, popups and requests from the scratch browser context.
                  "await page.context().route('**/*',route=>{const u=new URL(route.request().url()); return u.origin===new URL(baseURL).origin ? route.continue() : route.abort();});"]
        for step in case['steps']:
            op = step['op']
            loc = _locator(step['target']) if 'target' in step else None
            arg = js(step.get('value'))
            if op == 'goto':
                line = f'await page.goto({arg});'
            elif op == 'reload':
                line = 'await page.reload();'
            elif op in {'click', 'check', 'uncheck'}:
                line = f'await {loc}.{op}();'
            elif op in {'fill', 'select', 'press'}:
                method = 'selectOption' if op == 'select' else op
                line = f'await {loc}.{method}({arg});'
            elif op == 'url':
                line = f'await expect(page).toHaveURL(new URL({arg},baseURL).href);'
            else:
                method = {'visible': 'toBeVisible', 'text': 'toContainText', 'value': 'toHaveValue', 'count': 'toHaveCount'}[op]
                line = f'await expect({loc}).{method}({"" if op == "visible" else arg});'
            lines.append(line)
        lines.append('});')
    return '\n'.join(lines) + '\n'


def freeze_plan(plan, ir, directory):
    source = compile_plan(plan, ir).encode('utf-8')
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=False)
    files = {'journeys.spec.cjs': source, 'plan.json': json.dumps(plan, ensure_ascii=False, indent=2).encode('utf-8')}
    for name, content in files.items():
        (root / name).write_bytes(content)
    return {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}


def verify_frozen(directory, hashes):
    root = Path(directory)
    try:
        return (set(p.name for p in root.iterdir()) == set(hashes)
                and all(not (root / name).is_symlink() and hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
                        for name, digest in hashes.items()))
    except OSError:
        return False

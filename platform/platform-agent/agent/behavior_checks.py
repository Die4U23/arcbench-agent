"""Bounded, requirement-bound browser diagnostics; never official acceptance.

The model returns data only. This compiler owns all executable code, and the
frozen suite lives outside the editable application throughout repair rounds.
"""
from __future__ import annotations

from graphlib import CycleError, TopologicalSorter
import hashlib
import json
from pathlib import Path
from .behavior_contracts import requirement_contracts, plan_coverage, require_coverage, public_route
from .visual_contracts import normalize_visual, VISUAL_RUNTIME_JS

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
When the public contract provides initial_page, goto must use that exact path, including query/hash,
and the next step must assert url for that same path. A redirect to a same-named control is not proof.
Before the first non-reload interaction, follow every DOM assertion with another url assertion for
initial_page; controls may appear only after an asynchronous redirect. Retain these checks as explicit
steps so they consume the same step/tool budget. After a declared interaction, legitimate navigation
is governed by its public scenario instead of unconditionally forcing the initial URL.
Names and roles are not global identities: preserve explicit page and within scope. Duplicate names
are legitimate; an explicit positive count verifies a repeated collection without making it unique.
Navigate thereafter through source-observed UI controls using click or other supported interactions.
Use reload to check persistence at the current URL. Each transition case must include an interaction followed by
a positive assertion of its result; a url step asserts the current location and does not navigate.
For initial-render requirements, assert visible, text, value, count or visual after goto and after the final
reload; URL assertions may supplement these. Required transitions still need an interaction
and a subsequent positive assertion. A trailing reload without an assertion is incomplete.
For disappearance, use hidden, never count=0. The identical TARGET must first pass visible,
then an interaction must occur before hidden. Also assert a positive resulting state after the
interaction (for example a requirement-supported confirmation or remaining container); hidden alone
cannot establish success. A text, value or count assertion does not substitute for the explicit visible
step, even when it uses the identical TARGET. Copy the entire TARGET including any within region exactly
between visible and hidden. Place visible while the target is present and before the interaction that
removes it. A reload clears prior presence evidence. To check that disappearance persists,
first establish visible -> interaction -> hidden before reload, then assert hidden for the identical TARGET
after reload. Any new interaction or a new visible assertion for that target invalidates this disappearance
evidence; establish a new visible -> interaction -> hidden sequence. Include a positive assertion after reload.
Do not invent a confirmation or expected business state.
No assumptions about unspecified business behavior, no API shortcut for UI actions, no external URLs.
An unsupported flow should be omitted rather than inventing an oracle. These are fallible development
diagnostics, NOT formal acceptance or complete requirement coverage.

Step schemas (no extra fields):
{"op":"goto","value":"/relative/path"}, {"op":"reload"},
{"op":"click|check|uncheck","target":TARGET},
{"op":"fill|select|press","target":TARGET,"value":"string"},
{"op":"visible|hidden","target":TARGET},
{"op":"text|contains_text|value","target":TARGET,"value":"expected string"},
{"op":"count","target":TARGET,"value":positive_integer},
{"op":"url","value":"/exact/relative/path"},
{"op":"visual","target":TARGET,"value":{"properties":{"background-color":"rgb(255, 0, 0)","width":"80px","height":"80px","border-radius":"50%","shape":"circle"},"viewport":{"width":1280,"height":720}}}.
For visual, copy the supplied public contract target and full value. Never infer a color, size,
shape, tolerance or viewport from current source, screenshots without an oracle, or vague prose.
Properties allowed: background-color, color, border-color, width, height, border-radius, shape.
Colors are exact opaque sRGB. Width/height are rendered border-box CSS px, radius is uniform px/%.
Circle means equal nonzero axes and all four effective radii equal half the diameter; square means
identical axes with zero radii; rectangle means zero radii; pill means unequal axes with all four
circular corner radii equal to half the shorter axis. No tolerance is supported.
The observation environment is desktop light mode, DPR 1. The explicit requirement viewport is
used; omitted public viewport means the disclosed fixed 1280x720 observation, not responsive proof.
All visual steps in one case must use the same viewport. A visual assertion is a positive DOM
assertion and verifies its exact accessible target. Unsupported compositing (opacity, transforms,
overlap, pseudo elements, masks, filters, animations, clipping, painted children) fails closed.
A pass proves only the named CSS/geometry at that viewport, never full screenshot/pixel equivalence.
text uses exact whole text; contains_text is supplemental only and cannot satisfy an exact requirement; value uses exact input value; url checks exact relative path including query/hash.
hidden checks that the previously visible target is now hidden or removed.
press supports only Enter, Tab, Escape, ArrowDown, ArrowUp, Space.
TARGET is exactly one of {"role":"role","name":"exact accessible name"}, {"label":"exact label"},
{"text":"exact text"}, {"placeholder":"exact placeholder"}, {"css":"source-observed CSS selector"}.
TARGET may additionally contain "within": TARGET for one enclosing region (no deeper nesting).
Do not use arbitrary JavaScript, retries, sleeps, catches, conditional assertions or broad fallback locators.
The controller freezes this plan before executing it; checks cannot be rewritten after failures.
'''


class VerificationScope:
    """Public binding errors and atomic prerequisites, indexed once per IR."""

    def __init__(self, ir):
        contracts = requirement_contracts(ir)  # Validate global structure before scoping.
        self.errors = {ref: row['unresolved'] for ref, row in contracts.items()}
        self.dependencies = {}
        rows = ir.get('work_items') if isinstance(ir, dict) else None
        for row in rows if isinstance(rows, list) else ():
            if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                continue
            ref = row['id']
            deps = row.get('depends_on', [])
            if not isinstance(deps, list) or any(not isinstance(dep, str) or not dep for dep in deps):
                self.errors[ref] = [*self.errors.get(ref, ()), 'invalid_requirement_dependencies']
                continue
            self.dependencies[ref] = tuple(deps)

    def select(self, refs):
        dependencies = {ref: self.dependencies.get(ref, ()) for ref in refs}
        unresolved = {ref: self.errors.get(ref, ['public_requirement_missing'])
                      for ref in dependencies if ref not in self.errors or self.errors[ref]}
        graph = TopologicalSorter(dependencies)
        try:
            graph.prepare()
        except CycleError:
            pass  # Independent nodes can still run. The cycle stays blocked.
        ordered = []
        while ready := graph.get_ready():
            for ref in ready:
                # An undeclared or out-of-scope prerequisite is never completed.
                if ref in dependencies and ref not in unresolved:
                    ordered.append(ref)
                    graph.done(ref)
        runnable = set(ordered)
        blocked_dependencies = {}
        for ref, deps in dependencies.items():
            if missing := set(deps) - runnable:
                blocked_dependencies[ref] = sorted(missing)
        return ordered, unresolved, blocked_dependencies


def _text(value, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValueError('expected nonempty bounded string')
    return value


def _fields(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError('unexpected or missing fields: expected ' + ','.join(sorted(keys)))


def _path(value):
    _text(value)
    return public_route(value)


def _target(value, depth=0):
    if not isinstance(value, dict):
        raise ValueError('target must be an object')
    keys = set(value) - {'within'}
    if keys not in ({'role', 'name'}, {'label'}, {'text'}, {'placeholder'}, {'css'}):
        raise ValueError('target must use exactly one locator')
    for key in keys:
        if key == 'name' and value[key] == '':
            continue  # An explicitly unnamed accessible control is a valid exact target.
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
    contracts = requirement_contracts(ir)
    for ref, binding in bindings.items():
        binding['contract'] = contracts.get(ref)
    return list(bindings.values())


def validate_plan(value, ir):
    _fields(value, {'cases'})
    cases = value['cases']
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise ValueError('cases count outside bounds')
    items = {row['id']: row for row in requirement_bindings(ir)}
    names, total = set(), 0
    actions = {'click', 'fill', 'select', 'check', 'uncheck', 'press'}
    assertions = {'visible', 'text', 'contains_text', 'value', 'count', 'url', 'visual'}
    for case_index, case in enumerate(cases):
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
        if not isinstance(steps, list) or not 2 <= len(steps) <= MAX_STEPS:
            raise ValueError('step count outside bounds')
        total += len(steps)
        last_action = last_assertion = last_render = last_reload = -1
        visual_viewport = None
        visible_targets = {}
        disappeared_targets = set()
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or not isinstance(step.get('op'), str):
                raise ValueError('invalid step')
            op = step['op']
            keys = {'op'}
            if op in actions | (assertions - {'url'}) | {'hidden'}:
                keys.add('target')
            if op in {'goto', 'url', 'fill', 'select', 'press', 'text', 'contains_text', 'value', 'count', 'visual'}:
                keys.add('value')
            if op not in actions | assertions | {'goto', 'reload', 'hidden'}:
                raise ValueError('unsupported browser operation')
            _fields(step, keys)
            if index == 0 and op != 'goto':
                raise ValueError('journey must start with goto')
            if op == 'goto' and index:
                raise ValueError('navigate through controls after the initial goto')
            if op == 'reload':
                visible_targets.clear()
                last_reload = index
            if 'target' in keys:
                _target(step['target'])
            if op in {'visible', 'hidden'}:
                target = json.dumps(step['target'], sort_keys=True, ensure_ascii=False)
                if op == 'visible':
                    disappeared_targets.discard(target)
                    visible_targets[target] = index
                else:
                    if target not in disappeared_targets and not 0 <= visible_targets.get(target, -1) < last_action:
                        raise ValueError(f'cases[{case_index}].steps[{index}]: hidden requires the same target '
                            'visible before a subsequent interaction; text, value and count do not replace visible')
                    # Only an executed visible -> action -> hidden chain can
                    # support persistence across reloads; stale presence cannot.
                    disappeared_targets.add(target)
                    visible_targets.pop(target, None)
            if op in {'goto', 'url'}:
                _path(step['value'])
            elif op == 'visual':
                canonical = normalize_visual(step['value'])
                if visual_viewport is not None and visual_viewport != canonical['viewport']:
                    raise ValueError('visual_viewport_conflict_within_case')
                visual_viewport = canonical['viewport']
            elif op == 'count':
                if type(step['value']) is not int or not 1 <= step['value'] <= 10000:
                    raise ValueError('count assertion must be a positive bounded integer')
            elif 'value' in keys:
                if op in {'fill', 'value'} and step['value'] == '':
                    pass  # Clearing an input and asserting emptiness are explicit operations.
                else:
                    _text(step['value'])
            if op == 'press' and step['value'] not in {'Enter', 'Tab', 'Escape', 'ArrowDown', 'ArrowUp', 'Space'}:
                raise ValueError('unsupported key')
            if op in actions:
                disappeared_targets.clear()
                last_action = index
            if op in assertions:
                last_assertion = index
            if op in assertions - {'url'}:
                last_render = index
        if last_action < 0:
            if last_render <= last_reload:
                raise ValueError(f'cases[{case_index}].steps[{max(last_reload, 0)}]: '
                    'initial navigation or reload requires a subsequent positive DOM assertion; URL alone is insufficient')
        elif last_assertion <= max(last_action, last_reload):
            raise ValueError(f'cases[{case_index}].steps[{max(last_action, last_reload)}]: '
                'interaction or reload must have a subsequent positive assertion')
    if total > MAX_TOTAL_STEPS:
        raise ValueError('total steps outside bounds')
    # A valid subset is diagnostic evidence, not whole-task acceptance.
    present = {case['requirement_id'] for case in cases}
    # Evaluate unfiltered IR so malformed global records remain unresolved.
    # Only absent, otherwise valid refs may defer their own semantic obligations.
    evidence = plan_coverage(value, ir)
    scoped_missing = [row for row in evidence['missing'] if row['requirement_id'] in present]
    scoped_unresolved = [row for row in evidence['unresolved'] if row['requirement_id'] in present]
    if scoped_missing or scoped_unresolved or not present.issubset(evidence['covered']):
        raise ValueError('requirement_constraints_incomplete: ' + json.dumps(
            dict(missing=scoped_missing, unresolved=scoped_unresolved,
                 covered=[ref for ref in evidence['covered'] if ref in present]),
            ensure_ascii=False, sort_keys=True))
    return value


def _locator(target, root='page'):
    base = _locator(target['within'], root) if 'within' in target else root
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
    contracts = requirement_contracts(ir)
    if any(contracts[case['requirement_id']].get('initial_page') is not None for case in plan['cases']):
        # Resolve page identity when locators are resolved, including cross-document retries.
        # Registration precedes the page fixture and is shared by suites in a worker.
        lines += ["test.beforeAll(async()=>{const key=Symbol.for('arcbench.publicPageSelector.v1');",
                  "await (globalThis[key]??=require('@playwright/test').selectors.register('arc_public_page',()=>({",
                  "query(root,selector){const d=root.ownerDocument||root;return d.defaultView.location.href===JSON.parse(selector)?d.documentElement:null;},",
                  "queryAll(root,selector){const element=this.query(root,selector);return element?[element]:[];}",
                  "}),{contentScript:true}));});",
                  # An actionability wait may retain a node across same-document navigation.
                  # Observe the first action event's URL, rather than rejecting valid navigation after it.
                  "async function initialAction(page,expected,op,perform){",
                  "let observed;await page.exposeBinding('__arcInitialActionUrl',(_,url)=>{if(observed===undefined)observed=url;});",
                  "const install=op=>{const types=op==='press'?['keydown']:op==='fill'?['beforeinput','input']:op==='select'?['input','change']:['pointerdown'];",
                  "const capture=event=>{if(!event.isTrusted&&!['fill','select'].includes(op))return;",
                  "for(const type of types)document.removeEventListener(type,capture,true);window.__arcInitialActionUrl(window.location.href);};",
                  "for(const type of types)document.addEventListener(type,capture,true);};",
                  "await page.addInitScript(install,op);await page.evaluate(install,op);",
                  "await perform();if(observed===undefined){await expect(page).toHaveURL(expected);}else{expect(observed).toBe(expected);}",
                  "}"]
    if any(step['op'] == 'visual' for case in plan['cases'] for step in case['steps']):
        lines += ['test.use({deviceScaleFactor:1,isMobile:false,hasTouch:false,colorScheme:"light"});', VISUAL_RUNTIME_JS]
    for case in plan['cases']:
        title = case['requirement_id'] + ': ' + case['name']
        lines += [f'test({js(title)},async ({{page,baseURL}})=>{{', f'test.setTimeout({CASE_TIMEOUT_MS});',
                  # Block cross-origin redirects, popups and requests from the scratch browser context.
                  "await page.context().route('**/*',route=>{const u=new URL(route.request().url()); return u.origin===new URL(baseURL).origin ? route.continue() : route.abort();});"]
        visual_steps = [step for step in case['steps'] if step['op'] == 'visual']
        if visual_steps:
            viewport = normalize_visual(visual_steps[0]['value'])['viewport']
            lines.append(f'await page.setViewportSize({js(viewport)});')
        initial_page = contracts[case['requirement_id']].get('initial_page')
        initial_context = initial_page is not None
        for step in case['steps']:
            op = step['op']
            root = ('page.locator("arc_public_page="+JSON.stringify(new URL('
                    + js(initial_page) + ',baseURL).href))') if initial_context else 'page'
            loc = _locator(step['target'], root) if 'target' in step else None
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
            elif op == 'visual':
                line = f'await assertPublicVisual(page,{loc},{js(normalize_visual(step["value"]))});'
            else:
                method = {'visible': 'toBeVisible', 'hidden': 'toBeHidden', 'text': 'toHaveText', 'contains_text': 'toContainText', 'value': 'toHaveValue', 'count': 'toHaveCount'}[op]
                line = f'await expect({loc}).{method}({"" if op in {"visible", "hidden"} else arg});'
            if initial_context and op in {'click', 'fill', 'select', 'check', 'uncheck', 'press'}:
                line = (f'await initialAction(page,new URL({js(initial_page)},baseURL).href,{js(op)},'
                        f'async()=>{{{line}}});')
            lines.append(line)
            if op in {'click', 'fill', 'select', 'check', 'uncheck', 'press'}:
                initial_context = False
        lines.append('});')
    return '\n'.join(lines) + '\n'


def freeze_plan(plan, ir, directory):
    source = compile_plan(plan, ir).encode('utf-8')
    files = {'journeys.spec.cjs': source, 'plan.json': json.dumps(plan, ensure_ascii=False, indent=2).encode('utf-8')}
    return write_frozen_suite(directory, files)


def write_frozen_suite(directory, files: dict[str, bytes]):
    """Write immutable, flat suite artifacts and return their exact-byte hashes."""
    for name, content in files.items():
        if (not isinstance(name, str) or not name or name in {'.', '..'}
                or any(char in name for char in '/\\:\x00') or not isinstance(content, bytes)):
            raise ValueError('frozen suite requires plain filenames and byte contents')
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=False)
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

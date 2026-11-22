"""Bounded, requirement-bound browser diagnostics; never official acceptance.

The model returns data only. This compiler owns all executable code, and the
frozen suite lives outside the editable application throughout repair rounds.
"""
from __future__ import annotations

from graphlib import CycleError, TopologicalSorter
import hashlib
import json
from pathlib import Path
from .behavior_contracts import requirement_contracts, plan_coverage, require_coverage, public_route, project_requirements
from .behavior_contracts import MAX_STEPS, navigation_guards, scenario_guards
from .group_fixture_titles import locator_name, runtime_setup, value_expr
from .visual_contracts import normalize_visual, VISUAL_RUNTIME_JS
from .reference_visual import PIXEL_OP, PIXEL_RUNTIME_JS, asset_name, normalize_value as normalize_pixel, suite_assets
from .shared_public_checks import HOVER_INVENTORY_RUNTIME_JS, INVENTORY_OP, KEYBOARD_RUNTIME_JS, inventory_value

MAX_CASES = 12
MAX_TOTAL_STEPS = 192
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
Every case starts with goto. A later goto is valid only in the exact sequence of a public
exact_navigation contract. Preserve every declared operation in order, including its path.
Additional visible and URL observations can check public roles and routes.
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
{"op":"tabbable","target":TARGET},
{"op":"text|contains_text|value","target":TARGET,"value":"expected string"},
{"op":"checked","target":{"role":"checkbox","name":"exact accessible name"},"value":true|false},
{"op":"count","target":TARGET,"value":positive_integer},
{"op":"url","value":"/exact/relative/path"},
{"op":"visual","target":TARGET,"value":{"properties":{"background-color":"rgb(255, 0, 0)","width":"80px","height":"80px","border-radius":"50%","shape":"circle"},"viewport":{"width":1280,"height":720}}}.
A reference_pixels step is allowed only as an exact copy of the supplied public obligation.
Preserve its state marker, capture protocol and published asset binding as written.
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
checked observes the current Boolean state of one named checkbox; check and uncheck are actions.
press supports only Enter, Tab, Escape, ArrowDown, ArrowUp, Space.
TARGET is exactly one of {"role":"role","name":"exact accessible name"}, {"label":"exact label"},
{"text":"exact text"}, {"placeholder":"exact placeholder"}, {"css":"source-observed CSS selector"}.
TARGET may additionally contain "within": TARGET for one enclosing region (no deeper nesting).
An exact_navigation contract may supply {"roles":["button","link"],"name":"...","within":TARGET}
when the public source explicitly permits either role for that named entry.
Do not use arbitrary JavaScript, retries, sleeps, catches, conditional assertions or broad fallback locators.
The controller freezes this plan before executing it; checks cannot be rewritten after failures.
'''


class VerificationScope:
    """Public binding errors and atomic prerequisites, indexed once per IR."""

    def __init__(self, ir):
        contracts = requirement_contracts(ir)  # Validate global structure before scoping.
        self.contracts = contracts
        self.errors = {ref: row['unresolved'] for ref, row in contracts.items()}
        self.issue_details = {ref: list(row.get('issue_details', [])) for ref, row in contracts.items()}
        self.dependencies = {}
        rows = ir.get('work_items') if isinstance(ir, dict) else None
        for row in rows if isinstance(rows, list) else ():
            if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                continue
            ref = row['id']
            deps = row.get('depends_on', [])
            if not isinstance(deps, list) or any(not isinstance(dep, str) or not dep for dep in deps):
                self.errors[ref] = [*self.errors.get(ref, ()), 'invalid_requirement_dependencies']
                self.issue_details.setdefault(ref, []).append(dict(reason='invalid_requirement_dependencies',
                    source=f'work_items[{ref}].depends_on', scope='requirement'))
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
    if keys == {'roles', 'name'}:
        if value['roles'] != ['button', 'link']:
            raise ValueError('invalid alternative entry roles')
        _text(value['name'], 500)
        if depth or 'within' not in value:
            raise ValueError('alternative entry roles require a declared region')
        _target(value['within'], depth + 1)
        return
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
        # A scoped child can cite a public visual obligation inherited from its ancestor.
        for scenario in (binding['contract'] or {}).get('scenarios', []):
            if scenario.get('inherited_from'):
                binding['quote_sources'].extend(obligation['source'] for obligation in scenario['obligations']
                    if obligation['source'] not in binding['quote_sources'])
    return list(bindings.values())


def _matching_navigation(contract, steps):
    return [guards for scenario in (contract or {}).get('scenarios', [])
            if (guards := navigation_guards(scenario, steps)) is not None]


def _validate_case_steps(steps, contract, case_index):
    """Validate the bounded browser operations without granting requirement coverage."""
    actions = {'click', 'fill', 'select', 'check', 'uncheck', 'press'}
    assertions = {'visible', 'text', 'contains_text', 'value', 'count', 'url', 'visual', 'checked', 'tabbable', 'record_fields', PIXEL_OP}
    if not isinstance(steps, list) or not 2 <= len(steps) <= MAX_STEPS:
        raise ValueError('step count outside bounds')
    exact_navigation = _matching_navigation(contract, steps)
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
        if op in {'goto', 'url', 'fill', 'select', 'press', 'text', 'contains_text', 'value', 'count', 'visual', 'checked', 'record_fields', PIXEL_OP}:
            keys.add('value')
        if op not in actions | assertions | {'goto', 'reload', 'hidden'}:
            raise ValueError('unsupported browser operation')
        _fields(step, keys)
        if index == 0 and op != 'goto':
            raise ValueError('journey must start with goto')
        if op == 'goto' and index and not exact_navigation:
            raise ValueError('navigate through controls after the initial goto')
        if op == 'tabbable' and not any(
                o['step'] == step for s in (contract or {}).get('scenarios', [])
                for o in s['obligations']):
            raise ValueError('keyboard reachability requires a bound public target')
        if op == 'reload':
            visible_targets.clear()
            last_reload = index
        if 'target' in keys:
            _target(step['target'])
            if 'roles' in step['target'] and not exact_navigation:
                raise ValueError('alternative entry roles must be bound to a public journey')
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
        elif op in {'visual', PIXEL_OP}:
            if op == PIXEL_OP:
                canonical = normalize_pixel(step['value'])['capture']
                public = (contract or {}).get('scenarios', [])
                if not any(step == row['step'] for scenario in public for row in scenario['obligations']):
                    raise ValueError('pixel_reference_step_not_declared')
            else:
                canonical = normalize_visual(step['value'])
            if visual_viewport is not None and visual_viewport != canonical['viewport']:
                raise ValueError('visual_viewport_conflict_within_case')
            visual_viewport = canonical['viewport']
        elif op == 'checked':
            if type(step['value']) is not bool or step['target'].get('role') != 'checkbox' or not step['target'].get('name'):
                raise ValueError('checked requires a named checkbox and a Boolean value')
        elif op == 'record_fields' or (op == 'count' and type(step['value']) is int and step['value'] == 0):
            scenarios = (contract or {}).get('scenarios', [])
            if not any(step == row['step'] for scenario in scenarios for row in scenario['obligations']):
                raise ValueError('collection assertion requires a bound public obligation')
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


def validate_plan(value, ir):
    _fields(value, {'cases'})
    cases = value['cases']
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise ValueError('cases count outside bounds')
    items = {row['id']: row for row in requirement_bindings(ir)}
    names, total = set(), 0
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
        _validate_case_steps(steps, item.get('contract'), case_index)
        total += len(steps)
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


def _locator(target, root='page', fixture=None):
    base = _locator(target['within'], root, fixture) if 'within' in target else root
    js = json.dumps
    if 'roles' in target:
        button, link = [f'{base}.getByRole({js(role)}, {{name:{locator_name(target, fixture)},exact:true}})'
                        for role in target['roles']]
        return f'{button}.or({link})'
    if 'role' in target:
        return f'{base}.getByRole({js(target["role"])}, {{name:{locator_name(target, fixture)},exact:true}})'
    for key, method in (('label', 'getByLabel'), ('text', 'getByText'), ('placeholder', 'getByPlaceholder')):
        if key in target:
            return f'{base}.{method}({js(target[key])}, {{exact:true}})'
    return f'{base}.locator({js(target["css"])})'


def compile_plan(plan, ir):
    validate_plan(plan, ir)
    return _emit_plan(plan, requirement_contracts(ir))


def _emit_plan(plan, contracts):
    js = json.dumps
    lines = ["const {test,expect}=require('@playwright/test');", 'test.describe.configure({retries:0});']
    if any(c.get('initial_page') is not None or any(s.get('exact_navigation') for s in c['scenarios'])
           for c in contracts.values()):
        # Resolve page identity when locators are resolved, including cross-document retries.
        # Registration precedes the page fixture and is shared by suites in a worker.
        lines += ["test.beforeAll(async()=>{const key=Symbol.for('arcbench.publicPageSelector.v1');",
                  "await (globalThis[key]??=require('@playwright/test').selectors.register('arc_public_page',()=>({",
                  "query(root,selector){const d=root.ownerDocument||root;return d.defaultView.location.href===JSON.parse(selector)?d.documentElement:null;},",
                  "queryAll(root,selector){const element=this.query(root,selector);return element?[element]:[];}",
                  "}),{contentScript:true}));});",
                  # An actionability wait may retain a node across same-document navigation.
                  # Observe the first action event's URL, rather than rejecting valid navigation after it.
                  "async function initialAction(page,expected,op,index,perform){",
                  "const binding='__arcInitialActionUrl'+index;",
                  "let observed;await page.exposeBinding(binding,(_,url)=>{if(observed===undefined)observed=url;});",
                  "const install=({op,binding})=>{const types=op==='press'?['keydown']:op==='fill'?['beforeinput','input']:op==='select'?['input','change']:['pointerdown'];",
                  "const capture=event=>{if(!event.isTrusted&&!['fill','select'].includes(op))return;",
                  "for(const type of types)document.removeEventListener(type,capture,true);window[binding](window.location.href);};",
                  "for(const type of types)document.addEventListener(type,capture,true);};",
                  "await page.addInitScript(install,{op,binding});await page.evaluate(install,{op,binding});",
                  "await perform();if(observed===undefined){await expect(page).toHaveURL(expected);}else{expect(observed).toBe(expected);}",
                  "}"]
    if any(step['op'] in {'rendered_lines', 'record_fields'} for case in plan['cases'] for step in case['steps']):
        lines.append(r"""function readRecordFields(root, args) {
  const grouped = !Array.isArray(args);
  const [title, ...body] = grouped ? args.fields : args;
  const auxiliary = 'button,input,select,textarea,nav,dialog,[role=button],[role=menu],[role=menuitem],[role=tooltip],[role=region],[role=status]';
  const heading = 'h1,h2,h3,h4,h5,h6,[role=heading]';
  const paragraph = 'p,[role=paragraph]';
  const controls = 'button,a[href],[role=button],[role=link]';
  const records = '[role=region],section[aria-label],section[aria-labelledby]';
  const toolbarOrMenu = 'menu,[role=toolbar],[role=menu],[role=menubar],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio]';
  const visible = el => el.checkVisibility({visibilityProperty:true,opacityProperty:true});
  const groupedAuxiliary = 'label,section[aria-label],section[aria-labelledby],[aria-hidden=true]';
  if (!grouped && root.closest(toolbarOrMenu)) return false;
  const excluded = new Set(Array.from(root.querySelectorAll('*')).filter(el =>
    el.matches(auxiliary) || el.matches(grouped ? groupedAuxiliary : records + ',' + toolbarOrMenu) || !visible(el)));
  const included = (el, owner) => !Array.from(excluded).some(other => other !== owner && other.contains(el));
  const ownedControl = el => el.parentElement.closest(records) === root
    && !el.parentElement.closest(controls + ',[role=menu],[role=tooltip]');
  const headings = Array.from(root.querySelectorAll(heading)).filter(el => included(el));
  if (!grouped && headings.length && !headings.some(el => el.innerText === title)) return false;
  function read(el, titleControl) {
    if (el !== titleControl && excluded.has(el)) return [];
    if (!Array.from(excluded).some(other => other !== titleControl && el.contains(other))) {
      if (typeof el.innerText !== 'string') return [];
      const separator = grouped && el.matches('tr,[role=row]') ? /[\t\n]/ : /\r?\n/;
      return el.innerText.split(separator).filter(line => line !== '');
    }
    const lines = [];
    for (const node of el.childNodes) {
      if (node.nodeType === Node.ELEMENT_NODE) lines.push(...read(node, titleControl));
      else if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
        // Mixed direct text and interactive children has no separable rendered field.
        return [null];
      }
    }
    return lines;
  }
  const fieldText = el => grouped ? read(el).join('\n') : el.innerText;
  const actual = read(root);
  let titleField;
  if (grouped) {
    const references = (root.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    const labelled = references.map(id => root.ownerDocument.getElementById(id))
      .filter(el => el && el !== root && root.contains(el) && included(el));
    const rowHeaders = Array.from(root.querySelectorAll('th[scope=row],[role=rowheader]')).filter(el => included(el));
    const titles = rowHeaders.length ? rowHeaders : headings.length ? headings : labelled;
    if (titles.length) {
      const identified = titles.filter(el => labelled.length === 1 && el.contains(labelled[0]));
      const candidates = identified.length ? identified : titles;
      if (candidates.length !== 1 || fieldText(candidates[0]) !== title) return false;
      titleField = candidates[0];
    }
  }
  if (!actual.length || (grouped && !actual.includes(title))) {
    // An entire record may be a single actionable card. Its visible heading and
    // paragraphs must belong to the same control, independently of toolbar labels.
    if (grouped) {
      // A timestamp beside a card does not compete with the card's record fields.
      for (const timestamp of root.querySelectorAll('time')) {
        if (included(timestamp)) excluded.add(timestamp);
      }
      if (read(root).length) return false;
    }
    const content = Array.from(root.querySelectorAll(controls))
      .filter(el => visible(el) && !el.querySelector(auxiliary)
        && included(el, el) && ownedControl(el))
      .map(el => ({
        headings: Array.from(el.querySelectorAll(heading)).filter(field => visible(field) && included(field, el)),
        paragraphs: Array.from(el.querySelectorAll(paragraph)).filter(field => visible(field) && included(field, el))
      })).filter(fields => fields.headings.length && fields.paragraphs.length);
    if (content.length !== 1) return false;
    const fields = content[0];
    return fields.headings.length === 1 && fieldText(fields.headings[0]) === title
      && fields.paragraphs.map(fieldText).join('\n') === body.join('\n');
  }
  if (grouped) {
    const paragraphs = Array.from(root.querySelectorAll(paragraph))
      .filter(el => included(el) && !titleField?.contains(el)).map(fieldText);
    if (paragraphs.length) {
      if (!titleField && paragraphs.length > 1 && paragraphs.includes(title)) {
        paragraphs.splice(paragraphs.indexOf(title), 1);
      }
      return paragraphs.join('\n') === body.join('\n');
    }
    // An unlabelled layout has no source-grounded column order.
    actual.splice(actual.indexOf(title), 1);
    return body.every(value => actual.includes(value));
  }
  const matches = lines => [[title,...body],[...body,title]].some(expected =>
    lines.length === expected.length && expected.every((value,i) => lines[i] === value));
  if (matches(actual)) return true;
  const actions = Array.from(root.querySelectorAll(controls))
    .filter(el => included(el, el) && ownedControl(el) && visible(el));
  if (actions.some(el => Array.from(el.querySelectorAll(heading))
      .some(field => included(field, el) && field.innerText !== title))) return false;
  // The record's only exact visible title may be its primary action, with the body beside it.
  const titleControls = actions.filter(el => {
    const lines = read(el, el);
    return lines.length === 1 && lines[0] === title;
  });
  return titleControls.length === 1 && matches(read(root, titleControls[0]));
}""")
    if any(step['op'] == 'visual' for case in plan['cases'] for step in case['steps']):
        lines += ['test.use({deviceScaleFactor:1,isMobile:false,hasTouch:false,colorScheme:"light"});', VISUAL_RUNTIME_JS]
    if any(step['op'] == PIXEL_OP for case in plan['cases'] for step in case['steps']):
        lines += ['test.use({deviceScaleFactor:1,isMobile:false,hasTouch:false,colorScheme:"light",locale:"en-US",timezoneId:"UTC",serviceWorkers:"block"});',
                  PIXEL_RUNTIME_JS]
    if any(step['op'] in {'tabbable', 'keyboard_reachable', INVENTORY_OP}
           for case in plan['cases'] for step in case['steps']):
        lines.append(KEYBOARD_RUNTIME_JS)
    if any(step['op'] == INVENTORY_OP for case in plan['cases'] for step in case['steps']):
        lines.append(HOVER_INVENTORY_RUNTIME_JS)
    for case in plan['cases']:
        title = case['requirement_id'] + ': ' + case['name']
        fixture = case.get('fixture_titles')
        receipt = ',testInfo' if fixture or any(step['op'] == PIXEL_OP for step in case['steps']) else ''
        locale = next((step['value'] for step in case['steps'] if step['op'] == 'interface_locale'), None)
        if locale:
            lines += ['test.describe(()=>{', f'test.use({{locale:{js(locale)}}});']
        lines += [f'test({js(title)},async ({{page,baseURL}}{receipt})=>{{', f'test.setTimeout({CASE_TIMEOUT_MS});',
                  # Block cross-origin redirects, popups and requests from the scratch browser context.
                  "await page.context().route('**/*',route=>{const u=new URL(route.request().url()); return u.origin===new URL(baseURL).origin ? route.continue() : route.abort();});"]
        if fixture:
            lines.extend(runtime_setup(fixture))
        visual_step = next((step for step in case['steps'] if step['op'] in {'visual', PIXEL_OP}), None)
        if visual_step:
            value = visual_step['value']
            viewport = (normalize_visual(value)['viewport'] if visual_step['op'] == 'visual'
                        else normalize_pixel(value)['capture']['viewport'])
            lines.append(f'await page.setViewportSize({js(viewport)});')
        initial_page = contracts[case['requirement_id']].get('initial_page')
        exact_navigation = _matching_navigation(contracts[case['requirement_id']], case['steps'])
        opening_checks = [guards for scenario in contracts[case['requirement_id']]['scenarios']
                          if scenario.get('opened_targets') and (guards := scenario_guards(scenario, case['steps']))]
        if exact_navigation:
            initial_page = case['steps'][0]['value']
        initial_context = initial_page is not None
        for index, step in enumerate(case['steps']):
            op = step['op']
            if op == 'goto' and exact_navigation:
                initial_page, initial_context = step['value'], True
            root = ('page.locator("arc_public_page="+JSON.stringify(new URL('
                    + js(initial_page) + ',baseURL).href))') if initial_context else 'page'
            loc = _locator(step['target'], root, fixture) if 'target' in step else None
            arg = value_expr(step, fixture)
            if op == 'goto':
                line = f'await page.goto({arg});'
            elif op == 'reload':
                line = 'await page.reload();'
            elif op == 'hover':
                line = f'await {loc}.hover();'
            elif op in {'click', 'check', 'uncheck'}:
                line = f'await {loc}.{op}();'
            elif op in {'fill', 'select', 'press'}:
                method = 'selectOption' if op == 'select' else op
                line = f'await {loc}.{method}({arg});'
            elif op == 'url':
                line = f'await expect(page).toHaveURL(new URL({arg},baseURL).href);'
            elif op in {'tabbable', 'keyboard_reachable'}:
                required = js(op == 'tabbable')
                line = f'await assertKeyboardReach(page,{loc},{required});'
            elif op == INVENTORY_OP:
                inventory_value(step['value'])
                line = f'await assertHoverActionInventory(page,{loc},{arg});'
            elif op == 'activate_focused':
                if step['value'] != 'Enter' or step['target'].get('role') != 'button':
                    raise ValueError('focused activation requires an exact button and Enter')
                line = (f'await expect({loc}).toBeVisible();\nawait expect({loc}).toBeEnabled();\n'
                        f'await expect.poll(() => {loc}.evaluate(el => el.ownerDocument.hasFocus() && el.matches(":focus")), '
                        '{message:"The scoped button must have keyboard focus before activation"}).toBe(true);\n'
                        f'await page.keyboard.press({arg});')
            elif op == 'interface_locale':
                line = (f'await expect({loc}).toBeVisible();\n'
                        f'await expect.poll(() => {loc}.evaluate((element, expected) => {{\n'
                        'const language = element.closest("[lang]")?.lang;\n'
                        'if (!language) return true;\n'
                        'const actual = new Intl.Locale(language); const wanted = new Intl.Locale(expected);\n'
                        'return actual.language === wanted.language && (!actual.script || actual.script === wanted.maximize().script) '
                        '&& (!actual.region || actual.region === wanted.region);\n'
                        f'}}, {arg}), {{message:"Declared UI language conflicts with the public locale"}}).toBe(true);')
            elif op == 'current':
                line = (f'await expect({loc}).toBeVisible();\n'
                        f'await expect({loc}).toHaveAttribute("aria-current", /^(true|page|step|location|date|time)$/);')
            elif op == 'text' and ('\n' in step['value'] or '\r' in step['value']):
                # Playwright's string matcher folds line breaks, even when the contract specifies them.
                expected = step['value'].replace('\r\n', '\n').replace('\r', '\n')
                line = (f'await expect({loc}).toBeVisible();\n'
                        f'await expect.poll(() => {loc}.innerText()).toBe({js(expected)});')
            elif op in {'record_fields', 'rendered_lines'}:
                fields = f'{{fields:{arg}}}' if op == 'record_fields' else arg
                message = f"Step {index + 1}: {op} expected complete fields {js(step['value'])} within {js(step['target'])}"
                line = f'await expect.poll(() => {loc}.evaluate(readRecordFields,{fields}), {{message:{js(message)}}}).toBe(true);'
            elif op == 'set_pressed':
                # Internal source-bound setup. Never toggle an unknown or mixed state.
                wanted = js(str(step['value']).lower())
                scope = _locator(step['target']['within'], root, fixture)
                # The action label can change on toggle. Follow the activated button,
                # or its exact named replacement when a renderer remounts it.
                line = (f'{{const control = {loc};\n'
                        'await expect(control).toHaveAttribute("aria-pressed", /^(true|false)$/);\n'
                        'const button = await control.elementHandle();\n'
                        f'if ((await button.getAttribute("aria-pressed")) !== {wanted}) await button.click();\n'
                        f'await expect.poll(async () => {{const selected = {scope}.getByRole("button", '
                        f'{{pressed:{arg}}});\n'
                        'if (await button.evaluate(el => el.isConnected)) '
                        'return selected.evaluateAll((els, original) => els.includes(original), button);\n'
                        'return (await control.count()) === 1 && (await control.and(selected).count()) === 1;\n'
                        '}, {message:"The activated button must expose the requested pressed state"}).toBe(true);}')
            elif op == 'checked':
                # Mixed or unspecified state cannot prove either Boolean. Evaluation
                # requires a matching element even when the expected state is false.
                line = (f'await expect({loc}).toHaveCount(1);\n'
                        f'await expect.poll(() => {loc}.evaluate(el => '
                        'el instanceof HTMLInputElement && el.type === "checkbox" '
                        '? (el.indeterminate ? "mixed" : String(el.checked)) : el.getAttribute("aria-checked")))'
                        f'.toBe({js(str(step["value"]).lower())});')
            elif op == 'pressed':
                expected = js(str(step['value']).lower())
                line = (f'await expect({loc}).toBeVisible();\n'
                        f'await expect({loc}).toHaveAttribute("aria-pressed", {expected});')
            elif op == 'visual':
                line = f'await assertPublicVisual(page,{loc},{js(normalize_visual(step["value"]))});'
            elif op == PIXEL_OP:
                pixel = normalize_pixel(step["value"])
                line = (f'await assertReferencePixels(page,{loc},{js(pixel)},'
                        f'{js(asset_name(pixel["sha256"]))},baseURL,testInfo);')
            else:
                method = {'visible': 'toBeVisible', 'hidden': 'toBeHidden', 'text': 'toHaveText', 'contains_text': 'toContainText', 'value': 'toHaveValue', 'count': 'toHaveCount'}[op]
                line = f'await expect({loc}).{method}({"" if op in {"visible", "hidden"} else arg});'
            if initial_context and op in {'click', 'fill', 'select', 'check', 'uncheck', 'press'}:
                line = (f'await initialAction(page,new URL({js(initial_page)},baseURL).href,{js(op)},{index},'
                        f'async()=>{{{line}}});')
            for guards in opening_checks:
                opened = guards.get(index)
                if opened:
                    lines.append(f"await expect({_locator(opened)}, 'Click must open the declared target').toBeHidden();")
            lines.append(line)
            if op in {'click', 'fill', 'select', 'check', 'uncheck', 'press'}:
                initial_context = False
        lines.append('});')
        if locale:
            lines.append('});')
    return '\n'.join(lines) + '\n'


def freeze_plan(plan, ir, directory, *, reference_assets=None):
    source = compile_plan(plan, ir).encode('utf-8')
    files = {'journeys.spec.cjs': source, 'plan.json': json.dumps(plan, ensure_ascii=False, indent=2).encode('utf-8')}
    files.update(suite_assets(plan, reference_assets))
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

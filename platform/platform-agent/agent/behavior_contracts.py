"""Deterministic public-IR obligations. No implementation or model coverage claims."""
from collections import Counter
import hashlib
import json
import re
from urllib.parse import unquote, urlsplit
from .visual_contracts import normalize_visual
from .reference_visual import PIXEL_OP, capture_gap_details, public_pixel_step
from .navigation_contracts import _entry_state, _ordered_rows, bind_navigation
from .autosave_contracts import bind_autosave
from .grouped_collections import bind_group_readback
from .group_setup import bind_group_setup
from .group_exclusions import bind_group_exclusions
from .group_source_coverage import grouped_source_program
from .public_scenario_metadata import normalize_public_scenario, bind_public_step_references

MAX_STEPS = 24

def public_route(value):
    """The same origin/path boundary for public page bindings and executable goto."""
    if not isinstance(value, str) or not value:
        raise ValueError('navigation must be a same-origin relative path')
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


def _page_route(value, pages):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('public_page_unresolved')
    if value.startswith('/'):
        return public_route(value)
    matches = [p.get('path') for p in pages if p.get('name') == value]
    if len(matches) != 1:
        raise ValueError('public_page_unresolved_or_ambiguous')
    return public_route(matches[0])

def target(value, depth=0):
    if not isinstance(value, dict): return None
    keys = set(value) - {'within'}
    if keys == {'role', 'name'}:
        if not isinstance(value['role'], str) or not value['role'].strip() or not isinstance(value['name'], str): return None
    elif keys in ({'text'}, {'label'}, {'placeholder'}, {'css'}):
        if any(not isinstance(value[key], str) or not value[key].strip() for key in keys): return None
    else:
        return None
    result = {key: value[key] for key in keys}
    if 'within' in value:
        scope = target(value['within'], depth + 1) if depth == 0 else None
        if scope is None: return None
        result['within'] = scope
    return result

def target_matches(actual, required):
    if not isinstance(actual, dict): return False
    keys = set(required) - {'within'}
    if set(actual) - {'within'} != keys: return False
    if any(actual.get(key) != required[key] for key in keys): return False
    if 'within' in required: return actual.get('within') == required['within']
    return True


def _checkbox_predicate(clause, elements):
    match = re.fullmatch(r'(.+?) is (?:present and )?(checked|unchecked)[.]?', clause)
    if match is None:
        return None
    subject, state = match.groups()
    # A bare name requires a unique public identity within this requirement.
    candidates = [element for element in elements if subject == element.get('name') or
                  (element.get('role') == 'checkbox' and subject == (
                      json.dumps(element['name'], ensure_ascii=False) + '(role=checkbox)'))]
    if len(candidates) == 1 and candidates[0].get('role') == 'checkbox' and candidates[0].get('name'):
        return dict(op='checked', target=candidates[0], value=state == 'checked')
    return None


class _ScopedRequirements(dict):
    """Author a subset using the contracts resolved against the complete public tree."""

    def __init__(self, ir, refs):
        refs = set(refs)
        super().__init__(ir,
            work_items=[item for item in ir.get('work_items', []) if isinstance(item, dict) and
                        isinstance(item.get('id'), str) and item['id'] in refs],
            scenarios=[row for row in ir.get('scenarios', []) if isinstance(row, dict) and
                       isinstance(row.get('work_item'), str) and row['work_item'] in refs])
        self.contracts = {ref: contract for ref, contract in requirement_contracts(ir).items() if ref in refs}


def project_requirements(ir, refs):
    return _ScopedRequirements(ir, refs)


def _page_contexts(item, elements):
    return ([item['page']] if item.get('page') is not None else []) + [
        row['page'] for row in elements if row.get('source') == 'parsed' and
        row.get('work_item') == item['id'] and row.get('page') is not None]


def _page_matches(context, page):
    return isinstance(context, str) and context in (page.get('name'), page.get('path'))


def _bind_pixel_checkpoint(ir, image, scenarios, pages):
    capture = image.get('capture')
    state = capture.get('state_id') if isinstance(capture, dict) else None
    candidates = [(scenario['obligations'], index, clause)
                  for scenario in scenarios if scenario.get('inherited_from') is None
                  for index, clause in enumerate(scenario['obligations']) if clause['id'] == state]
    if len(candidates) != 1:
        raise ValueError('pixel_reference_state_not_bound')
    obligations, index, anchor = candidates[0]
    if anchor['step']['op'] != 'visible':
        raise ValueError('pixel_reference_state_marker_unavailable')
    page_path = _page_route(image.get('page'), pages)
    step = public_pixel_step(ir, image, page_path=page_path, state_id=state, target=anchor['step']['target'])
    obligations.insert(index + 1, dict(id=f"{state}:pixel:{step['value']['sha256']}",
        source=image['reference'], step=step))



def compile_public_step(phase, step):
    """Compile one supported, typed public action or assertion."""
    if not isinstance(step, dict) or not isinstance(step.get('text'), str):
        return None
    op = step.get('action' if phase == 'when' else 'kind')
    locator, value = target(step.get('target')), step.get('value')
    if not isinstance(op, str):
        return None
    if phase == 'when':
        # An address instruction cannot become an interaction with a named control
        # when the archived row and its source disagree.
        if re.match(r'(?:open|visit)\s+/', step['text'], re.I):
            return None
        if op == 'reload':
            return dict(op='reload')
        if locator and op in {'click', 'open', 'submit', 'check', 'uncheck'}:
            return dict(op='click' if op in {'open', 'submit'} else op, target=locator)
        if locator and op in {'fill', 'select', 'press'} and isinstance(value, str):
            return dict(op=op, target=locator, value=value)
    elif phase == 'then':
        if op == 'url' and isinstance(value, str) and value.startswith('/'):
            return dict(op=op, value=value)
        if not locator:
            return None
        if op in {'visible', 'hidden'}:
            return dict(op=op, target=locator)
        if op in {'text', 'contains_text', 'value'} and isinstance(value, str) and (value or op == 'value'):
            return dict(op=op, target=locator, value=value)
        if op == 'count':
            try:
                count = int(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if count > 0 and not isinstance(value, bool) and str(count) == str(value):
                return dict(op=op, target=locator, value=count)
        if op in {'tabbable', 'visual'}:
            from .platform_task_ir import _then_step
            if op == 'tabbable':
                return dict(op=op, target=locator) if step == _then_step(step['text']) else None
            try:
                canonical = normalize_visual(value)
                parsed = _then_step(step['text'])
            except (ValueError, TypeError, OverflowError) as error:
                raise ValueError('visual_invalid_exact_constraint') from error
            if (parsed.get('kind'), parsed.get('target'), parsed.get('value')) != (op, locator, canonical):
                raise ValueError('visual_source_binding_mismatch')
            return dict(op=op, target=locator, value=canonical)
    return None


def _public_clauses(text):
    """Return source slices separated by unquoted top-level sentence punctuation."""
    pairs = {'(': ')', '[': ']', '{': '}'}
    quotes = {'"': '"', "'": "'", '“': '”', '‘': '’', chr(96): chr(96)}
    stack, spans = [], []
    quote, escaped, start = None, False, 0
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in quotes:
            if char == "'" and index and text[index - 1].isalnum():
                continue  # Apostrophe inside or after a word.
            quote = quotes[char]
        elif char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values():
            if not stack or stack.pop() != char:
                return []
        elif not stack and (char == ';' or
                            (char == '.' and index + 1 < len(text) and text[index + 1].isspace())):
            # Retain a sentence's terminal period in its exact source slice.
            end = index + (char == '.')
            spans.append((start, end))
            start = index + 1
    if quote or stack:
        return []
    if text[start:].strip():
        spans.append((start, len(text)))
    result = []
    for start, end in spans:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if end == start:
            return []
        result.append((start, end))
    return result


def _bind_compound_step(phase, step, elements):
    from .platform_task_ir import _then_step, _when_step
    parse = _when_step if phase == 'when' else _then_step
    text = step['text']
    if step != parse(text):
        return []  # A contradictory archived row cannot be repaired by splitting its text.
    if phase == 'then':
        whole = _checkbox_predicate(text.strip(), elements)
        if whole is not None:
            return [(0, len(text), whole)]
    spans = _public_clauses(text)
    if len(spans) < 2:
        return []
    parts = []
    for start, end in spans:
        clause = text[start:end]
        bound = compile_public_step(phase, parse(clause))
        if bound and bound['op'] == 'url':
            try:
                public_route(bound['value'])
            except ValueError:
                bound = None
        if bound is None and phase == 'then':
            bound = _checkbox_predicate(clause, elements)
        parts.append((start, end, bound))
    return parts if any(bound is not None for _, _, bound in parts) else []


def _opening_declarations(text):
    """Index affirmative activation relations between two annotated public controls."""
    from .platform_task_ir import _ANNOTATION, _annotations
    declarations = {}
    for start, end in _public_clauses(text):
        clause = text[start:end]
        annotations = iter(_ANNOTATION.finditer(clause))
        trigger, result = next(annotations, None), next(annotations, None)
        if result is None or clause[:trigger.start()].strip().casefold() not in {'clicking', 'activating'}:
            continue
        if clause[trigger.end():result.start()].strip().casefold() != 'opens':
            continue
        tail = clause[result.end():].strip()
        if tail not in {'', '.'} and not tail.casefold().startswith(', where '):
            continue
        names = _annotations(clause[:result.end()])
        if len(names) != 2 or not all(name for name, _ in names) or names[0] == names[1]:
            continue
        destination, role = names[1]
        source = clause[:result.end()] if tail.startswith(',') else clause
        declarations.setdefault(names[0], []).append((dict(role=role, name=destination), source, (start, start + len(source))))
    return declarations


def _bind_openings(obligations, declarations, consumed):
    from .platform_task_ir import _when_step
    expanded, opened = [], {}
    for index, obligation in enumerate(obligations):
        expanded.append(obligation)
        step = obligation['step']
        locator = step.get('target', {})
        relations = declarations.get((locator.get('name'), locator.get('role')), [])
        if not relations or step['op'] != 'click' or set(locator) != {'name', 'role'}:
            continue
        if compile_public_step('when', _when_step(obligation['source'])) != step:
            continue
        destination, source, _ = relations[0]
        if any(other != destination for other, _, _ in relations):
            continue
        opened[obligation['id']] = destination
        effect = dict(op='visible', target=destination)
        if index + 1 == len(obligations) or obligations[index + 1]['step'] != effect:
            expanded.append(dict(id=obligation['id'] + ':opened', source=source, step=effect))
        consumed.update(span for _, _, span in relations)
    return expanded, opened


class _VerifiedRequirements(dict):
    """Full public IR carrying controller-validated contracts across projections."""

    def __init__(self, ir, contracts):
        super().__init__(ir)
        self.contracts = contracts


def requirement_contracts(ir):
    if isinstance(ir, (_ScopedRequirements, _VerifiedRequirements)):
        return ir.contracts
    return _compile_requirement_contracts(ir)


def compile_public_scenario_probe(contract, scenario_id, *, emit=True):
    """Compile only the independently bound diagnostic, leaving owner coverage untouched."""
    from .behavior_checks import _emit_plan, _text, _validate_case_steps

    probe = next((row for row in contract.get('diagnostic_probes', ())
                  if row['kind'] == 'public_scenario' and row['scenario_id'] == scenario_id), None)
    if probe is None:
        raise ValueError('public scenario diagnostic was not bound')
    ref = _text(contract['requirement_id'], 200)
    name = _text(scenario_id, 120)
    matching = [row for row in contract['scenarios']
                if row['id'] == scenario_id and 'inherited_from' not in row]
    if len(matching) != 1:
        raise ValueError('public scenario diagnostic is ambiguous')
    scenario = matching[0]
    entry = public_route(probe['entry_path'])
    start = [dict(op='goto', value=entry), dict(op='url', value=entry)]
    steps = [row['step'] for row in scenario['obligations']]
    if not scenario.get('exact_navigation'):
        steps = start + steps
    if steps[:2] != start or not any(
            step['op'] in {'click', 'fill', 'select', 'check', 'uncheck', 'press', 'reload', 'goto'}
            for step in steps[2:]):
        raise ValueError('public scenario has no independently bound entry and action')
    if any(step['op'] == PIXEL_OP for step in steps):
        raise ValueError('public scenario diagnostic cannot stage pixel assets')
    scoped = dict(initial_page=entry, scenarios=[scenario])
    _validate_case_steps(steps, scoped, 0)
    program = dict(steps=steps)
    if emit:
        case = dict(name=name, requirement_id=ref, steps=steps)
        program['source'] = _emit_plan(dict(cases=[case]), {ref: scoped})
    return program


def _compile_requirement_contracts(ir, *, review_ref=None, review_group=None):
    contracts = {}
    global_issues = []
    if not isinstance(ir, dict):
        ir = {}
        global_issues.append('public_ir_not_an_object')
    def records(field):
        value = ir.get(field, [])
        if not isinstance(value, list):
            global_issues.append(field + ':not_an_array')
            return []
        result = []
        for index, row in enumerate(value):
            if not isinstance(row, dict):
                global_issues.append(f'{field}[{index}]:not_an_object')
            else:
                result.append(row)
        return result
    items = records('work_items')
    public_elements = records('ui_elements')
    public_scenarios = records('scenarios')
    scenario_ids = Counter(row['id'] for row in public_scenarios if isinstance(row.get('id'), str))
    public_pages = records('public_pages' if 'public_pages' in ir else 'pages')
    pages = records('pages')
    extra_page_fields = [(index, sorted(set(page) - {'name', 'path', 'reference'}))
                         for index, page in enumerate(pages) if set(page) - {'name', 'path', 'reference'}]
    valid_items = []
    seen = set()
    for index, item in enumerate(items):
        ref = item.get('id')
        if not isinstance(ref, str) or not ref.strip():
            global_issues.append(f'work_items[{index}]:invalid_id')
        elif ref in seen:
            global_issues.append(f'work_items[{index}]:duplicate_id')
        else:
            seen.add(ref)
            valid_items.append(item)
    if not valid_items:
        # Diagnostic identity only: this cannot be considered a covered requirement.
        valid_items = [dict(id='__invalid_public_ir__', text='')]
        global_issues.append('work_items:no_valid_requirements')
    by_id = {item['id']: item for item in valid_items}
    parents = ir.get('parent_contexts') or {}
    hierarchy = {**{key: row for key, row in parents.items() if isinstance(row, dict) and row.get('id') == key},
                 **by_id} if isinstance(parents, dict) else by_id
    contexts = {ref: _page_contexts(item, public_elements) for ref, item in hierarchy.items()}
    # A declared destination can differ from a journey's initial page.
    visits = {ref: list(values) for ref, values in contexts.items()}
    for scenario in public_scenarios:
        owner = scenario.get('work_item')
        if isinstance(owner, str) and owner in visits and scenario.get('source') == 'parsed':
            steps = scenario.get('then')
            if isinstance(steps, list):
                visits[owner].extend(step['value'] for step in steps if isinstance(step, dict)
                                     and step.get('kind') == 'url' and isinstance(step.get('value'), str))
    # A reference with no identifiable owner remains an outstanding public obligation.
    page_refs_with_owners = []
    page_rows = [('pages', i, p) for i, p in enumerate(pages)]
    page_rows.extend(('public_pages', i, p) for i, p in enumerate(public_pages) if p not in pages)
    for field, index, page in page_rows:
        if page.get('reference'):
            owners = {ref for ref, values in visits.items() if any(_page_matches(v, page) for v in values)}
            for ref, item_context in hierarchy.items():
                if any(isinstance(image, dict) and (image.get('reference') == page['reference'] or
                       _page_matches(image.get('page'), page)) for image in item_context.get('references') or []):
                    owners.add(ref)
            page_refs_with_owners.append((f'{field}[{index}].reference', page, owners))

    for item in valid_items:
        ref = item['id']
        scenarios, elements, details, explicit_pixels = [], [], [], []
        unresolved = list(global_issues)
        item_source = f'work_items[{ref}]'
        def issue(reason, source, **context):
            unresolved.append(reason)
            context.setdefault('scope', 'requirement')
            details.append(dict(reason=reason, source=source, **context))

        source_references = []

        def record_reference(source, value, scope, *, supporting=False, **extra):
            metadata = dict(scope=scope, reference=value, **extra)
            inventory = ir.get('reference_availability')
            availability = inventory.get(value) if isinstance(inventory, dict) and isinstance(value, str) else None
            if availability:
                metadata['availability'] = availability
                if availability != 'readable':
                    issue('public_reference_unavailable', source, **metadata)
            if supporting:
                source_references.append(dict(source=source, **metadata))
            else:
                issue('visual_reference_pixel_oracle_unavailable', source, **metadata)

        lineage = []
        owner = item
        while owner is not None:
            if owner['id'] in lineage:
                issue('requirement_parent_cycle', item_source + '.parent')
                break
            lineage.append(owner['id'])
            parent = owner.get('parent')
            if parent is None:
                break
            if not isinstance(parent, str) or parent not in hierarchy:
                issue('public_parent_unresolved', f"work_items[{owner['id']}].parent")
                break
            owner = hierarchy[parent]

        # An explicit child page overrides the containing section's default page.
        # References declared on the section itself were retained above.
        page_owner = next((owner for owner in lineage if contexts[owner]), ref)
        page_refs = contexts[page_owner]
        visited_pages = page_refs + visits[ref]
        rows = [row for row in public_scenarios if row.get('work_item') == ref and row.get('source') == 'parsed']
        has_written_contract = isinstance(item.get('text'), str) and bool(item['text'].strip()) and bool(rows)
        for owner_id in lineage:
            owner = hierarchy[owner_id]
            for pos, image in enumerate(owner.get('references') or []):
                source = f'work_items[{owner_id}].references[{pos}]'
                if not isinstance(image, dict):
                    issue('invalid_public_reference', source)
                    continue
                image_page = image.get('page')
                if owner_id != ref and image_page:
                    matches = [page for page in public_pages if _page_matches(image_page, page)]
                    if len(matches) == 1 and visited_pages and all(any(_page_matches(v, page) for page in public_pages)
                                                                   for v in visited_pages):
                        if not any(_page_matches(v, matches[0]) for v in visited_pages):
                            continue
                    elif image_page not in page_refs:
                        issue('page_reference_ownership_ambiguous', source, scope='ancestor', owner=owner_id,
                              page=image_page)
                if owner_id == ref and 'capture' in image:
                    explicit_pixels.append((source, image))
                else:
                    # A local description illustration supplies context. Only the written
                    # obligations and an explicit capture specify what to check against it.
                    supporting = (owner_id == ref and has_written_contract and
                                  isinstance(image.get('reference'), str) and bool(image['reference'].strip()) and
                                  set(image) <= {'reference', 'page', 'id'})
                    record_reference(source, image.get('reference'), 'requirement' if owner_id == ref else 'ancestor',
                                     owner=owner_id, page=image_page, reference_id=image.get('id'),
                                     supporting=supporting, **({} if supporting else capture_gap_details(image)))
        for source, page, owners in page_refs_with_owners:
            applicable = ref in owners or page_owner in owners
            # No public context, or a repeated page name without a unique route binding.
            duplicate_name = sum(other.get('name') == page.get('name') for other in public_pages) > 1
            ambiguous = (not owners or (not page_refs and not applicable)
                         or (duplicate_name and page.get('name') in page_refs))
            if applicable or ambiguous:
                record_reference(source, page['reference'], 'page', page=page.get('name'), path=page.get('path'))
                if ambiguous:
                    issue('page_reference_ownership_ambiguous', source, scope='page', page=page.get('name'))
        for page_index, fields in extra_page_fields:
            issue('unconsumed_public_page_fields', f'pages[{page_index}]', scope='global', fields=fields)
        for rejected in ir.get('rejected') or ():
            issue('public_input_rejected_fragments', 'rejected', scope='global', clause=rejected)
        for index, element in enumerate(public_elements):
            if element.get('work_item') != ref or element.get('source') != 'parsed': continue
            t=target({key: element[key] for key in ('role','name','within') if key in element})
            if t is None: issue('ui_element_target_unresolved', f'ui_elements[{index}]')
            elif t not in elements: elements.append(t)
        routes = set()
        for page_ref in page_refs:
            try:
                routes.add(_page_route(page_ref, public_pages))
            except (ValueError, TypeError):
                issue('public_page_unresolved_or_ambiguous', item_source, page=page_ref)
        if len(routes) > 1:
            issue('multiple_public_page_contexts', item_source, routes=sorted(routes))
        initial_page = next(iter(routes)) if len(routes) == 1 else None
        # Explicit ancestor visual checks retain their original conditions and source.
        # Transplanting an assertion from a different initial route would change its meaning.
        for ancestor in lineage[1:]:
            ancestor_rows = [row for row in public_scenarios if row.get('work_item') == ancestor
                             and row.get('source') == 'parsed' and isinstance(row.get('then'), list)
                             and any(isinstance(step, dict) and step.get('kind') == 'visual' for step in row['then'])]
            if not ancestor_rows:
                continue
            if contexts[ancestor] and set(v for v in contexts[ancestor] if isinstance(v, str)) != set(
                    v for v in page_refs if isinstance(v, str)):
                issue('ancestor_visual_page_binding_unresolved', f'work_items[{ancestor}].page',
                      scope='ancestor', owner=ancestor)
            else:
                rows.extend(ancestor_rows)
        if not rows and not any(d['reason'] == 'visual_reference_pixel_oracle_unavailable' for d in details):
            issue('structured_scenario_missing', item_source + '.scenarios')
        if any(row.get('work_item')==ref and row.get('source')!='parsed' for row in public_scenarios):
            unresolved.append('unparsed_scenario_present')
        rows = [normalize_public_scenario(ir, row) for row in rows]
        linked_steps = [bind_public_step_references(ir, row) for row in rows]
        journeys = [bind_navigation(ir, item, row, elements, bindings, public_route,
                                    initial_page=initial_page, compile_step=compile_public_step)
                    if not errors else None for row, (bindings, errors) in zip(rows, linked_steps)]
        # Do not treat a parsed scenario as proof of unrelated prose clauses.
        remaining=item.get('text','')
        if not isinstance(remaining,str):
            unresolved.append('requirement_text_not_a_string')
            remaining=''
        declarations = _opening_declarations(remaining)
        statements=[]
        for journey in journeys:
            if journey:
                statements.extend(journey['consumed_description'])
        for row in rows:
            if row.get('work_item') != ref:
                continue
            for phase in ('when','then'):
                phase_steps=row.get(phase)
                if isinstance(phase_steps,list):
                    statements.extend(step.get('text','') for step in phase_steps if isinstance(step,dict))
        consumed_openings = set()
        group_probes = []
        grouped_programs = []
        scenario_candidates = []
        for index, scenario in enumerate(rows):
            sid=scenario.get('id') or str(index); obligations=[]
            issues_before_scenario = len(unresolved)
            bindings, binding_errors = linked_steps[index]
            for error in binding_errors:
                issue(**error)
            journey = journeys[index]
            group = None
            program = None
            exclusions = []
            if not journey and scenario.get('work_item') == ref and not binding_errors:
                # Separate scenario/requirement identities inside the shared scratch database.
                suffix = f'case-{valid_items.index(item) + 1}'
                try:
                    group = bind_group_readback(ir, item, scenario, suffix=suffix)
                except ValueError:
                    pass
                if group:
                    try:
                        setup = bind_group_setup(ir, item, scenario, group['members'], record_role=group['record_role'],
                                                 identity_field=group['identity_field'])
                    except ValueError:
                        setup = None
                    if setup:
                        try:
                            exclusions = bind_group_exclusions(ir, item, scenario, suffix=suffix)
                        except ValueError:
                            pass
                        program = grouped_source_program(ir, item, scenario, [hierarchy[k] for k in lineage],
                                                         group, setup, exclusions, elements,
                                                         entry_path=initial_page or '/')
                        grouped_programs.append(program)
                        for error in program['issues']:
                            issue(**error)
                        group_probes.append(dict(kind='grouped_collections', scenario_id=sid,
                                                 operations=1 + len(setup) + len(group['steps']),
                                                 workspace_precondition=group['source_clauses']['given'].partition('. ')[0],
                                                 diagnostic_only=True))
                        group_probes.extend(dict(kind='group_exclusion', scenario_id=sid, state=p['state'],
                                                 sources=p['sources'], operations=1 + len(p['steps']),
                                                 diagnostic_only=True) for p in exclusions)
                        if any(row['rule']['ability'] == 'keyboard_accessibility'
                               for row in program.get('keyboard_coverage', [])):
                            for probe in exclusions:
                                if probe['public_hover'] and probe['record_action']['role'] == 'button':
                                    group_probes.append(dict(kind='group_keyboard_activation', scenario_id=sid,
                                        state=probe['state'], action=probe['record_action'], key='Enter',
                                        sources=probe['sources'], operations=1 + len(probe['steps']),
                                        diagnostic_only=True))
            for binding in bindings:
                bound = (journey or program or {}).get('consumed_requirement_ids', ())
                if binding['kind'] == 'requirement' and binding['id'] not in bound:
                    issue('scenario_reference_semantics_unresolved', f'scenarios[{sid}].steps[{binding["step_index"]}]',
                          reference_id=binding['id'])
                if binding['kind'] == 'image' and not any(
                        isinstance(img, dict) and img.get('id') == binding['id']
                        and img.get('reference') == binding['reference']
                        for owner in lineage for img in hierarchy[owner].get('references') or []):
                    issue('scenario_image_state_unbound', f'scenarios[{sid}].steps[{binding["step_index"]}]',
                          reference_id=binding['id'])
            if journey:
                if initial_page is not None and initial_page != journey['initial_page']:
                    issue('conflicting_initial_page', f'scenarios[{sid}].given')
                if journey['seed_residual']:
                    issue('seed_fields_not_verified', f'scenarios[{sid}].given', fields=journey['seed_residual'])
                scenarios.append(dict(id=sid, exact_navigation=True, obligations=journey['obligations'],
                                      opened_targets=journey['opened_targets']))
                if len(unresolved) == issues_before_scenario:
                    scenario_candidates.append((scenario, journey['initial_page']))
                continue
            given=scenario.get('given')
            if given not in (None,{}) and not (program and program['workspace_bound']):
                if (not isinstance(given,dict) or given.get('session') not in (None,'anonymous')
                    or given.get('account') is not None or given.get('data')
                    or not isinstance(given.get('text',''),str)
                    or given.get('text','').strip().lower() not in ('','anonymous')):
                    issue(str(sid)+':unsupported_given', f'scenarios[{sid}].given',
                          clause=given.get('text') if isinstance(given, dict) else given)
            whens,thens=scenario.get('when'),scenario.get('then')
            if not isinstance(whens,list) or not isinstance(thens,list):
                issue(str(sid)+':scenario_steps_unresolved', f'scenarios[{sid}]');continue
            if not thens: issue(str(sid)+':postcondition_missing', f'scenarios[{sid}].then')
            ordered = [('when', i, step) for i, step in enumerate(whens)] + [('then', i, step) for i, step in enumerate(thens)]
            if 'ordered_steps' in scenario:
                sequence = scenario['ordered_steps']
                selected = []
                seen = {'when': [], 'then': []}
                valid = isinstance(sequence, list)
                if valid:
                    for entry in sequence:
                        if (not isinstance(entry, dict) or set(entry) != {'phase', 'index'}
                            or entry.get('phase') not in ('when', 'then')
                            or not isinstance(entry.get('index'), int) or isinstance(entry.get('index'), bool)):
                            valid = False; break
                        phase, position = entry['phase'], entry['index']
                        steps = whens if phase == 'when' else thens
                        if position < 0 or position >= len(steps): valid = False; break
                        seen[phase].append(position)
                        selected.append((phase, position, steps[position]))
                if not valid or seen != {'when': list(range(len(whens))), 'then': list(range(len(thens)))}:
                    issue(str(sid)+':ordered_steps_invalid_or_unconsumed', f'scenarios[{sid}].ordered_steps')
                    continue
                ordered = selected
            if group:
                for start, end in _public_clauses(item['text']):
                    clause = item['text'][start:end]
                    if re.search(r'\b(?:do not|never) appear in (?:either|any) (?:section|collection|group)', clause, re.I):
                        bound = any(source['work_item'] == ref and source.get('span') == [start, end]
                                    for probe in exclusions for source in probe['sources'])
                        if not bound:
                            issue('collection_excluded_states_not_verified', item_source + '.text', clause=clause,
                                  source_span=[start, end])
            for phase, position, step in ordered:
                key=f'{sid}:{phase}:{position}'
                step_source = f'scenarios[{sid}].{phase}[{position}]'
                if not isinstance(step,dict): issue(key+':invalid_step', step_source);continue
                op = step.get('action' if phase == 'when' else 'kind')
                if not isinstance(op,str):
                    issue(key+':operation_not_a_string', step_source, clause=step.get('text'));continue
                if not isinstance(step.get('text'),str):
                    issue(key+':source_text_not_a_string', step_source, clause=step.get('text'));continue
                if group is not None:
                    checks = group['inspection' if phase == 'when' else 'checks']
                    obligations.extend(dict(id=f'{key}:collection:{i}', source=step['text'], step=check)
                                       for i, check in enumerate(checks))
                    continue
                if op == 'unknown' and scenario.get('work_item') == ref:
                    clauses = _bind_compound_step(phase, step, elements)
                    if clauses:
                        for index, (start, end, check) in enumerate(clauses):
                            clause = step['text'][start:end]
                            provenance = dict(source_span=[start, end]) if len(clauses) > 1 else {}
                            identity = key if len(clauses) == 1 else f'{key}:clause:{index}'
                            if check is None:
                                issue(identity + ':unsupported_or_ambiguous', step_source, clause=clause, **provenance)
                            else:
                                obligations.append(dict(id=identity, source=clause, step=check, **provenance))
                        continue
                try:
                    compiled = compile_public_step(phase, step)
                except ValueError as error:
                    issue(key + ':' + str(error), step_source, clause=step['text'])
                    compiled = None
                if compiled is None:
                    issue(key+':unsupported_or_ambiguous', step_source, clause=step['text'])
                else: obligations.append(dict(id=key,source=step.get('text'),step=compiled))
            opened = {}
            if declarations and scenario.get('source') == 'parsed' and scenario.get('work_item') == ref:
                obligations, opened = _bind_openings(obligations, declarations, consumed_openings)
            # A single ordered scenario must fit one case, including its initial goto.
            if (opened or any('source_span' in o for o in obligations)) and len(obligations) + 1 > MAX_STEPS:
                issue('scenario_exceeds_step_limit', f'scenarios[{sid}]', operations=len(obligations))
            bound_scenario = dict(id=sid, obligations=obligations)
            if opened:
                bound_scenario['opened_targets'] = opened
            if scenario.get('work_item') != ref:
                bound_scenario['inherited_from'] = scenario['work_item']
            scenarios.append(bound_scenario)
            if group is None and len(unresolved) == issues_before_scenario:
                scenario_candidates.append((scenario, None))
        for start, end in sorted(consumed_openings, reverse=True):
            remaining = remaining[:start] + ' ' + remaining[end:]
        for statement in sorted((x for x in statements if isinstance(x,str) and x),key=len,reverse=True):
            remaining=remaining.replace(statement,' ')
        if not grouped_programs and any(c.isalnum() for c in remaining):
            issue('requirement_text_not_fully_bound', item_source + '.text', clause=remaining.strip())
        if grouped_programs and (len(grouped_programs) != 1 or len(rows) != 1 or explicit_pixels):
            issue('group_controller_companion_obligations_unbound', item_source)
        if grouped_programs:
            for reference in source_references:
                if reference.get('availability') != 'readable':
                    issue('public_reference_availability_unverified', reference['source'],
                          reference=reference['reference'])
        for source, image in explicit_pixels:
            try:
                _bind_pixel_checkpoint(ir, image, scenarios, public_pages)
            except (ValueError, TypeError) as error:
                issue(str(error) if isinstance(error, ValueError) else 'pixel_reference_capture_invalid',
                      source, reference=image.get('reference'), page=image.get('page'),
                      **capture_gap_details(image))
        unresolved = list(dict.fromkeys(unresolved))
        described = {row['reason'] for row in details}
        for reason in unresolved:
            if reason in described:
                continue
            source = reason.partition(':')[0] if reason in global_issues else item_source
            details.append(dict(reason=reason, source=source, scope='global' if reason in global_issues else 'requirement'))
        record=dict(requirement_id=ref,scenarios=scenarios,elements=elements,unresolved=unresolved)
        if grouped_programs:
            record['group_source_coverage'] = [dict(sources=p['sources'], selection_sources=p['selection_sources'],
                keyboard_coverage=p['keyboard_coverage'], annotation_evidence=p['annotation_evidence'],
                independent_obligations=p['independent_obligations'], classification_sources=p['classification_sources'],
                consumed_requirement_ids=p['consumed_requirement_ids'], workspace_bound=p['workspace_bound'],
                cases=[dict(name=c['name'], operations=len(c['steps'])) for c in p['cases']]) for p in grouped_programs]
            if not unresolved:
                record['controller_cases'] = grouped_programs[0]['cases']
        # A probe may exercise a published transition while other dependencies,
        # workspace setup, prose or visual obligations still block the requirement.
        probes = group_probes
        for row in rows:
            if row.get('work_item') != ref:
                continue
            try:
                probe = bind_autosave(ir, item, row)
            except ValueError:
                continue
            probes.append(dict(kind='dialog_autosave', scenario_id=probe['scenario_id'],
                               operations=1 + len(probe['steps']),
                               refresh=any(step['op'] == 'reload' for step in probe['steps']),
                               workspace_precondition=probe['workspace_precondition'], diagnostic_only=True))
        entry_issues = {'public_parent_unresolved', 'requirement_parent_cycle',
                        'public_page_unresolved_or_ambiguous', 'multiple_public_page_contexts'}
        if unresolved and not any(row['scope'] == 'global' or row['reason'] in entry_issues for row in details):
            existing = {probe['scenario_id'] for probe in probes}
            for raw, bound_entry in scenario_candidates:
                sid = raw.get('id')
                given = raw.get('given')
                if (raw.get('work_item') != ref or not isinstance(sid, str) or scenario_ids[sid] != 1
                        or sid in existing or not isinstance(given, dict)
                        or not isinstance(given.get('text'), str) or not given['text'].strip()
                        or not raw.get('when') or not raw.get('then')):
                    continue
                try:
                    if list(_ordered_rows(raw))[-1][0] != 'then':
                        continue
                    # The navigation binder already proved its own entry.
                    if bound_entry is None:
                        _, bound_entry, seed, _ = _entry_state(ir, given, None, public_route, initial_page)
                        if seed:
                            continue
                    probe = dict(kind='public_scenario', scenario_id=sid, entry_path=bound_entry,
                                 workspace_precondition=given['text'], diagnostic_only=True)
                    compiled = compile_public_scenario_probe(dict(record, diagnostic_probes=[probe]), sid, emit=False)
                except ValueError:
                    continue
                probes.append(dict(probe, operations=len(compiled['steps'])))
        if source_references:
            record['source_references'] = source_references
        if probes:
            record['diagnostic_probes'] = probes
        if details:
            record['issue_details'] = details
        if initial_page is not None:
            record['initial_page'] = initial_page
        if review_group is not None and ref == review_ref and len(grouped_programs) == 1:
            review_group(record, grouped_programs[0])
        record['sha256']=hashlib.sha256(json.dumps(record,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        contracts[ref]=record
    return contracts

def matches(actual,required):
    if actual.get('op')!=required['op']: return False
    if required['op'] == 'checked' and type(actual.get('value')) is not bool: return False
    if 'value' in required:
        if required['op'] == 'visual':
            try:
                if normalize_visual(actual.get('value')) != required['value']: return False
            except (ValueError, TypeError, OverflowError): return False
        elif actual.get('value')!=required['value']: return False
    if 'target' in required:
        got=actual.get('target') or {}
        # within may disambiguate layout, but CSS/text cannot replace the public role.
        if not target_matches(got,required['target']): return False
        # Exact collection size cannot be narrowed by an author-invented scope.
        if required['op'] in {'checked', 'count'} and got.get('within') != required['target'].get('within'):
            return False
    return True


def navigation_guards(scenario, steps):
    """Match every declared operation in order and locate its opening preconditions."""
    if not scenario.get('exact_navigation'):
        return None
    required = scenario['obligations']
    opened = scenario.get('opened_targets', {})
    guards = {}
    position = 0
    for index, step in enumerate(steps):
        if position < len(required) and step == required[position]['step']:
            identity = required[position]['id']
            if identity in opened:
                guards[index] = opened[identity]
            position += 1
        elif not isinstance(step, dict) or step.get('op') not in {'visible', 'url'}:
            return None
    return guards if position == len(required) else None

def scenario_guards(scenario, steps):
    """Match a case and return the pre-click checks at their actual step positions."""
    if scenario.get('exact_navigation'):
        return navigation_guards(scenario, steps)
    interactions = {'click', 'fill', 'select', 'check', 'uncheck', 'press', 'reload'}
    required = scenario['obligations']
    actions = [s for s in steps if s['op'] in interactions]
    expected = [o['step'] for o in required if o['step']['op'] in interactions]
    if not required or len(actions) != len(expected) or not all(matches(a, b) for a, b in zip(actions, expected)):
        return None
    barriers = interactions | {'goto'}
    guards, position = {}, 0
    openings = scenario.get('opened_targets', {})
    for number, obligation in enumerate(required):
        step = obligation['step']
        while position < len(steps) and not matches(steps[position], step):
            if number and steps[position]['op'] in barriers:
                return None
            position += 1
        if position == len(steps):
            return None
        if obligation['id'] in openings:
            guards[position] = openings[obligation['id']]
        position += 1
    return guards


def plan_coverage(plan,ir):
    if not isinstance(plan,dict) or not isinstance(plan.get('cases'),list):
        raise ValueError('invalid_plan_structure: cases must be an array')
    for index,case in enumerate(plan['cases']):
        if not isinstance(case,dict) or not isinstance(case.get('steps'),list):
            raise ValueError(f'invalid_plan_structure: cases[{index}] requires a steps array')
        if not isinstance(case.get('name'),str) or not isinstance(case.get('requirement_id'),str):
            raise ValueError(f'invalid_plan_structure: cases[{index}] requires string identities')
        for step in case['steps']:
            if not isinstance(step,dict) or not isinstance(step.get('op'),str):
                raise ValueError(f'invalid_plan_structure: cases[{index}] has an invalid step')
    contracts=requirement_contracts(ir);missing=[];unresolved=[];covered=[];matched={}

    for ref,contract in contracts.items():
        unresolved.extend(dict(requirement_id=ref,reason=x) for x in contract['unresolved'])
        cases=[c for c in plan.get('cases',[]) if c.get('requirement_id')==ref]
        before=len(missing);matched[ref]=[]
        if 'controller_cases' in contract:
            # JSON also distinguishes Boolean values from integer count assertions.
            expected = contract['controller_cases']
            if json.dumps(cases, sort_keys=True) != json.dumps(expected, sort_keys=True):
                missing.append(dict(requirement_id=ref, reason='source_bound_controller_cases_incomplete'))
            elif not contract['unresolved']:
                matched[ref] = [dict(scenario_id=contract['scenarios'][0]['id'], case=c['name']) for c in cases]
                covered.append(ref)
            continue
        if contract.get('initial_page') is not None:
            bound = []
            for case in cases:
                expected_url = dict(op='url', value=contract['initial_page'])
                page_bound = case['steps'][:2] == [dict(op='goto', value=contract['initial_page']), expected_url]
                for index, step in enumerate(case['steps'][2:], 2):
                    if (step.get('op') in {'click','fill','select','check','uncheck','press'}
                            or step.get('op') == 'goto' and any(
                                navigation_guards(s, case['steps']) is not None for s in contract['scenarios'])):
                        break  # User interactions may legitimately navigate to a new page.
                    if (step.get('target') is not None and
                            case['steps'][index+1:index+2] != [expected_url]):
                        page_bound = False
                if page_bound:
                    bound.append(case)
                else:
                    missing.append(dict(requirement_id=ref, case=case.get('name'), reason='public_page_not_bound'))
            cases = bound
        for scenario in contract['scenarios']:
            found=False
            for case in cases:
                if scenario.get('exact_navigation'):
                    if navigation_guards(scenario, case['steps']) is None:
                        continue
                    matched[ref].append(dict(scenario_id=scenario['id'], case=case['name']))
                    found = True
                    break
                if scenario_guards(scenario, case['steps']) is not None:
                    matched[ref].append(dict(scenario_id=scenario['id'], case=case['name']))
                    found = True
            if not found:missing.append(dict(requirement_id=ref,scenario_id=scenario['id'],reason='ordered_scenario_not_executed'))
        used_cases={m['case'] for m in matched[ref]}
        for case in cases:
            if case.get('name') not in used_cases and any(s.get('op') in {'click','fill','select','check','uncheck','press','reload'} for s in case.get('steps',[])):
                missing.append(dict(requirement_id=ref,case=case.get('name'),reason='interaction_not_bound_to_public_scenario'))
        for element in contract['elements']:
            if not any(target_matches(step.get('target'),element) and (
                    step.get('op') in {'visible','visual','tabbable','checked','click','check','uncheck','fill','select','press'}
                    or step.get('op') == 'count' and type(step.get('value')) is int and step['value'] > 0)
                    for c in cases for step in c.get('steps',[])):
                missing.append(dict(requirement_id=ref,target=element,reason='public_role_not_verified'))
        if not contract['unresolved'] and len(missing)==before: covered.append(ref)
    return dict(complete=bool(contracts) and not missing and not unresolved,covered=covered,missing=missing,unresolved=unresolved,matched=matched,contract_hashes={r:c['sha256'] for r,c in contracts.items()})

def require_coverage(plan,ir):
    result=plan_coverage(plan,ir)
    if not result['complete']:raise ValueError('requirement_constraints_incomplete: '+json.dumps(result,ensure_ascii=False,sort_keys=True))
    return result

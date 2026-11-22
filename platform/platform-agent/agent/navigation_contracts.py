"""Compile ordered public navigation clauses against their stated page and control identities."""
import json
import re

from .platform_task_ir import _when_step, _then_step
from .public_scenario_metadata import given_facts, unique_public_fields

# Public accessible identities use the same quoted name and role syntax as the task IR.
IDENTITY = r'"((?:[^"\\]|\\.)*)"\(role=([a-z]+)\)'
SEED_HEADER = '\nSeed prerequisites (prepared before this scenario):\n'


def _described_targets(text):
    targets = []
    end = 0
    for match in re.finditer(IDENTITY, text):
        separator = text[end:match.start()]
        if (not targets and separator) or (targets and separator not in (', ', ' and ', ', and ')):
            return []
        try:
            name = json.loads('"' + match[1] + '"')
        except ValueError:
            return []
        targets.append(dict(role=match[2], name=name))
        end = match.end()
    return targets if end == len(text) else []


def _collection_entry(ir, linked, trigger, region, seed):
    """Resolve a list region and its public entry roles from cited requirements."""
    public = [row for row in ir.get('ui_elements', ()) if isinstance(row, dict)
              and row.get('source') == 'parsed' and row.get('work_item') in linked]
    candidates = []
    for ref in linked:
        items = [row for row in public if row['work_item'] == ref]
        owner_text = next((row.get('text') for row in ir.get('work_items', ())
                           if isinstance(row, dict) and row.get('id') == ref), None)
        if not isinstance(owner_text, str):
            continue
        # Require both names in one public transition. A homonym in another list
        # cannot supply a locator for this journey.
        trigger_clauses = set()
        for row in items:
            evidence = row.get('evidence')
            if (not isinstance(evidence, str) or evidence not in owner_text
                    or {k: row[k] for k in ('role', 'name', 'within') if k in row} != trigger):
                continue
            transition = re.fullmatch(r'Activating (.+?)(?: on the page)? displays (.+)\.', evidence)
            if (transition and _described_targets(transition[1]) == [trigger]
                    and _described_targets(transition[2]) == [dict(role='region', name=region)]):
                trigger_clauses.add(evidence)
        if not any(row.get('name') == region and row.get('role') == 'region'
                   and row.get('evidence') in trigger_clauses for row in items):
            continue
        for entry in items:
            role, name, evidence = entry.get('role'), entry.get('name'), entry.get('evidence')
            if role not in ('link', 'button') or not isinstance(name, str) or not re.fullmatch(r'\{[^{}]+ name\}', name):
                continue
            if not isinstance(evidence, str) or evidence not in owner_text:
                continue
            entry_rule = re.fullmatch(
                r'Each ([\w -]+?) appears exactly once as an? (keyboard-accessible )?'
                + re.escape(json.dumps(name, ensure_ascii=False) + '(role=' + role + ')')
                + r' identified by its business name(?:; (?:a|an) (?:semantically )?equivalent '
                  r'(button|link) is (?:also )?(?:permitted|allowed))?\.', evidence)
            if not entry_rule or region.casefold() != (entry_rule[1] + ' list').casefold():
                continue
            roles = {role}
            if entry_rule[3]:
                roles.add(entry_rule[3])
            target = dict(name=seed, within=dict(role='region', name=region))
            if len(roles) == 1:
                target['role'] = role
            else:
                target['roles'] = sorted(roles)
            candidates.append((target, 'tabbable' if entry_rule[2] else 'visible', ref))
    return candidates[0] if len(candidates) == 1 else None


def _ordered_rows(scenario):
    phases = {phase: scenario.get(phase) for phase in ('when', 'then')}
    if not all(isinstance(rows, list) for rows in phases.values()) or not phases['then']:
        raise ValueError('missing scenario steps')
    default = [dict(phase=p, index=i) for p, rows in phases.items() for i in range(len(rows))]
    sequence = scenario.get('ordered_steps', default)
    if not isinstance(sequence, list):
        raise ValueError('invalid order')
    positions = dict(when=0, then=0)
    for entry in sequence:
        if not isinstance(entry, dict) or set(entry) != {'phase', 'index'}:
            raise ValueError('invalid order')
        phase, i = entry['phase'], entry['index']
        if phase not in ('when', 'then') or type(i) is not int or i != positions[phase] or i >= len(phases[phase]):
            raise ValueError('invalid order')
        row = phases[phase][i]
        parser = _when_step if phase == 'when' else _then_step
        if not isinstance(row, dict) or not isinstance(row.get('text'), str) or row != parser(row['text']):
            raise ValueError('step disagrees with public source')
        positions[phase] += 1
        yield phase, row
    if any(positions[p] != len(rows) for p, rows in phases.items()):
        raise ValueError('unconsumed steps')


def _return_description(text, elements):
    match = re.fullmatch(r'(?:Opening|Visiting) (\S+) from the ([^,\n]+) returns to the ([^,\n]+), '
                         r'which (?:exposes|displays|shows) (.+)\.', text)
    if match:
        targets = _described_targets(match[4])
        if targets and all(target in elements for target in targets):
            return dict(route=match[1], origin=match[2], destination=match[3], targets=targets, source=text)
    return None


def _entry_state(ir, given, description, public_route, initial_page):
    if (not isinstance(given, dict) or set(given_facts(given)) != {'session', 'account', 'data', 'text'}
            or given['account'] is not None or given['data'] != [] or not isinstance(given['text'], str)):
        raise ValueError('unsupported Given')
    text, marker, payload = given['text'].partition(SEED_HEADER)
    condition = re.fullmatch(r'The (anonymous )?(.+?) is open(?: and the seeded (.+?) is available in (.+?))?\.', text)
    if condition is None and text.lower() not in ('', 'anonymous'):
        raise ValueError('unconsumed Given')
    anonymous = text.lower() == 'anonymous' or (condition and condition[1])
    if given['session'] != ('anonymous' if anonymous else 'unknown'):
        raise ValueError('session disagrees with public source')
    route = initial_page
    if condition:
        label = condition[2]
        direct = re.fullmatch(r'(?:page at )?(/[^\s]*)(?: page)?', label)
        if direct:
            route = direct[1]
        else:
            matches = [p.get('path') for p in ir.get('pages', ()) if isinstance(p, dict) and p.get('name') == label]
            if description and label == description['destination']:
                if matches and matches != [description['route']]:
                    raise ValueError('conflicting public page identity')
                route = description['route']
            elif len(matches) == 1:
                route = matches[0]
            else:
                raise ValueError('entry page name unbound')
    if initial_page is not None and route != initial_page:
        raise ValueError('conflicting entry page')
    route = public_route(route)
    record = None
    if marker or (condition and condition[3]):
        seed_line = re.fullmatch(r'- [^:\n]+: (\{[^\n]+\})', payload)
        if not seed_line or not condition or not condition[3]:
            raise ValueError('unbound public seed declaration')
        record = json.loads(seed_line[1], object_pairs_hook=unique_public_fields)
        if not isinstance(record, dict) or record.get('name') != condition[3]:
            raise ValueError('seed name does not match condition')
    return text, route, record, condition[4] if condition else None


def _action_clauses(text):
    # Attempt the whole typed operation first. Its quoted names and literals may contain commas.
    start = 0
    for separator in re.finditer(r'"(?:\\.|[^"\\])*"|, (?:then )?', text, re.I):
        if separator[0].startswith(','):
            yield text[start:separator.start()]
            start = separator.end()
    yield text[start:]


def _identity(subject, identities):
    annotated = _described_targets(subject)
    if len(annotated) == 1:
        return annotated[0]
    candidates = [target for target in identities if target.get('name') == subject]
    if len(candidates) != 1:
        raise ValueError('ambiguous or undeclared accessible name')
    return candidates[0]


def bind_navigation(ir, item, scenario, elements, bindings, public_route, *, initial_page=None, compile_step=None):
    """Bind every scenario clause or defer to ordinary diagnostics. Descriptions are consumed separately."""
    if (not isinstance(scenario.get('id'), str) or not scenario['id'].strip()
            or scenario.get('source') != 'parsed' or scenario.get('work_item') != item.get('id')
            or not isinstance(item.get('text', ''), str)):
        return None
    if compile_step is None:
        from .behavior_contracts import compile_public_step
        compile_step = compile_public_step
    description = _return_description(item.get('text', ''), elements)
    linked = {b['id'] for b in bindings if b['kind'] == 'requirement' and b['step_index'] == 0}
    identities = list(elements)
    public_elements = ir.get('ui_elements', [])
    if not isinstance(public_elements, list):
        return None
    for row in public_elements if linked else ():
        if (isinstance(row, dict) and row.get('source') == 'parsed'
                and isinstance(row.get('work_item'), str) and row['work_item'] in linked):
            locator = {k: row[k] for k in ('role', 'name', 'within') if k in row}
            if {'role', 'name'} <= locator.keys() and locator not in identities:
                identities.append(locator)
    obligations, opened, consumed_refs, consumed_description = [], {}, [], []

    def add(source, step):
        identity = f"{scenario['id']}:navigation:{len(obligations)}"
        obligations.append(dict(id=identity, source=source, step=step))

    def visit(source, route):
        for op in ('goto', 'url'):
            add(source, dict(op=op, value=route))

    try:
        before, route, seed, seed_region = _entry_state(ir, scenario.get('given'), description, public_route, initial_page)
        visit(before or item.get('page') or route, route)
        special = before.lower() not in ('', 'anonymous')
        returned = None
        transition = None
        for phase, row in _ordered_rows(scenario):
            source = row['text']
            compiler = _when_step if phase == 'when' else _then_step
            clauses = list(_action_clauses(source)) if phase == 'when' and row['action'] == 'unknown' else [source]
            for clause in clauses:
                parsed = row if clause == source else compiler(clause)
                compiled = compile_step(phase, parsed)
                if compiled:
                    if compiled['op'] == 'url':
                        public_route(compiled['value'])
                    add(source, compiled)
                    if phase == 'when':
                        transition = returned = None
                    continue
                clause = clause.removesuffix('.')
                click = re.fullmatch(r'(?:click|activate) (.+)', clause, re.I) if phase == 'when' else None
                if click:
                    control = _identity(click[1], identities)
                    add(source, dict(op='click', target=control))
                    transition = returned = None
                    special = True
                    continue
                check = re.fullmatch(r'(?:(?:verify|confirm) )?(.+?) (opens|is visible)', clause, re.I)
                if check and obligations[-1]['step']['op'] == 'click':
                    previous = obligations[-1]
                    control = previous['step']['target']
                    target = _identity(check[1], identities)
                    add(source, dict(op='visible', target=target))
                    if check[2].lower() == 'opens':
                        opened[previous['id']] = target
                    transition = control, target
                    if seed and target == dict(name=seed_region, role='region') and not consumed_refs:
                        entry = _collection_entry(ir, linked, control, seed_region, seed['name'])
                        if not entry:
                            raise ValueError('seed location is unbound')
                        entry_target, entry_op, ref = entry
                        add(before, dict(op=entry_op, target=entry_target))
                        consumed_refs.append(ref)
                    special = True
                    continue
                path = re.fullmatch(r'(?:open|visit) (/\S*)(?: to return to the (.+))?', clause, re.I) if phase == 'when' else None
                if path:
                    destination = public_route(path[1])
                    returned = None
                    from_names = set()
                    if transition:
                        opener, view = transition
                        from_names.add(view.get('name', '').casefold())
                        if view.get('role') == 'region' and view.get('name', '').casefold().endswith(' list'):
                            from_names.add((opener.get('name', '') + ' list').casefold())
                    matches_return = (description and description['route'] == destination
                                      and description['origin'].casefold() in from_names)
                    if path[2] and (not matches_return or description['destination'] != path[2]):
                        raise ValueError('return destination or origin unbound')
                    visit(source, destination)
                    if matches_return:
                        visible_by_identity = {}
                        for target in description['targets']:
                            add(description['source'], dict(op='visible', target=target))
                            visible_by_identity[(target['name'], target['role'])] = obligations[-1]
                        add(description['source'], dict(op='url', value=destination))
                        returned = visible_by_identity, len(obligations)
                        if description['source'] not in consumed_description:
                            consumed_description.append(description['source'])
                    transition = None
                    special = True
                    continue
                result = re.fullmatch(r'The (.+?) returns with its (.+) ([a-z]+) visible', clause) if phase == 'then' else None
                if result and returned is not None and result[1] == description['destination']:
                    visible_by_identity, checkpoint = returned
                    visible = visible_by_identity.get((result[2], result[3]))
                    if visible is None:
                        raise ValueError('return assertion unbound')
                    if checkpoint == len(obligations):
                        visible['source'] = source
                    else:
                        add(source, visible['step'])
                    returned = visible_by_identity, None
                    continue
                raise ValueError('unconsumed navigation clause')
        if not special or (seed and not consumed_refs):
            return None
    except (ValueError, TypeError):
        return None
    return dict(obligations=obligations, initial_page=route, seed_residual=sorted(set(seed or {}) - {'name'}),
                consumed_requirement_ids=consumed_refs, consumed_description=consumed_description, opened_targets=opened)

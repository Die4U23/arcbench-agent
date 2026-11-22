"""Bind public example names to the run and Playwright worker/case that exercise them."""
import json
import re

from .grouped_collections import _inspect, regular_plural
from .platform_task_ir import _annotations


def _title_clause(text, entity, field):
    words = re.findall(r"[\w]+(?:-[\w]+)*|[^\w\s]", text.casefold().removesuffix('.'))
    def take(*phrases):
        for phrase in phrases:
            tokens = phrase.split()
            if words[:len(tokens)] == tokens:
                del words[:len(tokens)]
                return True
        return False
    take('the')
    if not take(entity.casefold()+' '+regular_plural(field), regular_plural(field)+' of '+regular_plural(entity)):
        return None
    if take('accept','allow'):
        operation = 'accept'
    elif take('retain','preserve','keep'):
        operation = 'retain'
    else:
        return None
    take('the','a','an')
    names = (field.casefold(),'name')
    if take('source example','scenario example','scenario-supplied example','scenario-supplied'):
        if not take(*names):
            return None
    elif not (take('example') and take(*names) and take('from the scenario','from the source')):
        return None
    if not take('plus','with','followed by'):
        return None
    take('a','the')
    if not take('deterministic'):
        return None
    after = take('suffix')
    if after and not take('for','from','identifying'):
        return None
    dimensions = set()
    while words:
        take('the')
        if not words or words[0] not in {'run','worker','case'} or words[0] in dimensions:
            return None
        dimensions.add(words.pop(0))
        if not take('/',', and',',','and'):
            break
        if not words:
            return None
    if not after and not take('suffix'):
        return None
    return operation if not words and dimensions == {'run','worker','case'} else None


def title_sources(lineage, group):
    from .behavior_contracts import _public_clauses
    _, entity, field = _inspect(group['source_clauses']['when'])
    sources = {}
    for item in lineage:
        text = item.get('text')
        if not isinstance(text, str):
            continue
        for start, stop in _public_clauses(text):
            quoted = text[start:stop]
            op = _title_clause(quoted,entity,field)
            if op:
                sources[item['id'], (start, stop), quoted] = op
    return sources


def fixture_bindings(ir, group, members):
    field = group['identity_field']
    token = '{' + field + '}'
    declared = {(name,role) for item in ir['work_items'] if isinstance(item.get('text'),str)
                for name,role in _annotations(item['text'])}
    examples = {m['title']:m['base'] for m in members}
    actions = {}
    for title in examples:
        labels = {}
        for template, role in sorted(declared):
            if role not in ('button','menuitem','link') or template.count(token) != 1:
                continue
            label = template.replace(token,title)
            if (label,role) not in declared:
                labels.setdefault(role, []).append(label)
        actions[title] = labels
    return dict(field=field, role=group['record_role'], examples=examples, actions=actions)


def title_evidence(case, group, *, setup_start, setup_count, readback_count):
    """A named public input commits, then the exact same title is returned in its owned record."""
    steps = case['steps']
    boundary = setup_start + setup_count
    reads = steps[boundary:boundary + readback_count]
    scopes = {step['target']['name']:step['target'] for step in group['inspection']}
    witnesses = []
    for member in group['members']:
        title = member['title']
        fills = [(i, s['target']) for i, s in enumerate(steps[setup_start:boundary], setup_start)
                 if s['op'] == 'fill' and s['value'] == title and s['target'].get('role') == 'textbox'
                 and s['target']['name'].casefold() == group['identity_field'].casefold()]
        if len(fills) != 1:
            return []
        filled, target = fills[0]
        modal = target.get('within')
        if not modal or modal.get('role') != 'dialog':
            return []
        closed = next((i for i in range(filled+1, boundary) if steps[i] == dict(op='hidden',target=modal)), None)
        if (closed is None or steps[closed-1].get('op') != 'click'
                or steps[closed-1]['target'].get('within') != modal
                or any(s['op']=='fill' and s['target']==target for s in steps[filled+1:closed])):
            return []
        record = dict(name=title, role=group['record_role'], within=scopes[member['group']])
        required = dict(op='record_fields',target=record,value=[title,member['content']])
        observed = [boundary + i for i, s in enumerate(reads) if s == required]
        if len(observed) != 1:
            return []
        witnesses.append(dict(example=member['base'], reference=title, fill_step=filled, commit_step=closed-1,
                              saved_record_step=observed[0], case=case['name'], requirement_id=case['requirement_id']))
    return witnesses


def review_title_sources(program, record, review):
    """Resolve each original title citation against the exact program named in the receipt."""
    base = program['cases'][0]
    start = program['readback_start']
    def resolve(source, cases, name):
        matching = [c for c in cases if c['name'] == base['name'] and c['requirement_id'] == base['requirement_id']
                    and c.get('fixture_titles') == base.get('fixture_titles') and c['steps'][:start] == base['steps'][:start]]
        steps = matching[0]['steps'] if len(matching) == 1 else []
        witnesses = []
        for original in source['witnesses']:
            observed = original['saved_record_step']
            expected = base['steps'][observed]
            # Strict cases retain the mandatory suffix, even if the proposal
            # also requested this exact operation. Diagnostics contain only
            # the selected checks, in the author's order.
            if name == 'controller_cases':
                shifted = observed + len(steps) - len(base['steps'])
                found = shifted if start <= shifted < len(steps) and steps[shifted] == expected else None
            else:
                found = next((i for i in range(start,len(steps)) if steps[i] == expected), None)
            witness = dict(original, saved_record_step=found)
            if not steps:
                witness.update(fill_step=None, commit_step=None)
            witnesses.append(witness)
        return dict(source, program=name, witnesses=witnesses,
                    planned=all(w['saved_record_step'] is not None for w in witnesses))

    sources = [s for s in program['sources'] if s.get('program') == 'base_mandatory_cases' and 'witnesses' in s]
    if sources:
        review['validated_title_sources'] = [resolve(s, review['validated_cases'], 'validated_cases') for s in sources]
        if review['complete']:
            strict = [resolve(s, record['controller_cases'], 'controller_cases') for s in sources]
            review['strict_title_sources'] = strict
            coverage, = record['group_source_coverage']
            by_source = {(s['work_item'],tuple(s['span'])):s for s in strict}
            coverage['sources'] = [by_source.get((s['work_item'],tuple(s.get('span',()))),s)
                                   for s in coverage['sources']]


def runtime_setup(binding):
    return [
        'const fixtureRun = process.env.ARC_FIXTURE_RUN_ID;',
        'if (!fixtureRun || !testInfo || !Number.isInteger(testInfo.workerIndex) || testInfo.workerIndex < 0 '
        '|| typeof testInfo.testId !== "string" || !testInfo.testId) '
        'throw new Error("Missing process.env.ARC_FIXTURE_RUN_ID or Playwright worker/case identity");',
        'const fixtureSuffix = fixtureRun + "-w" + testInfo.workerIndex + "-" + testInfo.testId;',
        'const fixtureTitles = Object.fromEntries(Object.entries(' + json.dumps(binding['examples']) +
        ').map(([reference,base]) => [reference,base+" "+fixtureSuffix+"-"+reference.slice(base.length+1)]));'
    ]


def _name_expr(name, title):
    if name == title:
        return 'fixtureTitles[' + json.dumps(title) + ']'
    if title and name.count(title) == 1:
        before, after = name.split(title)
        return json.dumps(before) + '+fixtureTitles[' + json.dumps(title) + ']+' + json.dumps(after)
    return json.dumps(name)


def locator_name(target, binding):
    name = target['name']
    if binding:
        if target.get('role') == binding['role'] and name in binding['examples']:
            return _name_expr(name, name)
        owner = target.get('within', {})
        if (owner.get('role') == binding['role'] and
                name in binding['actions'].get(owner.get('name'), {}).get(target.get('role'), ())):
            return _name_expr(name, owner['name'])
    return json.dumps(name)


def value_expr(step, binding):
    value = step.get('value')
    if binding:
        target = step.get('target', {})
        if (step['op'] == 'fill' and target.get('role') == 'textbox'
                and target['name'].casefold() == binding['field'].casefold() and value in binding['examples']):
            return _name_expr(value, value)
        title = target.get('name')
        if target.get('role') == binding['role'] and title in binding['examples']:
            if step['op'] == 'record_fields' and value and value[0] == title:
                return '[' + ','.join([_name_expr(title, title), *map(json.dumps, value[1:])]) + ']'
            if step['op'] == 'hover_action_inventory':
                from .shared_public_checks import inventory_value
                checked = inventory_value(value)
                witnesses = ['{role:'+json.dumps(w['role'])+',name:'+locator_name(dict(w,within=target),binding)+'}'
                             for w in checked['witnesses']]
                return '{action_kind:'+json.dumps(checked['action_kind'])+',witnesses:['+','.join(witnesses)+']}'
    return json.dumps(value)

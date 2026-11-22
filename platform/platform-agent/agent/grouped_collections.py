"""Bind exclusive collection membership to public role, identity and cardinality declarations."""
import re

from .platform_task_ir import _ANNOTATION, _annotations, _then_step, _when_step
from .public_scenario_metadata import bind_public_step_references, normalize_public_scenario


def _require(condition):
    if not condition:
        raise ValueError('group_source_unbound')


def _single(rows):
    rows = list(rows)
    _require(len(rows) == 1)
    return rows[0]


def _phase(scenario, phase, parser):
    rows = scenario.get(phase)
    _require(isinstance(rows, list))
    row = _single(rows)
    _require(isinstance(row, dict) and isinstance(row.get('text'), str) and row == parser(row['text']))
    return row['text']


def _inspect(text):
    controls = _annotations(text)
    parts = re.split(r" (?:using|by) each ", _ANNOTATION.sub('@', text.rstrip('.')), flags=re.I)
    _require(len(parts) == 2)
    scopes, identity = parts
    verb, _, objects = scopes.partition(' ')
    _require(verb.casefold() in {'inspect', 'examine', 'observe'})
    objects = objects.removeprefix('the ').removesuffix(' collections')
    _require(re.fullmatch(r'@(?:(?:, |,? and )@)+', objects))
    _require(objects.count('@') == len(controls))
    entity, separator, field = identity.partition("'s ")
    field = field.removeprefix('full ').removeprefix('isolated ')
    _require(separator and entity.isidentifier() and field.isidentifier())
    groups = dict(controls)
    _require(len(groups) == len(controls) and all(groups) and all(groups.values()))
    return groups, entity, field


def regular_plural(entity):
    noun = entity.casefold()
    if noun.endswith('y') and len(noun) > 1 and noun[-2] not in 'aeiou':
        return noun[:-1] + 'ies'
    return noun + ('es' if noun.endswith(('s', 'sh', 'ch', 'x', 'z')) else 's')


def _assignment_facts(text, groups, entity):
    from .behavior_contracts import _public_clauses
    assigned = {}
    facts = {'cardinality': [], 'exclusive': [], 'readback': [], 'members': {}}
    pair_quantifier = False
    # A membership assertion ends at a declared group name. The remaining
    # conjunction may introduce another member, a visibility check or an exclusion.
    quantifier = re.compile(r' (?:appears? |occurs? |is present )?(?:exactly once|once) only in ', re.I)
    for start, end in _public_clauses(text):
        clause = text[start:end].removesuffix('.')
        while clause:
            folded = clause.casefold()
            if folded.startswith(('each collection ', 'each group ')):
                _, _, predicate = folded.split(' ', 2)
                _require(predicate in {'exists once', 'exists exactly once', 'appears once', 'appears exactly once'})
                facts['cardinality'].append((start, start + len(clause)))
                break
            if folded.startswith(('neither ', 'no ')):
                predicate, conjunction, remainder = clause.partition(' and ')
                concept = predicate.casefold().split(' ', 1)[1]
                subject, separator, domain = concept.partition(' is duplicated across ')
                _require(separator and subject == entity.casefold() and domain in {'sections', 'collections', 'groups'})
                _require(not conjunction or remainder)
                pair_quantifier |= folded.startswith('neither ')
                facts['exclusive'].append((start, start + len(predicate)))
                start += len(predicate) + len(conjunction)
                clause = remainder
                continue
            if folded.startswith('each '):
                state, sep, payload = folded[5:].partition(' with ')
                _require(state in {'is visible', 'remains visible'} and sep and
                         payload in {'its saved content', 'its entered content', 'its saved text'})
                facts['readback'].append((start, start + len(clause)))
                break
            match = quantifier.search(clause)
            _require(match)
            base, tail = clause[:match.start()], clause[match.end():]
            _require(base and base == base.strip() and base not in assigned)
            group = _single(name for name in groups if tail == name or tail.startswith(name + ' and '))
            assigned[base] = group
            stop = start + match.end() + len(group)
            facts['members'][base] = (start, stop)
            clause = tail[len(group):].removeprefix(' and ')
            start = stop + len(' and ')
    _require(facts['cardinality'] and facts['readback'] and set(assigned.values()) == set(groups)
             and (not pair_quantifier or len(assigned) == 2))
    return assigned, facts


def split_next_member(text, entity):
    # Commas belong to the field value unless the next phrase introduces another record.
    boundary = re.search(r',\s*(?:and\s+)?(?=[^,]*?\b' + re.escape(entity) + r' with )', text)
    if boundary:
        return text[:boundary.start()], text[boundary.end():]
    return text, ''


def _member_values(given, assignments, entity, suffix):
    given = given.removesuffix('.')
    members = []
    _require(len(re.findall(r'\b' + re.escape(entity) + r' with ', given)) == len(assignments))
    for base, group in assignments.items():
        marker = f' {base} {entity} with '
        _require(given.count(marker) == 1)
        value, _ = split_next_member(given.split(marker)[1], entity)
        if value.endswith(' selected'):
            value, separator, selection = value[:-len(' selected')].rpartition(' and ')
            _require(separator and selection)
        _require(value and value == value.strip())
        members.append(dict(base=base, title=f'{base} {suffix}', content=value, group=group))
    return members


def bind_group_readback(ir, item, scenario, *, suffix='arc', variant=''):
    scenario = normalize_public_scenario(ir, scenario)
    _require(scenario.get('source') == 'parsed' and scenario.get('work_item') == item.get('id'))
    _require(isinstance(scenario.get('id'), str) and isinstance(item.get('text'), str))
    _require(not bind_public_step_references(ir, scenario)[1])
    _require(scenario.get('ordered_steps') == [dict(phase='when', index=0), dict(phase='then', index=0)])
    when = _phase(scenario, 'when', _when_step)
    then = _phase(scenario, 'then', _then_step)
    groups, entity, field = _inspect(when)
    declarations = _annotations(item['text'])
    _require(all((name, role) in declarations for name, role in groups.items()))
    record_role = _single(role for name, role in declarations if name == '{' + field + '}')
    assignments, _ = _assignment_facts(then, groups, entity)
    given = scenario.get('given')
    _require(isinstance(given, dict) and isinstance(given.get('text'), str))
    _require(isinstance(suffix, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', suffix))
    ids = [row.get('id') for row in ir.get('scenarios', []) if isinstance(row, dict) and row.get('work_item') == item['id']]
    _require(ids.count(scenario['id']) == 1)
    _require(isinstance(variant, str) and re.fullmatch(r'(?:-[0-9]+(?:-move)?)?', variant))
    fixture_suffix = f"{suffix}{variant}-{ids.index(scenario['id']) + 1}"
    members = _member_values(given['text'], assignments, entity, fixture_suffix)
    scopes = {name:dict(name=name, role=role) for name, role in groups.items()}
    inspection = [dict(op='visible', target=scope) for scope in scopes.values()]
    checks = [dict(op='count', target=scope, value=1) for scope in scopes.values()]
    for member in members:
        target = dict(role=record_role, name=member['title'])
        checks.append(dict(op='count', target=target, value=1))
        for group, scope in scopes.items():
            owned = dict(target, within=scope)
            checks.append(dict(op='count', target=owned, value=int(member['group'] == group)))
        owned = dict(target, within=scopes[member['group']])
        checks.extend([dict(op='visible', target=owned),
                       dict(op='record_fields', target=owned, value=[member['title'], member['content']])])
    return dict(requirement_id=item['id'], scenario_id=scenario['id'], members=members, record_role=record_role,
                identity_field=field, fixture_suffix=fixture_suffix,
                inspection=inspection, checks=checks, steps=inspection + checks,
                source_clauses=dict(given=given['text'], when=when, then=then), diagnostic_only=True)


def compile_group_probe(ir, ref, scenario_id, entry_path='/', *, suffix='arc'):
    """Exercise the published setup and observe the isolated members at the application entry."""
    from .behavior_contracts import public_route
    from .behavior_checks import _emit_plan
    from .group_setup import bind_group_setup
    from .group_fixture_titles import fixture_bindings, title_evidence, title_sources
    from .shared_public_checks import _lineage
    item = _single(row for row in ir['work_items'] if row['id'] == ref)
    scenario = _single(row for row in ir['scenarios'] if row['id'] == scenario_id and row['work_item'] == ref)
    scenario = normalize_public_scenario(ir, scenario)
    probe = bind_group_readback(ir, item, scenario, suffix=suffix)
    setup = bind_group_setup(ir, item, scenario, probe['members'], record_role=probe['record_role'],
                             identity_field=probe['identity_field'])
    steps = [dict(op='goto', value=public_route(entry_path)), *setup, *probe['steps']]
    case = dict(name=scenario_id, requirement_id=ref, steps=steps)
    if title_sources(_lineage(ir, item), probe):
        case['fixture_titles'] = fixture_bindings(ir, probe, probe['members'])
        probe['title_witnesses'] = title_evidence(case, probe, setup_start=1, setup_count=len(setup),
                                                  readback_count=len(probe['steps']),
                                                  controller_binding=case['fixture_titles'])
    probe.update(steps=steps, source=_emit_plan(dict(cases=[case]), {ref:dict(scenarios=[])}))
    return probe

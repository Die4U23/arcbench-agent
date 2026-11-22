"""Derive the finite public Given/When/Then denominator independently of any check plan."""
import json

from .group_classification import _classification
from .group_setup import bind_group_setup
from .group_saved_state import saved_record_state
from .grouped_collections import _assignment_facts, _inspect, _member_values, _phase
from .platform_task_ir import _ANNOTATION, _annotations, _then_step, _when_step
from .public_scenario_metadata import bind_public_step_references, normalize_public_scenario


def derive_group_obligations(ir, item, scenario, group, setup):
    scenario = normalize_public_scenario(ir, scenario)
    ref, sid = item['id'], scenario['id']
    if (scenario.get('work_item') != ref or scenario.get('source') != 'parsed'
            or scenario.get('ordered_steps') != [dict(phase='when', index=0), dict(phase='then', index=0)]
            or bind_public_step_references(ir, scenario)[1]):
        raise ValueError('group_obligation_scenario_unbound')
    when = _phase(scenario, 'when', _when_step)
    then = _phase(scenario, 'then', _then_step)
    given = scenario['given']['text']
    groups, entity, field = _inspect(when)
    assigned, facts = _assignment_facts(then, groups, entity)
    placeholder = '{' + field + '}'
    roles = {role for name, role in _annotations(item['text']) if name == placeholder}
    if len(roles) != 1:
        raise ValueError('group_obligation_record_role_unbound')
    role, = roles
    if (group.get('requirement_id') != ref or group.get('scenario_id') != sid or group.get('record_role') != role
            or group.get('identity_field') != field or not isinstance(group.get('fixture_suffix'), str)):
        raise ValueError('group_obligation_fixture_identity_unbound')
    # The controller chooses the fixture namespace. Concrete values must still
    # agree with the public example and the actual, source-compiled preparation.
    members = _member_values(given, assigned, entity, group['fixture_suffix'])
    origins = {}
    prepared = bind_group_setup(ir, item, scenario, members, record_role=role, identity_field=field,
                                member_sources=origins)
    if (members != group['members'] or
            json.dumps(prepared, sort_keys=True) != json.dumps(setup, sort_keys=True)):
        raise ValueError('group_obligation_preparation_mismatch')

    texts = dict(given=given, when=when, then=then, text=item['text'])
    def source(phase, span):
        start, end = span
        return dict(owner=ref, scenario_id=sid, phase=phase, span=[start, end], quote=texts[phase][start:end],
                    **({'index': 0} if phase in ('when', 'then') else {}))

    duties = []
    def add(identity, kind, src, inputs, expected, **metadata):
        duties.append(dict(id=identity, kind=kind, requirement_id=ref, scenario_id=sid, case=sid,
                           state='group_readback', source=src, inputs=inputs, expected=expected, **metadata))

    scopes = {name:dict(role=kind, name=name) for name, kind in groups.items()}
    locations = {pair[0]:source('when', match.span()) for match in _ANNOTATION.finditer(when)
                 for pair in _annotations(match[0])}
    role_sources = [source('text', match.span()) for match in _ANNOTATION.finditer(item['text'])
                    if _annotations(match[0]) == [(placeholder, role)]]
    inspection = source('when', (0, len(when)))
    group_counts = [source('then', span) for span in facts['cardinality']]
    for gi, (name, target) in enumerate(scopes.items(), 1):
        add(f'group-visible-{gi}', 'inspected_collection', inspection, [locations[name]], dict(op='visible', target=target))
        add(f'group-count-{gi}', 'collection_cardinality', group_counts[0], [locations[name], *group_counts[1:]],
            dict(op='count', target=target, value=1))

    visibility = [source('then', span) for span in facts['readback']]
    exclusive = [source('then', span) for span in facts['exclusive']]
    from .behavior_contracts import _public_clauses
    workspace = given.partition(' is open.')[0]
    classifications = [relation for start,end in _public_clauses(item['text'])
                       if (relation := _classification(item['text'][start:end],workspace,entity,groups))]
    categories = classifications[0] if len(classifications) == 1 else {}
    for mi, member in enumerate(members, 1):
        origin = origins[member['base']]
        preparation = source('given', origin['span'])
        membership = source('then', facts['members'][member['base']])
        condition = dict(member=member['base'], identity=member['title'], content=member['content'],
                         assigned_group=member['group'], qualifier=origin['qualifier'], selection=origin['selection'])
        target = dict(role=role, name=member['title'])
        inputs = [preparation, *role_sources]
        add(f'record-count-{mi}', 'exclusive_record_identity', membership, [*inputs, *exclusive],
            dict(op='count', target=target, value=1), condition=condition)
        for gi, (tested, scope) in enumerate(scopes.items(), 1):
            add(f'membership-{mi}-{gi}', 'conditional_collection_membership', membership,
                [*inputs, locations[tested]], dict(op='count', target=dict(target, within=scope),
                                                    value=int(assigned[member['base']] == tested)),
                condition=condition, tested_group=tested)
        owned = dict(target, within=scopes[member['group']])
        support = [*inputs, membership, *visibility[1:]]
        add(f'record-visible-{mi}', 'visible_scoped_record', visibility[0], support,
            dict(op='visible', target=owned), condition=condition)
        add(f'record-fields-{mi}', 'owned_record_fields', visibility[0], support,
            dict(op='record_fields', target=owned, value=[member['title'], member['content']]), condition=condition)
        alternatives = {origins[other['base']]['qualifier'] for other in members if other['group'] != member['group']
                        and not origins[other['base']]['selection']}
        state = None
        if (len(categories) == 2 and member['group'] in categories
                and alternatives == {category for dest,category in categories.items() if dest != member['group']}):
            state = saved_record_state(ir, scenario, origin, member, owned, entity, field, setup,
                                       categories, next(iter(alternatives)))
        if state and 'conflict' in state:
            condition['selected_state_conflict'] = state['conflict']
        elif state:
            add(f'record-state-{mi}', 'saved_record_boolean_state', state['source'],
                [preparation, membership, *state['inputs']], state['expected'], condition=condition,
                category=state['category'], property=state['property'], before=state['before'])
    return duties

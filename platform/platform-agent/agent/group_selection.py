"""Bind a grouped setup's selected state to its explicitly referenced public controls."""
from .group_setup import _linked_toggle, bind_group_setup
from .grouped_collections import _inspect
from .platform_task_ir import _annotations
from .public_scenario_metadata import bind_public_step_references, normalize_public_scenario


def _state_controls(ir, referenced, field):
    """Read standalone public property assertions on named, per-record buttons."""
    from .behavior_contracts import _public_clauses
    token = '{' + field + '}'
    names = {name for name, role in _annotations(referenced['text'])
             if role == 'button' and name.count(token) == 1}
    states, sources = {}, []
    for row in ir['scenarios']:
        if row.get('work_item') != referenced['id']:
            continue
        linked = normalize_public_scenario(ir, row)
        if linked.get('source') != 'parsed' or bind_public_step_references(ir, linked)[1]:
            raise ValueError('group_selection_reference_unbound')
        for assertion in linked['then']:
            text = assertion['text']
            for start, end in _public_clauses(text):
                clause = text[start:end].strip().removesuffix('.')
                name, relation, property_value = clause.rpartition(' exposes ')
                if not relation or name not in names:
                    continue
                key, equal, literal = property_value.partition('=')
                if (key, equal) != ('pressed', '=') or literal not in {'true', 'false'}:
                    continue
                state = literal == 'true'
                if state in states and states[state] != name:
                    raise ValueError('group_selection_conflicting_state')
                states[state] = name
                sources.append(dict(requirement_id=referenced['id'], scenario_id=linked['id'],
                                    phase='then', text=text[start:end]))
    if set(states) != {False, True} or set(states.values()) != names or len(names) != 2:
        raise ValueError('group_selection_states_unbound')
    return states, sources


def bind_group_selection(ir, item, scenario, group):
    scenario = normalize_public_scenario(ir, scenario)
    refs, issues = bind_public_step_references(ir, scenario)
    linked = [r for r in refs if r['kind'] == 'requirement']
    if issues or len(linked) != 1 or linked[0]['step_index'] != 0 or linked[0]['id'] == item['id']:
        raise ValueError('group_selection_reference_unbound')
    ref = next(row for row in ir['work_items'] if row['id'] == linked[0]['id'])
    field, members = group['identity_field'], group['members']
    setup = bind_group_setup(ir, item, scenario, members, record_role=group['record_role'], identity_field=field)
    identity = None
    selected = set()
    editor_controls = set()
    for step in setup:
        if step['op'] == 'fill' and step['target']['name'].casefold() == field.casefold():
            identity = step['value']
        elif step['op'] == 'set_pressed':
            if identity is None or step['value'] is not True:
                raise ValueError('group_selection_member_unbound')
            selected.add(identity)
            target = step['target']
            dialog = target['within']
            editor_controls.add((dialog['name'], dialog['role'], target['name']))
        elif step['op'] == 'hidden':
            identity = None
    selected_groups = {m['group'] for m in members if m['title'] in selected}
    regular_groups = {m['group'] for m in members if m['title'] not in selected}
    if len(selected_groups) != 1 or len(regular_groups) != 1 or selected_groups == regular_groups:
        raise ValueError('group_selection_members_unbound')
    states, sources = _state_controls(ir, ref, field)
    # The editor and record must name the same public action. Two unrelated
    # controls with opposite Boolean states are insufficient to bind this link.
    _, entity, _ = _inspect(group['source_clauses']['when'])
    if len(editor_controls) != 1:
        raise ValueError('group_selection_editor_ambiguous')
    dialog_name, dialog_role, editor_name = next(iter(editor_controls))
    if states[False].replace('{' + field + '}', entity).casefold() != editor_name.casefold():
        raise ValueError('group_selection_editor_action_unbound')
    _, declaration = _linked_toggle(ir, scenario, (dialog_name, dialog_role), editor_name)
    sources.insert(0, declaration)
    scopes = {step['target']['name']:step['target'] for step in group['inspection']}
    witness = []
    for member in members:
        active = member['title'] in selected
        record = dict(role=group['record_role'], name=member['title'], within=scopes[member['group']])
        name = states[active].replace('{' + field + '}', member['title'])
        witness.extend([dict(op='hover', target=record),
                        dict(op='pressed', target=dict(role='button', name=name, within=record), value=active)])
    return dict(steps=witness, consumed_requirement_ids=[ref['id']], sources=sources,
                selected_group=next(iter(selected_groups)), regular_group=next(iter(regular_groups)))

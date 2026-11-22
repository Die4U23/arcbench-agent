"""Locate public role/name annotations in a planned grouped readback."""
from .platform_task_ir import _ANNOTATION, _annotations


def _readback_steps(group, cases, start):
    identity = group['requirement_id'], group['scenario_id']
    matching = [(i, c) for i, c in enumerate(cases) if (c['requirement_id'], c['name']) == identity]
    if len(matching) != 1 or type(start) is not int or start < 0:
        return {}
    case_index, case = matching[0]
    expected = group['steps']
    segment = case['steps'][start:start + len(expected)]
    if segment != expected:
        return {}
    # Ordinary Python equality equates true with 1. Preserve numeric observation types.
    if any(s['op'] == 'count' and type(s.get('value')) is not int for s in segment):
        return {}
    scopes = {s['target']['name']: s['target'] for s in group['inspection']}
    members = {m['title']: m for m in group['members']}
    template = ('{' + group['identity_field'] + '}', group['record_role'])
    matches = {}
    for index, step in enumerate(segment, start):
        target, op = step['target'], step['op']
        pair = target['name'], target['role']
        if not (op in {'visible', 'record_fields'} or op == 'count' and step['value'] == 1):
            continue
        if target == scopes.get(target['name']) and op in {'visible', 'count'}:
            key = pair
        else:
            member = members.get(target['name'])
            if member is None or pair[1] != template[1]:
                continue
            record = dict(name=member['title'], role=template[1])
            owned = dict(record, within=scopes[member['group']])
            if target != owned and not (op == 'count' and target == record):
                continue
            if op == 'record_fields' and step['value'] != [member['title'], member['content']]:
                continue
            key = template
        matches.setdefault(key, []).append(dict(case_index=case_index, case=case['name'],
                                                step_index=index, step=step))
    return matches


def group_annotation_evidence(lineage, group, cases, *, readback_start):
    """Index locator evidence only; every containing sentence keeps its independent verdict."""
    from .behavior_contracts import _public_clauses
    readback = _readback_steps(group, cases, readback_start)
    if not readback:
        return []
    records = {}
    for owner in lineage:  # The leaf precedes its ancestors.
        text = owner.get('text')
        if not isinstance(text, str):
            continue
        for begin, end in _public_clauses(text):
            clause = text[begin:end]
            for match in _ANNOTATION.finditer(clause):
                parsed = _annotations(match[0])
                if len(parsed) != 1 or parsed[0] not in readback:
                    continue
                # A same-named ancestor control can belong to another page. Only
                # reuse an exact leaf clause and the same occurrence in that clause.
                key = (clause, match.span())
                if key not in records:
                    if owner['id'] != group['requirement_id']:
                        continue
                    records[key] = dict(scope='planned_annotation_readback_only', clause=clause,
                        annotation=match[0], locator=dict(name=parsed[0][0], role=parsed[0][1]),
                        occurrences=[], observations=readback[parsed[0]])
                records[key]['occurrences'].append(dict(work_item=owner['id'], source='text',
                    clause_span=[begin, end], span=[begin + match.start(), begin + match.end()]))
    return list(records.values())

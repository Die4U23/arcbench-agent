"""Relate a public classification declaration to a finite, independently prepared fixture."""
import re

from .platform_task_ir import _ANNOTATION, _annotations
from .grouped_collections import _inspect, regular_plural


def _words(text):
    return re.findall(r"[\w]+(?:[-'][\w]+)*|[^\s\w]", text.casefold())


def _classification(clause, workspace, entity, groups):
    """Consume a named view and coordinated population, cardinality and destination triples."""
    annotations = list(_ANNOTATION.finditer(clause))
    if len(annotations) < 2:
        return None
    names = []
    for annotation in annotations:
        bound = _annotations(annotation.group())
        if len(bound) != 1 or groups.get(bound[0][0]) != bound[0][1]:
            return None
        names.append(bound[0][0])
    if set(names) != set(groups) or len(set(names)) != len(names):
        return None

    lead = _words(clause[:annotations[0].start()])
    if lead[:1] == ['the']:
        lead = lead[1:]
    if lead[:1] == ['active']:
        lead = lead[1:]
    view = _words(workspace)
    if lead[:len(view)] != view:
        return None
    lead = lead[len(view):]
    if lead[:1] and lead[0] in {'view', 'workspace', 'page'}:
        lead = lead[1:]
    if not lead or lead[0] not in {'separates', 'groups', 'sorts', 'places', 'organizes'}:
        return None

    arms = [lead[1:]]
    for before, after in zip(annotations, annotations[1:]):
        tokens = _words(clause[before.end():after.start()])
        has_comma = tokens[:1] == [',']
        if has_comma:
            tokens = tokens[1:]
        if tokens[:1] == ['and']:
            tokens = tokens[1:]
        elif not has_comma:
            return None
        arms.append(tokens)
    if _words(clause[annotations[-1].end():]) not in ([], ['.']):
        return None

    plural = regular_plural(entity)
    assignment = {}
    for group_name, tokens in zip(names, arms):
        if (len(tokens) < 5 or tokens[-4] != plural or tokens[-3] not in {'into','in','within','under'}
                or tokens[-2] != 'exactly' or tokens[-1] not in {'one','1'}):
            return None
        qualifier = tokens[:-4]
        # One named category per arm. Logical expressions require their own
        # evidence and cannot be converted into a literal state name.
        if len(qualifier) != 1 or not qualifier[0].replace('-', '').isalpha():
            return None
        assignment[group_name] = qualifier[0]
    if len(set(assignment.values())) != len(assignment):
        return None
    return assignment


def bind_classification_source(item, scenario, group, obligations):
    """Map the original prose only as far as the independent public population and setup support."""
    from .behavior_contracts import _public_clauses
    if group.get('requirement_id') != item.get('id') or scenario.get('work_item') != item.get('id'):
        return []
    groups, entity, _ = _inspect(group['source_clauses']['when'])
    entry, opened, _ = group['source_clauses']['given'].partition(' is open.')
    if not opened:
        return []
    sources = []
    for start, end in _public_clauses(item['text']):
        quote = item['text'][start:end]
        assignment = _classification(quote, entry, entity, groups)
        if assignment is None:
            continue
        ids, remainder, assignments = [], [], []
        counts, prepared = set(), set()
        membership_cells, global_identities = set(), set()
        context_sources = []
        for obligation in obligations:
            check = obligation['expected']
            if check['op'] != 'count' or type(check.get('value')) is not int:
                continue
            target = check['target']
            name = target['name']
            if (obligation['kind'] == 'collection_cardinality' and name in assignment
                    and target == {'role':groups[name], 'name':name} and check['value'] == 1):
                ids.append(obligation['id'])
                counts.add(name)
                continue
            condition = obligation.get('condition') or {}
            owner = condition.get('assigned_group')
            base = condition.get('member')
            if owner not in assignment or name != condition.get('identity'):
                continue
            if (obligation['kind'] == 'exclusive_record_identity' and target.get('role') == group['record_role']
                    and 'within' not in target and check['value'] == 1):
                ids.append(obligation['id'])
                global_identities.add(base)
                if obligation['source'] not in context_sources:
                    context_sources.append(obligation['source'])
                continue
            tested = obligation.get('tested_group')
            if (obligation['kind'] != 'conditional_collection_membership' or tested not in groups
                    or target.get('role') != group['record_role'] or target.get('within') != {'role':groups[tested], 'name':tested}
                    or check['value'] != int(tested == owner)):
                continue
            ids.append(obligation['id'])
            membership_cells.add((base, tested))
            if tested != owner:
                continue
            prepared.add(owner)
            qualifier = condition.get('qualifier')
            conflict = condition.get('selected_state_conflict', [])
            grounded = isinstance(qualifier, str) and qualifier.casefold() == assignment[owner] and not conflict
            state_duties = [d for d in obligations if d['kind'] == 'saved_record_boolean_state'
                            and d['condition'] == condition and d['category'] == assignment[owner]
                            and d['expected']['target'].get('within') == target
                            and d['expected']['value'] is True and d['before'] == [dict(op='hover',target=target)]]
            if len(state_duties) == 1:
                state, = state_duties
                ids.append(state['id'])
                context_sources.extend([state['source'], *state['inputs']])
                grounded = True
            assignments.append(dict(member=condition['member'], destination=owner,
                                    declared_state=assignment[owner], given_qualifier=qualifier,
                                    selected_control=condition.get('selection'), independently_named_state=grounded))
            if not grounded:
                context_sources.extend(conflict)
                problem = ('selected_state_conflicts_with_classification' if conflict
                           else 'state_category_not_named_in_fixture_precondition')
                remainder.append(dict(reason=problem,
                                      member=condition['member'],declared_state=assignment[owner]))
        population = {m['base'] for m in group['members']}
        if (counts != set(assignment) or prepared != set(assignment) or global_identities != population
                or membership_cells != {(m, g) for m in population for g in groups}):
            remainder.append(dict(reason='classification_population_or_group_missing'))
        sources.append(dict(owner=item['id'],source='text',span=[start,end],quote=quote,
                            obligation_ids=ids, fixture_assignments=assignments, supporting_scenario_clauses=context_sources,
                            remaining=remainder))
    return sources

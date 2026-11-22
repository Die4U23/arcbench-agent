"""Attach public source slices to mandatory grouped cases and retain every residual."""
from .group_annotation_evidence import group_annotation_evidence
from .group_fixture_titles import fixture_bindings, title_evidence, title_sources
from .group_classification import bind_classification_source
from .group_obligations import derive_group_obligations
from .group_selection import bind_group_selection
from .public_keyboard_clause import bind_group_keyboard
from .platform_task_ir import _then_step
from .public_scenario_metadata import given_facts


def _bound_readback(clause, group):
    """Only an existing whole-source compiler or explicit readback can consume prose."""
    from .behavior_contracts import _public_clauses, compile_public_step, matches
    for phase in ('when', 'then'):
        source = group['source_clauses'][phase]
        if any(clause == source[start:end] for start, end in _public_clauses(source)):
            return 'ordered_group_readback'
    try:
        assertion = compile_public_step('then', _then_step(clause))
    except ValueError:
        return None
    if not assertion:
        return None
    locator = assertion.get('target', {})
    if locator.get('name') == '{' + group['identity_field'] + '}' and locator.get('role') == group['record_role']:
        required = [dict(assertion, target=dict(locator, name=member['title'])) for member in group['members']]
    else:
        required = [assertion]
    if all(any(matches(actual, expected) for actual in group['steps']) for expected in required):
        return 'public_assertion_in_group_readback'
    return None


def grouped_source_program(ir, item, scenario, lineage, group, setup, exclusions, elements, *, entry_path):
    from .behavior_contracts import _public_clauses
    from .shared_public_checks import bind_group_shared

    sid, ref = scenario['id'], item['id']
    try:
        selection = bind_group_selection(ir, item, scenario, group)
    except ValueError:
        selection = {}
    selection_steps = selection.get('steps', [])
    inventory_steps, keyboard_rules = bind_group_keyboard(lineage, group, selection_steps)
    try:
        shared = bind_group_shared(ir, item, group)
    except ValueError as error:
        shared = dict(entry_steps=[], issues=[dict(
            reason=str(error), source=f'scenarios[{sid}].given')])
    entry = [dict(op='goto', value=entry_path), dict(op='url', value=entry_path), *shared['entry_steps']]
    cases = []
    title_clauses = title_sources(lineage, group)
    def add_case(name, steps, quote, members):
        case = dict(name=name, requirement_id=ref, requirement_quote=quote[:500], steps=entry + steps)
        if title_clauses:
            case['fixture_titles'] = fixture_bindings(ir, group, members)
        cases.append(case)
    add_case(sid, setup + group['steps'] + inventory_steps + selection_steps,
             group['source_clauses']['when'], group['members'])
    for index, probe in enumerate(exclusions):
        add_case(f'{sid}-exclusion-{index + 1}', probe['steps'], probe['sources'][0]['text'], probe['members'])
    title_witnesses = (title_evidence(cases[0], group, setup_start=len(entry), setup_count=len(setup),
                                      readback_count=len(group['steps']), controller_binding=cases[0]['fixture_titles'])
                       if title_clauses else [])

    names = [c['name'] for c in cases]
    sources, issues = [], list(shared.get('issues', []))
    try:
        obligations = derive_group_obligations(ir, item, scenario, group, setup)
    except ValueError as error:
        obligations = []
        issues.append(dict(reason=str(error), source=f'scenarios[{sid}].then'))
    workspace_bound = bool(shared.get('workspace_bound'))
    if set(given_facts(scenario['given'])) - {'text', 'data', 'account', 'session'}:
        issues.append(dict(reason='unconsumed_given_fields', source=f'scenarios[{sid}].given'))
        workspace_bound = False
    for phase, evidence in [('when', 'collection_inspection'), ('then', 'exclusive_scoped_readback'),
                            ('given', 'entry_and_ui_created_records')]:
        if phase != 'given' or workspace_bound:
            sources.append(dict(work_item=ref, scenario_id=sid, source=phase, text=group['source_clauses'][phase],
                                evidence=evidence, cases=[names[0]]))
    exclusion_sources = {(p['sources'][0]['work_item'], tuple(p['sources'][0]['span']), p['sources'][0]['text'])
                         for p in exclusions}
    def residual(owner, text, span, **extra):
        reason = 'requirement_text_not_fully_bound' if owner == ref else 'ancestor_prose_applicability_or_evidence_unresolved'
        issues.append(dict(reason=reason,
                           source=f'work_items[{owner}].text', scope='requirement' if owner == ref else 'ancestor',
                           owner=owner, clause=text, source_span=span, **extra))
    keyboard_coverage = []
    for owner in lineage:
        if 'environment' in owner:
            environment = owner['environment']
            if not isinstance(environment, dict) or set(environment) - {'locale'}:
                issues.append(dict(reason='group_public_environment_unbound',
                                   source=f"work_items[{owner['id']}].environment",
                                   scope='requirement' if owner['id'] == ref else 'ancestor'))
        text = owner.get('text')
        if not isinstance(text, str):
            issues.append(dict(reason='public_source_text_unavailable', source=f"work_items[{owner['id']}].text",
                               scope='ancestor' if owner['id'] != ref else 'requirement'))
            continue
        spans = _public_clauses(text)
        if not spans and text.strip():
            residual(owner['id'], text, [0, len(text)])
        for start, end in spans:
            clause = text[start:end]
            key = (owner['id'], (start, end), clause)
            proof, covered_by = None, [names[0]]
            if key in title_clauses and title_witnesses:
                sources.append(dict(work_item=owner['id'], source='text',span=[start,end],text=clause,
                                    evidence='runtime_fixture_title_' + title_clauses[key], cases=covered_by,
                                    program='base_mandatory_cases', witnesses=title_witnesses))
                continue
            if key in keyboard_rules:
                receipt = keyboard_rules[key]
                keyboard_coverage.append(dict(work_item=owner['id'], span=[start, end], text=clause, case=names[0],
                                              **receipt))
                if receipt['remaining']:
                    residual(owner['id'], clause, [start, end], remaining=receipt['remaining'])
                    continue
                proof = 'scoped_hover_button_keyboard_reachability'
            elif key in exclusion_sources:
                proof, covered_by = 'destination_and_active_group_readback', names[1:]
            else:
                proof = _bound_readback(clause, group)
            if proof:
                sources.append(dict(work_item=owner['id'], source='text', span=[start, end], text=clause,
                                    evidence=proof, cases=covered_by))
            else:
                residual(owner['id'], clause, [start, end])
    # The published template is satisfied by concrete instantiated role/name pairs;
    # unknown elements or additional scopes still block the contract.
    def observed(locator):
        for c in cases:
            for step in c['steps']:
                target = step.get('target', {})
                positive = (step['op'] in {'visible', 'click', 'fill', 'record_fields', 'pressed', 'set_pressed',
                                           'keyboard_reachable', 'current'} or
                            step['op'] == 'count' and step['value'] > 0)
                if positive and all(target.get(k) == v for k, v in locator.items()):
                    return True
        return False
    for element in elements:
        if element == dict(role=group['record_role'], name='{' + group['identity_field'] + '}'):
            bound = all(observed(dict(role=element['role'], name=m['title'])) for m in group['members'])
        else:
            bound = observed(element)
        if not bound:
            issues.append(dict(reason='public_role_not_verified', source=f'work_items[{ref}].ui_elements',
                               target=element))
    return dict(cases=cases, sources=sources, issues=issues, selection_sources=selection.get('sources', []),
                independent_obligations=obligations,
                classification_sources=bind_classification_source(item, scenario, group, obligations),
                readback_start=len(entry) + len(setup), readback_count=len(group['steps']),
                keyboard_coverage=keyboard_coverage,
                annotation_evidence=group_annotation_evidence(lineage, group, cases, readback_start=len(entry) + len(setup)),
                consumed_requirement_ids=selection.get('consumed_requirement_ids', []),
                workspace_bound=workspace_bound)

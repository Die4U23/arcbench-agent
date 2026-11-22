"""Verify data-only proposals against independently compiled public browser evidence."""
from copy import deepcopy
import json
import re

from .platform_task_ir import _ANNOTATION, _annotations
from .public_scenario_metadata import bind_public_step_references, normalize_public_scenario


PROSE_REASONS = {'requirement_text_not_fully_bound', 'ancestor_prose_applicability_or_evidence_unresolved'}
MAX_CLAIMS = 96
MAX_CHECKS = 192


def _same(left, right):
    return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(right, sort_keys=True, ensure_ascii=False)


def _source_key(row):
    return row['owner'], row.get('source', 'text'), tuple(row['span']), row['quote']


def _context(ir, record, program):
    ref = record['requirement_id']
    case = program['cases'][0]
    start = program['readback_start']
    stop = start + program['readback_count']
    sources = [dict(owner=d['owner'], source='text', span=d['source_span'], quote=d['clause'])
               for d in record.get('issue_details', []) if d['reason'] in PROSE_REASONS and 'source_span' in d]
    then = next(row['text'] for row in program['sources'] if row['source'] == 'then')
    sources.append(dict(owner=ref, source='then', span=[0, len(then)], quote=then))
    evidence = [dict(id=f'readback-{i}', case=case['name'], step_index=i, step=step)
                for i, step in enumerate(case['steps']) if start <= i < stop or step['op'] == 'hover_action_inventory']
    # Each locator fragment keeps its source coordinates. Fragments never
    # authorize the entire containing sentence, even when an observation passes.
    annotations = [dict(owner=where['work_item'], span=where['span'], clause_span=where['clause_span'],
                        locator=row['locator'], evidence=[f"readback-{o['step_index']}" for o in row['observations']])
                   for row in program['annotation_evidence'] for where in row['occurrences']]
    items = {row['id']: row for row in ir['work_items']}
    scenario = normalize_public_scenario(ir, next(row for row in ir['scenarios'] if
        row.get('work_item') == ref and row.get('id') == case['name'] and row.get('source') == 'parsed'))
    bindings, _ = bind_public_step_references(ir, scenario)
    def public_item(row):
        return {field: row[field] for field in ('id','name','parent','page','text','references') if field in row}
    ancestors, owner = [], items[ref]
    visited = {ref}
    while owner.get('parent') in items and owner['parent'] not in visited:
        owner = items[owner['parent']]
        visited.add(owner['id'])
        ancestors.append(public_item(owner))
    public = dict(leaf=public_item(items[ref]), ancestors=ancestors,
                  scenario=dict(id=scenario['id'], given=scenario['given']['text'],
                                when=[s['text'] for s in scenario['when']], then=[s['text'] for s in scenario['then']],
                                order=scenario.get('ordered_steps', []), public_steps=scenario.get('public_steps', []),
                                references=bindings),
                  linked_requirements=[public_item(items[key]) for key in dict.fromkeys(
                      b['id'] for b in bindings if b['kind'] == 'requirement' and b['id'] in items)],
                  linked_assertion_sources=program['selection_sources'])
    return dict(version=1, requirement_id=ref, sources=sources, evidence=evidence, annotations=annotations,
                public=public, setup=dict(case=case['name'], steps=case['steps'][:start]),
                intents=['readback', 'count', 'visible', 'record_fields', 'hover_action_inventory', 'unresolved'])


def _compile(ir, ref, callback):
    from .behavior_contracts import _compile_requirement_contracts
    return _compile_requirement_contracts(ir, review_ref=ref, review_group=callback)


def group_proposal_context(ir, ref, *, version=1):
    if type(version) is not int or version not in (1, 2):
        raise ValueError('unsupported_group_proposal_version')
    context = {}
    def collect(record, program):
        packet = _context(ir, record, program)
        if version == 1:
            context.update(packet)
            return
        # V2 authors their own literal checks. Only public descriptions, finite
        # fixture setup and obligation identities are offered to the author.
        context.update(version=2, requirement_id=ref, public=packet['public'], setup=packet['setup'],
                       sources=packet['sources'], operations=['visible','count','record_fields','hover','pressed'],
                       obligations=[{k:v for k,v in o.items() if k not in {'expected','before'}}
                                    for o in program.get('independent_obligations', ())])
    _compile(ir, ref, collect)
    return context


def _number(token):
    names = ('zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten')
    if token in names:
        return names.index(token)
    return int(token) if isinstance(token, str) and re.fullmatch(r'0|[1-9][0-9]{0,5}', token) else None


def _source_assertion(clause):
    """Interpret a whole quantified existence/visibility proposition with literal public roles."""
    syntax = []
    pos = 0
    def words(part):
        return re.findall(r'[\w-]+|[^\s\w]', part.casefold())
    for match in _ANNOTATION.finditer(clause):
        syntax.extend(words(clause[pos:match.start()]))
        loc = _annotations(match[0])
        if len(loc) != 1 or not loc[0][0]:
            return None
        syntax.append(dict(name=loc[0][0], role=loc[0][1]))
        pos = match.end()
    syntax.extend(words(clause[pos:]))
    if syntax[-1:] == ['.']:
        syntax.pop()
    scope = None
    if len(syntax) >= 3 and isinstance(syntax[-1], dict) and syntax[-2] in ('within', 'inside'):
        scope = syntax[-1]
        del syntax[-2:]
    locators = [i for i, token in enumerate(syntax) if isinstance(token, dict)]
    if len(locators) != 1:
        return None
    index, = locators
    target = dict(syntax[index])
    if scope:
        target['within'] = scope
    before, after = syntax[:index], syntax[index+1:]
    if not before and after in (['is', 'visible'], ['is', 'shown']):
        return dict(op='visible', target=target)
    if not before and after[:1] in (['appears'], ['occurs'], ['exists']):
        tokens = after[1:]
        if tokens in (['once'], ['twice']):
            return dict(op='count', target=target, value=1 if tokens == ['once'] else 2)
        if tokens[:1] == ['exactly'] and len(tokens) == 3 and tokens[2] in ('time', 'times'):
            count = _number(tokens[1])
            if count is not None:
                return dict(op='count', target=target, value=count)
    if before[:2] in (['there', 'is'], ['there', 'are']) and not after:
        before = before[2:]
    elif after not in (['exists'], ['exist'], ['appears'], ['appear']):
        return None
    if before == ['no']:
        return dict(op='count', target=target, value=0)
    if len(before) == 2 and before[0] == 'exactly':
        count = _number(before[1])
        if count is not None:
            return dict(op='count', target=target, value=count)
    return None


def _claim(claim, context, program, index):
    if (not isinstance(claim, dict) or set(claim) not in (
            {'owner','span','quote','intent','evidence'}, {'owner','source','span','quote','intent','evidence'})
            or not isinstance(claim.get('owner'), str) or not isinstance(claim.get('quote'), str)
            or not isinstance(claim.get('source','text'), str) or not isinstance(claim.get('span'), list)
            or len(claim['span']) != 2 or any(type(i) is not int for i in claim['span'])
            or not isinstance(claim.get('intent'), dict) or not isinstance(claim.get('evidence'), list)
            or len(claim['evidence']) > MAX_CHECKS or any(not isinstance(e, str) for e in claim['evidence'])):
        raise ValueError('invalid_claim_structure')
    key = _source_key(claim)
    if not any(_source_key(s) == key for s in context['sources']):
        raise ValueError('source_owner_span_or_quote_mismatch')
    ids, intent = claim['evidence'], claim['intent']
    if any(identity not in index for identity in ids):
        raise ValueError('unknown_evidence_id')
    cited = [index[identity]['step'] for identity in ids]
    if intent == {'op':'unresolved'} and not ids:
        return 'unresolved', []
    if key[1] == 'then':
        if intent != {'op':'readback'} or not cited or any(s['op'] not in {'count','visible','record_fields'} for s in cited):
            raise ValueError('invalid_source_readback')
        # Authors choose which checks to execute and in which order. The public
        # scenario itself is already covered independently by its mandatory case.
        return 'scenario_observations', cited
    if intent == {'op':'hover_action_inventory'} and ids:
        row = next((r for r in program['keyboard_coverage'] if r['work_item']==key[0]
                    and tuple(r['span'])==key[2] and r['text']==key[3]), None)
        if row is None or any(s['op'] != 'hover_action_inventory' or
                              s['target'] not in row['bound_targets'] for s in cited):
            raise ValueError('keyboard_scope_or_evidence_mismatch')
        return 'partial', cited
    if set(intent) == {'op','annotation_span'} and intent['op'] == 'record_fields' and ids:
        rows = [r for r in context['annotations'] if r['owner'] == key[0] and tuple(r['clause_span']) == key[2]
                and _same(r['span'], intent['annotation_span'])]
        if len(rows) != 1 or any(e not in rows[0]['evidence'] for e in ids) or any(s['op'] != 'record_fields' for s in cited):
            raise ValueError('record_locator_or_evidence_mismatch')
        return 'locator_fragment', cited
    parsed = _source_assertion(claim['quote'])
    if parsed is None or not _same(parsed, intent) or any(not _same(step, parsed) for step in cited):
        raise ValueError('unverified_source_meaning_or_evidence')
    if key[0] != context['requirement_id'] and not any(
            s['owner'] == context['requirement_id'] and s['source']=='text' and s['quote']==key[3] for s in context['sources']):
        raise ValueError('ancestor_application_scope_unverified')
    if '{' in parsed['target']['name'] or '}' in parsed['target']['name']:
        raise ValueError('concrete_record_identity_required')
    # A new assertion at the inspected state must not contradict the already
    # bound public postcondition at that same target and scope.
    for row in context['evidence']:
        step = row['step']
        if step['op'] == 'count' and step['target'] == parsed['target']:
            if parsed['op'] == 'count' and parsed['value'] != step['value'] or parsed['op'] == 'visible' and not step['value']:
                raise ValueError('contradictory_public_cardinality')
    return 'complete', cited or [parsed]


def _validate(ir, record, program, proposal):
    from .behavior_contracts import target_matches
    ref = record['requirement_id']
    result = dict(complete=False, accepted=[], issues=[], validated_cases=[])
    if (not isinstance(proposal, dict) or set(proposal) != {'version','requirement_id','claims'}
            or type(proposal['version']) is not int or proposal['version'] not in (1, 2) or proposal['requirement_id'] != ref
            or not isinstance(proposal['claims'], list) or len(proposal['claims']) > MAX_CLAIMS):
        result['issues'].append(dict(reason='invalid_proposal_structure'))
        return result
    if proposal['version'] == 2:
        return _verify_obligation_claims(record, program, proposal['claims'], result)
    packet = _context(ir, record, program)
    index = {e['id']:e for e in packet['evidence']}
    seen, closed, checks = set(), set(), []
    for position, claim in enumerate(proposal['claims']):
        try:
            coverage, steps = _claim(claim, packet, program, index)
            key = _source_key(claim)
            if key in seen:
                raise ValueError('duplicate_claim_for_source')
            if len(checks) + len(steps) > MAX_CHECKS:
                raise ValueError('semantic_plan_step_limit')
            seen.add(key)
            if coverage == 'unresolved':
                continue
            if coverage == 'complete':
                closed.add(key)
            origin = 'selected' if claim['evidence'] else 'proposed'
            result['accepted'].append(dict(**claim, coverage=coverage, origin=origin))
            checks.extend(steps)
        except (ValueError, TypeError, OverflowError, RecursionError) as error:
            result['issues'].append(dict(reason=str(error)[:160], claim_index=position))
    original = program['cases'][0]
    start = program['readback_start']
    if checks:
        result['validated_cases'] = [dict(original, steps=deepcopy(original['steps'][:start] + checks))]
    remaining = []
    for detail in record.get('issue_details', []):
        if detail['reason'] in PROSE_REASONS and 'source_span' in detail:
            if (detail['owner'], 'text', tuple(detail['source_span']), detail['clause']) in closed:
                continue
        if detail['reason'] == 'public_role_not_verified' and any(
                target_matches(s.get('target'), detail['target']) and (s['op'] == 'visible' or
                s['op'] == 'count' and type(s['value']) is int and s['value'] > 0) for s in checks):
            continue
        remaining.append(detail)
    result['remaining'] = remaining
    if not remaining and not result['issues'] and checks and closed:
        result['complete'] = True
        record['unresolved'] = []
        record.pop('issue_details', None)
        record['controller_cases'] = deepcopy(program['cases'])
        record['controller_cases'][0]['steps'][start:start] = deepcopy(checks)
        record['verified_semantic_claims'] = deepcopy(result['accepted'])
    return result


def _verify_obligation_claims(record, program, claims, result):
    original = program['cases'][0]
    start = program['readback_start']
    duties = program.get('independent_obligations', [])
    expected = {d['id']:d['expected'] for d in duties}
    preparations = {d['id']:d['before'] for d in duties if d.get('before')}
    if (not duties or len(expected) != len(duties) or any(
            d['requirement_id'] != record['requirement_id'] or d['case'] != original['name']
            or d['scenario_id'] != original['name'] or d['state'] != 'group_readback'
            or d['expected'].get('op') not in {'count','visible','record_fields','pressed'} for d in duties)):
        result['issues'].append(dict(reason='independent_group_obligations_unavailable'))
        return result

    checks, witnesses, seen = [], {}, set()
    for position, claim in enumerate(claims):
        try:
            if (not isinstance(claim, dict) or set(claim) != {'obligation_id','checks'}
                    or not isinstance(claim['obligation_id'], str) or not isinstance(claim['checks'], list)
                    or not claim['checks']):
                raise ValueError('invalid_obligation_claim')
            duty, proposed = claim['obligation_id'], claim['checks']
            if duty not in expected:
                raise ValueError('unknown_obligation_id')
            if duty in seen:
                raise ValueError('duplicate_obligation_id')
            seen.add(duty)
            if len(checks) + len(proposed) > MAX_CHECKS:
                raise ValueError('semantic_plan_step_limit')
            matched = []
            if duty in preparations and not _same(proposed, [*preparations[duty], expected[duty]]):
                raise ValueError('obligation_preparation_or_observation_mismatch')
            for index, step in enumerate(proposed):
                # Strict JSON equality also distinguishes integer counts from Booleans.
                if _same(step, expected[duty]):
                    matched.append(dict(case_index=0, step_index=start + len(checks) + index))
                elif duty in preparations:
                    continue
                elif not any(_same(step, other) for key, other in expected.items() if key not in preparations):
                    raise ValueError('check_not_bound_to_public_readback')
            if not matched:
                raise ValueError('obligation_check_mismatch')
            witnesses[duty] = matched
            result['accepted'].append(dict(obligation_id=duty, checks=deepcopy(proposed), origin='proposed'))
            checks.extend(deepcopy(proposed))
        except (ValueError, TypeError, OverflowError, RecursionError) as error:
            result['issues'].append(dict(reason=str(error)[:160], claim_index=position))

    rows = [{**{k:v for k,v in d.items() if k != 'expected'}, 'planned':d['id'] in witnesses,
             'execution':'not_run', 'witnesses':witnesses.get(d['id'], [])} for d in duties]
    missing = [d['id'] for d in duties if d['id'] not in witnesses]
    result['obligation_coverage'] = dict(required=list(expected), covered=list(witnesses), missing=missing,
                                         complete=not missing, rows=rows)
    if checks:
        result['validated_cases'] = [dict(original, steps=deepcopy(original['steps'][:start]) + checks)]
    closed, mappings = set(), []
    for source in program.get('classification_sources', []):
        ids = source['obligation_ids']
        unbound = [identity for identity in ids if identity not in witnesses]
        finite = bool(ids) and len(ids) == len(set(ids)) and not unbound
        full = (finite and not source['remaining'] and source['owner'] == record['requirement_id']
                and source.get('source', 'text') == 'text')
        mappings.append(dict(source, missing_obligation_ids=unbound, finite_relationship_planned=finite,
                             fully_bound=full, execution='not_run'))
        if full:
            closed.add(_source_key(source))
    result['classification_sources'] = mappings
    residuals = []
    untraced = set(record.get('unresolved', [])) - {d['reason'] for d in record.get('issue_details', [])}
    residuals.extend(dict(reason=reason) for reason in sorted(untraced))
    for detail in record.get('issue_details', []):
        if detail['reason'] in PROSE_REASONS:
            key = (detail.get('owner'), 'text', tuple(detail.get('source_span', ())), detail.get('clause'))
            if key in closed:
                continue
        residuals.append(detail)
    result['remaining'] = residuals
    if not missing and not residuals and all(source['fully_bound'] for source in mappings) and not result['issues']:
        result['complete'] = True
        record['unresolved'] = []
        record.pop('issue_details', None)
        # Strict execution also retains every independently required scenario
        # and exclusion. A validated partial proposal only has its own checks.
        record['controller_cases'] = deepcopy(program['cases'])
        record['controller_cases'][0]['steps'][start:start] = checks
        record['verified_semantic_claims'] = deepcopy(result['accepted'])
    return result


def group_obligation_execution(report, case_results):
    """Attribute a trusted browser replay only to the exact submitted case and ordered checks."""
    rows = []
    cases = report['validated_cases']
    by_index = {}
    for index, case in enumerate(cases):
        matches = [row for row in case_results if row.get('case') == case['name']]
        if len(matches) == 1 and _same(matches[0].get('executed_case'), case):
            observation = matches[0]
            if type(observation.get('observed_steps')) is int and observation['observed_steps'] == len(case['steps']):
                by_index[index] = observation
    for duty in report['obligation_coverage']['rows']:
        observations_by_step = []
        for witness in duty['witnesses']:
            case = cases[witness['case_index']]
            observation = by_index.get(witness['case_index'])
            status = 'unknown' if case_results else 'not_run'
            if observation is not None and case.get('requirement_id') == duty['requirement_id']:
                failed = observation.get('failed_step_index')
                if observation['status'] == 'passed' and failed is None:
                    status = 'passed'
                elif observation['status'] == 'failed' and type(failed) is int and 0 <= failed < len(case['steps']):
                    index = witness['step_index']
                    status = 'passed' if index < failed else 'failed' if index == failed else 'not_reached'
                elif observation['status'] == 'skipped':
                    status = 'not_run'
            observations_by_step.append(dict(witness, execution=status))
        statuses = {step['execution'] for step in observations_by_step}
        status = next((s for s in ('failed','unknown','passed','not_reached') if s in statuses),'not_run')
        rows.append(dict(id=duty['id'], execution=status, witnesses=observations_by_step))
    outcomes = {row['id']:row['execution'] for row in rows}
    sources = []
    for source in report.get('classification_sources', []):
        statuses = {outcomes.get(identity, 'not_run') for identity in source['obligation_ids']}
        observed = source['finite_relationship_planned'] and statuses == {'passed'}
        status = ('passed' if observed else 'failed' if 'failed' in statuses else 'unknown' if 'unknown' in statuses
                  else 'partial' if 'passed' in statuses else 'not_reached' if 'not_reached' in statuses else 'not_run')
        sources.append(dict(source, execution=status, finite_relationship_passed=observed,
                            full_source_passed=source['fully_bound'] and observed))
    return dict(rows=rows, classification_sources=sources,
                **{s:[r['id'] for r in rows if r['execution']==s]
                   for s in ('passed','failed','not_reached','unknown','not_run')})


def strict_group_obligation_execution(report, frozen_cases, frozen_source, *, all_passed):
    """Project proved prefixes of a wholly passing frozen strict suite onto the reviewed obligations."""
    from .behavior_checks import _emit_plan
    contract = report['contract']
    ref = contract['requirement_id']
    observations = [{}]  # An aggregate failure does not identify a completed assertion.
    basis = 'unlocalized_suite_result'
    if all_passed:
        basis = 'frozen_program_mismatch'
        expected = contract['controller_cases']
        if (_same(frozen_cases, expected) and
                frozen_source == _emit_plan({'cases':expected}, {ref:contract}).encode('utf-8')):
            for selected in report['validated_cases']:
                matching = [case for case in frozen_cases if case['requirement_id'] == ref
                            and case['name'] == selected['name']]
                if len(matching) != 1:
                    break
                full, = matching
                prefix = dict(full, steps=full['steps'][:len(selected['steps'])])
                if not _same(prefix, selected):
                    break
                observations.append(dict(case=full['name'], executed_case=prefix, status='passed',
                                         observed_steps=len(prefix['steps'])))
            else:
                basis = 'complete_frozen_suite_passed'
            if basis != 'complete_frozen_suite_passed':
                observations = [{}]
    return dict(requirement_id=ref, basis=basis, **group_obligation_execution(report, observations))


def apply_group_proposal(ir, ref, proposal):
    from .behavior_contracts import _VerifiedRequirements
    from .group_fixture_titles import review_title_sources
    report = {}
    def verify(record, program):
        report.update(_validate(ir, record, program, proposal))
        review_title_sources(program, record, report)
    contracts = _compile(ir, ref, verify)
    if not report:
        report.update(complete=False, accepted=[], validated_cases=[], issues=[dict(reason='public_group_program_unavailable')])
    if isinstance(ir, _VerifiedRequirements):
        contracts = dict(ir.contracts, **({ref: contracts[ref]} if report['complete'] else {}))
    report['contract'] = contracts.get(ref)
    return (_VerifiedRequirements(ir, contracts) if report['complete'] else ir), report


def verify_group_proposal(ir, ref, proposal):
    return apply_group_proposal(ir, ref, proposal)[1]

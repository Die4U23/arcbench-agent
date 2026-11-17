"""Deterministic public-IR obligations. No implementation or model coverage claims."""
import hashlib
import json
import re
from urllib.parse import unquote, urlsplit
from .visual_contracts import normalize_visual


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


def requirement_contracts(ir):
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
    public_pages = records('public_pages' if 'public_pages' in ir else 'pages')
    for page in records('pages'):
        if set(page) - {'name', 'path', 'reference'}:
            global_issues.append('unconsumed_public_page_fields')
        if page.get('reference'):
            global_issues.append('visual_reference_pixel_oracle_unavailable')
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
    for item in valid_items:
        ref=item['id']; scenarios=[]; unresolved=list(global_issues); elements=[]
        page_refs = [item['page']] if item.get('page') is not None else []
        if item.get('references'):
            unresolved.append('visual_reference_pixel_oracle_unavailable')
        if ir.get('rejected'):unresolved.append('public_input_rejected_fragments')
        for element in public_elements:
            if element.get('work_item') != ref or element.get('source') != 'parsed': continue
            if element.get('page') is not None and element['page'] not in page_refs:
                page_refs.append(element['page'])
            t=target({key: element[key] for key in ('role','name','within') if key in element})
            if t is None: unresolved.append('ui_element_target_unresolved')
            elif t not in elements: elements.append(t)
        routes = set()
        for page_ref in page_refs:
            try:
                routes.add(_page_route(page_ref, public_pages))
            except (ValueError, TypeError):
                unresolved.append('public_page_unresolved_or_ambiguous')
        if len(routes) > 1:
            unresolved.append('multiple_public_page_contexts')
        initial_page = next(iter(routes)) if len(routes) == 1 else None
        rows=[row for row in public_scenarios if row.get('work_item')==ref and row.get('source')=='parsed']
        if not rows: unresolved.append('structured_scenario_missing')
        if any(row.get('work_item')==ref and row.get('source')!='parsed' for row in public_scenarios):
            unresolved.append('unparsed_scenario_present')
        # Do not treat a parsed scenario as proof of unrelated prose clauses.
        remaining=item.get('text','')
        if not isinstance(remaining,str):
            unresolved.append('requirement_text_not_a_string')
            remaining=''
        statements=[]
        for row in rows:
            for phase in ('when','then'):
                phase_steps=row.get(phase)
                if isinstance(phase_steps,list):
                    statements.extend(step.get('text','') for step in phase_steps if isinstance(step,dict))
        for statement in sorted((x for x in statements if isinstance(x,str) and x),key=len,reverse=True):
            remaining=remaining.replace(statement,' ')
        if any(c.isalnum() for c in remaining):unresolved.append('requirement_text_not_fully_bound')
        for index, scenario in enumerate(rows):
            sid=scenario.get('id') or str(index); obligations=[]
            given=scenario.get('given')
            if given not in (None,{}):
                if (not isinstance(given,dict) or given.get('session') not in (None,'anonymous')
                    or given.get('account') is not None or given.get('data')
                    or not isinstance(given.get('text',''),str)
                    or given.get('text','').strip().lower() not in ('','anonymous')):
                    unresolved.append(str(sid)+':unsupported_given')
            whens,thens=scenario.get('when'),scenario.get('then')
            if not isinstance(whens,list) or not isinstance(thens,list):
                unresolved.append(str(sid)+':scenario_steps_unresolved');continue
            if not thens: unresolved.append(str(sid)+':postcondition_missing')
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
                    unresolved.append(str(sid)+':ordered_steps_invalid_or_unconsumed')
                    continue
                ordered = selected
            for phase, position, step in ordered:
                key=f'{sid}:{phase}:{position}'
                if not isinstance(step,dict): unresolved.append(key+':invalid_step');continue
                op=step.get('action' if phase=='when' else 'kind');t=target(step.get('target'));value=step.get('value')
                if not isinstance(op,str):
                    unresolved.append(key+':operation_not_a_string');continue
                if not isinstance(step.get('text'),str):
                    unresolved.append(key+':source_text_not_a_string');continue
                compiled=None
                if phase=='when':
                    if op=='reload': compiled=dict(op='reload')
                    elif op in {'click','open','submit','check','uncheck','fill','select','press'} and t:
                        # An explicit URL-only open is not silently converted to click.
                        mapped='click' if op in {'open','submit'} else op
                        compiled=dict(op=mapped,target=t)
                        if mapped in {'fill','select','press'}:
                            if not isinstance(value,str): compiled=None
                            else: compiled['value']=value
                else:
                    if op in {'visible','hidden'} and t: compiled=dict(op=op,target=t)
                    elif op in {'text','contains_text','value'} and t and isinstance(value,str) and (value or op=='value'):
                        compiled=dict(op=op,target=t,value=value)
                    elif op=='count' and t:
                        try: count=int(value)
                        except (ValueError,TypeError,OverflowError): count=-1
                        if count>0 and not isinstance(value,bool) and str(count)==str(value): compiled=dict(op='count',target=t,value=count)
                    elif op=='url' and isinstance(value,str) and value.startswith('/'):
                        compiled=dict(op='url',value=value)
                    elif op=='visual' and t:
                        try:
                            canonical = normalize_visual(value)
                            # The source text, not an author/model assertion, owns the oracle.
                            from .platform_task_ir import _then_step
                            parsed = _then_step(step['text'])
                            if (parsed.get('kind') == 'visual' and parsed.get('target') == t
                                    and parsed.get('value') == canonical):
                                compiled=dict(op='visual',target=t,value=canonical)
                            else:
                                unresolved.append(key+':visual_source_binding_mismatch')
                        except (ValueError, TypeError, OverflowError):
                            unresolved.append(key+':visual_invalid_exact_constraint')
                if compiled is None: unresolved.append(key+':unsupported_or_ambiguous')
                else: obligations.append(dict(id=key,source=step.get('text'),step=compiled))
            scenarios.append(dict(id=sid,obligations=obligations))
        record=dict(requirement_id=ref,scenarios=scenarios,elements=elements,unresolved=unresolved)
        if initial_page is not None:
            record['initial_page'] = initial_page
        record['sha256']=hashlib.sha256(json.dumps(record,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        contracts[ref]=record
    return contracts

def matches(actual,required):
    if actual.get('op')!=required['op']: return False
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
        if required['op'] == 'count' and got.get('within') != required['target'].get('within'):
            return False
    return True

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
        if contract.get('initial_page') is not None:
            bound = []
            for case in cases:
                expected_url = dict(op='url', value=contract['initial_page'])
                page_bound = case['steps'][:2] == [dict(op='goto', value=contract['initial_page']), expected_url]
                for index, step in enumerate(case['steps'][2:], 2):
                    if step.get('op') in {'click','fill','select','check','uncheck','press'}:
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
                interaction_ops={'click','fill','select','check','uncheck','press','reload'}
                actual_actions=[s for s in case.get('steps',[]) if s.get('op') in interaction_ops]
                required_actions=[o['step'] for o in scenario['obligations'] if o['step']['op'] in interaction_ops]
                if len(actual_actions)!=len(required_actions) or any(not matches(a,b) for a,b in zip(actual_actions,required_actions)):
                    continue
                position=0;matched_count=0;steps=case.get('steps',[])
                for obligation in scenario['obligations']:
                    while position<len(steps) and not matches(steps[position],obligation['step']):
                        if position and matched_count and steps[position].get('op') in {'goto','reload','click','fill','select','check','uncheck','press'}:
                            position=len(steps);break
                        position+=1
                    if position==len(steps): break
                    position+=1;matched_count+=1
                else:
                    if scenario['obligations']:
                        matched[ref].append(dict(scenario_id=scenario['id'],case=case.get('name')));found=True
            if not found:missing.append(dict(requirement_id=ref,scenario_id=scenario['id'],reason='ordered_scenario_not_executed'))
        used_cases={m['case'] for m in matched[ref]}
        for case in cases:
            if case.get('name') not in used_cases and any(s.get('op') in {'click','fill','select','check','uncheck','press','reload'} for s in case.get('steps',[])):
                missing.append(dict(requirement_id=ref,case=case.get('name'),reason='interaction_not_bound_to_public_scenario'))
        for element in contract['elements']:
            if not any(target_matches(step.get('target'),element) and (
                    step.get('op') in {'visible','visual','click','check','uncheck','fill','select','press'}
                    or step.get('op') == 'count' and type(step.get('value')) is int and step['value'] > 0)
                    for c in cases for step in c.get('steps',[])):
                missing.append(dict(requirement_id=ref,target=element,reason='public_role_not_verified'))
        if not contract['unresolved'] and len(missing)==before: covered.append(ref)
    return dict(complete=bool(contracts) and not missing and not unresolved,covered=covered,missing=missing,unresolved=unresolved,matched=matched,contract_hashes={r:c['sha256'] for r,c in contracts.items()})

def require_coverage(plan,ir):
    result=plan_coverage(plan,ir)
    if not result['complete']:raise ValueError('requirement_constraints_incomplete: '+json.dumps(result,ensure_ascii=False,sort_keys=True))
    return result

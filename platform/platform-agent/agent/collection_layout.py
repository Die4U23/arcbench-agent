"""Controller-reviewed spatial assertions anchored in a public scenario."""
from copy import deepcopy


LAYOUT_OP = 'collection_layout'


class _LayoutContract(dict):
    """Keep supplemental review separate from the original public contract's identity."""

    def __init__(self, contract, sid, review):
        super().__init__(contract)
        self.layouts = dict(getattr(contract, 'layouts', {}))
        if sid in self.layouts:
            raise ValueError('layout_scenario_already_reviewed')
        self.layouts[sid] = deepcopy(review)


def _named(target):
    return (isinstance(target, dict) and set(target) == {'role', 'name'} and
            all(isinstance(v, str) and v.strip() and len(v) <= 500 for v in target.values()))


def _public_sources(ir, ref, sid, anchors):
    items = {item['id']: item for item in ir['work_items']}
    owners = set()
    owner = ref
    while owner is not None:
        if owner in owners or owner not in items:
            raise ValueError('layout_public_ancestry_unresolved')
        owners.add(owner)
        owner = items[owner].get('parent')
    scenarios = [s for s in ir['scenarios'] if s.get('id') == sid and s.get('source') == 'parsed']
    if len(scenarios) != 1 or scenarios[0].get('work_item') != ref:
        raise ValueError('layout_public_scenario_unbound')
    scenario = scenarios[0]
    evidence = {}
    if not isinstance(anchors, list):
        raise ValueError('layout_public_sources_missing')
    for anchor in anchors:
        if not isinstance(anchor, dict):
            raise ValueError('layout_public_anchor_invalid')
        phase, owner = anchor.get('phase'), anchor.get('owner')
        keys = {'phase', 'owner', 'span', 'quote'}
        if phase == 'description' and isinstance(owner, str) and owner in owners:
            text = items[owner].get('text')
        elif phase in ('given', 'when', 'then') and owner == ref and anchor.get('scenario_id') == sid:
            keys |= {'scenario_id', 'index'}
            source = [scenario['given']] if phase == 'given' else scenario[phase]
            index = anchor.get('index')
            if type(index) is not int or not 0 <= index < len(source):
                raise ValueError('layout_public_source_index_invalid')
            text = source[index].get('text')
        else:
            raise ValueError('layout_public_source_owner_or_phase_invalid')
        span, quote = anchor.get('span'), anchor.get('quote')
        if (set(anchor) != keys or not isinstance(text, str) or not isinstance(quote, str) or not quote.strip()
                or not isinstance(span, list) or len(span) != 2 or any(type(i) is not int for i in span)
                or not 0 <= span[0] < span[1] <= len(text) or text[span[0]:span[1]] != quote):
            raise ValueError('layout_public_source_span_mismatch')
        evidence.setdefault(phase, []).append(quote)
    if not {'given', 'when', 'then'} <= evidence.keys():
        raise ValueError('layout_requires_public_setup_actions_and_result')
    case_sources = [(None, items[ref].get('text', ''))]
    for row in ir['scenarios']:
        if row.get('source') == 'parsed' and row.get('work_item') == ref:
            for field in [row.get('given'), *row.get('when', []), *row.get('then', [])]:
                if isinstance(field, dict) and isinstance(field.get('text'), str):
                    case_sources.append((row['id'], field['text']))
    return evidence, case_sources


def bind_reviewed_layout(ir, ref, sid, review):
    """An independent controller review supplies the relation; the case author supplies only steps."""
    from .behavior_contracts import _VerifiedRequirements, requirement_contracts
    if not isinstance(review, dict) or set(review) != {'sources', 'target', 'members', 'states', 'member_sources'}:
        raise ValueError('layout_review_invalid')
    states, member_sources = review['states'], review['member_sources']
    if (not isinstance(review['sources'],list) or not isinstance(member_sources, dict) or not isinstance(states,list)
            or any(not isinstance(state,dict) or set(state) != {'relation','action','source'} for state in states)):
        raise ValueError('layout_requires_per_obligation_sources')
    state_sources = [state['source'] for state in states]
    evidence, case_sources = _public_sources(ir, ref, sid, [*review['sources'], *state_sources, *member_sources.values()])
    quotes = [q for texts in evidence.values() for q in texts]
    target, members = review['target'], review['members']
    if not _named(target) or not any(target['name'] in q for q in quotes):
        raise ValueError('layout_public_container_unbound')
    if not isinstance(members, list) or not 2 <= len(members) <= 24:
        raise ValueError('layout_requires_distinct_named_records')
    names = set()
    for member in members:
        if not isinstance(member, dict) or set(member) != {'role', 'name', 'contents'}:
            raise ValueError('layout_public_record_invalid')
        loc = {k: member[k] for k in ('role', 'name')}
        fields = member['contents']
        if (not _named(loc) or loc['name'] in names or not isinstance(fields, list) or not fields or
                any(not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in fields)):
            raise ValueError('layout_public_record_identity_or_content_unbound')
        source = member_sources.get(loc['name'])
        if not source or any(v not in source['quote'] for v in [loc['name'], *fields]):
            raise ValueError('layout_public_record_identity_or_content_unbound')
        names.add(loc['name'])
    if names != member_sources.keys():
        raise ValueError('layout_public_member_source_inventory_mismatch')
    from .behavior_contracts import MAX_STEPS
    if not 1 <= len(states) <= (MAX_STEPS - 1) // 2:
        raise ValueError('layout_state_sequence_outside_case_bounds')
    pairs = []
    for state in states:
        source = state['source']
        if (source['phase'] not in ('then','description') or any(other['source'] == source and
                other['relation'] != state['relation'] for other in states)):
            raise ValueError('layout_spatial_state_source_not_distinguished')
        action = state['action']
        if (not isinstance(action, dict) or set(action) != {'op', 'target'} or action['op'] != 'click' or
                not _named(action['target']) or not any(action['target']['name'] in q for q in evidence['when'])):
            raise ValueError('layout_public_state_action_unbound')
        if state['relation'] not in ('single_column', 'multiple_columns'):
            raise ValueError('layout_public_relation_invalid')
        check = dict(op=LAYOUT_OP, target=target, value=dict(relation=state['relation'], members=members))
        pairs.append([action, check])
    contracts = dict(requirement_contracts(ir))
    contracts[ref] = _LayoutContract(contracts[ref], sid, dict(sources=review['sources'], pairs=pairs,
                                state_sources=state_sources, member_sources=member_sources,
                                scenario_id=sid, case_sources=case_sources))
    return _VerifiedRequirements(ir, contracts)


def _case_quote_matches(case, review):
    quote = case.get('requirement_quote')
    if not isinstance(quote, str) or not 8 <= len(quote) <= 500:
        return False
    sources = [(sid, text) for sid, text in review['case_sources'] if quote in text]
    if len(sources) != 1 or sources[0][0] != review['scenario_id']:
        return False
    text = sources[0][1]
    return text.find(quote) == text.rfind(quote)


def layout_plan_evidence(cases, review):
    """Source-bound step positions are planned evidence; only the browser can observe geometry."""
    pairs = review['pairs']
    states = [dict(relation=check['value']['relation'], source=source, action=action, assertion=check,
                   witnesses=[]) for (action,check),source in zip(pairs,review['state_sources'])]
    matched = []
    for case_index,case in enumerate(cases):
        if not _case_quote_matches(case,review):
            continue
        positions, intact = _layout_observations(case['steps'],pairs)
        observed = [[case['steps'][a],case['steps'][b]] for a,b in positions]
        next_state = 0
        for i,pair in enumerate(observed):
            state = next((j for j in range(next_state,len(pairs)) if pairs[j] == pair), None)
            if state is not None:
                a,b = positions[i]
                states[state]['witnesses'].append(dict(case=case['name'], action_index=a, assertion_index=b))
                next_state = state + 1
        if intact and observed == pairs:
            matched.append(case_index)
    for state in states:
        state['status'] = 'planned' if state['witnesses'] else 'missing'
    return dict(scenario_id=review['scenario_id'], status='planned' if matched else 'missing',
                execution='not_run', members=review['member_sources'], states=states,
                matched_cases=[cases[i]['name'] for i in matched], matched_case_indices=matched)


def _layout_observations(steps, pairs):
    """Pure observations may intervene, but a mutation invalidates the pending state."""
    observed, pending, intact = [], None, True
    mutations = {'click','press','fill','check','uncheck','select','goto','reload'}
    for index,step in enumerate(steps):
        if step.get('op') == LAYOUT_OP:
            if pending is not None:
                observed.append((pending,index))
            else:
                intact = False
            pending = None
        elif any(step == action for action, _ in pairs):
            intact = intact and pending is None
            pending = index
        elif pending is not None and step.get('op') in mutations:
            pending, intact = None, False
    return observed, intact and pending is None


def compile_layout_diagnostic(ir, ref, sid, review, plan):
    """Compile one reviewed spatial diagnosis without discharging other public obligations."""
    from .behavior_checks import _emit_plan, _fields, _text, _validate_case_steps, requirement_bindings
    from .behavior_contracts import project_requirements, requirement_contracts
    bound = bind_reviewed_layout(ir, ref, sid, review)
    contract = requirement_contracts(bound)[ref]
    _fields(plan, {'cases'})
    if not isinstance(plan['cases'], list) or len(plan['cases']) != 1:
        raise ValueError('layout_diagnostic_requires_one_case')
    case, = plan['cases']
    _fields(case, {'name', 'requirement_id', 'requirement_quote', 'steps'})
    _text(case['name'], 120)
    quote = _text(case['requirement_quote'], 500)
    binding, = requirement_bindings(project_requirements(bound, [ref]))
    if case['requirement_id'] != ref or len(quote) < 8 or not any(quote in s for s in binding['quote_sources']):
        raise ValueError('layout_diagnostic_quote_unbound')
    _validate_case_steps(case['steps'], contract, 0)
    if layout_plan_evidence([case], contract.layouts[sid])['status'] != 'planned':
        raise ValueError('reviewed_layout_obligations_missing_or_wrong_state')
    return dict(source=_emit_plan(plan, {ref: contract}), steps=case['steps'], diagnostic_only=True,
                requirement_id=ref, scenario_id=sid)


LAYOUT_SNAPSHOT_JS = r'''
  const {nodes, expected} = args;
  const visible = el => el.isConnected && el.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  if (!visible(root) || nodes.some((el,i) => !root.contains(el) || !visible(el) ||
    nodes.some((other,j) => i !== j && el.contains(other)) ||
    !readRecordFields(el,[expected.members[i].name,...expected.members[i].contents]))) return false;
  const boxes = nodes.map(el => el.getBoundingClientRect());
  if (boxes.some(b => b.width <= 0 || b.height <= 0)) return false;
  let sideBySide = false;
  const single = expected.relation === 'single_column';
  for (let i=0; i<boxes.length; i++) {
    for (const b of boxes.slice(i+1)) {
      const a = boxes[i];
      const xOverlap = Math.min(a.right,b.right) > Math.max(a.left,b.left);
      const yOverlap = Math.min(a.bottom,b.bottom) > Math.max(a.top,b.top);
      if (xOverlap && yOverlap || single && (!xOverlap || yOverlap)) return false;
      if (!xOverlap && yOverlap) sideBySide = true;
    }
  }
  return single || sideBySide;
}'''

LAYOUT_OBSERVER_JS = r'''
async function assertCollectionLayout(scope, expected) {
  await expect.poll(async () => {
    if (await scope.count() !== 1) return false;
    const records = await Promise.all(expected.members.map(m => scope.getByRole(m.role,{name:m.name,exact:true}).elementHandles()));
    try {
      if (records.some(matches => matches.length !== 1)) return false;
      return await scope.evaluate(readLayoutSnapshot,{nodes:records.map(matches=>matches[0]),expected});
    } finally {
      await Promise.all(records.flat().map(handle => handle.dispose()));
    }
  }, {message:'Public collection layout, unique identities and owned content: '+expected.relation}).toBe(true);
}'''

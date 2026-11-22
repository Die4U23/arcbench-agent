"""Source-grounded browser observations. A proposal never defines a requirement verdict."""
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .navigation_contracts import SEED_HEADER, _ordered_rows
from .platform_task_ir import (_ANNOTATION, _ANONYMOUS_PHRASES, _annotations, _session,
                               _strict_statement, _then_step, _when_step)
from .public_scenario_metadata import bind_public_step_references, normalize_public_scenario, unique_public_fields


MAX_OBSERVATION_OPERATIONS = 10
MAX_SLICE_OBSERVATION_OPERATIONS = 26  # 24 proposed operations plus entry and optional clock.
MAX_OBSERVATION_OPTIONS = 8
_TIME = re.compile(r'\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d)?\b')
_ZONE = re.compile(r'\b(?:UTC|[A-Z][a-zA-Z_+-]*(?:/[A-Za-z_+-]+)+)\b')
_CLICK = ('activate ', 'click ', 'press ')
_SCOPES = {'navigation', 'main', 'form', 'region', 'dialog', 'toolbar', 'group'}
_VISIBLE = {'heading', 'textbox', 'searchbox', 'button', 'link', 'dialog', 'region', 'status',
            'navigation', 'checkbox', 'radio', 'combobox', 'tab'}


def _environment(given, root_text, declared=None):
    """Account for the complete Given. Additional data, actions or conditions cannot be assumed."""
    if given.get('session') != 'anonymous' or given.get('data') or given.get('account'):
        return None
    text = given.get('text', '')
    if not isinstance(text, str) or not text or len(text) > 600:
        return None
    rest = _ZONE.sub(' ', _TIME.sub(' ', text)).casefold().replace('-', ' ')
    if not any(phrase in rest for phrase in _ANONYMOUS_PHRASES):
        return None
    for phrase in _ANONYMOUS_PHRASES:
        rest = rest.replace(phrase, ' ')
    if not any(term in rest for term in ('homepage', 'home page')):
        return None
    rest = rest.replace('homepage', ' ').replace('home page', ' ')
    allowed = {'the', 'a', 'an', 'user', 'visitor', 'is', 'on', 'at', 'starts', 'and',
               'browser', 'benchmark', 'clock', 'time', 'set', 'to'}
    if set(re.findall(r'[^\s.,;]+', rest)) - allowed:
        return None
    clock_lines = [p for p in root_text.split('\n\n') if 'clock' in p.casefold() and _TIME.search(p)]
    instants = _TIME.findall(text)
    zones = set(_ZONE.findall(text))
    if not instants and ('clock' in rest or 'time' in rest):
        return None
    for line in clock_lines:
        instants.extend(_TIME.findall(line))
        zones.update(_ZONE.findall(line))
    if declared is not None:
        if not isinstance(declared, dict):
            return None
        if 'benchmark_now' in declared:
            instant = declared['benchmark_now']
            if not isinstance(instant, str) or not _TIME.fullmatch(instant):
                return None
            instants.append(instant)
        if 'timezone' in declared:
            if not isinstance(declared['timezone'], str):
                return None
            zones.add(declared['timezone'])
    if not instants and not zones:
        return {}
    if not zones and instants and all(s.endswith(('Z', '+00:00')) for s in instants):
        zones.add('UTC')
    if len(zones) != 1:
        return None
    zone_name, = zones
    try:
        zone = ZoneInfo(zone_name)
        if not instants:
            return dict(timezone=zone_name)
        resolved = set()
        for instant in instants:
            stamp = datetime.fromisoformat(instant)
            local = stamp.astimezone(zone) if stamp.tzinfo else stamp.replace(tzinfo=zone)
            if stamp.tzinfo is None and local.utcoffset() != local.replace(fold=1).utcoffset():
                return None
            resolved.add(local.isoformat())
    except (ValueError, ZoneInfoNotFoundError):
        return None
    if len(resolved) != 1:
        return None
    return dict(clock=resolved.pop(), timezone=zone_name)


def scenario_options(ir, refs, *, scenario_id=None):
    wanted = set(refs)
    items = {i['id']: i for i in ir.get('work_items', ()) if isinstance(i, dict) and 'id' in i}
    options = []
    for s in ir.get('scenarios', ()):
        if scenario_id is not None and s.get('id') != scenario_id:
            continue
        ref = s.get('work_item')
        if s.get('source') != 'parsed' or ref not in wanted or ref not in items:
            continue
        s = normalize_public_scenario(ir, s)
        if len(s.get('when', ())) != 1 or len(s.get('then', ())) != 1:
            continue
        when, then = s['when'][0]['text'], s['then'][0]['text']
        if not when.casefold().startswith(_CLICK) or len(when) > 400 or len(then) > 600:
            continue
        item = items[ref]
        ancestors, seen = [], {ref}
        parent = item.get('parent')
        while parent in items and parent not in seen:
            seen.add(parent)
            row = items[parent]
            ancestors.append(dict(id=parent, text=row.get('text', '')[:1500]))
            parent = row.get('parent')
        if parent is not None:
            continue
        root = items[ancestors[-1]['id']] if ancestors else item
        owner_text = item.get('text', '')[:2000]
        local = '\n'.join([owner_text, *(row['text'] for row in ancestors[:4])])
        if not _positive_visibility(then, _annotations(local)):
            continue
        environment = _environment(s['given'], root.get('text', ''), root.get('environment'))
        if environment is None:
            continue
        option = dict(scenario_id=s['id'], requirement_id=ref, name=s.get('name'),
                      given=s['given']['text'], when=when, then=then, owner_text=owner_text,
                      ancestor_texts=ancestors[:4], environment=environment)
        hints = _scope_hints(ir, items, when, local)
        if hints:
            option['scope_hints'] = hints
        options.append(option)
        if len(options) == MAX_OBSERVATION_OPTIONS:
            break
    return options


def _positive_visibility(text, names=()):
    # A publicly declared accessible name may itself contain a negative word.
    # Only mask a complete role-qualified mention; preserve all surrounding conditions.
    for name, role in sorted(set(names), key=lambda entry: -len(entry[0])):
        if name and role in _VISIBLE:
            mention = re.escape(name + ' ' + role)
            text = re.sub(r'(?<!\w)' + mention + r'(?!\w)', ' ', text)
    words = set(re.findall(r'[\w]+', text.casefold()))
    return (bool(words & {'visible', 'shown', 'displayed', 'appears'})
            and not words & {'not', 'no', 'hidden', 'invisible', 'unless', 'if', 'disappears'})


def _scope_hints(ir, items, action, local):
    seen, hints = {}, []
    for element in ir.get('ui_elements', ()):
        name, role, ref = element.get('name'), element.get('role'), element.get('work_item')
        if (element.get('source') != 'parsed' or not name or name not in action
                or ref not in items or (name, role) not in _annotations(items[ref].get('text', ''))):
            continue
        seen.setdefault(name, {}).setdefault(role, ref)
    for name, roles in seen.items():
        if len(roles) == 1:
            role, ref = next(iter(roles.items()))
            if role in _SCOPES and not any(_names_role(local, r, name) for r in _SCOPES):
                hints.append(dict(name=name, role=role, source_requirement=ref))
    return hints


def _name(value, text):
    if not isinstance(value, str) or not value.strip() or len(value) > 120 or value.strip() != value:
        raise ValueError('observation requires a bounded exact public name')
    if any(ord(c) < 32 for c in value) or not re.search(r'(?<!\w)' + re.escape(value) + r'(?!\w)', text):
        raise ValueError('target name is absent from its public clause')
    return value


def _names_role(text, role, name):
    # Parse declarations, never infer a role from the application's rendered output.
    if (name, role) in _annotations(text):
        return True
    terms = [role] + (['navigation landmark'] if role == 'navigation' else [])
    labels = [name, '"' + name + '"', "'" + name + "'"]
    for label in labels:
        for term in terms:
            if any(re.search(r'(?<!\w)' + re.escape(phrase) + r'(?!\w)', text, flags=re.IGNORECASE)
                   for phrase in (label + ' ' + term, term + ' ' + label, term + ' named ' + label)):
                return True
    return False


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() <= set(required) | set(optional):
        raise ValueError('unsupported observation fields')


def _full_target(phrase, name, roles):
    phrase = phrase.removeprefix('the ').strip()
    if _ANNOTATION.fullmatch(phrase):
        return any(n == name and r in roles for n, r in _annotations(phrase))
    labels = [name, '"' + name + '"', "'" + name + "'"]
    return any(phrase == label or any(phrase in (label + ' ' + role, role + ' ' + label)
                                      for role in roles) for label in labels)


def compile_observation(options, proposal):
    _keys(proposal, ('scenario_id', 'click', 'visible'))
    matches = [row for row in options if row['scenario_id'] == proposal['scenario_id']]
    if len(matches) != 1:
        raise ValueError('public scenario is absent or ambiguous')
    row, = matches
    click = proposal['click']
    _keys(click, ('roles', 'name'), ('within',))
    roles = click['roles']
    if (not isinstance(roles, list) or not all(isinstance(r, str) for r in roles)
            or not 1 <= len(roles) <= 2 or len(set(roles)) != len(roles) or set(roles) - {'button', 'link'}):
        raise ValueError('unsupported action roles')
    name = _name(click['name'], row['when'])
    declarations = '\n'.join([row['when'], row['then'], row['owner_text']]
                              + [a['text'] for a in row['ancestor_texts']])
    bound = {r for r in ('button', 'link') if _names_role(declarations, r, name)}
    if bound:
        if len(bound) != 1 or bound != set(roles):
            raise ValueError('action roles differ from the public role binding')
    elif len(roles) == 1 or any(n == name and r in {'tab', 'radio', 'checkbox', 'menuitem', 'switch'}
                                for n, r in _annotations(declarations)):
        raise ValueError('public text does not bind the proposed action roles')
    action = row['when'].strip().rstrip('.')
    target = action[len(action.split(maxsplit=1)[0]):].strip()
    confirm_scope = False
    if 'within' in click:
        scope = click['within']
        _keys(scope, ('role', 'name'))
        scope_name = _name(scope['name'], row['when'])
        if not isinstance(scope['role'], str) or scope['role'] not in _SCOPES:
            raise ValueError('scope lacks an explicit public role and name')
        if not _names_role(declarations, scope['role'], scope_name):
            confirm_scope = any(h['role'] == scope['role'] and h['name'] == scope_name
                                for h in row.get('scope_hints', ()))
            if not confirm_scope:
                raise ValueError('scope lacks a public declaration')
        for separator in (' within ', ' inside ', ' in '):
            head, sep, tail = target.rpartition(separator)
            if sep and _full_target(tail, scope_name, [scope['role']]):
                target = head
                break
        else:
            raise ValueError('scope is not attached to the public action')
    if not _full_target(target, name, [*roles, 'control']):
        raise ValueError('proposal does not identify the full public action target')
    visible = proposal['visible']
    if not isinstance(visible, list) or not 1 <= len(visible) <= 4:
        raise ValueError('observation requires one to four visible outcomes')
    for outcome in visible:
        _keys(outcome, ('role', 'name'))
        label = _name(outcome['name'], row['then'])
        if not isinstance(outcome['role'], str) or outcome['role'] not in _VISIBLE or not _names_role(declarations, outcome['role'], label):
            raise ValueError('visible outcome lacks an explicit public role and name')
    env = row['environment']
    steps = ([dict(op='clock', value=env['clock'])] if 'clock' in env else []) + [
        dict(op='goto', value='/'), dict(op='same_origin'),
        *([dict(op='unique_named_scope', name=click['within']['name'])] if confirm_scope else []),
        *([dict(op='scope', target=click['within'])] if 'within' in click else []),
        dict(op='click', target=click),
        *(dict(op='visible', target=target) for target in visible)]
    data = json.dumps(dict(id=row['scenario_id'], click=click, visible=visible, env=env,
                           scopeRoles=sorted(_SCOPES) if confirm_scope else []), ensure_ascii=True)
    source = ("const {test,expect}=require('@playwright/test');\nconst o=" + data + ";\n" + """
test.use({storageState:{cookies:[],origins:[]},serviceWorkers:'block',
          ...(o.env.timezone ? {timezoneId:o.env.timezone} : {})});
test(o.id,async({page,baseURL})=>{
  let externalNavigation=false;
  const precondition=reason=>new Error('public_observation_precondition: '+reason);
  try {
    const origin=new URL(baseURL).origin;
    page.on('framenavigated',frame=>{
      if(frame===page.mainFrame() && new URL(frame.url()).origin!==origin) externalNavigation=true;
    });
    await page.context().route('**/*',route=>{
      if(new URL(route.request().url()).origin===origin) return route.continue();
      if(route.request().isNavigationRequest()) externalNavigation=true;
      return route.abort();
    });
    if(o.env.clock) await page.clock.setFixedTime(new Date(o.env.clock));
    const response=await page.goto('/');
    if(!response || !response.ok()) throw new Error('homepage could not be loaded');
    expect(new URL(page.url()).origin).toBe(origin);
  } catch(error) { throw precondition(error.message); }
  const byRole=(scope,role,name)=>scope.getByRole(role,{name,exact:true});
  const within=o.click.within;
  const container=within ? byRole(page,within.role,within.name) : page;
  if(o.scopeRoles.length) {
    try {
      const named=o.scopeRoles.map(role=>byRole(page,role,within.name)).reduce((a,b)=>a.or(b));
      await expect(named).toHaveCount(1);
      await expect(container).toBeVisible();
    } catch(error) { throw precondition('public scope could not be confirmed: '+error.message); }
  }
  try {
    if(within && !o.scopeRoles.length) await expect(container).toBeVisible();
    const target=o.click.roles.map(role=>byRole(container,role,o.click.name)).reduce((a,b)=>a.or(b));
    await target.click();
    for(const outcome of o.visible) await expect(byRole(page,outcome.role,outcome.name)).toBeVisible();
  } catch(error) {
    if(externalNavigation) throw precondition('external navigation');
    throw error;
  }
  if(externalNavigation) throw precondition('external navigation');
});
""")
    return dict(kind='proposed_observation', scenario_id=row['scenario_id'], requirement_id=row['requirement_id'],
                entry_path='/', workspace_precondition=row['given'], operations=len(steps), steps=steps, source=source)


def slice_options(ir, refs, available):
    """Retain the published scenario and prerequisites together when shortening a plan step."""
    from .platform_acceptance import _seed_record
    items = {row['id']: row for row in ir['work_items']}
    owners = {row['id']: row['work_item'] for row in ir['scenarios']}
    for ref in refs:
        item = items.get(ref)
        if not item or set(item.get('depends_on', ())) - available:
            continue
        ancestors = []
        parent = item.get('parent')
        seen = {ref}
        while parent in items and parent not in seen:
            seen.add(parent)
            ancestor = items[parent]
            ancestors.append({'id': parent, 'text': ancestor.get('text', '')})
            parent = ancestor.get('parent')
        if parent:
            continue
        for raw in ir['scenarios']:
            if raw.get('source') != 'parsed' or raw.get('work_item') != ref:
                continue
            s = normalize_public_scenario(ir, raw)
            bindings, issues = bind_public_step_references(ir, s)
            if issues or any(b['kind'] == 'requirement' and b['id'] not in available | {ref}
                             for b in bindings):
                continue
            given = s.get('given') or {}
            when = '\n'.join(w['text'] for w in s.get('when', ()))
            then = '\n'.join(w['text'] for w in s.get('then', ()))
            if not given.get('text') or not when or not then:
                continue
            cited = re.findall(r'\\ref\[(requirement|scenario)\]\(([^()]+)\)', given['text']+'\n'+when)
            if any((owners.get(target) if kind == 'scenario' else target) not in available | {ref}
                   for kind, target in cited):
                continue
            root = items[ancestors[-1]['id']] if ancestors else item
            yield dict(kind='step_slice', requirement_id=ref, scenario_id=s['id'], name=s.get('name'),
                       given=given['text'], session=given.get('session'), when=when, then=then,
                       **({'ordered_steps': s['ordered_steps']} if 'ordered_steps' in s else {}),
                       owner_text=item.get('text', ''), ancestor_texts=ancestors,
                       environment=root.get('environment') or {},
                       **({'auth': ir['auth']} if given.get('session') == 'authenticated' and ir.get('auth') else {}),
                       **({'given_data': given['data']} if given.get('data') else {}),
                       **({'given_account': given['account'],
                           'public_account': _seed_record(ir, given['account'])} if given.get('account') else {}))


def _public_operation(line):
    for phase, kinds in [('when', {'click', 'check', 'uncheck', 'fill', 'reload'}),
                         ('then', {'visible'})]:
        bound = _strict_statement(line, phase)
        if not bound:
            continue
        op = bound.get('action', bound.get('kind'))
        if op in kinds:
            if op == 'reload':
                return dict(op=op)
            return dict(op=op, target=bound['target'], **({'value': bound['value']} if op == 'fill' else {}))
    return None


def _require_public_setup(row, setup, actions, outcomes):
    given = row['given'].strip().splitlines()
    env = _environment(dict(text=given[0], session=row.get('session'), data=row.get('given_data'),
                            account=row.get('given_account')), '', row.get('environment')) if given else None
    if env is None:
        raise ValueError('public Given needs a bound entry or prerequisite diagnostic')
    required = [_public_operation(line.strip()) for line in given[1:] if line.strip()]
    if None in required or setup != required:
        raise ValueError('setup must establish each explicit public Given operation in order')
    required = [_public_operation(line.strip()) for line in row['when'].splitlines() if line.strip()]
    if None in required or actions != required:
        raise ValueError('proposed actions must match the selected public operations in order')
    visible = [_public_operation(line.strip()) for line in row['then'].splitlines() if line.strip()]
    if any(dict(op='visible', target=target) not in visible for target in outcomes):
        raise ValueError('observations must identify explicitly visible public outcomes')
    return {'timezone': env.get('timezone'), 'benchmark_now': env.get('clock')}


def _public_execution_order(row, actions, observations):
    if 'ordered_steps' not in row:
        if len(actions) != 1:
            raise ValueError('public order is unknown for this multi-action observation')
        return [*actions, *observations]
    scenario = dict(when=[_when_step(text) for text in row['when'].splitlines()],
                    then=[_then_step(text) for text in row['then'].splitlines()],
                    ordered_steps=row['ordered_steps'])
    if len(scenario['when']) != len(actions):
        raise ValueError('public action boundaries do not match the proposal')
    pending = list(observations)
    sequence = []
    index = 0
    for phase, step in _ordered_rows(scenario):
        if phase == 'when':
            if step['action'] != actions[index]['op']:
                raise ValueError('unsupported ordered public action')
            sequence.append(actions[index])
            index += 1
        else:
            observation = dict(op=step['kind'], target=step['target'])
            if observation not in pending:
                raise ValueError('missing or unsupported ordered public observation')
            sequence.append(observation)
            pending.remove(observation)
    if pending or not sequence or sequence[-1]['op'] != 'visible':
        raise ValueError('public observation needs a bound result after the final action')
    return sequence


def compile_slice_observation(row, proposal, *, require_public_setup=False):
    """Compile a bounded diagnostic proposed from public text. Its PASS is local to the observed actions."""
    from .behavior_checks import _locator
    from .ui_inspection import _KEYS, _ROLES, _role_target
    _keys(proposal, ('scenario_id', 'setup', 'actions', 'expect'))
    if proposal['scenario_id'] != row['scenario_id']:
        raise ValueError('slice scenario mismatch')
    setup, actions, outcomes = (proposal[k] for k in ('setup', 'actions', 'expect'))
    if (not all(isinstance(seq, list) for seq in (setup, actions, outcomes)) or not outcomes
            or len(setup) + len(actions) + len(outcomes) > MAX_SLICE_OBSERVATION_OPERATIONS - 2):
        raise ValueError('expected bounded setup, actions and nonempty public observations')
    if not any(isinstance(action, dict) and action.get('op') != 'visible' for action in actions):
        raise ValueError('a public step slice must exercise an action')
    if require_public_setup:
        row = dict(row, environment=_require_public_setup(row, setup, actions, outcomes))
    if row.get('session') == 'authenticated' and not any(isinstance(s, dict) and s.get('op') == 'visible' for s in setup):
        raise ValueError('authenticated entry needs an observed setup condition')
    public = '\n'.join([row['given'], row['when'], row['then'], row['owner_text'],
                         *(a['text'] for a in row['ancestor_texts'])])
    timeout = '{timeout:actionBudget()}' if require_public_setup else ''

    def locator(target, evidence):
        _role_target(target, allow_scope=True)
        _name(target['name'], evidence)
        if 'within' in target:
            _name(target['within']['name'], public)
        return _locator(target)

    def statement(action):
        op = action.get('op') if isinstance(action, dict) else None
        if op == 'reload':
            _keys(action, ('op',))
            return f"await page.reload({timeout});"
        if op not in ('click', 'focus', 'fill', 'press', 'check', 'uncheck', 'visible'):
            raise ValueError('unsupported public observation action')
        with_value = op in ('fill', 'press')
        _keys(action, ('op', 'target', 'value') if with_value else ('op', 'target'))
        loc = locator(action['target'], public)
        if op == 'visible':
            return f'await expect({loc}).toBeVisible({timeout});'
        if op == 'press' and action['value'] not in _KEYS:
            raise ValueError('unsupported keyboard action')
        if op == 'fill':
            _name(action['value'], public)
        args = [json.dumps(action['value'])] if with_value else []
        if timeout:
            args.append(timeout)
        return f'await {loc}.{op}({",".join(args)});'

    setup_lines = [statement(a) for a in setup]
    for target in outcomes:
        locator(target, row['then'])
        declared = {role for role in _ROLES if role in row['then'].casefold()
                    and _names_role(row['then'], role, target['name'])}
        if declared and target['role'] not in declared:
            raise ValueError('observation contradicts the public result role')
    observations = [dict(op='visible', target=t) for t in outcomes]
    journey = (_public_execution_order(row, actions, observations) if require_public_setup
               else [*actions, *observations])
    checks = [statement(a) for a in journey]
    steps = [*setup, *journey]
    if require_public_setup:
        # Public UI operations and their postconditions are application behavior here.
        checks = setup_lines + checks
        setup_lines = []
    return _slice_program(row, steps, setup_lines, checks, timed_actions=require_public_setup)


def _slice_program(row, steps, setup_lines, checks, *, timed_actions=False):
    env = row.get('environment', {})
    options = dict(storageState=dict(cookies=[], origins=[]), serviceWorkers='block')
    if env.get('timezone'):
        options['timezoneId'] = env['timezone']
    clock = env.get('benchmark_now')
    # Resolve a naive public instant in the declared browser timezone.
    clock_setup = [f'await page.clock.setFixedTime(await page.evaluate(s=>new Date(s).getTime(),{json.dumps(clock)}));'] if clock else []
    source = "\n".join([
        "const {test,expect}=require('@playwright/test');", f'test.use({json.dumps(options)});',
        f'test({json.dumps(row["scenario_id"])},async({{page,baseURL}})=>{{',
        "const origin=new URL(baseURL).origin;",
        # Let Playwright return the operation/locator error before it closes the browser.
        *(["const started=performance.now();",
           "const actionBudget=()=>Math.max(1,Math.floor(test.info().timeout-(performance.now()-started)-500));"]
          if timed_actions else []),
        # Gate each Chromium request, including redirect hops, before it reaches the network.
        # The browser owns the response body, which may still be streaming after the assertion.
        """const network=await page.context().newCDPSession(page);
const pending=new Set(); let networkError;
network.on('Fetch.requestPaused', ({requestId,request}) => {
  const allowed=new URL(request.url).origin===origin;
  const command=allowed ? network.send('Fetch.continueRequest',{requestId}) :
                          network.send('Fetch.failRequest',{requestId,errorReason:'BlockedByClient'});
  pending.add(command);
  command.then(()=>pending.delete(command),error=>{pending.delete(command);networkError=error;});
});
await network.send('Network.enable');
await network.send('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
try {""",
        "try {", *clock_setup,
        "const response=await page.goto('/'); if(!response?.ok())throw Error('homepage unavailable');",
        *setup_lines,
        "} catch(e){throw Error('public_observation_precondition: '+e.message);}",
        *checks, "expect(new URL(page.url()).origin).toBe(origin);",
        """} finally {
  await network.send('Network.setBlockedURLs',{urls:['*']});
  while(pending.size) await Promise.all(pending);
  await network.send('Fetch.disable');
  if(networkError) throw networkError;
}
});"""])
    steps = [dict(op='goto', value='/'), *steps]
    if clock:
        steps.insert(0, dict(op='clock', value=clock))
    return dict(kind='proposed_observation', scenario_id=row['scenario_id'], requirement_id=row['requirement_id'],
                entry_path='/', workspace_precondition=row['given'], operations=len(steps), steps=steps, source=source)


def _public_sign_in(row):
    from .platform_acceptance import _field_value
    given, marker, declarations = row['given'].partition(SEED_HEADER)
    auth = row.get('auth')
    if _session(given) != 'authenticated' or not isinstance(auth, dict):
        return None
    try:
        accounts = []
        if marker:
            for line in declarations.splitlines():
                _, sep, raw = line.partition(': ')
                if not sep or not line.startswith('- '):
                    return None
                data = json.loads(raw, object_pairs_hook=unique_public_fields)
                if isinstance(data, dict) and 'accounts' in data:
                    accounts.extend(data['accounts'])
        if row.get('given_account'):
            linked = row.get('public_account')
            if not isinstance(linked, dict) or (accounts and accounts != [linked]):
                return None
            accounts = [linked]
        if len(accounts) != 1 or not isinstance(accounts[0], dict):
            return None
        record, = accounts
        username = record.get('username')
        if not isinstance(username, str) or not username or len(username) > 120:
            return None
        if not row.get('given_account'):
            _name(username, given)
        password = record.get('password')
        if not isinstance(password, str) or not password or len(password) > 200 or password == username:
            return None
        entry, submit = auth['login_entry'], auth['submit']
        for target in (entry, submit):
            _keys(target, ('role', 'name'))
            if target['role'] not in ('link', 'button') or not isinstance(target['name'], str) or not target['name']:
                return None
        if entry == submit or not isinstance(auth['fields'], list) or len(auth['fields']) != 2:
            return None
        fields = []
        credentials = {k: record[k] for k in ('username', 'email', 'mobile', 'password') if k in record}
        for field in auth['fields']:
            label = field['label']
            parts = [part.strip() for part in label.split('/')]
            if any(part.casefold() == 'username' for part in parts):
                value = username
            else:
                values = {v for part in parts if (v := _field_value(credentials, part)) is not None}
                if len(values) != 1:
                    return None
                value = values.pop()
            fields.append((label, value))
        identities = {record.get(k) for k in ('username', 'email', 'mobile') if isinstance(record.get(k), str)}
        if (sum(v == password for _, v in fields) != 1
                or sum(v in identities for _, v in fields) != 1
                or len({k for k, _ in fields}) != 2):
            return None
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        return None
    return entry, fields, submit


def public_prerequisite_probe(row):
    """Probe a published credential submission independently of the unbound downstream scenario."""
    from .behavior_checks import _locator
    binding = _public_sign_in(row)
    if binding is None:
        return None
    entry, fields, submit = binding
    steps = [dict(op='click', target=entry)]
    interactions = [f"await {_locator(entry)}.click({{timeout:3500}});"]
    for label, value in fields:
        name = json.dumps(label)
        loc = f"page.getByLabel({name},{{exact:true}}).or(page.getByPlaceholder({name},{{exact:true}}))"
        interactions.append(f"await {loc}.fill({json.dumps(value)},{{timeout:3500}});")
        steps.append(dict(op='fill', target=dict(label=label), value_source='public_account_record'))
    steps.extend([dict(op='click', target=submit), dict(op='observe_credential_response')])
    # Only attribute a rejection to this interaction if the browser sent both public inputs
    # to this origin. A successful HTTP response alone proves neither login nor later setup.
    values = json.dumps([value for _, value in fields])
    checks = ['let receipt;', 'try {', *interactions,
              f'const values={values};',
              """const observed=page.waitForResponse(response=>{
  const req=response.request();
  if(req.method()!=='POST' || new URL(req.url()).origin!==origin) return false;
  try {
    const fields=Object.values(req.postDataJSON()||{});
    return values.every(v=>fields.includes(v));
  } catch { return false; }
},{timeout:3500}).catch(()=>null);""",
              f"await {_locator(submit)}.click({{timeout:3500}}); receipt=await observed;",
              "} catch {throw Error('public_observation_precondition: public sign-in controls could not be exercised');}",
              """if(receipt && [400,401,403,422].includes(receipt.status()))
  throw Error('Public sign-in submission with the specified account was rejected: HTTP '+receipt.status());
throw Error('public_observation_precondition: '+(receipt?'Public credentials submitted; HTTP '+receipt.status()+'; ':'No credential response identified; ')+
    'the remaining published setup and downstream action have not been verified');"""]
    return dict(_slice_program(row, steps, [], checks), scope='public_prerequisite')

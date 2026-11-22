"""Exercise publicly declared moves out of a grouped collection, with destination readback."""
import re

from .group_setup import bind_group_setup
from .grouped_collections import _inspect, bind_group_readback
from .platform_task_ir import _ANNOTATION, _annotations, _when_step
from .public_scenario_metadata import bind_public_step_references, normalize_public_scenario


def _one(values, reason):
    rows = list(values)
    if len(rows) != 1:
        raise ValueError('group_exclusion_' + reason)
    return rows[0]


def _clauses(ir):
    from .behavior_contracts import _public_clauses
    for item in _rows(ir, 'work_items'):
        text = item.get('text')
        if isinstance(text, str):
            for start, end in _public_clauses(text):
                clause = text[start:end]
                if len(_annotations(clause)) != len(list(_ANNOTATION.finditer(clause))):
                    raise ValueError('group_exclusion_invalid_annotation')
                yield dict(work_item=item['id'], source='text', span=[start, end], text=clause)


def _rows(ir, key):
    rows = ir.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get('id'), str)
                                         for row in rows):
        raise ValueError('group_exclusion_source_unbound')
    return rows


def _excluded(clause, entity, count):
    # The domain and quantifier must describe the groups this scenario actually inspects.
    match = re.fullmatch(r'([\w -]+) ' + re.escape(entity) +
                         r's (?:do not|never) appear in (either|any) (?:section|group|collection)\.?',
                         clause, re.I)
    if match and (match[2].casefold() != 'either' or count == 2):
        states = re.split(r',\s*(?:and\s+)?|\s+and\s+', match[1].casefold())
        if len(set(states)) == len(states) and all(re.fullmatch(r'[\w-]+', state) for state in states):
            return states
    return []


def _target(pair, within=None):
    name, role = pair
    return dict(role=role, name=name, **(dict(within=within) if within else {}))


def _past_forms(word):
    return {word, word + 'ed', word.removesuffix('e') + 'ed'}


def _navigation_views(clauses, state, entity):
    # A state can be named by the displayed population or by the destination
    # itself (e.g., an action's regular past participle). Actual state is
    # independently witnessed by the record's identity/content at that destination.
    for src in clauses:
        controls = _annotations(src['text'])
        skeleton = _ANNOTATION.sub('@', src['text']).rstrip('.')
        match = re.fullmatch(r'(?:clicking|selecting|activating) @ (?:displays|shows|lists) '
                             r'([\w-]+) ' + re.escape(entity) +
                             r's(?: in @)?(?:, excluding [\w ,\-]+| separately from [\w \-]+)?', skeleton, re.I)
        if not match or not controls or controls[0][1] not in {'button', 'link'}:
            continue
        if len(controls) not in (1, 2) or len(controls) == 2 and controls[1][1] != 'region':
            continue
        exclusion = re.search(r', excluding (.+)$', skeleton, re.I)
        if exclusion and re.search(r'\b' + re.escape(state) + r'\b', exclusion[1], re.I):
            continue
        population = match[1].casefold()
        if state == population or state in _past_forms(controls[0][0].casefold()):
            yield src, controls[0], controls[1] if len(controls) == 2 else None, population


def _journeys(ir, nav, entity, field):
    # Consume the complete action prefix up to navigation. Destination-only
    # search by text would also accept an Undo or a mutation of another record.
    placeholder = '{' + field + '}'
    for raw in _rows(ir, 'scenarios'):
        if (not isinstance(raw.get('work_item'), str) or not isinstance(raw.get('given'), dict)
                or not isinstance(raw['given'].get('text'), str)
                or not isinstance(raw.get('when'), list) or any(not isinstance(step, dict)
                or not isinstance(step.get('text'), str) for step in raw['when'])):
            continue
        row = normalize_public_scenario(ir, raw)
        if row.get('source') != 'parsed' or bind_public_step_references(ir, row)[1]:
            continue
        given = (row.get('given') or {}).get('text', '')
        for index, step in enumerate(row.get('when') or []):
            text = step.get('text', '')
            order = row.get('ordered_steps')
            if step != _when_step(text) or index or not isinstance(order, list) or order[:1] != [dict(phase='when', index=0)]:
                continue
            commands = re.split(r',\s*(?:then\s+)?|\s+then\s+', text.rstrip('.'), flags=re.I)
            mutations, hover = [], False
            for command in commands:
                navigation = re.fullmatch(r'(?:open|click|select|activate) ' + re.escape(nav) + r' in (.+)', command, re.I)
                if navigation and mutations:
                    end = text.find(command) + len(command)
                    yield row['work_item'], navigation[1], hover, mutations, dict(
                        work_item=row['work_item'], scenario_id=row['id'], source='when', index=index,
                        span=[0, end], text=text[:end])
                    break
                pointing = re.fullmatch(r'hover (.+?)(?: if necessary)?', command, re.I)
                if pointing and not hover and not mutations:
                    subject = pointing[1]
                    if subject.casefold() != 'that ' + entity.casefold() and subject not in given:
                        break
                    hover = True
                    continue
                action = re.fullmatch(r'(click|activate|choose|select) (.+?)(?: from the menu)?', command, re.I)
                if not action:
                    break
                kind = 'menuitem' if action[1].casefold() in {'choose', 'select'} else 'button'
                if (not mutations and (kind != 'button' or action[2].count(placeholder) != 1)
                        or mutations and (kind != 'menuitem' or len(mutations) != 1)):
                    break
                mutations.append((action[2], kind))


def _movement(clauses, owner, actions, nav, region, entity):
    owned = [row for row in clauses if row['work_item'] == owner]
    record = r'(?:that|the|a) (?:selected |active |regular )*' + re.escape(entity)
    for index, src in enumerate(owned):
        controls = _annotations(src['text'])
        if controls[:len(actions)] != actions:
            continue
        syntax = _ANNOTATION.sub('@', src['text']).rstrip('.')
        if len(actions) == 1:
            if not re.match(r'^(?:clicking|activating|selecting) @ moves? ', syntax, re.I):
                continue
        elif not re.fullmatch(r'(?:(?:users?|operators?) )?(?:reveal (?:a|the) ' + re.escape(entity)
                              + r"'s actions by hovering(?: if necessary)?, then )?"
                              r'(?:click|activate) @ and (?:choose|select) @', syntax, re.I):
            continue
        # Retain a source declaration for the mutation and a directional outcome.
        # A two-step menu can describe the outcome in the immediately following sentence.
        for effect in owned[index:index + (2 if len(actions) == 2 else 1)]:
            annotated = _annotations(effect['text'])
            rendered = _ANNOTATION.sub(lambda m: _annotations(m[0])[0][0], effect['text'])
            # Only the control that actually initiates the mutation may be
            # the grammatical subject (or the explicit anaphor "this action").
            if len(actions) == 1:
                prefix = r'^(?:clicking|activating|selecting) ' + re.escape(actions[0][0]) + ' '
            else:
                verb = actions[-1][0].casefold().split()[0]
                stem = verb.removesuffix('e')
                nouns = {verb, verb + 'ment', stem + 'ion', stem + 'ation', stem + 'ing',
                         'this action', 'the action', 'this selection'}
                prefix = r'^(?:' + '|'.join(map(re.escape, sorted(nouns))) + r') '
            prefix += (r'(?:immediately )?(?:removes ' + record
                       + r' from (?:the )?[\w -]+, (?:and )?)?')
            lead = re.match(prefix + r'moves? (?:it|' + record + r') ', rendered, re.I)
            if not lead:
                continue
            destinations = [region] if region else [c for c in annotated if c[1] == 'region']
            for destination in destinations:
                if not destination:
                    continue
                bound = '|'.join(re.escape(v) for v in {destination[0], nav})
                move = re.match(r'(?:out of [\w -]+ )?(?:to|into) (?:the )?(?:'
                                + bound + r')(?=$|[.,;]| and | without )', rendered[lead.end():], re.I)
                if move and not re.search(r"\b(?:not|never|unless|if|may|might|instead|except|only when)\b|n't\b", rendered, re.I):
                    yield destination, [src] if src == effect else [src, effect]


def _return_controls(clauses, workspace, navigation):
    for src in clauses:
        controls = _annotations(src['text'])
        scope = (navigation, 'navigation')
        if scope not in controls:
            continue
        # Locate the exact control whose declared selected state identifies the
        # open workspace. Sharing a sentence with a navigation landmark is insufficient.
        tokens = list(_ANNOTATION.finditer(src['text']))
        for left, right in zip(tokens, tokens[1:]):
            landmark, = _annotations(left[0])
            entry, = _annotations(right[0])
            link = src['text'][left.end():right.start()].strip()
            if (landmark == scope and entry[0] == workspace and entry[1] in ('button', 'link')
                    and re.fullmatch(r'(?:is )?expanded and|(?:opens )?with|has|contains|provides|shows', link, re.I)
                    and re.match(r'\s+(?:is )?(?:selected|current)\b', src['text'][right.end():], re.I)
                    and not re.search(r"\b(?:if|unless|not|never|without)\b|n't\b", src['text'][:right.end()], re.I)):
                yield src, entry


def _state_path(ir, clauses, state, entity, field, workspace):
    paths = []
    for nav_src, nav, region, population in _navigation_views(clauses, state, entity):
        for owner, navigation, hover, actions, journey in _journeys(ir, nav[0], entity, field):
            # A destination name by itself cannot override which population the
            # source says it displays. Bind aliases to the invoked operation.
            if population != state and population not in _past_forms(actions[-1][0].casefold().split()[0]):
                continue
            scope = (navigation, 'navigation')
            # A destination and a return action belong to the same public
            # navigation landmark. Bind the return label to its declared role.
            entries = list(_return_controls(clauses, workspace, navigation))
            roles = {entry for _, entry in entries}
            if len(roles) != 1:
                continue
            for destination, movement_sources in _movement(clauses, owner, actions, nav[0], region, entity):
                paths.append(dict(state=state, actions=actions, hover=hover, destination=destination,
                                  navigate=_target(nav, _target(scope)), back=_target(next(iter(roles)), _target(scope)),
                                  sources=[nav_src, journey, *movement_sources, entries[0][0]]))
    return _one(paths, 'transition_unbound')


def bind_group_exclusions(ir, item, scenario, *, suffix='arc', keyboard=False):
    """Return independent source-bound diagnostics, each with a live sibling control."""
    _rows(ir, 'scenarios')
    _rows(ir, 'work_items')
    scenario = normalize_public_scenario(ir, scenario)
    original = bind_group_readback(ir, item, scenario, suffix=suffix)
    groups, entity, field = _inspect(original['source_clauses']['when'])
    workspace = re.fullmatch(r'(.+?) is open', original['source_clauses']['given'].split('. ', 1)[0])
    if not workspace or len(original['members']) < 2:
        raise ValueError('group_exclusion_controls_unbound')
    clauses = list(_clauses(ir))
    exclusion, states = _one(((source, states) for source in clauses if source['work_item'] == item['id']
                              and (states := _excluded(source['text'], entity, len(groups)))), 'states_unbound')
    probes = []
    for index, state in enumerate(states):
        path = _state_path(ir, clauses, state, entity, field, workspace[1])
        # Independent cases can share the same application/database.
        case_number = index + 1 + (len(states) if keyboard else 0)
        group = bind_group_readback(ir, item, scenario, suffix=suffix, variant=f'-{case_number}')
        role = group['record_role']
        steps = bind_group_setup(ir, item, scenario, group['members'], record_role=role, identity_field=field)
        steps.extend(group['steps'])
        # Preserve an active witness in every original group. Reuse the public
        # creation sequence under distinct identities for the mutation witnesses.
        moved_group = bind_group_readback(ir, item, scenario, suffix=suffix, variant=f'-{case_number}-move')
        moving = moved_group['members']
        steps.extend(bind_group_setup(ir, item, scenario, moving, record_role=role, identity_field=field))
        members = group['members'] + moving
        subject = moving[index % len(moving)]
        identity = (subject['title'], role)
        active = _target(identity, _target((subject['group'], groups[subject['group']])))
        steps.extend([dict(op='count', target=active, value=1), dict(op='visible', target=active),
                      dict(op='record_fields', target=active, value=[subject['title'], subject['content']])])
        if path['hover'] and not keyboard:
            steps.append(dict(op='hover', target=active))
        template, kind = path['actions'][0]
        control = _target((template.replace('{' + field + '}', subject['title']), kind), active)
        menu = _target(path['actions'][1]) if len(path['actions']) == 2 else None
        if menu:
            steps.append(dict(op='hidden', target=menu))
        if keyboard:
            # Move the pointer away from the record. The destination readback
            # witnesses the key's effect without forcing focus or synthesizing a click.
            steps.extend([dict(op='keyboard_reachable', target=control),
                          dict(op='activate_focused', target=control, value='Enter')])
        else:
            steps.append(dict(op='click', target=control))
        if menu:
            steps.extend([dict(op='visible', target=menu), dict(op='click', target=menu)])
        steps.append(dict(op='click', target=path['navigate']))
        destination = _target(path['destination'])
        owned = _target(identity, destination)
        steps.extend([dict(op='count', target=destination, value=1), dict(op='visible', target=destination),
                      dict(op='count', target=owned, value=1), dict(op='visible', target=owned),
                      dict(op='record_fields', target=owned, value=[subject['title'], subject['content']]),
                      dict(op='click', target=path['back'])])
        for name, kind in groups.items():
            scope = _target((name, kind))
            steps.extend([dict(op='count', target=scope, value=1), dict(op='visible', target=scope),
                          dict(op='count', target=_target(identity, scope), value=0)])
        for sibling in members:
            if sibling == subject:
                continue
            control = _target((sibling['title'], role), _target((sibling['group'], groups[sibling['group']])))
            steps.extend([dict(op='count', target=control, value=1), dict(op='visible', target=control),
                          dict(op='record_fields', target=control, value=[sibling['title'], sibling['content']])])
        probes.append(dict(state=state, requirement_id=item['id'], scenario_id=scenario['id'], steps=steps,
                           sources=[exclusion, *path['sources']], members=members, subject=subject,
                           record_action=dict(name=template, role=path['actions'][0][1]), public_hover=path['hover'],
                           diagnostic_only=True))
    return probes


def compile_group_exclusion_probes(ir, ref, scenario_id, entry_path='/', *, suffix='arc', keyboard=False):
    from .behavior_contracts import public_route
    from .behavior_checks import _emit_plan
    item = _one((row for row in _rows(ir, 'work_items') if row['id'] == ref), 'item_unbound')
    scenario = _one((row for row in _rows(ir, 'scenarios') if row.get('work_item') == ref and row['id'] == scenario_id),
                    'scenario_unbound')
    probes = bind_group_exclusions(ir, item, scenario, suffix=suffix, keyboard=keyboard)
    for index, probe in enumerate(probes):
        steps = [dict(op='goto', value=public_route(entry_path)), *probe['steps']]
        mode = 'keyboard-exclusion' if keyboard else 'exclusion'
        case = dict(name=f'{scenario_id}-{mode}-{index + 1}', requirement_id=ref, steps=steps)
        probe.update(steps=steps, source=_emit_plan(dict(cases=[case]), {ref:dict(scenarios=[])}))
    return probes

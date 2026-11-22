"""Join a selected editor property to the first observation of its newly saved record."""
import re

from .group_selection import _state_controls
from .platform_task_ir import _ANNOTATION, _annotations, _given, _when_step
from .public_scenario_metadata import bind_public_step_references, given_facts, normalize_public_scenario


def _save_property(clause, declaration, dialog, entity):
    head, cause, effect = clause.removesuffix('.').partition(' so ')
    if not cause or head != declaration:
        return None
    # A save has an actor, a created object and an explicitly retained property.
    action, chosen, prop = effect.casefold().partition(' with the chosen ')
    name, state, tail = prop.rpartition(' state')
    if not chosen or not state or tail or not name.isidentifier():
        return None
    forms = {dialog.casefold(), dialog.casefold().split()[-1]}
    for verb in ('creates', 'saves'):
        owner, result, obj = action.partition(' ' + verb + ' ')
        if result and obj == 'the ' + entity.casefold():
            operation, _, form = owner.partition(' ')
            if operation in {'closing','saving'} and form.removeprefix('the ') in forms:
                return name
    return None


def _property_transition(clause, entity, destination, prop, previous):
    first, last = _ANNOTATION.finditer(clause)
    lead = clause[:first.start()].casefold()
    relation = clause[first.end():last.start()].casefold()
    result = clause[last.end():].removesuffix('.').casefold()
    if lead.strip() not in {'clicking','pressing','activating'} or result.strip() != 'with pressed=true':
        return False
    transfer, join, update = relation.strip().partition(' and ')
    assignment = update.split()
    if not join or assignment not in ([v, 'its', prop, 'action', 'to'] for v in ('changes', 'sets', 'updates')):
        return False
    verb, _, rest = transfer.partition(' ')
    if verb not in {'moves','places','transfers'}:
        return False
    subject, directed, group = rest.partition(' into ')
    if not directed or group.removesuffix(' exactly once') != destination.casefold():
        return False
    # The action owns both the transferred record and its updated property.
    words = subject.split()
    return (len(words) >= 2 and words[0] in {'that','the'} and words[-1] == entity.casefold()
            and words[1:-1] in ([], [previous], [previous,'active']))


# Unquoted public example values can contain spaces, but cannot introduce a
# second actor, condition, command or operand boundary into this construction.
_OPERATORS = frozenset('and or but if unless not never no without except then other another previous earlier '
                       'different second any each all every neither nor instead only from after before with through '
                       'using create save open click press select toggle set'.split())


def _example(value):
    words = value.casefold().split()
    return bool(words) and all(re.fullmatch(r"[\w-]+(?:\.[\w-]+)*", w) and w not in _OPERATORS for w in words)


def _saved_snapshot(linked, dialog, selected, entity, inverse):
    given = linked['given']['text']
    command, _, arguments = given.removesuffix('.').partition(' ')
    if command.casefold() not in {'create','add'}:
        return None
    data, via, editor = arguments.partition(' through ')
    identity, with_content, content = data.partition(' with ')
    isolation, _, literal = identity.partition(' ')
    if not (via and with_content and isolation.casefold() in {'isolated','case-isolated'}
            and _example(literal) and _example(content)
            and editor == dialog + ' with ' + selected + ' selected'):
        return None
    whens = linked.get('when', [])
    if not whens or linked.get('ordered_steps', [])[:1] != [dict(phase='when',index=0)]:
        return None
    when = whens[0]['text']
    if whens[0] != _when_step(when):
        return None
    # Resolve the definite object before any mutation. Later clauses cannot
    # establish what the editor initially saved.
    first, sep, rest = when.partition(', ')
    verb, _, subject = first.casefold().partition(' ')
    if not sep or verb != 'hover':
        return None
    parts = subject.split()
    if len(parts) != 4 or parts[:2] != ['the','saved'] or parts[-1] != entity.casefold():
        return None
    category = parts[2]
    next_action = rest.split(', ', 1)[0].removesuffix('.')
    op, _, obj = next_action.partition(' ')
    if op.casefold() not in {'click','activate','press'} or obj != inverse or not _example(category):
        return None
    return category, len(first + sep + next_action)


def saved_record_state(ir, scenario, origin, member, owned, entity, field, setup, categories, previous):
    """Return a finite state duty only if selection, save, named state and observation connect."""
    from .behavior_contracts import _public_clauses
    from .group_setup import _linked_toggle

    selected = origin['selection']
    category = categories[member['group']]
    if not selected or origin['qualifier'] not in (None, category):
        return None
    dialog = origin['dialog']
    identity, setter, close = (origin[k] for k in ('identity_step','selection_step','close_step'))
    chosen = dict(role='button', name=selected, within=dialog)
    if (not identity < setter or close != setter + 1 or setup[identity].get('op') != 'fill'
            or setup[identity]['value'] != member['title'] or setup[identity]['target'].get('within') != dialog
            or setup[setter] != dict(op='set_pressed', target=chosen, value=True)
            or setup[setter]['value'] is not True
            or setup[close:close+2] != [dict(op='click',target=origin['closer']), dict(op='hidden',target=dialog)]):
        return None
    refs, issues = bind_public_step_references(ir, scenario)
    linked = [r for r in refs if r['kind']=='requirement']
    if issues or len(linked) != 1 or linked[0]['step_index'] != 0 or linked[0]['id']==scenario['work_item']:
        return None
    ref = next(r for r in ir['work_items'] if r['id']==linked[0]['id'])
    try:
        _, declaration = _linked_toggle(ir, scenario, (dialog['name'],dialog['role']), selected)
        states, _ = _state_controls(ir, ref, field)
    except ValueError:
        return None
    parameter = '{' + field + '}'
    if states[False].replace(parameter, entity).casefold() != selected.casefold():
        return None

    def source(phase, text, start, end, sid=None, **kwargs):
        return dict(owner=ref['id'], phase=phase, span=[start,end], quote=text[start:end],
                    **({'scenario_id':sid} if sid else {}), **kwargs)

    text = ref['text']
    clauses = _public_clauses(text)
    saves = [(prop,source('text',text,a,b)) for a,b in clauses
             if (prop := _save_property(text[a:b], declaration['text'], dialog['name'], entity))]
    if len(saves) != 1:
        return None
    prop, editor_effect = saves[0]
    pairs = [(a,b) for a,b in clauses if _annotations(text[a:b]) == [(states[False],'button'),(states[True],'button')]]
    destinations = [(dest,source('text',text,a,b)) for a,b in pairs for dest in categories
                    if _property_transition(text[a:b],entity,dest,prop,previous)]
    if not destinations:
        return None
    snapshots = []
    for raw in ir.get('scenarios', []):
        if raw.get('work_item') != ref['id']:
            continue
        linked = normalize_public_scenario(ir, raw)
        if linked.get('source') != 'parsed' or bind_public_step_references(ir, linked)[1]:
            return None
        given = linked['given']
        if (given_facts(given) != given_facts(_given([given['text']], ir.get('entities', []))) or given['session'] == 'authenticated'
                or given['data'] or given['account']):
            continue
        observed = _saved_snapshot(linked, dialog['name'], selected, entity, states[True])
        if observed:
            observed_category, limit = observed
            given, when = linked['given']['text'], linked['when'][0]['text']
            snapshots.append((observed_category,source('given',given,0,len(given),linked['id']),
                              source('when',when,0,limit,linked['id'],index=0)))
    if not snapshots:
        return None
    conflicts = [src for dest,src in destinations if dest != member['group']]
    conflicts.extend(src for adjective,_,src in snapshots if adjective in categories.values() and adjective != category)
    if conflicts:
        return dict(conflict=conflicts)
    if len(pairs) != 1 or len(snapshots) != 1 or snapshots[0][0] != category:
        return None
    _, transition = destinations[0]
    category, prepared, observed = snapshots[0]
    name = states[True].replace(parameter, member['title'])
    return dict(category=category, property=prop, source=observed,
                inputs=[prepared, editor_effect, transition, *origin['save_sources']], before=[dict(op='hover',target=owned)],
                expected=dict(op='pressed',target=dict(role='button',name=name,within=owned),value=True))

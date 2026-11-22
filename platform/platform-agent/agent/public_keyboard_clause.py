"""Read affirmative, quantified keyboard properties on controls of a public entity."""
import re


def _subject(words, entity):
    # Preserve the extent of the owner quantifier. A class of records is wider
    # than the finite objects a published scenario explicitly inspects.
    prefix = (len(words) >= 5 and words[0] in {'in', 'within'} and words[1] in {'each', 'every'} and
              words[2] in {'inspected', 'observed', 'examined'} and words[3:5] == [entity, ','])
    words = words[5:] if prefix else words
    count = words.pop(0) if words[:1] in (['all'], ['each'], ['every']) else None
    if not words:
        return None
    singular = count in {'each', 'every'}
    kinds = {'action': 'action', 'button': 'button'} if singular else {'actions': 'action', 'buttons': 'button'}
    enabled = words[:1] == ['enabled']
    if enabled:
        words = words[1:]
    if prefix:
        if len(words) == 1 and words[0] in kinds:
            return kinds[words[0]], 'inspected_entities', singular, enabled, count
        return None
    if len(words) == 2 and words[0] == entity and words[1] in kinds:
        return kinds[words[1]], 'entity_class', singular, enabled, count
    return None


def parse_scoped_keyboard(text, entity):
    """Return the subject, condition and expected operation only on full consumption."""
    if not isinstance(text, str) or not isinstance(entity, str) or not entity.isidentifier():
        return None
    words = re.findall(r"[\w]+(?:[-'][\w]+)*|[^\w\s]", text.casefold())
    if words[-1:] == ['.']:
        words.pop()
    # This restricted relative clause describes controls made visible by the
    # pointer, as opposed to controls created after clicking or always visible.
    reveals = [index for index, token in enumerate(words) if token in {'revealed', 'shown', 'displayed'}]
    if len(reveals) != 1:
        return None
    index = reveals[0]
    head, tail = words[:index], words[index + 1:]
    relative = head[-2:] if head[-2:-1] in (['that'], ['which']) else []
    owner = _subject(head[:-2] if relative else head, entity.casefold())
    if owner is None:
        return None
    kind, scope, singular, enabled, quantifier = owner
    if relative and relative[1] != ('is' if singular else 'are'):
        return None
    if tail[:7] == ['by', 'hovering', 'the', 'center', 'of', 'that', entity.casefold()]:
        area = 'owner_center'
        tail = tail[7:]
    elif len(tail) >= 2 and tail[0] in {'on', 'upon', 'by'} and tail[1] in {'hover', 'hovering'}:
        area = 'unspecified'
        tail = tail[2:]
    else:
        return None
    if not tail:
        return None
    if tail[:2] == ['must', 'be']:
        tail = tail[2:]
    elif tail[0] in ({'is', 'remains'} if singular else {'are', 'remain'}):
        tail = tail[1:]
    else:
        return None
    if tail == ['keyboard-accessible']:
        ability = 'keyboard_accessibility'
    elif (len(tail) == 3 and tail[0] in {'reachable', 'focusable'} and tail[1] in {'by', 'via', 'using'}
          and tail[2] in {'keyboard', 'tab'}):
        ability = 'keyboard_focus' if tail[2] == 'keyboard' else 'tab_focus'
    else:
        return None
    return dict(owner=entity, owner_scope=scope, control_kind=kind, quantifier=quantifier or 'bare_plural', enabled_only=enabled,
                condition='revealed_on_hover', hover_area=area, ability=ability)


def bind_group_keyboard(lineage, group, selection_steps):
    """Observe real scoped buttons. Retain every wider subject or property as a residual."""
    from .behavior_contracts import _public_clauses
    from .grouped_collections import _inspect

    groups, entity, _ = _inspect(group['source_clauses']['when'])
    rules = {}
    for owner in lineage:
        text = owner.get('text')
        if isinstance(text, str):
            for start, end in _public_clauses(text):
                clause = text[start:end]
                rule = parse_scoped_keyboard(clause, entity)
                if rule is not None:
                    rules[(owner['id'], (start, end), clause)] = rule
    if not rules:
        return [], {}

    inventories = []
    for member in group['members']:
        owner = dict(role=group['record_role'], name=member['title'],
                     within=dict(role=groups[member['group']], name=member['group']))
        witnesses = []
        for step in selection_steps:
            target = step.get('target', {})
            if step['op'] == 'pressed' and target.get('role') == 'button' and target.get('within') == owner:
                identity = dict(role='button', name=target['name'])
                if identity not in witnesses:
                    witnesses.append(identity)
        if 1 <= len(witnesses) <= 8:
            inventories.append(dict(op='hover_action_inventory', target=owner,
                                    value=dict(action_kind='button', witnesses=witnesses)))

    receipts = {}
    for key, rule in rules.items():
        remaining = []
        if len(inventories) != len(group['members']):
            remaining.append('public_button_witness_missing_for_inspected_record')
        if rule['owner_scope'] != 'inspected_entities':
            remaining.append('owner_scope_extends_beyond_inspected_records')
        if rule['control_kind'] != 'button':
            remaining.append('action_kinds_beyond_semantic_buttons_unverified')
        if not rule['enabled_only']:
            remaining.append('disabled_controls_outside_observed_inventory')
        if rule['hover_area'] != 'owner_center':
            remaining.append('hover_locations_beyond_record_center_unverified')
        if rule['ability'] == 'keyboard_accessibility':
            remaining.append('keyboard_activation_outcomes_unverified')
        receipts[key] = dict(rule=rule, remaining=remaining,
                             bound_targets=[op['target'] for op in inventories])
    return inventories, receipts

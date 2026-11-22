"""Build grouped record setup from published form, input and toggle declarations."""
import re

from .autosave_contracts import _dialog_schema
from .grouped_collections import regular_plural, split_next_member
from .platform_task_ir import _ANNOTATION, _annotations, _session
from .public_scenario_metadata import bind_public_step_references


def _role_target(control, within=None):
    name, role = control
    target = dict(name=name, role=role)
    if within is not None:
        target['within'] = within
    return target


def _named_form(ir, source, *, save_sources=None):
    """Resolve an ordered public list of entry, data fields and dismissal controls."""
    forms = []
    for item in ir.get('work_items', []):
        if not isinstance(item, dict) or not isinstance(item.get('text'), str):
            continue
        try:
            provenance = {} if save_sources is not None else None
            form = _dialog_schema(item['text'],save_source=provenance)
        except ValueError:
            continue
        entry, dialog, fields, closer, entity = form
        for ordered_fields in (fields, fields[::-1]):
            names = [control[0] for control in (entry, *ordered_fields, closer)]
            # Match literal names first because control names themselves can contain commas.
            pattern = r'\s*(?:,\s*(?:and\s+)?|\s+and\s+)'.join(map(re.escape, names))
            candidate = (entry, dialog, ordered_fields, closer, entity)
            if re.fullmatch(pattern, source):
                if candidate not in forms:
                    forms.append(candidate)
                if save_sources is not None:
                    save_sources.append(dict(owner=item['id'], **provenance))
    if len(forms) != 1:
        raise ValueError('group_creation_controls_unbound')
    return forms[0]


def _linked_toggle(ir, scenario, dialog, selected):
    from .behavior_contracts import _public_clauses
    refs, errors = bind_public_step_references(ir, scenario)
    if errors:
        raise ValueError('group_setup_reference_unbound')
    linked = {row['id'] for row in refs if row['kind'] == 'requirement'}
    matches = []
    for item in ir.get('work_items', []):
        if not isinstance(item, dict) or item.get('id') not in linked or not isinstance(item.get('text'), str):
            continue
        text = item['text']
        for start, end in _public_clauses(text):
            clause = text[start:end].rstrip('.')
            declaration = clause.split(' so ', 1)[0]
            controls = _annotations(declaration)
            if controls != [(selected, 'button')]:
                continue
            skeleton = _ANNOTATION.sub('@', declaration)
            if re.fullmatch(r'in (?:the )?' + re.escape(dialog[0]) +
                            r',\s*@ toggles (?:its |the )?pressed state', skeleton, re.I):
                matches.append((controls[0], dict(requirement_id=item['id'], source='text',
                    span=[start, start + len(declaration)], text=declaration)))
    if len(matches) != 1:
        raise ValueError('group_selection_control_unbound')
    return matches[0]


def _classification_groups(text, qualifier, entity):
    """Resolve a category from an affirmative public assignment to a named collection."""
    from .behavior_contracts import _public_clauses
    nouns = '|'.join(map(re.escape, (entity, regular_plural(entity))))
    category = re.escape(qualifier) + r'\s+(?:' + nouns + ')'
    destination = r'\s+(?:into|in|under|within|to)\s+(?:(?:exactly )?one |the |a |an )?'
    grouping = r'\b(?:separates|groups|sorts|places|routes|organizes|displays|shows|lists)\s+'
    groups = set()
    for start, end in _public_clauses(text):
        clause = text[start:end].rstrip('.')
        annotations = list(_ANNOTATION.finditer(clause))
        if not annotations:
            continue
        syntax = _ANNOTATION.sub('@', clause)
        if re.search(r"\b(?:not|never|no|except|without|unless|if)\b|n't\b", syntax, re.I):
            continue
        parts = syntax.split('@')
        first = re.fullmatch(r'[^@]*?' + grouping + r'(.+)', parts[0], re.I)
        for index, annotation in enumerate(annotations):
            prefix, suffix = parts[index], parts[index + 1]
            subject = first[1] if first and index == 0 else re.sub(r'^\s*(?:,\s*(?:and\s+)?|and\s+)', '', prefix)
            direct = first and re.fullmatch(category + destination, subject, re.I)
            reverse = (not prefix.strip() and re.fullmatch(
                r'\s+(?:holds|contains|displays|shows|lists)\s+(?:(?:every|each|all|the)\s+)?' + category, suffix, re.I))
            if direct or reverse:
                groups.update(name for name, _ in _annotations(annotation[0]))
    return groups


def bind_group_setup(ir, item, scenario, members, *, record_role=None, identity_field=None, member_sources=None):
    given = scenario.get('given')
    if (not isinstance(given, dict) or not isinstance(given.get('text'), str)
        or given.get('session') != _session(given['text']) or given.get('session') == 'authenticated'
        or given.get('account') is not None or given.get('data')):
        raise ValueError('group_setup_context_unbound')
    # An open workspace is the entry precondition. Resolve form controls from the
    # whole published schema, not from application DOM or assistant-invented ARIA labels.
    workspace, period, setup = given['text'].strip().rstrip('.').partition('. ')
    if not period or not workspace.endswith(' is open'):
        raise ValueError('group_setup_entry_unbound')
    via = re.fullmatch(r'(?:Through|Using) (.+?),\s*(?:create|add) (.+)', setup, re.I)
    if not via:
        raise ValueError('group_setup_creation_unbound')
    saves = []
    entry, dialog, fields, closer, entity = _named_form(ir, via[1], save_sources=saves if member_sources is not None else None)
    pending = via[2]
    data_end = given['text'].index(workspace + period + setup) + len(workspace + period + setup)
    modal = _role_target(dialog)
    if record_role is None or identity_field is None:
        identities = [(name[1:-1], role) for name, role in _annotations(item['text']) if re.fullmatch(r'\{\w+\}', name)]
        if len(identities) != 1:
            raise ValueError('group_setup_identity_role_unbound')
        inferred_field, inferred_role = identities[0]
        identity_field = identity_field or inferred_field
        record_role = record_role or inferred_role
    if sum(field[0].casefold() == identity_field.casefold() for field in fields) != 1:
        raise ValueError('group_setup_identity_field_unbound')
    steps = [dict(op='count', target=dict(role=record_role, name=member['title']), value=0)
             for member in members]
    # Initial data changes follow the Given, independent of assertion order.
    for member in sorted(members, key=lambda m: via[2].find(m['base'] + ' ' + entity + ' with ')):
        start = data_end - len(pending)
        marker = member['base'] + ' ' + entity + ' with ' + member['content']
        if pending.count(marker) != 1:
            raise ValueError('group_setup_member_unbound')
        qualifiers, tail = pending.split(marker, 1)
        # Category adjectives must occur in the public classification of this
        # entity. Other modifiers can specify additional preparation and stay unbound.
        words = qualifiers.strip().casefold().split()
        if words and words[0] in ('a','an','the'):
            words = words[1:]
        words = [w for w in words if w not in ('isolated','case-isolated')]
        if words and _classification_groups(item['text'], ' '.join(words), entity) != {member['group']}:
            raise ValueError('group_setup_qualifier_unbound')
        selection, pending = split_next_member(tail, entity)
        toggle = None
        if selection:
            match = re.fullmatch(r' and (.+) selected', selection)
            if not match:
                raise ValueError('group_setup_selection_unbound')
            toggle, _ = _linked_toggle(ir, scenario, dialog, match[1])
        if member_sources is not None:
            member_sources[member['base']] = dict(
                span=[start, start + len(qualifiers + marker + selection)], qualifier=' '.join(words) or None,
                selection=toggle[0] if toggle else None, dialog=modal, closer=_role_target(closer, modal),
                save_sources=saves)
        steps.extend([dict(op='hidden', target=modal),
                      dict(op='click', target=_role_target(entry)), dict(op='visible', target=modal)])
        for field in fields:
            key = 'title' if field[0].casefold() == identity_field.casefold() else 'content'
            if member_sources is not None and key == 'title':
                member_sources[member['base']]['identity_step'] = len(steps)
            steps.append(dict(op='fill', target=_role_target(field, modal), value=member[key]))
        if toggle:
            if member_sources is not None:
                member_sources[member['base']]['selection_step'] = len(steps)
            steps.append(dict(op='set_pressed', target=_role_target(toggle, modal), value=True))
        if member_sources is not None:
            member_sources[member['base']]['close_step'] = len(steps)
        steps.extend([dict(op='click', target=_role_target(closer, modal)), dict(op='hidden', target=modal)])
    if pending:
        raise ValueError('group_setup_source_unconsumed')
    return steps

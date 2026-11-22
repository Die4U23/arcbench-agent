"""Source-bound, diagnostic probes for creation through a two-field dialog."""

import re

from .platform_task_ir import _ANNOTATION, _annotations, _session, _then_step, _when_step
from .public_scenario_metadata import bind_public_step_references, normalize_public_scenario


def _one(values):
    values = list(values)
    if len(values) != 1:
        raise ValueError('autosave_source_ambiguous')
    return values[0]


def _control_list(text, role):
    """Accept complete, affirmative lists of annotated controls."""
    names = _annotations(text)
    skeleton = _ANNOTATION.sub('@', text)
    if (not re.fullmatch(r'@(?:(?:, |,? and )@)*', skeleton, re.I)
        or not names or any(not name or kind != role for name, kind in names)):
        raise ValueError('autosave_control_list_unbound')
    return names


def _dialog_schema(text, *, save_source=None):
    from .behavior_contracts import _opening_declarations, _public_clauses
    openings = _opening_declarations(text)
    entry, declarations = _one(openings.items())
    destination, _, _ = _one(declarations)
    dialog = (destination['name'], destination['role'])
    if dialog[1] != 'dialog' or entry[1] != 'button':
        raise ValueError('autosave_form_unbound')

    fields, saves = [], []
    name = re.escape(dialog[0])
    refers_to_dialog = rf'(?:{name}|the {re.escape(dialog[0].split()[-1])})'
    for start, end in _public_clauses(text):
        clause = text[start:end].rstrip('.')
        # Field declarations may accompany the opening or belong to their own sentence.
        declaration = re.fullmatch(
            rf'{refers_to_dialog} (?:contains|provides|has) (.+)', clause, re.I)
        if ', where ' in clause and _annotations(clause)[:2] == [entry, dialog]:
            declaration = re.fullmatch(r'(?:users? (?:enter|fill)|(?:it )?(?:contains|has)) (.+)',
                                       clause.split(', where ', 1)[1], re.I)
        if declaration and _ANNOTATION.search(declaration[1]):
            fields.extend(_control_list(declaration[1], 'textbox'))

        controls = _annotations(clause)
        if len(controls) != 1 or controls[0][1] != 'button':
            continue
        syntax = _ANNOTATION.sub('@', clause)
        dismissal = re.fullmatch(
            rf'(?:clicking|activating) @ (?:closes|dismisses) {refers_to_dialog} and (.+)', syntax, re.I)
        if not dismissal:
            continue
        write = re.fullmatch(r'(?:automatically saves|autosaves) (?:one|a|an) (?:active )?'
                             r'([\w -]+?) with (?:the )?(?:entered )?(.+)', dismissal[1], re.I)
        if write:
            saves.append((controls[0], write[1], re.split(r', | and ', write[2]), (start,end)))

    closer, entity, saved, (start,end) = _one(saves)
    by_name = {field[0].casefold():field for field in fields}
    if (len(fields) != len(by_name) or len(saved) != 2 or len(set(saved)) != 2
        or set(map(str.casefold, saved)) != set(by_name) or closer == entry):
        raise ValueError('autosave_fields_unbound')
    if save_source is not None:
        save_source.update(phase='text',span=[start,end],quote=text[start:end])
    return entry, dialog, [by_name[label.casefold()] for label in saved], closer, entity


def _identity(given, field, entity):
    # Recognize an initially absent case-scoped business identity. The prefix is
    # retained as a residual workspace precondition, never mapped to a URL.
    context, sep, absent = given.rstrip('.').rpartition(' and ')
    if not sep or not context.endswith(' is open'):
        raise ValueError('autosave_workspace_unbound')
    parts = absent.split('case-isolated ')
    if len(parts) != 2:
        raise ValueError('autosave_identity_unbound')
    lead, tail = parts
    ending = ' does not yet exist' if lead == 'the ' else ''
    if not ending and lead.casefold() != f'no {entity} uses the '.casefold():
        raise ValueError('autosave_absence_unbound')
    marker = ' ' + field.casefold() + ending
    if not tail.casefold().endswith(marker):
        raise ValueError('autosave_identity_unbound')
    base = tail[:-len(marker)]
    if not re.fullmatch(r'[\w -]+', base) or base != base.strip():
        raise ValueError('autosave_identity_invalid')
    return context, base


def _actions(text, entry, dialog, fields, closer, base):
    def annotated(match):
        name, role = _one(_annotations(match[0]))
        if (name, role) not in (entry, dialog, *fields, closer):
            raise ValueError('autosave_control_unbound')
        return name

    commands = []
    for part in re.split(r'[,;]|\.\s+', _ANNOTATION.sub(annotated, text).rstrip('.')):
        commands.append(re.sub(r'^(?:and |then )', '', part.strip(), flags=re.I))
    first = commands.pop(0)
    if first in ('Click ' + entry[0], 'Activate ' + entry[0]):
        if commands and commands[0] == 'in ' + dialog[0]:
            commands.pop(0)
    elif first != 'Open ' + dialog[0] + ' through ' + entry[0]:
        raise ValueError('autosave_open_unbound')

    refresh = bool(commands and commands[-1].casefold() in {
        'reload the page', 'refresh the page', 'reload the current page', 'refresh the current page'})
    if refresh:
        commands.pop()
    if len(commands) < 2:
        raise ValueError('autosave_action_order_unbound')
    close, sep, exclusion = commands.pop().partition(' without ')
    if close.casefold() not in {verb + ' ' + closer[0].casefold() for verb in ('click', 'activate')}:
        raise ValueError('autosave_close_unbound')
    if sep:
        excluded = re.fullmatch(r'(?:using|clicking|activating) (?:any |the )?([\w -]+?)(?: action)?', exclusion)
        if not excluded or excluded[1].casefold() in {entry[0].casefold(), closer[0].casefold()}:
            raise ValueError('autosave_action_exclusion_conflict')

    first_field, body_field = fields
    names = {f[0].casefold():f for f in fields}
    alternatives = '|'.join(re.escape(f[0]) for f in fields)
    assignments = []
    for command in commands:
        verb, _, rest = command.partition(' ')
        if verb.casefold() not in {'fill', 'enter', 'set'}:
            raise ValueError('autosave_fill_unbound')
        chunks = re.split(rf' and (?=(?:{alternatives}) (?:with|to) )', rest, flags=re.I)
        for chunk in chunks:
            field = re.fullmatch(rf'({alternatives}) (?:with|to) (.+)', chunk, re.I)
            if field:
                assignments.append((names[field[1].casefold()], field[2]))
            elif verb.casefold() == 'enter' and len(commands) == len(chunks) == 1:
                positional = chunk.split(' and ', 1)
                if len(positional) != len(fields):
                    raise ValueError('autosave_positional_fields_unbound')
                assignments.extend(zip(fields, positional))
            else:
                raise ValueError('autosave_fill_unbound')
    if len(assignments) != len(fields) or {f for f, _ in assignments} != set(fields):
        raise ValueError('autosave_fields_ambiguous')
    data = dict(assignments)
    if data[first_field] not in {'the isolated ' + base + ' ' + k for k in ('name', first_field[0].casefold())}:
        raise ValueError('autosave_fill_identity_conflict')
    lines = re.split(r' followed by a newline and |\n', data[body_field])
    if any(not re.fullmatch(r'[\w -]+', line) or line != line.strip() for line in lines):
        raise ValueError('autosave_input_unbound')
    return lines, refresh, [f for f, _ in assignments]


def _readback(text, dialog, fields, entity, lines, refresh):
    # Parse a close outcome and a cardinality-qualified result. Identity and
    # body references must agree with the values assigned by the actions.
    subject, body = (f[0].casefold() for f in fields)
    parts = re.split(r'; |\. | and (?=(?:after (?:reload|refresh) )?exactly )', text.rstrip('.'), flags=re.I)
    if len(parts) != 2:
        raise ValueError('autosave_outcomes_unbound')
    close, result = parts
    modal_name = re.escape(dialog[0])
    dialog_alias = re.escape(dialog[0].split()[-1])
    if not re.fullmatch(rf'(?:{modal_name}|the {dialog_alias}) (?:closes|is (?:closed|hidden))(?: on save)?',
                        close, re.I):
        raise ValueError('autosave_dismissal_unbound')
    temporal = re.match(r'after (?:reload|refresh) ', result, re.I)
    if bool(temporal) != refresh:
        raise ValueError('autosave_reload_order_conflict')
    if temporal:
        result = result[temporal.end():]
    quantifier = re.match(rf'exactly (?:one|1) visible {re.escape(entity)} region ', result, re.I)
    if not quantifier:
        raise ValueError('autosave_result_identity_unbound')
    identity = 'that ' + subject
    predicate = re.fullmatch(rf'(?:with {re.escape(identity)} )?(displays|retains|contains|shows) (.+)',
                             result[quantifier.end():], re.I)
    if not predicate:
        raise ValueError('autosave_result_fields_unbound')
    title, and_, content = predicate[2].partition(' and ')
    if not and_ or title.casefold() not in {identity, 'the entered ' + subject}:
        raise ValueError('autosave_rendered_identity_unbound')
    counts = {'both': 2, 'two': 2, 'three': 3}
    line_reference = re.fullmatch(rf'(both|two|three|[0-9]+) {re.escape(body)} lines', content, re.I)
    line_count = counts.get(line_reference[1].lower(), line_reference[1]) if line_reference else 0
    if content != '\n'.join(lines) and int(line_count) != len(lines):
        raise ValueError('autosave_readback_conflict')


def bind_autosave(ir, item, scenario, *, suffix='arc'):
    if not isinstance(item.get('text'), str) or not isinstance(scenario.get('id'), str):
        raise ValueError('autosave_source_unbound')
    scenario = normalize_public_scenario(ir, scenario)
    _, reference_errors = bind_public_step_references(ir, scenario)
    if reference_errors:
        raise ValueError('autosave_source_references_unbound')
    phases = []
    for key, parse in (('when', _when_step), ('then', _then_step)):
        rows = scenario.get(key)
        if not isinstance(rows, list) or not rows:
            raise ValueError('autosave_source_unbound')
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get('text'), str) or row != parse(row['text']):
                raise ValueError('autosave_source_conflict')
            phases.append(dict(phase=key, index=index))
    if (scenario.get('source') != 'parsed' or scenario.get('work_item') != item['id']
        or scenario.get('ordered_steps') != phases
        or any(type(row.get('index')) != int for row in scenario['ordered_steps'])):
        raise ValueError('autosave_step_order_unbound')
    when = '; '.join(row['text'].rstrip('.') for row in scenario['when'])
    then = '; '.join(row['text'].rstrip('.') for row in scenario['then'])
    entry, dialog, fields, closer, entity = _dialog_schema(item['text'])
    given = scenario.get('given')
    if (not isinstance(given, dict) or not isinstance(given.get('text'), str)
        or given.get('session') != _session(given['text']) or given.get('session') == 'authenticated'
        or given.get('data') or given.get('account') is not None):
        raise ValueError('autosave_setup_unbound')
    context, base = _identity(given['text'], fields[0][0], entity)
    lines, refresh, field_order = _actions(when, entry, dialog, fields, closer, base)
    _readback(then, dialog, fields, entity, lines, refresh)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', suffix):
        raise ValueError('autosave_suffix_invalid')
    ids = [row.get('id') for row in ir['scenarios']
           if isinstance(row, dict) and row.get('work_item') == item['id']]
    if ids.count(scenario['id']) != 1:
        raise ValueError('autosave_case_identity_ambiguous')
    title = f"{base} {suffix}-{ids.index(scenario['id']) + 1}"
    def target(control, within=None):
        name, role = control
        return dict(name=name, role=role, **({'within':within} if within else {}))
    modal = target(dialog)
    obj = target((title, 'region'))
    reads = [dict(op='count', target=obj, value=1), dict(op='visible', target=obj),
             dict(op='rendered_lines', target=obj, value=[title, *lines])]
    steps = [dict(op='count', target=obj, value=0), dict(op='hidden', target=modal),
             dict(op='click', target=target(entry)), dict(op='visible', target=modal),
             *(dict(op='fill', target=target(field, modal), value=title if field == fields[0] else '\n'.join(lines))
               for field in field_order),
             dict(op='click', target=target(closer, modal)), dict(op='hidden', target=modal), *reads]
    if refresh:
        steps.extend([dict(op='reload'), *reads])
    return dict(steps=steps, requirement_id=item['id'], scenario_id=scenario['id'],
                source_clauses=dict(given=given['text'], when=[row['text'] for row in scenario['when']],
                                    then=[row['text'] for row in scenario['then']]),
                workspace_precondition=context, diagnostic_only=True)


def compile_autosave_probe(ir, ref, scenario_id, entry_path='/', *, suffix='arc'):
    """Emit a diagnostic from original public clauses at the caller's application entry.

    The caller supplies a run/worker suffix when contexts share persistent data.
    Logical workspace setup and all residual contract obligations stay open.
    """
    from .behavior_contracts import public_route
    from .behavior_checks import _emit_plan
    item = _one(row for row in ir['work_items'] if row['id'] == ref)
    scenario = _one(row for row in ir['scenarios'] if row['id'] == scenario_id and row['work_item'] == ref)
    probe = bind_autosave(ir, item, scenario, suffix=suffix)
    steps = [dict(op='goto', value=public_route(entry_path)), *probe['steps']]
    case = dict(name=scenario_id, requirement_id=ref, steps=steps)
    probe.update(steps=steps, source=_emit_plan(dict(cases=[case]), {ref:dict(scenarios=[])}))
    return probe

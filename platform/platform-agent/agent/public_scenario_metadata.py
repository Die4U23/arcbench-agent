"""Read historical parsed scenarios and resolve explicitly typed public step references."""
import json
import re

from .platform_task_ir import _consume_step, _given, _scenario
from .sandbox_files import validate_relative_path


_WRAPPER = 'Unsupported public step: '
_PUBLIC_REFERENCE = re.compile(r'\\ref\[(requirement|image)\]\(([^()\s]+)\)')


def unique_public_fields(pairs):
    record = {}
    for key, value in pairs:
        if key in record:
            raise ValueError('duplicate public step field')
        record[key] = value
    return record


def given_facts(given):
    """The public obligations consumed by existing checkers; seed annotations repeat the source text."""
    return {key: value for key, value in given.items() if key != 'seed_prerequisites'}


def normalize_public_scenario(ir, scenario):
    """Fold only a complete, reproducible legacy projection; otherwise keep all original rows."""
    if (scenario.get('source') != 'parsed' or 'public_steps' in scenario
            or not isinstance(scenario.get('when'), list) or not isinstance(scenario.get('then'), list)
            or not isinstance(scenario.get('ordered_steps'), list)):
        return scenario
    raw_steps, givens, whens, thens, order = [], [], [], [], []
    for row in scenario['when']:
        text = row.get('text') if isinstance(row, dict) else None
        if not isinstance(text, str) or not text.startswith(_WRAPPER):
            continue
        try:
            raw = json.loads(text[len(_WRAPPER):], object_pairs_hook=unique_public_fields)
        except (ValueError, RecursionError):
            return scenario
        if (not isinstance(raw, dict) or not {'id', 'references'} <= raw.keys()
                or not isinstance(raw['id'], str) or not raw['id'].strip()
                or not isinstance(raw['references'], list)):
            return scenario
        body = {key: value for key, value in raw.items() if key not in ('id', 'references')}
        phases = [key.lower() for key in body]
        if (not phases or len(set(phases)) != len(phases) or set(phases) - {'given', 'when', 'then'}
                or any(not isinstance(value, str) or not value.strip() for value in body.values())):
            return scenario
        # The historical parser emitted this envelope before processing its G/W/T fields.
        # Consequently only a first Given can acquire a spurious mid-scenario sentinel.
        order.append(dict(phase='when', index=len(whens)))
        whens.append(dict(action='unknown', target=dict(role=None, name=None), value=None, text=text))
        _consume_step(body, givens, whens, thens, order)
        raw_steps.append(raw)
    if not raw_steps:
        return scenario
    entities = ir.get('entities', [])
    if not isinstance(entities, list) or any(not isinstance(item, dict) or not isinstance(item.get('id'), str)
            or not isinstance(item.get('seed_records', []), list) for item in entities):
        return scenario
    if not isinstance(scenario.get('given'), dict):
        return scenario
    expected = dict(given=given_facts(_given(givens, entities)), when=whens, then=thens, ordered_steps=order)
    # JSON distinguishes booleans from integer order indices; ordinary dict equality does not.
    projected = {key: scenario.get(key) for key in expected}
    projected['given'] = given_facts(projected['given'])
    if json.dumps(projected, sort_keys=True) != json.dumps(expected, sort_keys=True):
        return scenario
    clean, _ = _scenario(scenario.get('work_item'), {'id': scenario.get('id'), 'steps': raw_steps}, 0, entities)
    return dict(scenario, **{key: clean[key] for key in ('when', 'then', 'ordered_steps', 'public_steps')})


def _image_binding(items, image_id):
    declarations = [image for item in items for image in (
        item['references'] if isinstance(item.get('references'), list) else [])
        if isinstance(image, dict) and image.get('id') == image_id]
    if not declarations:
        return None
    image = declarations[0]
    if (any(other != image for other in declarations)
            or not {'id', 'reference'} <= image.keys() <= {'id', 'reference', 'page', 'capture'}
            or ('capture' in image and not isinstance(image['capture'], dict))
            or ('page' in image and (not isinstance(image['page'], str) or not image['page'].strip()))):
        return None
    try:
        validate_relative_path(image['reference'])
    except ValueError:
        return None
    return dict(image)


def bind_public_step_references(ir, scenario):
    """Resolve against the full public IR; identities prove no application state or visual match."""
    bindings, issues = [], []
    sid = scenario.get('id')
    source = f'scenarios[{sid}].public_steps'
    rows = scenario.get('public_steps', [])
    if not isinstance(rows, list):
        return [], [dict(reason='invalid_public_step_metadata', source=source)]
    whens, thens = scenario.get('when'), scenario.get('then')
    if isinstance(whens, list) and any(isinstance(step, dict) and isinstance(step.get('text'), str)
            and step['text'].startswith(_WRAPPER) for step in whens):
        return [], [dict(reason='invalid_public_step_metadata', source=source)]
    if not rows:
        return [], []
    items = ir.get('work_items', [])
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        return [], [dict(reason='scenario_reference_binding_unresolved', source=source)]
    if (scenario.get('source') != 'parsed' or not isinstance(scenario.get('work_item'), str)
            or sum(item.get('id') == scenario['work_item'] for item in items) != 1):
        return [], [dict(reason='scenario_reference_binding_unresolved', source=source)]
    given = scenario.get('given') or {}
    if (not isinstance(whens, list) or not isinstance(thens, list) or not isinstance(given, dict)
            or not isinstance(given.get('text', ''), str)):
        return [], [dict(reason='invalid_public_step_metadata', source=source)]
    # Several phases can occupy one public row. Every public row contributed at least one phase.
    max_rows = len(whens) + len(thens) + len(given.get('text', '').splitlines())
    seen_ids, previous_index = set(), -1
    for row in rows:
        if (not isinstance(row, dict) or not {'index'} < row.keys() <= {'index', 'id', 'references'}
                or type(row['index']) is not int or not previous_index < row['index'] < max_rows):
            issues.append(dict(reason='invalid_public_step_metadata', source=source))
            continue
        index, step_id = row['index'], row.get('id')
        previous_index = index
        step_source = f'scenarios[{sid}].steps[{index}]'
        if 'id' in row:
            if not isinstance(step_id, str) or not step_id.strip() or step_id in seen_ids:
                issues.append(dict(reason='invalid_public_step_id', source=step_source + '.id', clause=step_id))
                continue
            seen_ids.add(step_id)
        references = row.get('references', [])
        if not isinstance(references, list):
            issues.append(dict(reason='scenario_reference_binding_unresolved', source=step_source + '.references',
                               step_id=step_id, clause=references))
            continue
        for reference in references:
            match = _PUBLIC_REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
            bound = None
            if match:
                kind, identity = match.groups()
                if kind == 'requirement' and sum(item.get('id') == identity for item in items) == 1:
                    bound = dict(id=identity)
                elif kind == 'image':
                    bound = _image_binding(items, identity)
            if bound is None:
                issues.append(dict(reason='scenario_reference_binding_unresolved', source=step_source + '.references',
                                   step_id=step_id, clause=reference))
            else:
                bindings.append(dict(step_index=index, step_id=step_id, kind=kind, **bound))
    return bindings, issues

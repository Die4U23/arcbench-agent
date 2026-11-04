"""Task contract drafted from requirements and public tests, then checked statically.

The draft is deterministic and names no application. The model keeps
docs/contract.json valid while it implements routes and seed files. Read probes
are parameter-free GET requests; everything else is recorded and not called,
so a check cannot create records before the public tests run.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .platform_seed import _load_requirements

CONTRACT_RELATIVE = 'docs/contract.json'
MAX_NAMES = 80
MAX_NAME_CHARS = 120
_METHODS = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'}
_SEED_FILE = re.compile(r'^seed/[a-z0-9]+(?:-[a-z0-9]+)*\.json$')
_TEMPLATE = re.compile(r'\{|/:')
_DIRECT_NAME = re.compile(
    r"""\bgetBy(?:Label|Text|Placeholder|Title|AltText)\(\s*(['"])(.*?)\1""")
_ROLE_NAME = re.compile(
    r"""\bgetByRole\(\s*['"][^'"]+['"]\s*,\s*\{[^}]{0,300}?\bname\s*:\s*(['"])(.*?)\1""",
    re.S)
_TEST_SUFFIXES = {'.ts', '.js', '.mts', '.cts', '.mjs', '.cjs', '.tsx', '.jsx'}


def _slug(text: str, fallback: str) -> str:
    def norm(value: str) -> str:
        return re.sub(r'[^a-z0-9]+', '-', value.casefold()).strip('-')[:48].strip('-')
    return norm(text) or norm(fallback) or 'item'


def _entities(tree: dict) -> list[dict[str, Any]]:
    items = tree.get('data')
    if not isinstance(items, list):
        return []
    entities = []
    for item in items:
        if not isinstance(item, dict):
            continue
        properties = item.get('properties') if isinstance(item.get('properties'), dict) else {}
        entities.append(dict(
            id=item.get('id'),
            lifecycle=item.get('lifecycle'),
            entity=item.get('entity'),
            fields=sorted(str(key) for key in properties),
        ))
    return entities


def _seed_entries(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    used: set[str] = set()
    seeds = []
    for entity in entities:
        if str(entity.get('lifecycle') or '').casefold() != 'seed':
            continue
        item_id = str(entity.get('id') or '')
        collection = _slug(str(entity.get('entity') or ''), item_id)
        if collection in used:
            collection = _slug(f"{collection}-{item_id}", item_id)
        used.add(collection)
        seeds.append(dict(
            id=entity.get('id'),
            lifecycle='SEED',
            entity=entity.get('entity'),
            collection=collection,
            file=f'seed/{collection}.json',
            fields=list(entity.get('fields') or []),
        ))
    return seeds


def accessible_names(tests_dir: Path | None) -> list[str]:
    """Exact accessible names written in public test and helper sources."""
    if tests_dir is None or not Path(tests_dir).is_dir():
        return []
    found: list[str] = []
    seen: set[str] = set()
    for path in sorted(Path(tests_dir).rglob('*')):
        if not path.is_file() or path.suffix not in _TEST_SUFFIXES or 'node_modules' in path.parts:
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        matches = [(match.start(), match.group(2)) for match in _DIRECT_NAME.finditer(text)]
        matches += [(match.start(), match.group(2)) for match in _ROLE_NAME.finditer(text)]
        for _start, value in sorted(matches):
            name = value.strip()
            if not name or len(name) > MAX_NAME_CHARS or '\n' in name or name in seen:
                continue
            seen.add(name)
            found.append(name)
            if len(found) >= MAX_NAMES:
                return found
    return found


def draft_contract(requirements, tests_dir=None) -> dict[str, Any]:
    """Build the contract the implementer must keep. No model call and no task-specific names."""
    tree = _load_requirements(requirements)
    entities = _entities(tree if isinstance(tree, dict) else {})
    return dict(
        version=1,
        entities=entities,
        seeds=_seed_entries(entities),
        routes=[dict(method='GET', path='/api/health', probe='read', expect=200)],
        accessible_names=accessible_names(Path(tests_dir) if tests_dir is not None else None),
    )


def write_contract(project_dir, document: dict) -> Path:
    path = Path(project_dir) / CONTRACT_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    return path


def read_contract(project_dir) -> dict | None:
    path = Path(project_dir) / CONTRACT_RELATIVE
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def contract_errors(project_dir) -> list[dict[str, str]]:
    """Validate an existing contract. A missing file is not an error here."""
    path = Path(project_dir) / CONTRACT_RELATIVE
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return [dict(path=CONTRACT_RELATIVE, error=f'Invalid JSON: {error}')]
    if not isinstance(data, dict):
        return [dict(path=CONTRACT_RELATIVE, error='Contract must be a JSON object')]
    errors: list[dict[str, str]] = []
    for key in ('entities', 'seeds', 'routes', 'accessible_names'):
        if key in data and not isinstance(data[key], list):
            errors.append(dict(path=CONTRACT_RELATIVE, error=f'{key} must be a list'))
    routes = data.get('routes') if isinstance(data.get('routes'), list) else []
    seen_routes: set[tuple[str, str]] = set()
    for route in routes:
        if not isinstance(route, dict):
            errors.append(dict(path=CONTRACT_RELATIVE, error='Each route must be an object'))
            continue
        method = route.get('method')
        route_path = route.get('path')
        probe = route.get('probe')
        if not isinstance(method, str) or method.upper() not in _METHODS:
            errors.append(dict(path=CONTRACT_RELATIVE, error=f'Unsupported method {method!r}'))
            continue
        method = method.upper()
        if not isinstance(route_path, str) or not route_path.startswith('/') or route_path.startswith('//'):
            errors.append(dict(path=CONTRACT_RELATIVE, error=f'Route path must start with one slash: {route_path!r}'))
            continue
        identity = (method, route_path)
        if identity in seen_routes:
            errors.append(dict(path=CONTRACT_RELATIVE, error=f'Duplicate route {method} {route_path}'))
        seen_routes.add(identity)
        templated = _TEMPLATE.search(route_path) is not None
        if probe == 'read' and (method != 'GET' or templated):
            errors.append(dict(path=CONTRACT_RELATIVE, error=f'{method} {route_path} cannot use probe read'))
        elif probe not in {'read', 'skip'}:
            errors.append(dict(path=CONTRACT_RELATIVE, error=f'{method} {route_path} probe must be read or skip'))
    seeds = data.get('seeds') if isinstance(data.get('seeds'), list) else []
    seen_files: set[str] = set()
    for seed in seeds:
        if not isinstance(seed, dict):
            errors.append(dict(path=CONTRACT_RELATIVE, error='Each seed must be an object'))
            continue
        file = seed.get('file')
        collection = seed.get('collection')
        if not isinstance(file, str) or not _SEED_FILE.fullmatch(file):
            errors.append(dict(path=CONTRACT_RELATIVE, error=f'Seed file must be seed/<collection>.json: {file!r}'))
            continue
        if file in seen_files:
            errors.append(dict(path=CONTRACT_RELATIVE, error=f'Duplicate seed file {file}'))
        seen_files.add(file)
        if not isinstance(collection, str) or f'seed/{collection}.json' != file:
            errors.append(dict(path=CONTRACT_RELATIVE, error=f'Seed collection does not match {file}'))
            continue
        target = Path(project_dir) / file
        if not target.is_file():
            errors.append(dict(path=file, error='Seed file listed in docs/contract.json does not exist'))
            continue
        try:
            parsed = json.loads(target.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            errors.append(dict(path=file, error=f'Invalid JSON: {error}'))
            continue
        if not isinstance(parsed, dict) or parsed.get('collection') != collection:
            errors.append(dict(path=file, error=f'Seed collection must be {collection!r}'))
    return errors

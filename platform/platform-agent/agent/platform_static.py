"""Non-executing source checks in a credential-sanitized trusted parser."""
import hashlib,json,os,posixpath,re,shlex,shutil,subprocess
from pathlib import Path
from .platform_scaffold import has_scaffold_seed_loader
from .platform_seed import validate_seeds
from .sandbox_files import snapshot_project


def _has_deploy_install_before_build(deploy):
    """Check direct launcher commands, not comments or uncalled package scripts.

    This is a conservative static prerequisite, not a shell interpreter or
    proof that a conditional branch runs. Runtime deployment remains unverified.
    """
    installed = False
    for line in deploy.splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=';&|()')
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            return False
        commands = [[]]
        for token in tokens:
            if token in {';', '&&', '||', '&', '|', '(', ')'}:
                commands.append([])
            else:
                commands[-1].append(token)
        for command in commands:
            while command and (command[0] in {'then', 'do', 'exec'} or
                               re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*=.*', command[0])):
                command.pop(0)
            if not command:
                continue
            tool, *args = command
            if ((tool in {'npm', 'pnpm'} and args[:1] in [['ci'], ['install'], ['i']]) or
                    (tool == 'yarn' and (not args or args[0] == 'install'))):
                installed = True
            elif ((tool in {'npm', 'pnpm', 'yarn'} and args[:2] in
                   [['run', 'build'], ['run', 'start']]) or
                  (tool in {'npm', 'pnpm', 'yarn'} and args[:1] in [['build'], ['start']]) or
                  tool in {'node', 'vite', 'tsc'}):
                if not installed:
                    return False
    return installed


def _test_sources(project, tests):
    roots = []
    if tests:
        roots.append(Path(tests))
    local = Path(project) / 'tests'
    if local.is_dir():
        roots.append(local)
    files = []
    seen = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob('*')):
            if path.suffix.lower() not in {'.ts', '.tsx', '.js', '.jsx'} or path.name.endswith('.d.ts'):
                continue
            if 'node_modules' in path.parts:
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            files.append(dict(path=path.relative_to(root).as_posix(), content=path.read_text(encoding='utf-8')))
    return files


def _interaction_errors(sources, test_files):
    if not test_files:
        return []
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Trusted static parser requires Node from the official runner')
    checker = Path(__file__).resolve().parents[1] / 'static-tools' / 'interaction-scope.cjs'
    payload = dict(appFiles=sources, testFiles=test_files)
    clean = {name: value for name, value in os.environ.items() if name.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'}}
    result = subprocess.run([node, str(checker)], input=json.dumps(payload), capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=30, check=False, env=clean, cwd=str(checker.parent))
    if result.returncode:
        raise RuntimeError('Interaction scope parser failed: ' + result.stderr[-1200:])
    return json.loads(result.stdout).get('errors') or []


_COVERAGE_KINDS = {'work_item', 'ui_element', 'scenario'}
_COVERAGE_STATUSES = {'covered', 'partial', 'missing'}


def ui_element_ref(element, index):
    """Stable coverage ref for one parsed UI element."""
    role = str(element.get('role') or '').strip()
    name = str(element.get('name') or '').strip()
    work = element.get('work_item')
    work = '' if work is None else str(work)
    return f'{work}|{index}|{role}|{name}'


def _parsed_ui_elements(ir):
    return [
        item for item in (ir or {}).get('ui_elements') or []
        if isinstance(item, dict) and item.get('source') == 'parsed'
    ]


def coverage_targets(ir):
    """Maps of work-item id, parsed UI ref, and scenario id to their IR records."""
    document = ir if isinstance(ir, dict) else {}
    work, elements, scenarios = {}, {}, {}
    for item in document.get('work_items') or []:
        if isinstance(item, dict) and item.get('id') is not None:
            work[str(item['id'])] = item
    for index, item in enumerate(_parsed_ui_elements(document)):
        elements[ui_element_ref(item, index)] = item
    for item in document.get('scenarios') or []:
        if isinstance(item, dict) and item.get('id') is not None:
            scenarios[str(item['id'])] = item
    return work, elements, scenarios


def seed_coverage(ir):
    """One missing ledger row per work item, parsed UI element, and scenario."""
    work, elements, scenarios = coverage_targets(ir)
    items = [{'kind': 'work_item', 'ref': ref, 'files': [], 'status': 'missing'} for ref in work]
    items.extend({'kind': 'ui_element', 'ref': ref, 'files': [], 'status': 'missing'} for ref in elements)
    items.extend({'kind': 'scenario', 'ref': ref, 'files': [], 'status': 'missing'} for ref in scenarios)
    return {'items': items}


def write_coverage(project, document):
    path = Path(project) / 'docs' / 'coverage.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    return path


def _read_json_object(path):
    try:
        loaded = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def ir_coverage_errors(project):
    """Require docs/task-ir.json and docs/coverage.json to parse, and refs to hit the IR."""
    errors = []
    root = Path(project)
    ir_path = root / 'docs' / 'task-ir.json'
    coverage_path = root / 'docs' / 'coverage.json'
    ir = None
    if not ir_path.is_file():
        errors.append(dict(path='docs/task-ir.json', error='Missing task IR'))
    else:
        try:
            ir = json.loads(ir_path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            errors.append(dict(path='docs/task-ir.json', error=f'Task IR is not valid JSON: {error}'))
        else:
            if not isinstance(ir, dict):
                errors.append(dict(path='docs/task-ir.json', error='Task IR must be a JSON object'))
                ir = None
    if not coverage_path.is_file():
        errors.append(dict(path='docs/coverage.json', error='Missing coverage ledger'))
        return errors
    try:
        coverage = json.loads(coverage_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        errors.append(dict(path='docs/coverage.json', error=f'Coverage ledger is not valid JSON: {error}'))
        return errors
    if not isinstance(coverage, dict) or not isinstance(coverage.get('items'), list):
        errors.append(dict(path='docs/coverage.json', error='Coverage ledger must be an object with an items list'))
        return errors
    if ir is None:
        return errors
    work, elements, scenarios = coverage_targets(ir)
    catalogs = {'work_item': work, 'ui_element': elements, 'scenario': scenarios}
    for index, item in enumerate(coverage['items']):
        label = f'items[{index}]'
        if not isinstance(item, dict):
            errors.append(dict(path='docs/coverage.json', error=f'{label} must be an object'))
            continue
        kind, ref, status, files = item.get('kind'), item.get('ref'), item.get('status'), item.get('files')
        if (kind not in _COVERAGE_KINDS or not isinstance(ref, str) or status not in _COVERAGE_STATUSES
                or not isinstance(files, list) or any(not isinstance(name, str) for name in files)):
            errors.append(dict(path='docs/coverage.json', error=f'{label} must have kind, ref, files, and status'))
            continue
        if ref not in catalogs[kind]:
            errors.append(dict(path='docs/coverage.json', error=f'Coverage ref {ref} does not match a {kind}'))
    return errors


def missing_coverage(project, ir, work_ids=None):
    """Require covered rows backed by current source files; this is not behavioral acceptance."""
    expected = seed_coverage(ir)['items']
    if work_ids is not None:
        allowed = {str(item) for item in work_ids}
        _work, elements, scenarios = coverage_targets(ir)

        def selected(item):
            if item['kind'] == 'work_item':
                return item['ref'] in allowed
            if item['kind'] == 'ui_element':
                return str((elements.get(item['ref']) or {}).get('work_item')) in allowed
            return str((scenarios.get(item['ref']) or {}).get('work_item')) in allowed

        expected = [item for item in expected if selected(item)]
    path = Path(project) / 'docs' / 'coverage.json'
    document = _read_json_object(path) if path.is_file() else None
    found = {}
    if document and isinstance(document.get('items'), list):
        for item in document['items']:
            if isinstance(item, dict) and isinstance(item.get('kind'), str) and isinstance(item.get('ref'), str):
                found[(item['kind'], item['ref'])] = item
    missing = []
    sources = set(snapshot_project(Path(project))) - {'docs/task-ir.json', 'docs/coverage.json'}
    for item in expected:
        current = found.get((item['kind'], item['ref']))
        status = current.get('status') if isinstance(current, dict) else 'missing'
        files = current.get('files') if isinstance(current, dict) else None
        backed = isinstance(files, list) and bool(files) and all(isinstance(name, str) and name in sources for name in files)
        if current is None or status != 'covered' or not backed:
            missing.append({'kind': item['kind'], 'ref': item['ref'], 'status': 'missing'})
    return missing


def validate_source(project, tests=None):
    files=snapshot_project(Path(project))
    errors=[]
    manifests=[]
    for name,data in files.items():
        if name.endswith('package.json'):
            try:
                package=json.loads(data)
                if not isinstance(package,dict): raise ValueError('expected object')
                manifests.append(name)
            except (ValueError,UnicodeError) as error:
                errors.append(dict(path=name,error=str(error)))
    if not manifests: errors.append(dict(path='package.json',error='No application package manifest'))
    if 'deploy.sh' not in files and not {'frontend/package.json','backend/package.json'} <= set(files):
        errors.append(dict(path='deploy.sh',error='A custom layout must provide a root deploy.sh honoring HOST and PORT'))
    if 'deploy.sh' in files and 'package.json' in manifests:
        package=json.loads(files['package.json'])
        dependencies={**package.get('dependencies',{}),**package.get('devDependencies',{})}
        deploy=files['deploy.sh'].decode('utf-8',errors='replace')
        if dependencies and not _has_deploy_install_before_build(deploy):
            errors.append(dict(path='deploy.sh',error='Fresh platform has no node_modules. Add a direct dependency install command (including build-time devDependencies) in deploy.sh before build/start; comments and uncalled package scripts do not install dependencies.'))
    sources=[]
    for name,data in sorted(files.items()):
        if name.startswith('requirements/') or Path(name).suffix not in {'.js','.jsx','.ts','.tsx','.cjs','.mjs','.mts','.cts'}: continue
        if name.endswith('.d.ts'): continue
        try: sources.append(dict(path=name,content=data.decode('utf-8')))
        except UnicodeError: errors.append(dict(path=name,error='source is not UTF-8'))
    if not sources: errors.append(dict(path='.',error='No JavaScript or TypeScript application source'))
    node=shutil.which('node')
    if not node: raise RuntimeError('Trusted static parser requires Node from the official runner')
    checker=Path(__file__).resolve().parents[1]/'static-tools/parse-source.cjs'
    clean={name:value for name,value in os.environ.items() if name.upper() in {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP'}}
    result=subprocess.run([node,str(checker)],input=json.dumps(dict(files=sources)),capture_output=True,
        text=True,encoding='utf-8',errors='replace',timeout=30,check=False,env=clean,cwd=str(checker.parent))
    if result.returncode: raise RuntimeError('Trusted parser failed: '+result.stderr[-1200:])
    parsed=json.loads(result.stdout)
    for row in parsed['files']:
        if not row['valid']:
            errors.append(dict(path=row['path'],error=row['error']))
            continue
        for name in row.get('unbound',[]):
            errors.append(dict(path=row['path'],error='Undeclared runtime identifier '+name))
        for reference in row['imports']:
            if not reference.startswith('.'): continue
            base=posixpath.normpath(posixpath.join(posixpath.dirname(row['path']),reference.split('?',1)[0]))
            candidates=[base]+[base+suffix for suffix in ('.js','.jsx','.ts','.tsx','.json','.cjs','.mjs')]+[base+'/index'+suffix for suffix in ('.js','.jsx','.ts','.tsx')]
            if row['path'].endswith(('.ts','.tsx','.mts','.cts')):
                for emitted,source in (('.js','.ts'),('.js','.tsx'),('.mjs','.mts'),('.cjs','.cts')):
                    if base.endswith(emitted): candidates.append(base[:-len(emitted)]+source)
            if not any(candidate in files for candidate in candidates):
                errors.append(dict(path=row['path'],error='Missing relative import '+reference))
    errors.extend(_interaction_errors(sources, _test_sources(project, tests)))
    errors.extend(ir_coverage_errors(project))
    if has_scaffold_seed_loader(project):
        errors.extend(validate_seeds(Path(project)))
    return dict(passed=not errors,kind='STATIC_SYNTAX_IMPORTS_AND_PAGE_SCOPE',
                functional_status='UNRUN',formal_status='UNVERIFIED',files=len(sources),errors=errors[:30])

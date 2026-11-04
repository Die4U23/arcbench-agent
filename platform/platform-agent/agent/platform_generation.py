"""Source generation in the official runner; platform owns build and acceptance.

No application command is executed by the model in the credential-bearing
generator process. The only editable filesystem is the output application.
Tests are inputs only when this task supplies them. The program carries no
suite of its own. The orchestrator may build, start and run those supplied
tests against a scratch COPY of the delivered source and feed concrete failures
back for a bounded repair; the delivered project never receives build output.
"""
from __future__ import annotations
import argparse
from dataclasses import replace
import hashlib
import json
import logging
import math
import os
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from .campaign_budget import CampaignBudgetStopped
from .competition_model import CompetitionModel
from .competition_context import assemble_context, build_work_plan, global_contract_schema
from .llm import ModelClient
from .platform_acceptance import triage
from .platform_inputs import bundle_kwargs, resolve_task_inputs
from .platform_runtime_check import api_failures, failure_payload, run_runtime_check, runtime_log_fields, safe_rmtree
from .platform_scaffold import SCAFFOLD_GUIDE, has_full_scaffold, is_scaffold, prepare_scaffold
from .platform_seed import collect_seed_evidence, seed_instructions
from .platform_settings import PlatformSettings, model_allowed
from .platform_static import missing_coverage, seed_coverage, ui_element_ref, validate_source, write_coverage
from .platform_task_ir import build_task_ir, detect_runtime, write_task_ir
from .sandbox_files import apply_snapshot, snapshot_project
from .task_bundle import load_task_bundle
from .tools import ProjectTools

WHOLE_TASK_PREFIX = 'SOURCE_COMPLETENESS: ALL_TEXT_SOURCES_INCLUDED\n'
PARTIAL_TASK_PREFIX = 'SOURCE_COMPLETENESS: PARTIAL_READ_OMITTED_RANGES\n'
BUDGET_STOP_REASONS = {'platform_token_allocation', 'platform_request_allocation', 'request_allowance', 'context_limit'}
ALLOCATION_STOP_REASONS = {'platform_token_allocation', 'platform_request_allocation'}
MIN_BATCH_REQUESTS = 4
RUNTIME_MIN_SECONDS = 60
RUNTIME_REPAIR_MIN_SECONDS = 300
RUNTIME_REPAIR_MIN_FRACTION = 0.25
SEED_SUMMARY_CHARS = 6000
RUNTIME_SKIP_STATUSES = {'test_harness_failed'}
WALKTHROUGH_INSTRUCTIONS = (
    'LOCATOR_WALKTHROUGH\n'
    'Review every locator quoted below, copied from the supplied tests. '
    'For each one, confirm all of the following before any other edit: '
    'found, the role, accessible name, label, or placeholder matches the quoted text exactly; '
    'unique, an unscoped page.getByRole, page.getByText, or page.getByLabel matches one element at that moment, '
    'so do not repeat a navigation name in the page body; '
    'contained, if a helper filters a navigation or region with .filter({ has: ... }), the target is inside that container; '
    'anonymous, if the test never calls a sign-in helper, the page and the API must allow the action with no session; '
    'after the action, the heading, list, or region the test expects is visible, and it is still visible after reload. '
    'Apply source edits for every miss. If nothing is wrong, list each checked file and write "no change".'
)
MANIFEST_NAMES = {'package.json', 'package-lock.json', 'npm-shrinkwrap.json', 'pnpm-lock.yaml', 'yarn.lock'}
PLAYWRIGHT_CACHE_NAME = 'arc-runtime-check-playwright-cache'
STATIC_REPAIR_FEEDBACK = ('Repair only these observed static errors, then validate and finish. A page-level exact link or '
                          'button name must occur once across the shared layout and current route. Do not redesign '
                          'passing source or edit tests.\n')
RUNTIME_REPAIR_FEEDBACK = ('The orchestrator installed, built and started a copy of your delivered source. '
                           'Repair the source only, keep passing flows and docs/task-ir.json and docs/coverage.json valid, '
                           're-read the failing check for exact names, routes, prerequisite navigation and seed data, '
                           'do not edit tests or requirements, then call validate_source and finish.\n')
INTERACTION_CONVENTIONS = (
    'Render each parsed element with its role and accessible name unchanged (accessible-name semantics).\n'
    'Unless the IR marks multiple=true, one role+name is unique in a given page state (Playwright strict mode).\n'
    'Entries that share a group and persistent=true go in one persistent navigation landmark, not repeated in the body (landmark semantics).\n'
    'A named region or list uses a region landmark with that accessible name; a dialog uses role=dialog with its name; form controls have an associated label (WAI-ARIA).\n'
    'The action in When must succeed in the Given session. Add a sign-in gate only when the IR auth block and the scenario say so (scenario semantics).\n'
    'State that must survive reload is stored on the server; the UI updates immediately (observable-result semantics).\n'
    'Lists can hold many uniquely suffixed records without a forced pager; no default animation; time uses the zone the requirement states; the first view is interactive within one second (external automation constraints).\n'
)


class PlatformTokenBudget:
    """One uploaded run's allocation, reserved from the external 30M campaign.

    Reserve the documented full provider input bound before each call. Unknown
    usage retains its reservation and blocks further calls, including after restart.
    The injected endpoint may be the official runner proxy; no upstream override.
    Limits come from PlatformSettings; the defaults equal the previous constants.
    """
    def __init__(self, path, *, model, endpoint, limit, request_limit=None, settings=None):
        settings = settings or PlatformSettings()
        parsed = urlsplit(endpoint)
        if not model_allowed(settings, model) or parsed.scheme != 'https' or not parsed.netloc:
            raise ValueError('This campaign accepts only an injected HTTPS endpoint and an allowed model')
        self.settings = settings
        self.path = Path(path)
        self.config = SimpleNamespace(model=model, endpoint=endpoint, max_input_tokens=settings.max_input_tokens)
        self.live_allowed = True
        self.state = dict(limit=limit, request_limit=settings.request_limit if request_limit is None else request_limit,
                          used=0, requests=0, tools=0, pending=None, stopped=None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('x', encoding='utf-8') as stream:
            json.dump(self.state, stream)

    def _save(self):
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.state), encoding='utf-8')
        os.replace(temp, self.path)

    def stop(self, reason):
        self.state['stopped'] = reason
        self._save()

    def check_time(self, stage):
        if self.state['stopped'] or self.state['pending']:
            raise CampaignBudgetStopped(self.state['stopped'] or 'usage_unknown')

    def reserve(self, stage, input_upper_bound, max_output):
        self.check_time(stage)
        if input_upper_bound != self.config.max_input_tokens or not 1 <= max_output <= self.settings.max_output_tokens:
            raise ValueError('request outside documented input/output reservation')
        held = input_upper_bound + max_output
        if self.state['used'] + held > self.state['limit']:
            raise CampaignBudgetStopped('platform_token_allocation')
        if self.state['requests'] >= self.state['request_limit']:
            raise CampaignBudgetStopped('platform_request_allocation')
        identity = uuid4().hex
        self.state['pending'] = dict(id=identity, held=held, output=max_output, stage=stage)
        self.state['requests'] += 1
        self._save()
        return identity

    def settle(self, identity, usage):
        pending = self.state['pending']
        if not pending or pending['id'] != identity:
            raise ValueError('reservation identity differs')
        def field(key):
            return usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        prompt, completion = field('prompt_tokens'), field('completion_tokens')
        if (type(prompt) is not int or type(completion) is not int or
                not 0 <= prompt <= self.config.max_input_tokens or not 0 <= completion <= pending['output']):
            self.freeze(identity, 'usage_unknown_or_out_of_bounds')
            raise CampaignBudgetStopped('usage_unknown_or_out_of_bounds')
        self.state['used'] += prompt + completion
        self.state['pending'] = None
        self._save()
        logging.info('PLATFORM_TOKEN_BUDGET %s', json.dumps(self.state, sort_keys=True))

    def freeze(self, identity, reason):
        if not self.state['pending'] or self.state['pending']['id'] != identity:
            raise ValueError('reservation identity differs')
        self.stop(reason)

    def record_tools(self, stage, count):
        if self.state['tools'] + count > self.settings.tool_limit:
            raise CampaignBudgetStopped('platform_tool_allocation')
        self.state['tools'] += count
        self._save()


class StaticProjectTools(ProjectTools):
    def __init__(self, project_dir):
        super().__init__(project_dir)
        self._fixed_deploy=is_scaffold(project_dir)
        self.public_tests=None
        self._sealed=False

    def unseal(self):
        """Reopen edits for the next batch or repair round after a passing static check sealed them."""
        self._sealed=False

    def call(self, name, arguments):
        if name in {'write_files','replace_text'} and getattr(self,'_sealed',False):
            raise ValueError('Source validation passed; this stage is sealed and further edits are forbidden')
        logging.info('PLATFORM_TOOL %s',json.dumps(dict(name=name,paths=[item.get('path') for item in arguments.get('files',[]) if isinstance(item,dict)] or [arguments.get('path') or arguments.get('resource')]),ensure_ascii=False))
        if name=='validate_source':
            files=snapshot_project(self.project_dir)
            tests=getattr(self,'public_tests',None)
            digest=hashlib.sha256(b''.join(name.encode()+hashlib.sha256(data).digest() for name,data in sorted(files.items()))+str(tests or '').encode()).hexdigest()
            if getattr(self,'_validation_hash',None)!=digest:
                self._validation_result=validate_source(self.project_dir, tests=tests)
                self._validation_hash=digest
            if self._validation_result.get('passed'):
                self._sealed=True
            return json.dumps(self._validation_result)
        if name in {'run_project_script', 'run_project_scripts'}:
            raise ValueError('The official platform owns build/test execution after source generation')
        paths = ([item.get('path','') for item in arguments.get('files',[]) if isinstance(item,dict)]
                 if name=='write_files' else [arguments.get('path','')] if name=='replace_text' else [])
        if self._fixed_deploy and any(self._resolve(path)==self.project_dir/'deploy.sh' for path in paths):
            raise ValueError('Generic deploy.sh is already provided and immutable; implement package build/start scripts and initialize declared seed data during server startup')
        if any(Path(path).name in {'package-lock.json','pnpm-lock.yaml','yarn.lock'} for path in paths):
            raise ValueError('Dependency lockfiles are retained unchanged; use existing dependencies')
        if any(Path(path).parts and Path(path).parts[0]=='requirements' for path in paths):
            raise ValueError('The platform requirement mirror is immutable')
        return super().call(name, arguments)


class PlatformGenerationModel(CompetitionModel):
    def implement(self, packet, tools, **kwargs):
        # Every implement call (batch, static repair, runtime repair) starts with
        # an open tool choice; a passing validate_source closes only that call.
        self._completion_tool_choice='auto'
        self._whole_task_visible = packet.text.startswith(WHOLE_TASK_PREFIX)
        try:
            return super().implement(packet,tools,**kwargs)
        finally:
            self._completion_tool_choice='auto'

    def review_sources(self, packet, tools, **kwargs):
        return self.implement(packet, tools, **kwargs)

    def _messages(self, packet, stage, instructions):
        if getattr(self,'_whole_task_visible',False):
            instructions=instructions.replace('Inspect before editing. ','').replace('Use list_context to discover omitted source. ','')
        instructions += ('\nComplete source contents supplied in the initial message count as inspection. '
                         'SOURCE application/ is a display namespace, not a directory inside the project. '
                         'All edit-tool paths are project-relative: SOURCE application/src/App.jsx means path src/App.jsx. '
                         'When all text sources are included, only edit tools are exposed: use that source '
                         'and the exact contents of your own previous edits. Do not request repeated reads. '
                         'Write 4-8 complete related files per write_files batch when possible, within the output limit. '
                         'For existing files prefer replace_text using the shortest unique old_text span; batch independent edits in the same request. '
                         'Avoid a separate model request for each small file; include its related modules in one batch. '
                         'Before validate_source, walk every supplied requirement and its tests and confirm each one is covered: '
                         'the quoted locator, the container a helper filters, whether the test signs in, and the state after the action. '
                         'A passing static check only means syntax and imports. Do not return completion JSON until that walk is done. '
                         'Then call validate_source once. Fix only its concrete errors if any. '
                         'The orchestrator then builds, starts and runs the public tests on a copy and returns '
                         'concrete failures if a repair round is needed. Do not continue speculative polishing.')
        return super()._messages(packet,stage,instructions)

    def _tool_schemas(self, mode):
        schemas=[item for item in super()._tool_schemas(mode)
                 if item['function']['name'] not in {'run_project_script', 'run_project_scripts'}]
        if mode=='implement' and getattr(self,'_whole_task_visible',False):
            schemas=[item for item in schemas if item['function']['name'] in {'write_files','replace_text'}]
        if mode=='implement':
            schemas.append({'type':'function','function':{'name':'validate_source','description':'After completing all required implementation, check syntax, relative imports and seed files without executing application code. When this passes, deliver immediately; it is not a behavioral test.','parameters':{'type':'object','properties':{},'additionalProperties':False}}})
        return schemas

    def _call_tool(self, tools, name, arguments, packet, stage):
        try:
            if getattr(self,'_whole_task_visible',False) and name not in {'write_files','replace_text','validate_source'}:
                raise ValueError('All editable source is already supplied; use write_files, replace_text or validate_source only')
            result=tools.call(name,arguments) if name=='validate_source' else super()._call_tool(tools,name,arguments,packet,stage)
        except Exception as error:
            logging.warning('PLATFORM_TOOL_FAILURE %s',json.dumps(dict(name=name,error_type=type(error).__name__,detail=str(error)[:800])))
            raise
        if name=='validate_source' and json.loads(result).get('passed'):
            self._completion_tool_choice='none'
        return result


PLATFORM_CONTRACT = {
    'delivery': 'Generate a complete runnable application. The official platform builds and evaluates it after this process exits. No local test pass is claimed.',
    'workspace': 'Inspect the actual supplied project layout and retain its conventions. A root package with src/server is valid, as are frontend/backend packages. Backend must serve the built frontend and bind 0.0.0.0 at process.env.PORT or 3000. Preserve working package locks and dependencies.',
    'commands': 'Provide build/start package scripts. The platform is fresh: node_modules are NOT preinstalled. A root deploy.sh must install declared dependencies including build-time devDependencies before building, initialize declared SEED once per fresh storage, and exec the server honoring HOST/PORT. Do not require Docker. Prefer standard Vite/React build tooling; every ES module must declare/import its own identifiers. A const in another file is not a shared global.',
    'scaffold': ('If the generic scaffold is already supplied (root package.json with build/start scripts, deploy.sh, '
                 'server/index.js, server/store.js, server/seed.js, server/routes/index.js, seed/README.md, index.html, '
                 'vite.config.js, src/main.jsx, src/App.jsx, src/api.js), retain deploy.sh byte-for-byte and do not write it: '
                 'it installs dependencies exactly once, runs the root build script, and starts the root start script. '
                 'Implement the API in server/routes/ (server/routes/index.js is an Express router mounted at /api and at the root), '
                 'the React/Vite UI in src/, and prerequisite data as seed/<collection>.json files that server/seed.js loads once per '
                 'fresh storage at server startup. Persist through server/store.js instead of rewriting it; do not rewrite '
                 'server/index.js or server/seed.js. Retain the pinned packages where possible and do not add install/ci commands '
                 'inside build/start scripts.'),
    'state': ('Only initialize declared SEED data (seed/*.json in the scaffold). CREATED records must result from real user actions. '
              'Persist business data in the backend store and preserve it across process restarts. Do not seed case-created '
              'records or replace business state with constants.'),
    'contracts': 'Read the actual task environment, data registry and prerequisites before choosing initialization and clock behavior. Public test helpers are test setup evidence. Never modify or weaken tests or requirements. Do not assume local Docker-only world hooks exist on the platform.',
    'design_contract': ('docs/task-ir.json is the task IR and docs/coverage.json is the coverage ledger. '
                        'Both are already on disk. Leave them as valid JSON. Coverage refs point at IR work items, '
                        'parsed UI elements, or scenarios. Mark an item covered or partial only by editing '
                        'docs/coverage.json. A completion message does not mark coverage. GET /api/health stays '
                        'available and returns 200.'),
    'interaction_review': 'Review exact accessible names and locator scopes from the supplied tests AND shared helpers before delivery. For a strict single-element action/assertion, ensure exactly one matching element in its actual scope: page-level locators search the combined shared header/navigation and current route, while region/dialog locators search that region/dialog. A link-or-button union must also identify one target in that scope. Retain required accessible names and distinguish optional secondary controls semantically when the task allows it; do not change tests or add first/nth selection to hide ambiguity. The same name in separate routes or separately scoped regions is not automatically a conflict. Check prerequisite navigation and login before downstream task flows. Icon-only controls keep their names when labels are hidden. Prefer accessible in-page confirmation over native confirm. Preserve navigation/filter state across reload where required; restore it before the first render and do not overwrite it with defaults in a startup effect. These are general interaction constraints, not permission to seed CREATED records.',
    'generation': ('Application commands are unavailable to the model: write complete source and perform code review. '
                   'After each delivery the orchestrator independently installs, builds and starts a copy of the project. '
                   'When this task supplies tests, it runs those tests and returns concrete failures for a bounded repair round. '
                   'It does not invent or carry a test suite. Do not request build/test tools. '
                   'Formal evaluation runs on the platform after this process exits.'),
}


def seed_hint_summary(evidence, limit=SEED_SUMMARY_CHARS):
    """Compact, bounded prose for the packet: Given steps and seed-hinted test constants."""
    steps = evidence.get('given_steps') or []
    constants = [item for item in (evidence.get('test_constants') or []) if item.get('seed_hint')]
    if not steps and not constants:
        return ''
    lines = ['SEED EVIDENCE SUMMARY (declared prerequisites only; CREATED records come from user actions):']
    for step in steps:
        lines.append(f"- Given [{step.get('requirement_id')}/{step.get('scenario_id')}]: {step.get('given')}")
    for item in constants:
        source = ' '.join(str(item.get('source', '')).split())
        lines.append(f"- Test constant {item.get('name')} ({item.get('file')}): {source[:300]}")
    text = '\n'.join(lines)
    if len(text) > limit:
        text = text[:limit] + '\n- ... (truncated; full list in evidence/seed-evidence.json)'
    return text + '\n'


def manifest_digests(files):
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items() if Path(name).name in MANIFEST_NAMES}


def sync_project_copy(source, copy, installed_manifests=None):
    """Mirror the editable project into the scratch copy used by the runtime check.

    Everything except node_modules is replaced each round, so stale build output
    and persisted data never leak between rounds. node_modules is reused only
    when the previous round's install SUCCEEDED for byte-identical manifests and
    lockfiles (installed_manifests); otherwise it is removed so the next check
    reinstalls. Returns the current manifest digest map.
    """
    copy = Path(copy)
    files = snapshot_project(Path(source))
    manifests = manifest_digests(files)
    keep_modules = installed_manifests is not None and installed_manifests == manifests
    if copy.exists():
        for current, dirs, names in os.walk(copy):
            for name in list(dirs):
                if name == 'node_modules':
                    dirs.remove(name)
                    if not keep_modules:
                        safe_rmtree(Path(current)/name)
            for name in names:
                (Path(current)/name).unlink(missing_ok=True)
        for current, dirs, names in os.walk(copy, topdown=False):
            path = Path(current)
            if path != copy and path.name != 'node_modules' and not any(path.iterdir()):
                path.rmdir()
    for name, data in files.items():
        target = copy.joinpath(*name.split('/'))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return manifests


def restore_snapshot(project, files):
    """Put the editable project back to a recorded snapshot; .arc/, .git and node_modules are never touched."""
    project = Path(project)
    current = snapshot_project(project)
    if current == dict(files):
        return False
    apply_snapshot(project, files, before=current)
    for current_dir, dirs, names in os.walk(project, topdown=False):
        path = Path(current_dir)
        relative = path.relative_to(project).parts
        if relative and relative[0] not in {'.arc', '.git', 'node_modules'} and not any(path.iterdir()):
            path.rmdir()
    return True


def monotonic():
    """Wall clock used by the runtime loop (indirection so offline tests can drive a fake clock)."""
    return time.monotonic()


def repair_minimum_seconds(settings):
    """Do not start a repair (model call) unless this much wall budget remains."""
    return min(RUNTIME_REPAIR_MIN_SECONDS, settings.runtime_seconds * RUNTIME_REPAIR_MIN_FRACTION)


def _signal_passed(result, name):
    block = (getattr(result, 'signals', None) or {}).get(name) or {}
    value = block.get('passed') if isinstance(block, dict) else 0
    return value if type(value) is int else 0


def candidate_rank(result):
    """Prefer a round that ran checks, then more input, compiled, authored, and probe passes, then fewer failures.

    The leading flag keeps a build or install failure from outranking a round that actually ran.
    """
    return (
        result.status in {'passed', 'failed'},
        _signal_passed(result, 'input'),
        _signal_passed(result, 'compiled'),
        _signal_passed(result, 'authored'),
        _signal_passed(result, 'probes'),
        -int(getattr(result, 'failed', 0) or 0),
    )


def review_request_budget(work_item_count):
    """Total review requests across work-item batches."""
    return min(64, 8 + max(0, int(work_item_count)))


def provenance_counts(items):
    parsed = inferred = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get('source') == 'parsed':
            parsed += 1
        elif item.get('source') == 'inferred':
            inferred += 1
    return {'parsed': parsed, 'inferred': inferred}


def ir_prompt_summary(ir, starter_layout):
    """Layout, persistent groups, and parsed elements. The task title is omitted."""
    document = ir if isinstance(ir, dict) else {}
    runtime = document.get('runtime') if isinstance(document.get('runtime'), dict) else {}
    lines = [
        'TASK IR SUMMARY',
        f'Detected starter layout: {starter_layout}.',
        f'Runtime layout: {runtime.get("layout")}.',
    ]
    for key in ('install', 'build', 'start', 'port', 'health'):
        value = runtime.get(key)
        if value:
            lines.append(f'Runtime {key}: {value}.')
    groups, order, parsed = {}, [], []
    for element in document.get('ui_elements') or []:
        if not isinstance(element, dict) or element.get('source') != 'parsed':
            continue
        parsed.append(element)
        if element.get('persistent') is True and element.get('group'):
            group = str(element['group'])
            if group not in groups:
                order.append(group)
                groups[group] = []
            groups[group].append(element)
    lines.append('Persistent groups:')
    if not order:
        lines.append('- none')
    for group in order:
        members = ', '.join(f"role={item.get('role')} name={item.get('name')}" for item in groups[group])
        lines.append(f'- {group}: {members}')
    lines.append('Parsed UI elements:')
    if not parsed:
        lines.append('- none')
    for index, element in enumerate(parsed):
        lines.append(
            f"- ref={ui_element_ref(element, index)} role={element.get('role')} name={element.get('name')} "
            f"work_item={element.get('work_item')} multiple={element.get('multiple') is True} "
            f"persistent={element.get('persistent') is True}"
        )
    lines.append('Work items:')
    for item in document.get('work_items') or []:
        if isinstance(item, dict) and item.get('id') is not None:
            lines.append(f"- {item.get('id')}")
    lines.append('Scenarios:')
    for item in document.get('scenarios') or []:
        if isinstance(item, dict) and item.get('id') is not None:
            given = item.get('given') if isinstance(item.get('given'), dict) else {}
            lines.append(f"- {item.get('id')} work_item={item.get('work_item')} session={given.get('session')}")
    return '\n'.join(lines) + '\n'


def review_feedback(ir, work_item):
    """Conventions plus the IR slice for one work item."""
    document = ir if isinstance(ir, dict) else {}
    work_id = work_item.get('id')
    elements = [
        {
            'role': element.get('role'),
            'name': element.get('name'),
            'group': element.get('group'),
            'persistent': element.get('persistent') is True,
            'multiple': element.get('multiple') is True,
        }
        for element in document.get('ui_elements') or []
        if isinstance(element, dict) and element.get('source') == 'parsed' and element.get('work_item') == work_id
    ]
    scenarios = [
        scenario for scenario in document.get('scenarios') or []
        if isinstance(scenario, dict) and scenario.get('work_item') == work_id
    ]
    groups = {}
    for element in elements:
        if element['persistent'] and element.get('group'):
            groups.setdefault(element['group'], []).append({'role': element['role'], 'name': element['name']})
    payload = {
        'work_item': {key: work_item.get(key) for key in ('id', 'name', 'text', 'depends_on')},
        'parsed_ui_elements': elements,
        'persistent_groups': groups,
        'scenarios': scenarios,
        'auth': document.get('auth'),
    }
    return INTERACTION_CONVENTIONS + 'IR SLICE\n' + json.dumps(payload, ensure_ascii=False)


def coverage_feedback(missing):
    return (
        'COVERAGE_REVIEW\n'
        'docs/coverage.json still has missing items. Mark each listed item covered or partial by editing that file '
        'with write_files or replace_text. A completion declaration does not mark coverage. Do not declare the task '
        'complete while any listed item is missing.\n'
        + json.dumps({'missing': missing}, ensure_ascii=False)
    )


def signals_for_triage(result):
    signals = {}
    for name, block in (getattr(result, 'signals', None) or {}).items():
        if isinstance(block, dict):
            signals[name] = block
    if 'input' not in signals and getattr(result, 'failures', None):
        signals['input'] = {
            'passed': result.passed,
            'failed': result.failed,
            'skipped': result.skipped,
            'failures': [failure_payload(item) for item in result.failures],
        }
    if 'probes' not in signals:
        probes = api_failures(getattr(result, 'api_probes', None) or [])
        if probes:
            signals['probes'] = {
                'passed': 0,
                'failed': len(probes),
                'skipped': 0,
                'failures': [failure_payload(item) for item in probes],
            }
    return signals


def repair_feedback(result):
    payload = triage(signals_for_triage(result))
    labels = [
        f"source={group.get('source')} confidence={group.get('confidence')}"
        for group in payload.get('groups') or []
    ]
    label = 'Repair labels: ' + (', '.join(labels) if labels else 'none') + '.\n'
    return RUNTIME_REPAIR_FEEDBACK + label + json.dumps(payload, ensure_ascii=False)


def runtime_observability(result):
    signals = {}
    for name, block in (getattr(result, 'signals', None) or {}).items():
        if isinstance(block, dict):
            signals[name] = {'passed': block.get('passed', 0), 'failed': block.get('failed', 0)}
    return {'signals': signals, 'diagnostics': getattr(result, 'diagnostics', None) or {}}


def load_task_ir(project, fallback):
    path = Path(project) / 'docs' / 'task-ir.json'
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            return loaded
    return fallback if isinstance(fallback, dict) else {}


def walkthrough_feedback(tests_dir):
    """Review prompt built from the supplied tests. The instruction text carries no task names."""
    excerpts = []
    if tests_dir is not None:
        root = Path(tests_dir)
        for path in sorted(root.rglob('*')):
            if path.suffix not in {'.ts', '.js', '.mjs', '.cjs', '.tsx', '.jsx'} or 'node_modules' in path.parts:
                continue
            try:
                text = path.read_text(encoding='utf-8')
            except (OSError, UnicodeError):
                continue
            kept = [line.strip() for line in text.splitlines()
                    if any(token in line for token in ('getBy', '.filter(', 'test(', 'function '))]
            if kept:
                excerpts.append(path.name + '\n' + '\n'.join(kept[:40]))
    return WALKTHROUGH_INSTRUCTIONS + '\n' + '\n\n'.join(excerpts)[:12000]


def run_runtime_loop(app, tests_dir, settings, *, model, tools, make_packet, record, validate, task_id, report_path, ir):
    """Check, repair and re-check the project on a scratch copy.

    The only browser suite is a tests directory supplied with this task.
    The program does not compile or store a suite. With no supplied tests,
    the check is HTTP probes. Rank is (checks ran, input passed, compiled
    passed, authored passed, probe passed, fewer failures); compiled and
    authored stay at zero unless a supplied suite reports them.
    A full tie keeps the later round. A worse rank restores the best snapshot. A missing browser does not
    count as skipped: probe failures are repaired, and otherwise one coverage review runs before stopping.
    """
    report = dict(task_id=task_id, enabled=True, skipped=None, rounds=[], final_status=None, last_round_status=None,
                  best_round=None, stopped=None, wall_budget_seconds=settings.runtime_seconds, max_rounds=settings.runtime_rounds,
                  repair_minimum_seconds=repair_minimum_seconds(settings))
    deadline = monotonic() + settings.runtime_seconds
    work_root = Path(tempfile.mkdtemp(prefix='arc-runtime-'))
    copy_dir, check_dir = work_root/'project', work_root/'check'
    cache = Path(tempfile.gettempdir())/PLAYWRIGHT_CACHE_NAME
    best = None
    installed_manifests = None

    def write_report():
        if report.get('stopped') == 'unavailable_after_review':
            report['final_status'] = 'unavailable'
        elif best is not None:
            report['final_status'] = best['status']
            report['best_round'] = best['round']
        if report.get('stopped') != 'unavailable_after_review' and best is not None:
            report['best_round'] = best['round']
        if report['rounds']:
            report['last_round_status'] = report['rounds'][-1]['status']
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')

    def restore_best(entry, action):
        try:
            if best is not None and restore_snapshot(app, best['files']):
                tools.source_revision += 1
                tools.unseal()
            entry['action'] = action
        except Exception as error:  # delivery continues; main() re-validates the project afterwards
            entry['action'] = 'restore_failed'
            entry['restore_error'] = f'{type(error).__name__}: {str(error)[:300]}'
            logging.warning('PLATFORM_RUNTIME_RESTORE_FAILED %s', json.dumps(dict(task_id=task_id, **entry), ensure_ascii=False))

    try:
        for number in range(1, settings.runtime_rounds + 1):
            remaining = deadline - monotonic()
            if remaining < RUNTIME_MIN_SECONDS:
                report['stopped'] = 'wall_budget'
                break
            try:
                manifests = manifest_digests(snapshot_project(app))
                sync_project_copy(app, copy_dir, installed_manifests=installed_manifests)
                suites = [('input', Path(tests_dir))] if tests_dir is not None else []
                result = run_runtime_check(copy_dir, None, suites=suites, work_dir=check_dir, playwright_cache=cache,
                                           deadline=deadline, timeouts=dict(test=settings.runtime_test_timeout_ms / 1000))
            except Exception as error:  # the optional check must never discard a static-valid delivery
                entry = dict(round=number, status='harness_error', error=f'{type(error).__name__}: {str(error)[:300]}',
                             signals={}, diagnostics={})
                report['rounds'].append(entry)
                logging.warning('PLATFORM_RUNTIME_CHECK %s', json.dumps(dict(task_id=task_id, **entry)))
                restore_best(entry, 'restored_best' if best is not None and number > 1 else 'skipped')
                report['stopped'] = 'harness_error'
                break
            install_ok = not (result.status == 'install_failed' or (result.status == 'timeout' and result.stage == 'install'))
            installed_manifests = manifests if install_ok else None
            entry = dict(round=number, status=result.status, stage=result.stage, passed=result.passed, failed=result.failed,
                         skipped=result.skipped, precondition=result.precondition, total=result.total,
                         duration_seconds=result.duration_seconds, launcher=result.launcher,
                         env_supplied=sorted(result.env_supplied), env_unset=list(result.env_unset),
                         harness_preconditions=[dict(name=item.name, file=item.file) for item in result.preconditions],
                         install_reusable=install_ok, action='kept')
            report['rounds'].append(entry)
            entry.update(runtime_log_fields(result))
            entry.update(runtime_observability(result))
            logging.info('PLATFORM_RUNTIME_CHECK %s', json.dumps(dict(task_id=task_id, **entry), ensure_ascii=False))
            write_report()
            if result.status in RUNTIME_SKIP_STATUSES:
                # No behavioral evidence: never judge a repair by a harness problem.
                restore_best(entry, 'restored_best' if best is not None and number > 1 else 'skipped')
                report['stopped'] = result.status
                break
            if result.status == 'unavailable' and not api_failures(result.api_probes):
                try:
                    tools.unseal()
                    reviewed = model.review_sources(make_packet(), tools, request_allowance=settings.review_request_allowance,
                                                    feedback=coverage_feedback(missing_coverage(app, load_task_ir(app, ir))))
                    record('coverage_review', f'round-{number}', reviewed, allow_delivery=True, optional=True)
                    entry['action'] = 'reviewed'
                    entry['review'] = dict(status=reviewed.get('status'), turns=reviewed.get('turns'))
                except Exception as error:
                    entry['action'] = 'review_failed'
                    entry['review_error'] = f'{type(error).__name__}: {str(error)[:300]}'
                report['stopped'] = 'unavailable_after_review'
                write_report()
                break
            rank = candidate_rank(result)
            feedback = repair_feedback(result)
            if best is None or rank >= best['rank']:
                best = dict(rank=rank, round=number, status=result.status, files=snapshot_project(app),
                            feedback=feedback)
            else:
                restore_best(entry, f"restored_round_{best['round']}")
            write_report()
            if result.status == 'passed':
                break
            if result.status == 'failed' and not result.failures:
                report['stopped'] = 'only_harness_preconditions'
                break
            if number >= settings.runtime_rounds:
                report['stopped'] = 'rounds'
                break
            if deadline - monotonic() < repair_minimum_seconds(settings):
                report['stopped'] = 'wall_budget_before_repair'
                break
            try:
                tools.unseal()
                repair = model.implement(make_packet(), tools, request_allowance=settings.repair_request_allowance,
                                         feedback=best['feedback'])
                record('runtime_repair', f'round-{number}', repair, allow_delivery=True, optional=True)
                checked = validate()
            except CampaignBudgetStopped as stop:
                entry['repair'] = dict(status='stopped', reason=stop.reason)
                restore_best(entry, 'restored_after_repair')
                report['stopped'] = 'repair_incomplete'
                break
            except Exception as error:  # a broken repair round must not abort delivery of the best candidate
                entry['repair'] = dict(status='error', error=f'{type(error).__name__}: {str(error)[:300]}')
                logging.warning('PLATFORM_RUNTIME_REPAIR_ERROR %s', json.dumps(dict(task_id=task_id, round=number, **entry['repair'])))
                restore_best(entry, 'restored_after_repair')
                report['stopped'] = 'repair_error'
                break
            entry['repair'] = dict(status=repair.get('status'), reason=repair.get('reason'), turns=repair.get('turns'),
                                   static_passed=bool(checked.get('passed')))
            if repair.get('status') != 'complete' or not checked.get('passed'):
                restore_best(entry, 'restored_after_repair')
                report['stopped'] = 'repair_incomplete' if repair.get('status') != 'complete' else 'repair_static_invalid'
                break
            write_report()
    finally:
        safe_rmtree(work_root)
        write_report()
    return report


def main():
    from arcbench_agent_runtime import AgentRuntime
    parser = argparse.ArgumentParser()
    parser.add_argument('requirements')
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    app = Path(args.output_dir).resolve()
    if root == app or root in app.parents or app in root.parents:
        raise ValueError('submission and editable project must be separate')
    settings = PlatformSettings.from_env()
    inputs = resolve_task_inputs(Path(args.requirements).resolve())
    task, req = inputs.task_id, inputs.requirement_dir
    starter = detect_runtime(app)
    inject_scaffold = starter.get('layout') == 'empty'
    if inject_scaffold:
        prepare_scaffold(app)
    scaffolded = inject_scaffold and has_full_scaffold(app)
    ir = build_task_ir(inputs.requirements_path, app)
    write_task_ir(app, ir)
    write_coverage(app, seed_coverage(ir))
    for note in inputs.notes:
        logging.info('PLATFORM_INPUT_NOTE %s', json.dumps(dict(task_id=task, note=note)))
    logging.info('PLATFORM_INPUTS %s', json.dumps(dict(task_id=task, task_name=inputs.task_name,
        registered=inputs.registry_entry is not None, tests_source=inputs.tests_source,
        tests_dir=str(inputs.tests_dir) if inputs.tests_dir else None, declared_tests=inputs.declared_tests,
        scenario_count=inputs.scenario_count, token_limit=inputs.token_limit, token_limit_override=settings.token_limit,
        scaffolded=scaffolded, launcher_only=is_scaffold(app) and not scaffolded,
        entities=provenance_counts(ir.get('entities')), ui_elements=provenance_counts(ir.get('ui_elements')),
        scenarios=provenance_counts(ir.get('scenarios'))), ensure_ascii=False))
    # Requirements and reference assets are the current official inputs.
    bundle = load_task_bundle(**bundle_kwargs(inputs))
    reports = app/'.arc'/'platform-budget'/task
    reports.mkdir(parents=True, exist_ok=True)
    if not os.environ.get('MODEL', '').strip():
        os.environ['MODEL'] = 'deepseek-flash'
    endpoint = os.environ.get('OPENAI_BASE_URL', '').strip()
    if endpoint and '://' not in endpoint:
        endpoint = 'https://' + endpoint.lstrip('/')
        os.environ['OPENAI_BASE_URL'] = endpoint
    print(f"PLATFORM_AGENT task={task} model={os.environ['MODEL'].strip()} endpoint_host={urlsplit(endpoint).hostname or '-'}", flush=True)
    budget = PlatformTokenBudget(reports/'tokens.json', model=os.environ['MODEL'].strip(),
        endpoint=endpoint, limit=settings.token_limit or inputs.token_limit, settings=settings)
    session = ModelClient(max_tool_calls=settings.max_tool_calls)
    model = PlatformGenerationModel(session, context_window_tokens=settings.context_window_tokens,
        output_reserve_tokens=settings.max_output_tokens, request_allowance=settings.request_allowance,
        audit_log=lambda row: append_jsonl(reports/'context.jsonl',row), campaign_budget=budget)
    tools = StaticProjectTools(app)
    tools.public_tests = inputs.tests_dir
    runtime = AgentRuntime.from_env(project_dir=str(app))
    runtime.traceability.init_db()
    runtime.traceability.store_requirement_tree(json.loads(json.dumps(bundle.to_dict()['requirements'])))
    runtime.events.mark_run_started('Source generation with static validation and runtime check on a scratch copy; formal evaluation follows')
    contract = global_contract_schema(bundle)
    contract['execution_contract'] = PLATFORM_CONTRACT
    if inject_scaffold:
        contract['scaffold_guide'] = SCAFFOLD_GUIDE
        contract['seed_rules'] = seed_instructions()
    else:
        contract['scaffold_guide'] = ('The starter layout is already present. Retain the supplied project layout, its build and start '
                                      'scripts, and its own startup data mechanism. Only declared prerequisite data is seeded.')
    evidence = collect_seed_evidence(inputs.requirements_path, inputs.tests_dir)
    seed_evidence = json.dumps(evidence, ensure_ascii=False)
    seed_summary = seed_hint_summary(evidence)
    work = build_work_plan(bundle)
    all_ids = tuple(bundle.index.atomic_ids)
    actions = []
    def packet(selection):
        selection = tuple(selection)
        whole = set(selection) == set(all_ids)
        files = snapshot_project(app)
        paths = []
        for path, data in files.items():
            try: data.decode('utf-8')
            except UnicodeError: continue
            paths.append(path)
        result = assemble_context(bundle,selection,app,settings.context_chars,global_contract=contract,source_paths=paths)
        for path in req.glob('*.md'):
            result.repository.add_evidence('evidence/platform-'+path.name,path.read_text(encoding='utf-8'))
        result.repository.add_evidence('evidence/platform-contract.json',json.dumps(PLATFORM_CONTRACT))
        result.repository.add_evidence('evidence/seed-evidence.json',seed_evidence)
        # Deliver unchanged sources once in a stable prefix. Small tasks fit
        # completely; larger ones retain explicit offsets for read_context and
        # a batch packet narrows tests to the selected flow and its helpers.
        wanted = None if whole else set(result.manifest['test_paths']) | set(result.manifest['helper_setup_paths'])
        sources = [(record.relative_path, record.raw_bytes) for record in bundle.files
                   if record.relative_path.endswith(('.yaml','.md','.ts','.js','.json'))
                   and record.relative_path != 'requirements/requirements.md'
                   and (wanted is None or not record.relative_path.startswith('tests/') or record.relative_path in wanted)]
        sources.sort(key=lambda row:(0 if row[0].endswith('requirements.yaml') else
                                    1 if '/helpers' in row[0] or '/fixtures/' in row[0] else 2,row[0]))
        sources += [('application/'+name,data) for name,data in sorted(files.items())
                    if name in paths and not name.startswith('requirements/')
                    and Path(name).name not in {'package-lock.json','pnpm-lock.yaml','yarn.lock'}]
        lead = ('Implement the ENTIRE task in one shared application, including all atomic requirements. ' if whole else
                'Implement the selected requirement(s) ' + ', '.join(selection) + ' in the one shared application. '
                'Other requirements are implemented in separate batches: preserve existing work for them, extend shared '
                'layout, routes, store collections and seed data instead of replacing them. ')
        text = (lead + ir_prompt_summary(load_task_ir(app, ir), starter.get('layout')) + INTERACTION_CONVENTIONS +
                'The following immutable source and current application contents have already been read '
                'for you. Do not reread unchanged files shown completely below. Inspect the supplied '
                'contents, plan shared state once, then batch complete source files with write_files. '
                'Preserve package dependencies where possible. Avoid repeated explanations and repeated '
                'checks. Return completion JSON only after every required flow is implemented. '
                'Dependency lockfiles are immutable and excluded from the text payload; the complete '
                'package.json files describe the dependencies available to your implementation. '
                'Do not stop after the first requirement. No application commands are available to you; '
                'the orchestrator builds and tests the delivered source and the platform evaluates it.\n'
                + json.dumps(contract,ensure_ascii=False)+'\n' + seed_summary)
        remaining = settings.packet_chars
        complete = True
        for name,data in sources:
            try: content=data.decode('utf-8')
            except UnicodeError: continue
            take = min(len(content),max(0,remaining//3)) if len(data)>remaining else len(content)
            visible=content[:take]
            complete = complete and take == len(content)
            text += (f'\nSOURCE {name} sha256={hashlib.sha256(data).hexdigest()} '
                     f'characters={len(content)} provided=[0,{take}) complete={take==len(content)}\n'+visible+'\n')
            remaining -= len(visible.encode('utf-8'))
        logging.info('SOURCE_PACKET %s',json.dumps(dict(task_id=task,selection=list(selection),whole_task=whole,
            all_text_visible=complete,source_files=len(sources),utf8_bytes=len(text.encode('utf-8')),
            dependency_locks='preserved_without_repeated_payload',requirement_mirror='excluded_duplicate')))
        return replace(result,text=(WHOLE_TASK_PREFIX if complete else PARTIAL_TASK_PREFIX)+text)
    def record(kind, identity, result, *, allow_delivery=False, optional=False):
        logging.info('PLATFORM_STAGE_RESULT %s',json.dumps({k:result[k] for k in ('status','reason','tool','turns') if k in result}))
        actions.append(dict(stage=kind,item=identity,result=result))
        (reports/'actions.json').write_text(json.dumps(actions,ensure_ascii=False,indent=2),encoding='utf-8')
        bundle.assert_integrity()
        if optional or result.get('status') == 'complete':
            return
        if allow_delivery and result.get('reason') in BUDGET_STOP_REASONS | {'coverage_incomplete'}:
            return
        raise RuntimeError(f"{kind} stopped: {result.get('reason', result.get('status'))}")
    def validate():
        return json.loads(tools.call('validate_source', {}))
    def budget_open():
        return budget.state.get('stopped') is None
    def implement_until_covered(selection, identity, allowance, scope_ids):
        remaining = allowance
        feedback = None
        result = {'status': 'stopped', 'reason': 'request_allowance', 'turns': 0}
        while remaining > 0 and budget_open():
            tools.unseal()
            result = model.implement(packet(selection), tools, request_allowance=remaining, feedback=feedback)
            record('implement', identity, result, allow_delivery=True)
            if result.get('status') != 'complete':
                return result
            missing = missing_coverage(app, load_task_ir(app, ir), scope_ids)
            if not missing:
                logging.info('PLATFORM_COVERAGE %s', json.dumps(dict(task_id=task, status='complete', item=identity)))
                return result
            used = result.get('turns')
            remaining -= used if type(used) is int and used > 0 else 1
            if remaining <= 0 or not budget_open():
                logging.warning('PLATFORM_COVERAGE %s', json.dumps(dict(
                    task_id=task, status='incomplete', item=identity, missing=missing), ensure_ascii=False))
                stopped = dict(result)
                stopped['status'] = 'stopped'
                stopped['reason'] = 'coverage_incomplete'
                return stopped
            feedback = coverage_feedback(missing)
            logging.info('PLATFORM_COVERAGE %s', json.dumps(dict(
                task_id=task, status='rejected', item=identity, missing=[item['ref'] for item in missing]), ensure_ascii=False))
        logging.warning('PLATFORM_COVERAGE %s', json.dumps(dict(task_id=task, status='incomplete', item=identity)))
        stopped = dict(result)
        stopped['status'] = 'stopped'
        stopped['reason'] = 'coverage_incomplete'
        return stopped
    def review_batches():
        current = load_task_ir(app, ir)
        items = [item for item in current.get('work_items') or [] if isinstance(item, dict) and item.get('id') is not None]
        remaining = review_request_budget(len(items))
        for item in items:
            if remaining <= 0 or not budget_open():
                break
            share = min(settings.review_request_allowance, remaining)
            tools.unseal()
            reviewed = model.review_sources(packet(all_ids), tools, request_allowance=share,
                                            feedback=review_feedback(current, item))
            record('locator_review', str(item.get('id')), reviewed, allow_delivery=True, optional=True)
            remaining -= share
    for item in work:
        for req_id in item.requirement_ids:
            runtime.events.mark_implementation_started(req_id,'Implement requirement in shared application')
    completed = {}
    whole_packet = packet(all_ids)
    if whole_packet.text.startswith(WHOLE_TASK_PREFIX):
        result = implement_until_covered(all_ids, 'all-atomic-requirements', settings.implement_request_allowance, None)
        for item in work:
            completed[item.id] = result.get('status') == 'complete'
    else:
        # Sources do not fit one packet: implement flow by flow in dependency
        # order, each batch seeing the narrowed tests plus the current application.
        logging.info('PLATFORM_BATCHING %s',json.dumps(dict(task_id=task,items=len(work),
            implement_request_allowance=settings.implement_request_allowance,minimum_per_batch=MIN_BATCH_REQUESTS)))
        # The SUM of batch allowances never exceeds implement_request_allowance:
        # each batch reserves its share from the pool up front.
        pool = settings.implement_request_allowance
        result = dict(status='complete')
        for position, item in enumerate(work):
            if pool <= 0:
                logging.info('PLATFORM_BATCHING %s',json.dumps(dict(task_id=task,stopped='request_pool_exhausted',item=item.id)))
                break
            share = min(pool, max(min(MIN_BATCH_REQUESTS, pool), math.ceil(pool / (len(work) - position))))
            result = implement_until_covered(item.requirement_ids, item.id, share, item.requirement_ids)
            pool -= share
            completed[item.id] = result.get('status') == 'complete'
            if result.get('reason') in ALLOCATION_STOP_REASONS or result.get('reason') == 'coverage_incomplete' or not budget_open():
                break
        missing = missing_coverage(app, load_task_ir(app, ir), None)
        if missing and result.get('status') == 'complete' and pool > 0 and budget_open():
            result = implement_until_covered(all_ids, 'coverage', pool, None)
        elif missing and result.get('status') == 'complete':
            logging.warning('PLATFORM_COVERAGE %s', json.dumps(dict(
                task_id=task, status='incomplete', item='coverage', missing=missing), ensure_ascii=False))
            result = dict(result, status='stopped', reason='coverage_incomplete')
        tools.unseal()
    checked = validate()
    if not checked['passed'] and budget_open() and result.get('reason') not in ALLOCATION_STOP_REASONS:
        tools.unseal()
        result = model.implement(packet(all_ids),tools,request_allowance=settings.repair_request_allowance,
            feedback=STATIC_REPAIR_FEEDBACK+json.dumps(checked))
        record('static_repair','all-atomic-requirements',result,allow_delivery=True)
        checked = validate()
    (reports/'static-validation.json').write_text(json.dumps(checked,indent=2),encoding='utf-8')
    if not checked['passed']: raise RuntimeError('Source is not ready for platform build: '+json.dumps(checked['errors']))
    if budget_open() and result.get('reason') not in ALLOCATION_STOP_REASONS:
        review_batches()
    if result.get('status')!='complete':
        logging.warning('Delivering structurally valid candidate after bounded generation stopped: %s; functional acceptance remains UNVERIFIED',result.get('reason'))
    runtime_report = dict(task_id=task, enabled=settings.runtime_check, skipped=None, rounds=[], final_status=None)
    if not settings.runtime_check:
        runtime_report['skipped'] = 'disabled_by_settings'
    elif not shutil.which('node'):
        runtime_report['skipped'] = 'node_unavailable'
    elif not budget_open() or result.get('reason') in ALLOCATION_STOP_REASONS:
        runtime_report['skipped'] = 'model_budget_exhausted'
    if runtime_report['skipped']:
        logging.info('PLATFORM_RUNTIME_CHECK %s', json.dumps(dict(
            task_id=task, skipped=runtime_report['skipped'], signals={}, diagnostics={})))
        (reports/'runtime-check.json').write_text(json.dumps(runtime_report,indent=2),encoding='utf-8')
    else:
        runtime_report = run_runtime_loop(app, inputs.tests_dir, settings, model=model, tools=tools,
            make_packet=lambda: packet(all_ids), record=record, validate=validate, task_id=task,
            report_path=reports/'runtime-check.json', ir=load_task_ir(app, ir))
        final = validate()
        (reports/'static-validation.json').write_text(json.dumps(final,indent=2),encoding='utf-8')
        if not final['passed']:
            raise RuntimeError('Source is not ready for platform build after runtime repair: '+json.dumps(final['errors']))
    for item in work:
        for req_id in item.requirement_ids:
            if completed.get(item.id):
                runtime.events.mark_implementation_done(req_id,'Source delivered; formal tests pending')
    runtime.events.mark_run_completed('Source generation completed; no local acceptance claim; platform build and tests pending')
    logging.info('PLATFORM_DELIVERY %s',json.dumps(dict(task_id=task,formal_status='PENDING',budget=budget.state,
        runtime_check=dict(skipped=runtime_report.get('skipped'),final_status=runtime_report.get('final_status'),
                           rounds=len(runtime_report.get('rounds') or [])))))
    return 0


def append_jsonl(path,row):
    with path.open('a',encoding='utf-8') as stream: stream.write(json.dumps(row,ensure_ascii=False)+'\n')

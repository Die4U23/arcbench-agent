"""Source generation in the official runner; platform owns build and acceptance.

No application command is executed by the model in the credential-bearing
generator process. The only editable filesystem is the output application.
The default requirements-only policy withholds evaluation tests. The explicit
provided_tests compatibility policy can run supplied tests on a scratch copy
and return their failures for bounded repair. The program carries no suite of
its own; the delivered project never receives build output.
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
from .platform_execution import (ExecutionLimits, ExecutionStopped, PlanExecutor, ProjectLease, RunControl,
                                 SourceTransaction, source_digest, validate_arguments, validate_plan)
from .model_transport import ModelRequestInterrupted
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
from .task_bundle import is_test_path, load_task_bundle
from .tools import ProjectTools

WHOLE_TASK_PREFIX = 'SOURCE_COMPLETENESS: ALL_TEXT_SOURCES_INCLUDED\n'
PARTIAL_TASK_PREFIX = 'SOURCE_COMPLETENESS: PARTIAL_READ_OMITTED_RANGES\n'
EXECUTION_STOP_REASONS = {'execution_deadline', 'execution_request_limit', 'execution_tool_limit',
    'tool_batch_limit', 'repeated_action', 'source_oscillation', 'no_progress', 'replan_limit',
    'request_interrupted_usage_unknown', 'model_request_failed'}
BUDGET_STOP_REASONS = EXECUTION_STOP_REASONS | {'platform_token_allocation', 'platform_request_allocation', 'request_allowance', 'context_limit'}
ALLOCATION_STOP_REASONS = EXECUTION_STOP_REASONS | {'platform_token_allocation', 'platform_request_allocation'}
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
    'authentication, infer session requirements from the supplied specification and setup evidence; missing sign-in calls alone do not prove anonymous access; '
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
            self.stop('platform_token_allocation')
            raise CampaignBudgetStopped('platform_token_allocation')
        if self.state['requests'] >= self.state['request_limit']:
            self.stop('platform_request_allocation')
            raise CampaignBudgetStopped('platform_request_allocation')
        identity = uuid4().hex
        self.state['pending'] = dict(id=identity, held=held, output=max_output, stage=stage)
        self.state['requests'] += 1
        self._save()
        return identity

    def can_request(self, max_output):
        """Read-only admission probe; never releases unknown reservations."""
        return (not self.state['stopped'] and self.state['pending'] is None
                and self.state['requests'] < self.state['request_limit']
                and self.state['used'] + self.config.max_input_tokens + max_output <= self.state['limit'])

    def decision_capacity(self, max_output):
        """Advisory measured runway; admission still reserves the full bound."""
        reserve = self.config.max_input_tokens + max_output
        spendable = max(0, self.state['limit'] - self.state['used'] - reserve)
        peak = self.state.get('peak_request_tokens')
        estimate = (min(self.state['request_limit'] - self.state['requests'],
                        1 + spendable // max(1, peak))) if type(peak) is int else None
        return dict(tokens_after_next_reservation=spendable,
                    observed_peak_request_tokens=peak,
                    affordable_requests_estimate=estimate if self.can_request(max_output) else 0,
                    estimate_is_guarantee=False)

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
        self.state['peak_request_tokens'] = max(self.state.get('peak_request_tokens', 0), prompt + completion)
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


COVERAGE_UPDATE_SCHEMA = dict(type='object', additionalProperties=False, required=['items'], properties={
    'items': dict(type='array', minItems=1, maxItems=128, items=dict(type='object',
        additionalProperties=False, required=['kind', 'ref', 'status', 'files'], properties={
            'kind': dict(type='string', enum=['work_item', 'ui_element', 'scenario']),
            'ref': dict(type='string'), 'status': dict(type='string', enum=['covered', 'partial', 'missing']),
            'files': dict(type='array', maxItems=32, items=dict(type='string'))}))})


class StaticProjectTools(ProjectTools):
    def __init__(self, project_dir, *, include_tests=True):
        super().__init__(project_dir)
        self.include_tests = include_tests
        self._fixed_deploy=is_scaffold(project_dir)
        self.public_tests=None
        self._sealed=False

    def _resolve(self, raw_path, *, allow_root=False):
        target = super()._resolve(raw_path, allow_root=allow_root)
        if not self.include_tests and is_test_path(target.relative_to(self.project_dir).as_posix()):
            raise ValueError('Evaluation tests are outside generation inputs')
        return target

    def unseal(self):
        """Reopen edits for the next batch or repair round after a passing static check sealed them."""
        self._sealed=False

    def preflight(self, name, arguments):
        if name == 'update_coverage':
            validate_arguments(arguments, COVERAGE_UPDATE_SCHEMA)
            self._resolve('docs/coverage.json')
            expected = {(r['kind'], r['ref']) for r in seed_coverage(
                json.loads(self._resolve('docs/task-ir.json').read_text(encoding='utf-8')))['items']}
            seen = set()
            for row in arguments['items']:
                key = (row['kind'], row['ref'])
                if key not in expected or key in seen:
                    raise ValueError('Unknown or duplicate coverage reference')
                seen.add(key)
                if row['status'] == 'covered' and not row['files']:
                    raise ValueError('Covered claims need source paths')
                for path in row['files']:
                    target = self._resolve(path)
                    relative = target.relative_to(self.project_dir).as_posix()
                    if relative.startswith(('docs/', 'requirements/')) or is_test_path(relative):
                        raise ValueError('Coverage must cite application source, not metadata or evaluation inputs')
        if name in {'write_files', 'replace_text'}:
            paths = ([item['path'] for item in arguments['files']] if name == 'write_files' else [arguments['path']])
            for path in paths:
                target = self._resolve(path)
                relative = target.relative_to(self.project_dir).as_posix()
                if (relative == 'docs/task-ir.json' or relative.split('/')[0] in {'requirements', 'tests'}
                        or target.name in {'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock'}
                        or (self._fixed_deploy and relative == 'deploy.sh')):
                    raise ValueError('Protected input or launcher is immutable')

    def call(self, name, arguments):
        self.preflight(name, arguments)
        if name in {'write_files','replace_text','update_coverage'} and getattr(self,'_sealed',False):
            raise ValueError('Source validation passed; this stage is sealed and further edits are forbidden')
        logging.info('PLATFORM_TOOL %s',json.dumps(dict(name=name,paths=[item.get('path') for item in arguments.get('files',[]) if isinstance(item,dict)] or [arguments.get('path') or arguments.get('resource')]),ensure_ascii=False))
        if name == 'update_coverage':
            document = json.loads(self._resolve('docs/coverage.json').read_text(encoding='utf-8'))
            rows = document.get('items')
            if not isinstance(rows, list):
                raise ValueError('Invalid coverage ledger')
            keys = [(r['kind'], r['ref']) for r in rows]
            if len(set(keys)) != len(keys):
                raise ValueError('Duplicate existing coverage reference')
            updates = {(r['kind'], r['ref']): r for r in arguments['items']}
            # Files may be created earlier in the same validated tool batch.
            sources = snapshot_project(self.project_dir)
            for row in updates.values():
                if any(path not in sources for path in row['files']):
                    raise ValueError('Coverage cites absent application source')
            document['items'] = [updates.pop(key, row) for key, row in zip(keys, rows)] + list(updates.values())
            super().call('write_files', {'files': [dict(path='docs/coverage.json',
                content=json.dumps(document, ensure_ascii=False, indent=2) + '\n')]})
            return json.dumps(dict(updated=len(arguments['items']), acceptance='source_claim_only'))
        if name=='validate_source':
            files=snapshot_project(self.project_dir)
            tests=getattr(self,'public_tests',None)
            digest=hashlib.sha256(b''.join(name.encode()+hashlib.sha256(data).digest() for name,data in sorted(files.items()))+str(tests or '').encode()).hexdigest()
            if getattr(self,'_validation_hash',None)!=digest:
                self._validation_result=validate_source(self.project_dir, tests=tests,
                                                        include_test_evidence=self.include_tests)
                self._validation_hash=digest
            if self._validation_result.get('passed'):
                self._sealed=True
            return json.dumps(self._validation_result)
        if name in {'run_project_script', 'run_project_scripts'}:
            raise ValueError('The official platform owns build/test execution after source generation')
        return super().call(name, arguments)


class PlatformGenerationModel(CompetitionModel):
    def __init__(self, *args, execution_mode='react', execution_limits=None,
                 workspace_context_bytes=200000, **kwargs):
        super().__init__(*args, **kwargs)
        if execution_mode not in {'react', 'plan_execute'}:
            raise ValueError('execution_mode must be react or plan_execute')
        self.execution_mode = execution_mode
        self.execution_control = RunControl(execution_limits, emit=self._append_log)
        if type(workspace_context_bytes) is not int or workspace_context_bytes < 0:
            raise ValueError('invalid workspace context limit')
        self.workspace_context_bytes = workspace_context_bytes

    def _run(self, packet, tools, **kwargs):
        self._visible_source_hashes = {}
        self._decision_stage = dict(packet=packet, mode=kwargs['mode'],
            started_requests=self.execution_control.requests,
            allowance=kwargs.get('request_allowance') if kwargs.get('request_allowance') is not None else self.request_allowance)
        messages = list(kwargs.get('initial_messages') or self._messages(
            packet, kwargs['stage'], kwargs['instructions']))
        schemas = self._tool_schemas(kwargs['mode'])
        occupied = len(json.dumps(dict(messages=messages, tools=schemas), ensure_ascii=False).encode('utf-8'))
        available = min(self.workspace_context_bytes,
            self.context_window_tokens - kwargs['max_tokens'] - occupied - 2048)
        if available >= 1024 and hasattr(tools, 'project_dir'):
            files = self.execution_control.source(tools.project_dir)
            eligible = {name: raw for name, raw in sorted(files.items())
                if not is_test_path(name) and not name.startswith('requirements/')
                and name not in {'docs/task-ir.json', 'package-lock.json', 'npm-shrinkwrap.json',
                                 'pnpm-lock.yaml', 'yarn.lock'}}
            document = dict(kind='current_workspace', source_sha256=source_digest(eligible), files=[],
                inventory_complete=False, note=(
                    'Observed at this stage start; supersedes older application source in the task packet. '
                    'Paths are project-relative tool paths, with no application/ prefix. '
                    'Included text is complete for that file. Missing content is explicit; read only '
                    'omitted or subsequently changed files as needed. Requirements remain in the task packet.'))

            def message():
                return dict(role='user', content=json.dumps(document, ensure_ascii=False))

            def size():
                return len(json.dumps(message(), ensure_ascii=False).encode('utf-8'))

            for name, raw in eligible.items():
                document['files'].append(dict(path=name, sha256=hashlib.sha256(raw).hexdigest(),
                                             bytes=len(raw), content_included=False))
                if len(document['files']) > 300 or size() > available // 2:
                    document['files'].pop()
                    break
            document['inventory_complete'] = len(document['files']) == len(eligible)
            for row in document['files']:
                try:
                    content = eligible[row['path']].decode('utf-8')
                except UnicodeError:
                    continue
                # Avoid serializing a large file that cannot possibly fit.
                if len(eligible[row['path']]) > available:
                    continue
                row.update(content=content, content_included=True)
                if size() > available:
                    row.pop('content')
                    row['content_included'] = False
            if size() <= available:
                messages.insert(2, message())
                self._visible_source_hashes = {row['path']: row['sha256'] for row in document['files']
                                              if row['content_included']}
                self._append_log(dict(event='workspace_context', source_sha256=document['source_sha256'],
                    bytes=size(), files=len(document['files']),
                    included_files=sum(row['content_included'] for row in document['files']),
                    inventory_complete=document['inventory_complete']))
        kwargs['initial_messages'] = messages
        try:
            return super()._run(packet, tools, **kwargs)
        finally:
            self._decision_stage = None

    def _planning_capacity(self, packet, allowance):
        """Expose accounting, not a task-specific plan or a promise of tokenization."""
        result = dict(requests_remaining=max(0, allowance), max_steps=self.execution_control.limits.plan_steps)
        ledger = getattr(self.campaign_budget, 'state', None)
        if isinstance(ledger, dict) and 'limit' in ledger and 'used' in ledger:
            reserve = self.campaign_budget.config.max_input_tokens + self.output_reserve_tokens
            spendable = max(0, ledger['limit'] - ledger['used'] - reserve)
            payload = len(self._packet_text(packet).encode('utf-8')) + self.workspace_context_bytes
            # A conservative planning heuristic; hard admission still uses the unchanged ledger.
            turn_cost = max(1, payload + self.output_reserve_tokens)
            turns = min(max(0, allowance), 1 + spendable // turn_cost)
            result.update(tokens_remaining=ledger['limit']-ledger['used'], next_request_reserve=reserve,
                          planning_turn_cost_bytes_plus_output=turn_cost, affordable_turns_estimate=turns,
                          suggested_steps=min(result['max_steps'], max(1, (turns - 1) // 2)))
        return result

    def _request(self, *args, **kwargs):
        control = self.execution_control
        decision = getattr(self, '_decision_stage', None)
        if decision:
            remaining = max(0, decision['allowance'] - (control.requests - decision['started_requests']))
            state = dict(kind='controller_decision_state', requests_remaining_including_this=remaining,
                         total_requests=control.requests, total_tools=control.tools,
                         phase='finish' if decision['mode'] == 'implement' and remaining <= 3 else 'execute')
            ledger = getattr(self.campaign_budget, 'state', None)
            if isinstance(ledger, dict):
                state.update(tokens_remaining=ledger['limit']-ledger['used'],
                             next_request_reserve=self.campaign_budget.config.max_input_tokens+kwargs['max_tokens'])
            capacity = getattr(self.campaign_budget, 'decision_capacity', None)
            if capacity is not None:
                state.update(capacity(kwargs['max_tokens']))
            if control.plan_progress is not None:
                state['plan_progress'] = control.plan_progress
                estimate = state.get('affordable_requests_estimate')
                if estimate is not None:
                    state['requests_per_remaining_step_estimate'] = max(0, estimate - 2) // control.plan_progress['remaining_steps']
            if decision['mode'] == 'implement':
                state['instruction'] = (
                    'This is the LAST permitted request. Batch necessary edits, explicit update_coverage claims, '
                    'and validate_source LAST. Do not spend it on inspection, cosmetic work or prose. '
                    'Do not claim incomplete behavior is covered.' if remaining == 1 else
                    'Enter closure now: finish integration, record explicit source-backed coverage, and call '
                    'validate_source. Leave time to fix its errors. Defer cosmetic work; do not expand scope.'
                    if remaining <= 3 else
                    'Complete the current step within this allowance. Batch independent edits, reuse supplied '
                    'source, and reserve the final requests for coverage and validation. Establish a runnable '
                    'checkpoint early; do not polish before completing required behavior.')
                if control.plan_progress is not None:
                    state['instruction'] += (
                        ' Account for ALL remaining steps in plan_progress. The measured request estimate '
                        'is advisory and can shrink as source grows; preserve room for integration and review. '
                        'Batch related source edits and coverage updates, avoid cosmetic passes while required '
                        'behavior remains, and close this step once its required behavior is complete. '
                        'Never skip a requirement or report an unimplemented behavior as covered.')
            else:
                state['instruction'] = 'Return a valid complete plan within this allowance; use supplied observations before requesting more reads.'
            messages = args[0] if args else kwargs['messages']
            messages.append(dict(role='user', content=json.dumps(state, ensure_ascii=False)))
            if not self._check_context(messages, kwargs['tools'], kwargs['stage'], decision['packet'], kwargs['max_tokens']):
                control.stop('context_limit')
            self._append_log(dict(event='decision_state', **state))
        control.request()
        client = getattr(self.session, 'client', None)
        deadline = getattr(client, 'deadline_seconds', None)
        bounded = isinstance(deadline, (int, float))
        if bounded:
            client.deadline_seconds = min(deadline, max(0.001, control.deadline - control.clock()))
        try:
            response = super()._request(*args, **kwargs)
            control.check()
            if response is None:
                control.stop(self._stopped_reason or 'invalid_response')
            return response
        except ModelRequestInterrupted:
            control.stop('request_interrupted_usage_unknown')
        except (CampaignBudgetStopped, ExecutionStopped):
            raise
        except Exception:
            control.stop('model_request_failed')
        finally:
            if bounded:
                client.deadline_seconds = deadline

    def _react(self, packet, tools, *, step=None, **kwargs):
        self.execution_control.check()
        checkpoint = SourceTransaction(tools, self.execution_control)
        started_requests = self.execution_control.requests
        self._completion_tool_choice = 'auto'
        previous_refs = getattr(self, '_active_step_refs', None)
        self._active_step_refs = step.requirement_ids if step else None
        if hasattr(tools, 'unseal'):
            tools.unseal()
        try:
            if step is None:
                result = super().implement(packet, tools, **kwargs)
            else:
                instructions = (
                    'Implement only the current validated plan step. The full task packet is reference '
                    'context; its whole-task delivery instruction applies to the controller, not this step. '
                    'Make bounded source edits through tools and preserve completed work. '
                    'Inspect current files as needed; application/ is a display prefix, not a tool path. '
                    'Use list_files with path="." for the project root. Each response must fit its output '
                    'budget; split large changes across tool turns instead of emitting the entire application '
                    'in one response. Leave structurally valid source and call validate_source. '
                    'Finish with JSON containing summary and changed_paths. Static validity is not '
                    'behavioral acceptance. Current step:\n' + json.dumps(vars(step), ensure_ascii=False))
                messages = self._messages(packet, 'competition_implementation', instructions)
                messages.append(dict(role='user', content=kwargs.get('feedback') or 'Execute the current step.'))
                result = self._run(packet, tools, stage='competition_implementation', mode='implement',
                    instructions=instructions, max_tokens=self.output_reserve_tokens,
                    request_allowance=kwargs.get('request_allowance'), initial_messages=messages)
            result['turns'] = self.execution_control.requests - started_requests
            self.execution_control.check()
            if result.get('status') == 'complete':
                validation = self._validate_step(tools)
                result['validation'] = validation
                if not validation.get('passed'):
                    result.update(status='stopped', reason='static_validation_failed')
            if result.get('status') == 'complete':
                checkpoint.commit()
            else:
                checkpoint.rollback()
            return result
        except BaseException:
            checkpoint.rollback()
            raise
        finally:
            self._completion_tool_choice = 'auto'
            self._active_step_refs = previous_refs

    def _validate_step(self, tools):
        checked = json.loads(tools.call('validate_source', {}))
        refs = getattr(self, '_active_step_refs', None)
        ir_path = Path(tools.project_dir) / 'docs/task-ir.json' if refs and hasattr(tools, 'project_dir') else None
        if checked.get('passed') and ir_path is not None and ir_path.is_file():
            missing = missing_coverage(tools.project_dir, json.loads(ir_path.read_text(encoding='utf-8')), refs)
            if missing:
                tools.unseal()
                checked = dict(checked, passed=False, errors=[dict(path='docs/coverage.json',
                    error='Current step has missing source-backed claims', missing=missing)])
        return checked

    def implement(self, packet, tools, *, feedback=None, request_allowance=None):
        allowance = self.request_allowance if request_allowance is None else request_allowance
        if type(allowance) is not int or allowance < 0:
            raise ValueError('request allowance must be a nonnegative integer')
        control = self.execution_control
        started_requests = control.requests
        try:
            control.check()
            if self.execution_mode == 'react':
                return self._react(packet, tools, feedback=feedback, request_allowance=allowance)
            requirements = list(self._manifest(packet).get('requirement_ids', []))

            def planner(pending, completed, failure, budget):
                planning = planning_evidence(tools.project_dir, pending)
                capacity = self._planning_capacity(packet, allowance-(control.requests-started_requests))
                instructions = (
                    'Plan the supplied task from its actual requirements and current project. '
                    'Inspect as needed using read-only tools. Return JSON with steps: each step has '
                    'exactly id (nonempty string, max 80 characters), goal (nonempty string, max 2000), '
                    'requirement_ids (array of unique supplied ID strings), depends_on (array of unique '
                    'step ID strings), acceptance (nonempty string, max 2000). No extra step fields, '
                    'including file lists or status. IDs must be unique; dependencies '
                    'must form a DAG. Cover every pending requirement using only its supplied ID. '
                    'Each step must fit the supplied request/output limits and leave structurally valid source. '
                    'For an unimplemented project, the first step must establish a runnable foundation; '
                    'do not allocate a code-free analysis step that cannot pass source validation. '
                    'Group related end-to-end behaviors that share source modules into cohesive steps. '
                    'Use declared requirement dependencies and parent groups supplied below. Requirements '
                    'with a declared prerequisite must share a step or follow it through depends_on. '
                    'Shared source claims are grouping hints, not verified behavior or a reason to combine '
                    'everything into one step. Within a step, finish a complete user-visible behavior across '
                    'its layers; split when the scope cannot be edited and validated within the allowance. '
                    'Avoid one step per individual requirement when that repeats the same file edits. '
                    'Use the supplied max_steps and resource estimates to finish ALL pending work, '
                    'including integration, within the remaining resources. suggested_steps is a pessimistic '
                    'cost heuristic, NOT a limit: never inflate a step beyond step_request_limit just to meet it. '
                    'Establish and validate a minimal runnable foundation early, before filling every feature. '
                    'Reserve room for final verification. '
                    'Prefer a small complete implementation over polishing a partial application. '
                    'Acceptance is a proposed condition, '
                    'not a claim that a behavior has passed. Do not invent business workflows, accounts '
                    'or routes. Respect successful earlier work. Return only the bounded plan.')
                context = json.dumps(dict(pending_requirement_ids=pending, completed_steps=completed,
                                          planning_evidence=planning,
                                          feedback=failure, max_steps=capacity['max_steps'], resource_capacity=capacity,
                                          total_requests_remaining=allowance-(control.requests-started_requests),
                                          step_request_limit=control.limits.step_requests,
                                          response_output_limit=self.output_reserve_tokens), ensure_ascii=False)
                messages = self._messages(packet, 'plan', instructions)
                messages.append(dict(role='user', content=context))
                return self._run(packet, tools, stage='competition_implementation', mode='design',
                    instructions=instructions, max_tokens=self.output_reserve_tokens,
                    request_allowance=budget, initial_messages=messages,
                    final_validator=lambda value: validate_plan(value, pending, capacity['max_steps'],
                                                               planning['requirement_dependencies']))

            def execute(step, budget):
                current = source_digest(snapshot_project(tools.project_dir))
                instruction = json.dumps(dict(step=vars(step), current_source_sha256=current,
                    step_requests_remaining=budget, controller_feedback=feedback,
                    note='Execute this step using the current workspace snapshot. Read omitted or changed files as needed. Preserve completed steps.'), ensure_ascii=False)
                return self._react(packet, tools, step=step, feedback=instruction, request_allowance=budget)

            runner = PlanExecutor(control, tools, planner, execute,
                                  lambda: json.loads(tools.call('validate_source', {})))
            result = runner.run(requirements, allowance, feedback)
            if result.get('reason') in {'rollback_failed', 'concurrent_source_change'}:
                raise ExecutionStopped(result['reason'])
            return result
        except ExecutionStopped as error:
            if error.reason in {'rollback_failed', 'concurrent_source_change'}:
                raise
            return dict(status='stopped', reason=error.reason, turns=control.requests - started_requests)

    def review_sources(self, packet, tools, **kwargs):
        # A review is a bounded ReAct pass, not another nested planning tree.
        started_requests = self.execution_control.requests
        try:
            return self._react(packet, tools, **kwargs)
        except ExecutionStopped as error:
            if error.reason in {'rollback_failed', 'concurrent_source_change'}:
                raise
            return dict(status='stopped', reason=error.reason, turns=self.execution_control.requests - started_requests)

    def _messages(self, packet, stage, instructions):
        instructions += (
            '\nSOURCE application/ is a display namespace; edit paths are project-relative. '
            'Supplied source is an initial observation and may be stale after prior steps. '
            'Read current files when necessary; use list_files with path="." for the project root. '
            'Choose edit scope from dependencies and output limits. '
            'Use complete current_workspace contents directly; do not reread unchanged included files. '
            'Batch dependent edits that can be decided together, then validate once. Use update_coverage '
            'to record explicit source-backed claims in one batch instead of rewriting the coverage ledger. '
            'Do not mark an incomplete behavior covered merely to finish a step. '
            'Use observed tool results to decide the next action. Repeating unchanged reads, identical '
            'failures or oscillating edits will stop execution. Preserve requirements and tests. '
            'A passing static check establishes structural validity only; behavioral acceptance is separate.')
        return super()._messages(packet, stage, instructions)

    def _tool_schemas(self, mode):
        schemas = [item for item in super()._tool_schemas(mode)
                   if item['function']['name'] not in {'run_project_script', 'run_project_scripts'}]
        if mode == 'implement':
            schemas.append({'type': 'function', 'function': {'name': 'update_coverage',
                'description': 'Update explicit coverage rows atomically by kind/ref, preserving other rows. '
                               'Cite existing application files after editing them. Claims are not behavioral acceptance.',
                'parameters': COVERAGE_UPDATE_SCHEMA}})
            schemas.append({'type': 'function', 'function': {'name': 'validate_source',
                'description': 'Check source structure without executing the application. This ends the current implementation step when successful, not formal acceptance.',
                'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}})
        return schemas

    def _call_tool(self, tools, name, arguments, packet, stage):
        control = self.execution_control
        files = control.source(tools.project_dir) if hasattr(tools, 'project_dir') else {}
        state = source_digest(files)
        control.action(name, arguments, state)
        if name == 'read_file' and arguments.get('start_line', 1) == 1 and arguments.get('end_line') is None:
            path = tools._resolve(arguments['path']).relative_to(tools.project_dir).as_posix()
            digest = hashlib.sha256(files[path]).hexdigest() if path in files else None
            if digest and getattr(self, '_visible_source_hashes', {}).get(path) == digest:
                self._append_log(dict(event='source_read_reused', path=path, source_sha256=digest))
                return json.dumps(dict(kind='source_observation_reference', path=path, sha256=digest,
                    unchanged=True, content_location='complete current_workspace content in this stage',
                    note='Use the already supplied complete content. A targeted line range remains available if needed.'))
        checkpoint = SourceTransaction(tools, control) if name in {'write_files', 'replace_text', 'update_coverage'} else None
        try:
            result = (json.dumps(self._validate_step(tools)) if name == 'validate_source'
                      else tools.call(name, arguments) if name == 'update_coverage'
                      else super()._call_tool(tools, name, arguments, packet, stage))
            if checkpoint:
                control.source(tools.project_dir, changed_by_controller=True)
            control.check()
            if checkpoint:
                checkpoint.commit()
        except BaseException:
            if checkpoint:
                # A tool may have failed after writing only part of its batch.
                # The controller owns that batch; restore it before any next action.
                control.source(tools.project_dir, changed_by_controller=True)
                checkpoint.rollback()
            raise
        self._append_log(dict(event='react_observation', tool=name, source_sha256=state,
                              result_sha256=hashlib.sha256(result.encode('utf-8')).hexdigest()))
        if name == 'validate_source' and json.loads(result).get('passed'):
            self._completion_tool_choice = 'none'
        return result

    def _remember_project_source(self, name, arguments, result, packet):
        # A reference is not file content and must not become a frozen-source receipt.
        if name == 'read_file' and result.startswith('{"kind": "source_observation_reference"'):
            return
        super()._remember_project_source(name, arguments, result, packet)


INTERACTION_REVIEW_RULES = (
    'For a strict single-element action/assertion, ensure exactly one matching element in its actual scope: '
    'page-level locators search the combined shared header/navigation and current route, while region/dialog '
    'locators search that region/dialog. A link-or-button union must also identify one target in that scope. '
    'Retain required accessible names and distinguish optional secondary controls semantically when the task '
    'allows it; do not change tests or add first/nth selection to hide ambiguity. The same name in separate '
    'routes or separately scoped regions is not automatically a conflict. Check prerequisite navigation and '
    'login before downstream task flows. Icon-only controls keep their names when labels are hidden. Follow '
    'the specified confirmation behavior. Preserve navigation/filter state across reload where required; '
    'restore it before the first render and do not overwrite it with defaults in a startup effect. These are '
    'general interaction constraints, not permission to seed CREATED records.')


PLATFORM_CONTRACT = {
    'delivery': 'Generate a complete runnable application. The official platform builds and evaluates it after this process exits. No local test pass is claimed.',
    'workspace': 'Inspect the actual supplied project layout and retain its conventions. A root package with src/server is valid, as are frontend/backend packages. Backend must serve the built frontend and bind 0.0.0.0 at process.env.PORT or 3000. Preserve working package locks and dependencies.',
    'commands': 'Provide build/start package scripts. The platform is fresh: node_modules are NOT preinstalled. A root deploy.sh must install declared dependencies including build-time devDependencies before building, initialize declared SEED once per fresh storage, and exec the server honoring HOST/PORT. Do not require Docker. Use the supplied project toolchain; every ES module must declare/import its own identifiers. A const in another file is not a shared global.',
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
    'interaction_review': ('Review exact accessible names and locator scopes from the supplied tests AND '
                           'shared helpers before delivery. ' + INTERACTION_REVIEW_RULES),
    'generation': ('Application commands are unavailable to the model: write complete source and perform code review. '
                   'After each delivery the orchestrator independently installs, builds and starts a copy of the project. '
                   'When this task supplies tests, it runs those tests and returns concrete failures for a bounded repair round. '
                   'It does not invent or carry a test suite. Do not request build/test tools. '
                   'Formal evaluation runs on the platform after this process exits.'),
}


def platform_contract(input_policy):
    """A fresh prompt contract matching the actual inputs and diagnostic policy."""
    if input_policy not in {'requirements_only', 'provided_tests'}:
        raise ValueError('invalid platform input policy')
    contract = dict(PLATFORM_CONTRACT)
    if input_policy == 'requirements_only':
        contract['contracts'] = (
            'Read the public task environment, data registry and declared prerequisites before choosing '
            'initialization and clock behavior. Use only public requirements, their examples and reference '
            'assets, and current application source. Evaluation tests and helpers are unavailable; do not '
            'seek them or infer evaluator internals. Preserve requirements. Do not assume local Docker-only '
            'world hooks exist on the platform.')
        contract['interaction_review'] = (
            'Review exact accessible names and interaction scopes from the public requirements and current '
            'application source before delivery. ' + INTERACTION_REVIEW_RULES)
        contract['generation'] = (
            'Application commands are unavailable to the model: write complete source and perform code review. '
            'The orchestrator may install, build and start a scratch copy and return build/HTTP diagnostics '
            'for bounded repair. It does not run evaluation tests or return their assertions during generation. '
            'Do not request build/test tools. Independent formal evaluation follows the frozen source delivery.')
    return contract


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


def planning_evidence(project, requirement_ids):
    """Public dependency metadata and source ownership claims, never test data."""
    root = Path(project)
    def document(name):
        try:
            value = json.loads((root / name).read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, ValueError):
            return {}
    wanted = set(requirement_ids)
    items = {row['id']: row for row in document('docs/task-ir.json').get('work_items', [])
             if isinstance(row, dict) and isinstance(row.get('id'), str)}
    dependencies = {ref: [dep for dep in items.get(ref, {}).get('depends_on', []) if isinstance(dep, str)]
                    for ref in requirement_ids}
    ownership = {}
    source_paths = set(snapshot_project(root))
    for row in document('docs/coverage.json').get('items', []):
        if not isinstance(row, dict) or row.get('kind') != 'work_item' or row.get('ref') not in wanted:
            continue
        for path in row.get('files', []) if isinstance(row.get('files'), list) else []:
            if (isinstance(path, str) and path in source_paths and not is_test_path(path)
                    and not path.startswith(('docs/', 'requirements/'))):
                ownership.setdefault(path, set()).add(row['ref'])
    shared = [dict(path=path, requirement_ids=sorted(refs)) for path, refs in sorted(ownership.items()) if len(refs) > 1]
    return dict(requirement_dependencies=dependencies,
                parent_groups={ref: items.get(ref, {}).get('parent') for ref in requirement_ids},
                shared_source_claims=shared[:64], omitted_shared_sources=max(0, len(shared)-64),
                source_claims_are_verified=False)


def verification_summary(project, ir, static, runtime, *, input_policy, source_sha256):
    """Separate delivery claims from runtime diagnostics and formal acceptance."""
    missing = missing_coverage(project, ir)
    return dict(source_sha256=source_sha256, input_policy=input_policy,
        static_structure=dict(passed=bool(static.get('passed')), error_count=len(static.get('errors') or [])),
        source_coverage_claims=dict(total=len(seed_coverage(ir)['items']), missing=len(missing),
                                    complete=not missing, proves_behavior=False),
        runtime_diagnostics=dict(status=runtime.get('final_status'), skipped=runtime.get('skipped'),
            stopped=runtime.get('stopped'), rounds=len(runtime.get('rounds') or []),
            scope='build_and_http' if input_policy == 'requirements_only' else 'supplied_tests_and_runtime'),
        formal_behavior=dict(status='UNRUN', evaluated_cases=None, passed_cases=None))


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


def run_runtime_loop(app, tests_dir, settings, *, model, tools, make_packet, record, validate, task_id, report_path, ir,
                     model_calls_allowed=lambda: True):
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
    control = getattr(model, 'execution_control', None)
    if control is not None:
        deadline = min(deadline, control.deadline)
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
                if control is not None:
                    control.source(app, changed_by_controller=True)
            entry['action'] = action
        except Exception as error:  # delivery continues; main() re-validates the project afterwards
            entry['action'] = 'restore_failed'
            entry['restore_error'] = f'{type(error).__name__}: {str(error)[:300]}'
            logging.warning('PLATFORM_RUNTIME_RESTORE_FAILED %s', json.dumps(dict(task_id=task_id, **entry), ensure_ascii=False))
            raise RuntimeError('Rollback failed; refusing further edits or delivery') from error

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
                if not model_calls_allowed():
                    report['stopped'] = 'model_budget_exhausted'
                    report['final_status'] = 'unavailable'
                    break
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
            if not model_calls_allowed():
                report['stopped'] = 'model_budget_exhausted'
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
    with ProjectLease(app):
        return generate(args)


def generate(args):
    from arcbench_agent_runtime import AgentRuntime
    app = Path(args.output_dir).resolve()
    settings = PlatformSettings.from_env()
    include_tests = settings.input_policy == 'provided_tests'
    inputs = resolve_task_inputs(Path(args.requirements).resolve(), include_tests=include_tests)
    if not include_tests and any(is_test_path(path.relative_to(app).as_posix()) for path in app.rglob('*')):
        raise ValueError('Requirements-only generation needs a project without evaluation tests; use a separate source-only workspace')
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
        registered=inputs.registry_entry is not None, input_policy=settings.input_policy, tests_source=inputs.tests_source,
        tests_dir=str(inputs.tests_dir) if inputs.tests_dir else None, declared_tests=inputs.declared_tests,
        scenario_count=inputs.scenario_count, token_limit=inputs.token_limit, token_limit_override=settings.token_limit,
        scaffolded=scaffolded, launcher_only=is_scaffold(app) and not scaffolded,
        entities=provenance_counts(ir.get('entities')), ui_elements=provenance_counts(ir.get('ui_elements')),
        scenarios=provenance_counts(ir.get('scenarios'))), ensure_ascii=False))
    # Requirements and reference assets are the current official inputs.
    bundle = load_task_bundle(**bundle_kwargs(inputs), include_tests=include_tests)
    reports = app/'.arc'/'platform-budget'/task
    reports.mkdir(parents=True, exist_ok=True)
    if not os.environ.get('MODEL', '').strip():
        raise ValueError('MODEL must be supplied explicitly by the runner')
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
        audit_log=lambda row: append_jsonl(reports/'context.jsonl',row), campaign_budget=budget,
        execution_mode=settings.execution_mode, execution_limits=settings.execution_limits(),
        reasoning_effort=settings.reasoning_effort, workspace_context_bytes=settings.workspace_context_bytes)
    logging.info('PLATFORM_MODEL_POLICY %s', json.dumps(dict(
        reasoning_effort=settings.reasoning_effort, max_output_tokens=settings.max_output_tokens,
        execution_mode=settings.execution_mode, token_limit=budget.state['limit'])))
    tools = StaticProjectTools(app, include_tests=include_tests)
    tools.public_tests = inputs.tests_dir
    runtime = AgentRuntime.from_env(project_dir=str(app))
    runtime.traceability.init_db()
    runtime.traceability.store_requirement_tree(json.loads(json.dumps(bundle.to_dict()['requirements'])))
    runtime.events.mark_run_started('Source generation with static validation and runtime check on a scratch copy; formal evaluation follows')
    contract = global_contract_schema(bundle)
    execution_contract = platform_contract(settings.input_policy)
    contract['execution_contract'] = execution_contract
    contract['input_policy'] = settings.input_policy
    if not include_tests:
        contract['evaluation_boundary'] = ('Implement the complete supplied requirements, including behavior not sampled by tests. '
            'Evaluation tests and their results are withheld until the source is frozen; do not seek or infer evaluator internals. '
            'Build and HTTP probes are development diagnostics, not behavioral acceptance.')
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
            if not include_tests and is_test_path(path.name):
                continue
            result.repository.add_evidence('evidence/platform-'+path.name,path.read_text(encoding='utf-8'))
        result.repository.add_evidence('evidence/platform-contract.json',json.dumps(execution_contract))
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
        # The packet survives multiple plan steps. Mutable files here would be
        # stale as well as duplicated by _run's fresh current_workspace message.
        # Retain pinned repository entries for explicit, hash-checked reads.
        if not settings.workspace_context_bytes:
            sources += [('application/'+name,data) for name,data in sorted(files.items())
                        if name in paths and not name.startswith('requirements/')
                        and Path(name).name not in {'package-lock.json','pnpm-lock.yaml','yarn.lock'}]
        lead = ('Implement the ENTIRE task in one shared application, including all atomic requirements. ' if whole else
                'Implement the selected requirement(s) ' + ', '.join(selection) + ' in the one shared application. '
                'Other requirements are implemented in separate batches: preserve existing work for them, extend shared '
                'layout, routes, store collections and seed data instead of replacing them. ')
        text = (lead + ir_prompt_summary(load_task_ir(app, ir), starter.get('layout')) + INTERACTION_CONVENTIONS +
                ('Application source is supplied separately in the current_workspace observation at each stage. '
                 'It may omit files explicitly; use read_file for omitted or changed source. '
                 'The full derived docs/task-ir.json remains available by read_file when its summary is insufficient. '
                 if settings.workspace_context_bytes else '') +
                'The following source contents have already been read '
                'for you. Do not reread unchanged files shown completely below. Inspect the supplied '
                'contents, plan shared state once, then batch complete source files with write_files. '
                'Preserve package dependencies where possible. Avoid repeated explanations and repeated '
                'checks. Return completion JSON only after every required flow is implemented. '
                'Dependency lockfiles are immutable and excluded from the text payload; the complete '
                'package.json files describe the dependencies available to your implementation. '
                'Do not stop after the first requirement. No application commands are available to you; '
                'the orchestrator runs the development checks permitted by the input policy; '
                'independent formal evaluation follows source delivery.\n'
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
        control = getattr(model, 'execution_control', None)
        if control is not None:
            try:
                control.check()
            except ExecutionStopped:
                return False
        return budget.can_request(settings.max_output_tokens)
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
            if reviewed.get('reason') in ALLOCATION_STOP_REASONS:
                break
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
    elif getattr(getattr(model, 'execution_control', None), 'stopped', None):
        control_reason = getattr(getattr(model, 'execution_control', None), 'stopped', None)
        runtime_report['skipped'] = 'execution_stopped'
        runtime_report['stop_reason'] = control_reason or budget.state.get('stopped') or result.get('reason')
    if runtime_report['skipped']:
        logging.info('PLATFORM_RUNTIME_CHECK %s', json.dumps(dict(
            task_id=task, skipped=runtime_report['skipped'], signals={}, diagnostics={})))
        (reports/'runtime-check.json').write_text(json.dumps(runtime_report,indent=2),encoding='utf-8')
    else:
        runtime_report = run_runtime_loop(app, inputs.tests_dir, settings, model=model, tools=tools,
            make_packet=lambda: packet(all_ids), record=record, validate=validate, task_id=task,
            report_path=reports/'runtime-check.json', ir=load_task_ir(app, ir), model_calls_allowed=budget_open)
        final = validate()
        (reports/'static-validation.json').write_text(json.dumps(final,indent=2),encoding='utf-8')
        if not final['passed']:
            raise RuntimeError('Source is not ready for platform build after runtime repair: '+json.dumps(final['errors']))
    control = getattr(model, 'execution_control', None)
    if control is not None and control.stopped in {'rollback_failed', 'concurrent_source_change'}:
        raise ExecutionStopped(control.stopped)
    final = validate()
    (reports/'static-validation.json').write_text(json.dumps(final, indent=2), encoding='utf-8')
    if not final.get('passed'):
        raise RuntimeError('Final delivery source is invalid after review or rollback')
    final_ir = load_task_ir(app, ir)
    final_missing = missing_coverage(app, final_ir)
    if final_missing:
        logging.warning('PLATFORM_COVERAGE %s', json.dumps(dict(task_id=task,
            status='incomplete', item='final_delivery', missing=final_missing), ensure_ascii=False))
    for item in work:
        for req_id in item.requirement_ids:
            if completed.get(item.id) and not missing_coverage(app, final_ir, (req_id,)):
                runtime.events.mark_implementation_done(req_id,'Source delivered; formal tests pending')
    runtime.events.mark_run_completed('Source generation completed; no local acceptance claim; platform build and tests pending')
    delivery_source = source_digest(snapshot_project(app))
    verification = verification_summary(app, final_ir, final, runtime_report,
                                        input_policy=settings.input_policy, source_sha256=delivery_source)
    (reports/'verification.json').write_text(json.dumps(verification, indent=2), encoding='utf-8')
    (reports/'generation-boundary.json').write_text(json.dumps(dict(
        input_policy=settings.input_policy, requirement_source_sha256=bundle.source_hash,
        delivered_source_sha256=delivery_source, evaluation_tests_visible=include_tests,
        behavioral_acceptance='UNRUN' if not include_tests else 'SEE_RUNTIME_REPORT',
        formal_status='UNRUN'), indent=2), encoding='utf-8')
    logging.info('PLATFORM_DELIVERY %s',json.dumps(dict(task_id=task,input_policy=settings.input_policy,
        source_sha256=delivery_source,formal_status='PENDING',budget=budget.state,
        runtime_check=dict(skipped=runtime_report.get('skipped'),final_status=runtime_report.get('final_status'),
                           rounds=len(runtime_report.get('rounds') or [])))))
    return 0


def append_jsonl(path,row):
    with path.open('a',encoding='utf-8') as stream: stream.write(json.dumps(row,ensure_ascii=False)+'\n')

"""Source generation in the official runner; platform owns build and acceptance.

No application command is executed by the model in the credential-bearing
generator process. The only editable filesystem is the output application.
The default requirements-only policy withholds evaluation tests. The explicit
provided_tests compatibility policy can run supplied tests on a scratch copy
and return their failures for bounded repair. Requirements-only generation can
freeze its own bounded diagnostics; the delivered project receives no build output.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass, field, replace
import hashlib
import json
import logging
import math
import os
import random
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
                                 ControlPolicy,
                                 SourceTransaction, source_digest, validate_arguments, validate_plan,
                                 SOURCE_SAFETY_STOPS, EXECUTION_STOP_REASONS, BUDGET_STOP_REASONS,
                                 ALLOCATION_STOP_REASONS, PHASE_BUDGET_STOPS, diagnostics_allowed)
from .model_transport import MAX_MODEL_DEADLINE_SECONDS, ModelRequestInterrupted
from .competition_context import assemble_context, build_work_plan, global_contract_schema
from .llm import ModelClient
from .platform_acceptance import triage
from . import behavior_checks
from .platform_inputs import bundle_kwargs, resolve_task_inputs
from .platform_runtime_check import api_failures, failure_payload, run_runtime_check, runtime_log_fields, safe_rmtree
from .platform_scaffold import SCAFFOLD_GUIDE, has_full_scaffold, is_scaffold, prepare_scaffold
from .platform_seed import collect_seed_evidence, seed_instructions
from .platform_settings import PlatformSettings, model_allowed
from .platform_self_test import initialize_self_test, finalize_self_test, record_delivery_failure
from .platform_static import missing_coverage, seed_coverage, ui_element_ref, validate_source, write_coverage
from .platform_task_ir import build_task_ir, detect_runtime, write_task_ir
from .sandbox_files import apply_snapshot, snapshot_project
from .source_observations import READ_TOOLS, SourceObservations
from .verification_handoff import VerificationHandoff
from .task_bundle import is_test_path, load_task_bundle
from .tools import ProjectTools

WHOLE_TASK_PREFIX = 'SOURCE_COMPLETENESS: ALL_TEXT_SOURCES_INCLUDED\n'
PARTIAL_TASK_PREFIX = 'SOURCE_COMPLETENESS: PARTIAL_READ_OMITTED_RANGES\n'
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
STATIC_REPAIR_FEEDBACK = ('Repair only these observed static errors, then validate and finish. Preserve the required '
                          'roles, names and cardinality; resolve an interaction within its specified scope rather than '
                          'removing or renaming required repeated controls. Do not redesign '
                          'passing source or edit tests.\n')
RUNTIME_REPAIR_FEEDBACK = ('The orchestrator installed, built and started a copy of your delivered source. '
                           'Repair the source only, keep passing flows and docs/task-ir.json and docs/coverage.json valid, '
                           're-read the failing check for exact names, routes, prerequisite navigation and seed data, '
                           'do not edit tests or requirements, then call validate_source and finish.\n')
INTERACTION_CONVENTIONS = (
    'Render each parsed element with its role and accessible name unchanged (accessible-name semantics).\n'
    'Use the original public requirements to determine cardinality and interaction scope. A strict action needs one '
    'target in that scope (Playwright strict mode); required repeated controls in separate rows or containers remain distinct. IR multiple=false '
    'can mean repetition was not recognized, not that the page must contain one instance.\n'
    'IR groups and persistent flags are extraction hints about availability, not instructions to choose a navigation '
    'landmark. Preserve the specified toolbar, navigation, region or other container semantics.\n'
    'Preserve an explicit list as a list and a region as a region; a named list may be nested in a region without '
    'changing its role. Dialogs and form controls retain their required roles and accessible labels (WAI-ARIA).\n'
    'The action in When must succeed in the Given session. Derive authentication prerequisites from the full public '
    'requirements and supplied setup, including flows the parser does not recognize. An absent IR auth block is unknown; '
    'it neither permits anonymous access nor justifies adding a sign-in gate to an anonymous operation.\n'
    'State that must survive reload is stored on the server; the UI updates immediately (observable-result semantics).\n'
    'Lists can hold many uniquely suffixed records without a forced pager; no default animation; time uses the zone the requirement states; the first view is interactive within one second (external automation constraints).\n'
)


class PlatformTokenBudget:
    """One uploaded run's allocation, reserved from the external 30M campaign.

    Reserve the documented full provider input bound before each call. Unknown
    usage retains its reservation. A classified adaptive transport recovery may
    transfer it to permanent liability; all subsequent admission includes it.
    Reopening an existing ledger remains forbidden, including after restart.
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
                          used=0, requests=0, tools=0, pending=None, stopped=None, unknown_reservations=[],
                          logical_requests=0, api_retries=0, api_wait_seconds=0.0,
                          service_state='AVAILABLE', service_reason=None,
                          self_test_reserve_requests=0, self_test_reserve_tokens=0,
                          token_reserve_unfunded=False)
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

    @property
    def committed_tokens(self):
        return self.state['used'] + sum(item['held'] for item in self.state['unknown_reservations'])

    def service_transition(self, state, reason):
        """Run-local, terminal service gate; never grants retry capacity."""
        previous = self.state['service_state']
        if previous == 'OPEN' and state != 'OPEN':
            raise CampaignBudgetStopped('request_interrupted_usage_unknown')
        if state not in {'AVAILABLE', 'RETRY_WAIT', 'OPEN'}:
            raise ValueError('invalid service state')
        self.state['service_state'], self.state['service_reason'] = state, reason
        try:
            self._save()
        except BaseException:
            # Persistence failure must never leave an in-memory admission open.
            self.state['service_state'], self.state['service_reason'] = 'OPEN', 'ledger_write_failed'
            raise
        return previous

    def recovery_capacity_reason(self, max_output, api_wait):
        pending = self.state['pending']
        if self.state['api_retries'] >= self.settings.api_retry_total:
            return 'run_retry_limit'
        if self.state['api_wait_seconds'] + api_wait > self.settings.api_wait_seconds:
            return 'wait_budget'
        if (self.committed_tokens + pending['held'] + self.config.max_input_tokens + max_output
                + self.state['self_test_reserve_tokens'] > self.state['limit']):
            return 'token_capacity'
        if self.state['requests'] + 1 + self.state['self_test_reserve_requests'] > self.state['request_limit']:
            return 'request_capacity'
        return None

    def retain_unknown_for_recovery(self, identity, max_output, *, api_wait=None):
        """Retain the entire unknown reservation; admit a distinct recovery request."""
        pending = self.state['pending']
        if (not pending or pending['id'] != identity
                or self.state['stopped'] != 'request_interrupted_usage_unknown'
                or type(max_output) is not int or not 1 <= max_output <= self.settings.max_output_tokens):
            raise ValueError('invalid unknown-usage recovery reservation')
        if api_wait is not None:
            if type(api_wait) not in {int, float} or not math.isfinite(api_wait) or api_wait < 0:
                raise ValueError('invalid API retry wait')
            if (self.state['api_retries'] >= self.settings.api_retry_total
                    or self.state['api_wait_seconds'] + api_wait > self.settings.api_wait_seconds):
                return False
        if (self.committed_tokens + pending['held'] + self.config.max_input_tokens + max_output
                + self.state['self_test_reserve_tokens'] > self.state['limit']
                or self.state['requests'] + 1 + self.state['self_test_reserve_requests'] > self.state['request_limit']):
            return False
        previous = self.state
        self.state = dict(previous, pending=None, stopped=None,
            unknown_reservations=[*previous['unknown_reservations'],
                dict(pending, reason=previous['stopped'], usage_complete=False)])
        if api_wait is not None:
            self.state['api_retries'] += 1
            self.state['api_wait_seconds'] += api_wait
        try:
            self._save()
        except BaseException:
            self.state = previous
            raise
        return True

    def begin_logical_request(self):
        if self.state['service_state'] == 'OPEN':
            raise CampaignBudgetStopped('request_interrupted_usage_unknown')
        self.check_time('logical_request')
        self.state['logical_requests'] += 1
        self._save()

    def finish_api_wait(self, reserved, elapsed):
        self.state['api_wait_seconds'] += max(0.0, elapsed - reserved)
        self._save()
        return self.state['api_wait_seconds'] <= self.settings.api_wait_seconds

    def protect_self_test_requests(self, count):
        """Hold author requests and an affordable token buffer without refunding usage.

        When the initial allocation cannot fund two full request bounds, keeping
        a second token bound would prevent any implementation. Preserve the
        request/time reserve and report that token protection is unavailable;
        every actual call still reserves the original full input/output bound.
        """
        if type(count) is not int or count < 0:
            raise ValueError('self-test request reserve must be a nonnegative integer')
        self.state['self_test_reserve_requests'] = count
        bound = self.config.max_input_tokens + self.settings.max_output_tokens
        if count:
            self.state['token_reserve_unfunded'] = self.state['limit'] < (count + 1) * bound
        self.state['self_test_reserve_tokens'] = count * bound if not self.state['token_reserve_unfunded'] else 0
        self._save()

    def reserve(self, stage, input_upper_bound, max_output):
        if self.state['service_state'] == 'OPEN':
            raise CampaignBudgetStopped('request_interrupted_usage_unknown')
        self.check_time(stage)
        if input_upper_bound != self.config.max_input_tokens or not 1 <= max_output <= self.settings.max_output_tokens:
            raise ValueError('request outside documented input/output reservation')
        held = input_upper_bound + max_output
        if self.committed_tokens + held > self.state['limit']:
            self.stop('platform_token_allocation')
            raise CampaignBudgetStopped('platform_token_allocation')
        if self.state['requests'] >= self.state['request_limit']:
            self.stop('platform_request_allocation')
            raise CampaignBudgetStopped('platform_request_allocation')
        if (self.committed_tokens + held + self.state['self_test_reserve_tokens'] > self.state['limit']
                or self.state['requests'] + 1 + self.state['self_test_reserve_requests'] > self.state['request_limit']):
            # This phase has reached its share; final authoring can still use its held share.
            raise CampaignBudgetStopped('self_test_reserve')
        identity = uuid4().hex
        self.state['pending'] = dict(id=identity, held=held, output=max_output, stage=stage)
        self.state['requests'] += 1
        self._save()
        return identity

    def can_request(self, max_output):
        """Read-only admission probe; never releases unknown reservations."""
        return (self.state['service_state'] != 'OPEN' and not self.state['stopped'] and self.state['pending'] is None
                and self.state['requests'] + self.state['self_test_reserve_requests'] < self.state['request_limit']
                and self.committed_tokens + self.config.max_input_tokens + max_output
                    + self.state['self_test_reserve_tokens'] <= self.state['limit'])

    def decision_capacity(self, max_output):
        """Advisory measured runway; admission still reserves the full bound."""
        reserve = self.config.max_input_tokens + max_output
        spendable = max(0, self.state['limit'] - self.committed_tokens - reserve - self.state['self_test_reserve_tokens'])
        peak = self.state.get('peak_request_tokens')
        estimate = (min(self.state['request_limit'] - self.state['requests'] - self.state['self_test_reserve_requests'],
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

    def record_tools(self, stage, count, *, diagnostic=False):
        if type(count) is not int or count < 0:
            raise ValueError('tool count must be a nonnegative integer')
        if not (self.state['stopped'] and diagnostic and diagnostics_allowed(self.state['stopped'])):
            self.check_time(stage)
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


@dataclass
class GenerationStage:
    """One model loop's observations and local limits; never shared across stages."""
    packet: object
    mode: str
    started_requests: int
    allowance: int
    step: object = None
    inspection_turns: int = 0
    state_message: dict | None = None
    visible_source_hashes: dict = field(default_factory=dict)
    source_read_receipts: dict = field(default_factory=dict)
    observations: SourceObservations = field(default_factory=SourceObservations)
    new_information: bool = False
    completed: bool = False
    can_yield_verification: bool = False
    verify_on_return: bool = False

    def observe_tool_turn(self, before, after, names, *, new_evidence=False):
        if self.mode != 'implement':
            return
        if before != after or new_evidence:
            self.inspection_turns = 0
        elif names and all(name in READ_TOOLS for name in names):
            self.inspection_turns += 1


class PlatformGenerationModel(CompetitionModel):
    def __init__(self, *args, execution_mode='react', execution_limits=None,
                 workspace_context_bytes=200000, step_packet_factory=None, step_behavior_gate=None,
                 control_policy=None, budget_planner=None, **kwargs):
        super().__init__(*args, **kwargs)
        if execution_mode not in {'react', 'plan_execute'}:
            raise ValueError('execution_mode must be react or plan_execute')
        self.execution_mode = execution_mode
        self.execution_control = RunControl(execution_limits, emit=self._append_log, policy=control_policy)
        self.budget_planner = budget_planner
        if type(workspace_context_bytes) is not int or workspace_context_bytes < 0:
            raise ValueError('invalid workspace context limit')
        self.workspace_context_bytes = workspace_context_bytes
        self.step_packet_factory = step_packet_factory
        self.step_behavior_gate = step_behavior_gate
        self.request_deadline = self.execution_control.deadline
        self.prospective_request_admission = False
        self.request_durations = []

    def _check_request_time_admission(self, *, wait=0.0):
        """Yield before spending a request that is unlikely to fit the phase.

        Recent complete responses inform admission, not a promise of completion.
        Interrupted requests retain their original usage liability and deadline.
        """
        if not self.prospective_request_admission:
            return
        control = self.execution_control
        control.check()
        remaining = min(control.deadline, self.request_deadline) - control.clock() - wait
        required = max(30.0, 1.25 * max(self.request_durations[-8:], default=0.0))
        client_cap = getattr(getattr(self.session, 'client', None), 'deadline_seconds', None)
        if isinstance(client_cap, (int, float)) and client_cap > 0:
            required = min(required, client_cap)
        if remaining <= required:
            self._append_log(dict(event='request_admission_yield', reason='self_test_time_reserve',
                remaining_seconds=remaining, required_seconds=required,
                observed_complete_seconds=self.request_durations[-8:], requests=control.requests))
            raise ExecutionStopped('self_test_time_reserve')

    def _run(self, packet, tools, *, step=None, verify_on_return=False, **kwargs):
        control = self.execution_control
        phase = {'design': 'PLAN', 'implement': 'CODE'}.get(kwargs['mode'])
        if phase and not control.stopped:
            control.set_task_state(phase, reason=kwargs['stage'])
        stage_context = GenerationStage(packet=packet, mode=kwargs['mode'], step=step,
            can_yield_verification=bool(step is not None and self.step_behavior_gate is not None
                                        and control.policy.enabled), verify_on_return=verify_on_return,
            started_requests=self.execution_control.requests,
            allowance=kwargs.get('request_allowance') if kwargs.get('request_allowance') is not None else self.request_allowance)
        files = control.source(tools.project_dir) if hasattr(tools, 'project_dir') else {}
        visible_ranges = getattr(packet, 'manifest', {}).get('visible_source_ranges')
        if visible_ranges is None:
            visible_ranges = [dict(path=chunk.path, sha256=chunk.sha256, start=chunk.start,
                                   end=chunk.start + len(chunk.text)) for chunk in getattr(packet, 'chunks', ())]
        for row in visible_ranges:
            stage_context.observations.context_range(row['path'], row['sha256'], row['start'], row['end'], files)
        messages = list(kwargs.get('initial_messages') or self._messages(
            packet, kwargs['stage'], kwargs['instructions']))
        schemas = self._tool_schemas(kwargs['mode'])
        occupied = len(json.dumps(dict(messages=messages, tools=schemas), ensure_ascii=False).encode('utf-8'))
        available = min(self.workspace_context_bytes,
            self.context_window_tokens - kwargs['max_tokens'] - occupied - 2048)
        if available >= 1024 and hasattr(tools, 'project_dir'):
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
                stage_context.visible_source_hashes = {row['path']: row['sha256'] for row in document['files']
                                                      if row['content_included']}
                stage_context.observations.entries_seen(('file', row['path']) for row in document['files'])
                for row in document['files']:
                    if row['content_included']:
                        stage_context.observations.file(tools, files, row['path'],
                            row['content'].replace('\r\n', '\n').replace('\r', '\n'))
                self._append_log(dict(event='workspace_context', source_sha256=document['source_sha256'],
                    bytes=size(), files=len(document['files']),
                    included_files=sum(row['content_included'] for row in document['files']),
                    inventory_complete=document['inventory_complete']))
        kwargs['initial_messages'] = messages
        result = super()._run(packet, tools, stage_context=stage_context, **kwargs)
        result['turns'] = control.requests - stage_context.started_requests
        return result

    def _planning_capacity(self, packet, allowance, *, requirement_ids=None):
        """Expose accounting, not a task-specific plan or a promise of tokenization."""
        control = self.execution_control
        # At least one PLAN request precedes execution. Later planning calls and
        # per-step allocation can further reduce this admission upper bound.
        step_budget = control.soft_request_allowance(min(control.limits.step_requests,
            max(0, allowance - 1), max(0, control.limits.requests - control.requests - 1)), stage='CODE')
        refs = requirement_ids if requirement_ids is not None else self._manifest(packet).get('requirement_ids', [])
        author_reserve = (self.step_behavior_gate.request_reserve(SimpleNamespace(requirement_ids=refs))
                          if self.step_behavior_gate is not None else 0)
        result = dict(requests_remaining=max(0, allowance), max_steps=control.limits.plan_steps,
            hard_step_request_limit=control.limits.step_requests,
            next_step_request_allowance=step_budget, next_step_author_request_reserve=author_reserve,
            next_step_implementation_allowance=max(0, step_budget - author_reserve),
            step_allowance_note='Upper bound after one planning request; further planning and allocation remain charged. '
                                'Author reserve applies to a step with unfrozen requirements.')
        ledger = getattr(self.campaign_budget, 'state', None)
        if isinstance(ledger, dict) and 'limit' in ledger and 'used' in ledger:
            reserve = self.campaign_budget.config.max_input_tokens + self.output_reserve_tokens
            committed = getattr(self.campaign_budget, 'committed_tokens', ledger['used'])
            spendable = max(0, ledger['limit'] - committed - reserve - ledger.get('self_test_reserve_tokens', 0))
            payload = len(self._packet_text(packet).encode('utf-8')) + self.workspace_context_bytes
            # A conservative planning heuristic; hard admission still uses the unchanged ledger.
            turn_cost = max(1, payload + self.output_reserve_tokens)
            turns = min(max(0, allowance), 1 + spendable // turn_cost)
            result.update(tokens_remaining=ledger['limit']-ledger['used'], next_request_reserve=reserve,
                          planning_turn_cost_bytes_plus_output=turn_cost, affordable_turns_estimate=turns,
                          suggested_steps=min(result['max_steps'], max(1, (turns - 1) // 2)))
        return result

    def _update_decision_message(self, messages, *, tools, stage, max_tokens, decision):
        """Replace advisory controller context without changing previously sent requests."""
        control = self.execution_control
        remaining = max(0, decision.allowance - (control.requests - decision.started_requests))
        tool_remaining = max(0, min(control.limits.tools - control.tools,
            self.max_tool_calls - getattr(self.session, 'tool_calls_used', 0)))
        state = dict(kind='controller_decision_state', requests_remaining_including_this=remaining,
                     total_requests=control.requests, total_tools=control.tools,
                     tool_calls_remaining=tool_remaining,
                     tool_calls_per_response_limit=min(control.limits.batch_tools, tool_remaining),
                     inspection_turns=decision.inspection_turns,
                     inspection_turn_limit=control.limits.inspection_turns,
                     phase='finish' if decision.mode == 'implement' and remaining <= 3 else 'execute')
        if getattr(control.policy, 'enabled', False):
            state.update(task_state=control.task_state, control_state=control.control_state,
                         warning_reason=control.warning_reason, fault_reason=control.fault_reason,
                         recovery_count=control.recovery_count,
                         soft_requests_remaining=control.soft_request_allowance(remaining))
        ledger = getattr(self.campaign_budget, 'state', None)
        if isinstance(ledger, dict):
            state.update(tokens_remaining=ledger['limit']-getattr(self.campaign_budget, 'committed_tokens', ledger['used']),
                         next_request_reserve=self.campaign_budget.config.max_input_tokens+max_tokens,
                         requests_reserved_for_self_tests=ledger.get('self_test_reserve_requests', 0),
                         tokens_reserved_for_self_tests=ledger.get('self_test_reserve_tokens', 0))
        capacity = getattr(self.campaign_budget, 'decision_capacity', None)
        if capacity is not None:
            state.update(capacity(max_tokens))
        if control.plan_progress is not None:
            state['plan_progress'] = control.plan_progress
            plan_remaining = max(0, control.plan_progress['request_ceiling'] - control.requests)
            state['plan_requests_remaining_including_this'] = plan_remaining
            estimate = state.get('affordable_requests_estimate')
            available = plan_remaining if estimate is None else min(plan_remaining, estimate)
            state['requests_per_remaining_step_estimate'] = max(0, available - 2) // control.plan_progress['remaining_steps']
        if decision.mode == 'implement':
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
                    'plan_requests_remaining_including_this is the remaining allocation for this entire '
                    'plan phase, including replanning; a larger global token estimate does not extend it. '
                    'The final step must integrate and validate source-backed claims for ALL original '
                    'phase_requirement_ids, preserving earlier work. When plan_progress.whole_task is true, '
                    'this also includes parent work items and public scenarios and UI elements without '
                    'a work-item association; atomic selection does not waive those source-backed claims. '
                    'Batch related source edits and coverage updates, avoid cosmetic passes while required '
                    'behavior remains, and close this step once its required behavior is complete. '
                    'Never skip a requirement or report an unimplemented behavior as covered.')
            if decision.inspection_turns >= control.limits.inspection_turns - 1:
                state['instruction'] += (' Inspection allowance is nearly exhausted. Use the supplied '
                    'source and observations to edit or validate now; another read turn without new information '
                    'at the limit ends this step without another model request. Batch necessary reads.')
            state['inspection_semantics'] = 'consecutive read-only turns without new visible source information'
        else:
            state['instruction'] = ('Return the requested read-only diagnostic JSON within this allowance; '
                'use supplied observations before requesting more reads.' if decision.mode == 'audit' else
                'Return a valid complete plan within this allowance; use supplied observations before requesting more reads.')
        if getattr(control.policy, 'enabled', False) and control.control_state == 'WARNING':
            state['instruction'] += (' The controller requires a bounded closeout: validate_source and '
                'finish the current step so actual verification can measure progress. New reads or writes '
                'alone do not establish quality improvement.')
        state['instruction'] += (
            ' Each response must stay within tool_calls_per_response_limit, including the final '
            'request; group related edits using the tools within their individual schemas. '
            'All other shared budgets still apply. Do not assume a final request permits an oversized batch.')
        previous = decision.state_message
        if previous is not None:
            messages[:] = [message for message in messages if message is not previous]
            self._append_log(dict(event='controller_state_replaced',
                removed_utf8_bytes=len(json.dumps(previous, ensure_ascii=False).encode('utf-8'))))
        current = dict(role='user', content=json.dumps(state, ensure_ascii=False))
        messages.append(current)
        decision.state_message = current
        if not self._check_context(messages, tools, stage, decision.packet, max_tokens):
            control.stop('context_limit')
        self._append_log(dict(event='decision_state', **state))

    def _service_transition(self, state, reason):
        budget = self.campaign_budget
        previous = budget.service_transition(state, reason)
        try:
            self._append_log(dict(event='service_state_transition', previous=previous,
                service_state=state, reason=reason, logical_request=budget.state['logical_requests'],
                api_retries=budget.state['api_retries'], api_wait_seconds=budget.state['api_wait_seconds']))
        except BaseException:
            budget.state['service_state'], budget.state['service_reason'] = 'OPEN', 'service_event_failed'
            try:
                budget._save()
            except BaseException:
                pass  # No reopening after restart; the live owner stays fail-closed.
            raise


    def _request(self, *args, stage_context=None, **kwargs):
        budget = self.campaign_budget
        if isinstance(budget, PlatformTokenBudget) and budget.state['service_state'] == 'OPEN':
            self.execution_control.stop('request_interrupted_usage_unknown')
        try:
            result = self._request_with_recovery(*args, stage_context=stage_context, **kwargs)
            if isinstance(budget, PlatformTokenBudget) and budget.state['service_state'] == 'RETRY_WAIT':
                self._service_transition('AVAILABLE', 'complete_response')
            return result
        except BaseException as error:
            if isinstance(budget, PlatformTokenBudget) and budget.state['service_state'] == 'RETRY_WAIT':
                try:
                    self._service_transition('OPEN', getattr(error, 'reason', None) or 'recovery_failed')
                except BaseException:
                    # service_transition already closes in memory; preserve the original failure.
                    error.add_note('Service OPEN persistence or event recording also failed; admission remains closed.')
            raise

    def _request_with_recovery(self, *args, stage_context=None, **kwargs):
        control, budget, session = self.execution_control, self.campaign_budget, self.session
        self._check_request_time_admission()
        pause = getattr(self, 'live_pause', None)
        if pause is not None:
            pause.boundary(self, messages=args[0] if args else kwargs['messages'],
                tools=kwargs['tools'], stage=kwargs['stage'], max_tokens=kwargs['max_tokens'])
        if isinstance(budget, PlatformTokenBudget):
            budget.begin_logical_request()
        retries = 0
        while True:
            try:
                return self._request_once(*args, stage_context=stage_context, **kwargs)
            except ModelRequestInterrupted as error:
                service_retry = isinstance(session, ModelClient) and session.can_resume_after_service_error(error)
                service_fault = isinstance(budget, PlatformTokenBudget) and error.reason in {'http_503', 'http_503_retry_after_invalid'}
                if service_fault:
                    self._service_transition('RETRY_WAIT', error.reason)
                base = min(30.0, 2.0 ** min(retries, 5))
                backoff = max(error.retry_after_seconds, random.uniform(base / 2, base)) if service_retry else 0.0
                declined = None
                if not isinstance(budget, PlatformTokenBudget) or not isinstance(session, ModelClient):
                    declined = 'unsupported_recovery_owner'
                elif not (service_retry or session.can_resume_with_json(error)):
                    declined = 'ineligible_interruption'
                elif control.control_state not in {'RUNNING', 'WARNING', 'PROBATION'} or control.stopped:
                    declined = 'controller_stopped'
                elif retries >= budget.settings.api_retries_per_call:
                    declined = 'per_call_retry_limit'
                elif stage_context is not None and control.requests - stage_context.started_requests >= stage_context.allowance:
                    declined = 'stage_request_allowance'
                elif control.plan_progress is not None and control.requests >= control.plan_progress['request_ceiling']:
                    declined = 'plan_request_allowance'
                elif control.requests + 1 + budget.state['self_test_reserve_requests'] > control.limits.requests:
                    declined = 'protected_request_capacity'
                elif control.clock() + backoff >= min(control.deadline, self.request_deadline):
                    declined = 'retry_deadline'
                messages = list(args[0] if args else kwargs['messages'])
                messages.append(dict(role='user', content=(
                    'The service returned HTTP 503 without a complete response. No tool calls ran. '
                    'The controller retained the full unknown token liability and admitted a bounded retry '
                    'with the same transport. Continue from confirmed tool results and leave time for self-tests.'
                    ) if service_retry else (
                    'The previous response stream could not be decoded completely. Its response was discarded; '
                    'none of its tool calls ran. The controller retained its full unknown token liability and '
                    'changed to complete JSON transport. Continue from the last confirmed tool results; '
                    'keep edits bounded and leave time for actual self-tests. Do not assume the failed response changed files.')))
                context_bytes = len(json.dumps(dict(messages=messages, tools=kwargs['tools']), ensure_ascii=False).encode('utf-8'))
                if declined is None and context_bytes + kwargs['max_tokens'] > self.context_window_tokens:
                    declined = 'recovery_context_limit'
                if declined is None:
                    try:
                        self._check_request_time_admission(wait=backoff)
                    except ExecutionStopped:
                        declined = 'retry_time_admission'
                if declined is not None:
                    if service_fault:
                        self._service_transition('OPEN', declined)
                    control.stop('request_interrupted_usage_unknown')
                pending = budget.state['pending']
                if not pending or budget.state['stopped'] != 'request_interrupted_usage_unknown':
                    if service_fault:
                        self._service_transition('OPEN', 'unknown_reservation_missing')
                    control.stop('request_interrupted_usage_unknown')
                control.permit('request')
                for project_dir in tuple(control.expected_sources):
                    control.source(project_dir)
                capacity_reason = budget.recovery_capacity_reason(kwargs['max_tokens'], backoff)
                if not budget.retain_unknown_for_recovery(pending['id'], kwargs['max_tokens'], api_wait=backoff):
                    if service_fault:
                        self._service_transition('OPEN', capacity_reason or 'recovery_capacity')
                    self._append_log(dict(event='api_recovery_declined', reason='retry_or_reserved_capacity'))
                    control.stop('request_interrupted_usage_unknown')
                retries += 1
                if service_retry:
                    self._append_log(dict(event='service_recovery_wait', seconds=backoff,
                        api_retries=budget.state['api_retries'], logical_retry=retries))
                    waiting_since = control.clock()
                    try:
                        time.sleep(backoff)
                    finally:
                        wait_allowed = budget.finish_api_wait(backoff, control.clock() - waiting_since)
                    if not wait_allowed:
                        self._service_transition('OPEN', 'wait_overrun')
                        control.stop('request_interrupted_usage_unknown')
                error_pause = getattr(self, 'error_pause', None)
                if error_pause is not None:
                    error_pause.boundary(self, messages=messages, tools=kwargs['tools'],
                                         stage=kwargs['stage'], max_tokens=kwargs['max_tokens'])
                control.permit('request')
                if control.clock() >= self.request_deadline:
                    control.stop('self_test_time_reserve')
                for project_dir in tuple(control.expected_sources):
                    control.source(project_dir)
                if service_retry:
                    session.resume_after_service_error(error)
                else:
                    session.resume_with_json(error)
                self._append_log(dict(event='service_recovery' if service_retry else 'transport_recovery',
                    transport='assembled_stream' if session.client.assemble_stream else 'json',
                    api_retries=budget.state['api_retries'], source_recoveries=control.recovery_count,
                    unknown_reservation=dict(pending), reason=error.reason, cause_types=list(error.cause_types)))
                original_messages = args[0] if args else kwargs['messages']
                original_messages[:] = messages

    def _request_once(self, *args, stage_context=None, **kwargs):
        control = self.execution_control
        control.check()
        if (stage_context is not None
                and control.requests - stage_context.started_requests >= stage_context.allowance
                or control.plan_progress is not None and control.requests >= control.plan_progress['request_ceiling']):
            raise ExecutionStopped('request_allowance')
        if control.clock() >= self.request_deadline:
            raise ExecutionStopped('self_test_time_reserve')
        self._check_request_time_admission()
        if isinstance(self.campaign_budget, PlatformTokenBudget):
            self.campaign_budget.check_time(kwargs['stage'])
            reserved = self.campaign_budget.state['self_test_reserve_requests']
            if reserved and (control.requests + reserved >= control.limits.requests
                             or not self.campaign_budget.can_request(kwargs['max_tokens'])):
                raise ExecutionStopped('self_test_reserve')
        decision = stage_context
        if decision:
            if decision.mode == 'implement' and decision.inspection_turns >= control.limits.inspection_turns:
                self._append_log(dict(event='stage_stopped', reason='inspection_limit',
                    inspection_turns=decision.inspection_turns, requests=control.requests))
                # Local closeout: preserve global resources for validation. The
                # caller still rolls back this unverified transaction.
                raise ExecutionStopped('inspection_limit')
            self._update_decision_message(args[0] if args else kwargs['messages'],
                tools=kwargs['tools'], stage=kwargs['stage'], max_tokens=kwargs['max_tokens'], decision=decision)
        self._check_request_time_admission()
        control.request()
        client = getattr(self.session, 'client', None)
        deadline = getattr(client, 'deadline_seconds', None)
        bounded = hasattr(client, 'deadline_seconds') and (deadline is None or isinstance(deadline, (int, float)))
        if bounded:
            ceiling = deadline if deadline is not None else MAX_MODEL_DEADLINE_SECONDS
            client.deadline_seconds = min(ceiling, max(0.001, min(control.deadline, self.request_deadline) - control.clock()))
        started = control.clock()
        try:
            # Give the provider a request snapshot. Future closeout-state
            # replacement must not mutate an already sent/logged request.
            if args:
                args = (list(args[0]), *args[1:])
            elif 'messages' in kwargs:
                kwargs['messages'] = list(kwargs['messages'])
            response = super()._request(*args, **kwargs)
            if response is not None:
                self.request_durations.append(max(0.0, control.clock() - started))
                self.request_durations[:] = self.request_durations[-8:]
            control.check()
            if control.clock() >= self.request_deadline:
                raise ExecutionStopped('self_test_time_reserve')
            if response is None:
                control.stop(self._stopped_reason or 'invalid_response')
            return response
        except ModelRequestInterrupted as error:
            self._append_log(dict(event='request_interruption_boundary', reason=error.reason,
                elapsed_seconds=max(0.0, control.clock() - started),
                phase_deadline_reached=control.clock() >= self.request_deadline,
                hard_deadline_reached=control.clock() >= control.deadline,
                usage_status='unknown'))
            raise
        except (CampaignBudgetStopped, ExecutionStopped):
            raise
        except Exception:
            control.stop('model_request_failed')
        finally:
            if bounded:
                client.deadline_seconds = deadline

    def _react(self, packet, tools, *, step=None, **kwargs):
        repair_feedback = kwargs.pop('repair_feedback', None)
        self.execution_control.check()
        started_requests = self.execution_control.requests
        if hasattr(tools, 'unseal'):
            tools.unseal()
        if step is not None:
            # PlanExecutor owns this step's transaction, validation and final decision.
            instructions = (
                'Implement only the current validated plan step using its requirements and dependency contracts. '
                'Make bounded source edits through tools and preserve completed work. '
                'Inspect current files as needed; application/ is a display prefix, not a tool path. '
                'Use list_files with path="." for the project root. Each response must fit its output '
                'budget; split large changes across tool turns instead of emitting the entire application '
                'in one response. Leave structurally valid source and call validate_source. '
                'Finish with JSON containing summary and changed_paths. Static validity is not '
                'behavioral acceptance. The controller supplies current_plan_step after the task context.')
            if repair_feedback is not None:
                instructions += (' Repair the current candidate using the attached development diagnostics. '
                    'Preserve the step scope and prior accepted behavior. Frozen browser diagnostics may be wrong; '
                    'only fix independently supported requirement violations, never change the tests or invent behavior. '
                    'The controller will repeat static and behavior checks within the original step budget.')
            messages = self._messages(packet, 'competition_implementation', instructions)
            messages.append(dict(role='user', content=json.dumps(dict(
                kind='current_plan_step', step=vars(step),
                controller_feedback=repair_feedback or kwargs.get('feedback') or 'Execute the current step.'), ensure_ascii=False)))
            result = self._run(packet, tools, step=step, stage='competition_implementation', mode='implement',
                instructions=instructions, max_tokens=self.output_reserve_tokens,
                request_allowance=kwargs.get('request_allowance'), initial_messages=messages,
                verify_on_return=repair_feedback is not None)
            result['turns'] = self.execution_control.requests - started_requests
            return result

        checkpoint = SourceTransaction(tools, self.execution_control)
        try:
            result = super().implement(packet, tools, **kwargs)
            result['turns'] = self.execution_control.requests - started_requests
            self.execution_control.check()
            if result.get('status') == 'complete':
                validation = self._validate_step(tools)
                self.execution_control.check()
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

    def _validate_step(self, tools, *, step=None):
        checked = json.loads(tools.call('validate_source', {}))
        refs = step.requirement_ids if step is not None else None
        ir_path = Path(tools.project_dir) / 'docs/task-ir.json' if refs and hasattr(tools, 'project_dir') else None
        if checked.get('passed') and ir_path is not None and ir_path.is_file():
            ir = json.loads(ir_path.read_text(encoding='utf-8'))
            progress = self.execution_control.plan_progress
            if progress is not None and progress['remaining_steps'] == 1:
                refs = progress['phase_requirement_ids']
                if progress.get('whole_task') is True or set(refs) == {str(item['id']) for item in ir.get('work_items', [])
                                 if isinstance(item, dict) and item.get('id') is not None}:
                    refs = None
            missing = missing_coverage(tools.project_dir, ir, refs)
            if missing:
                tools.unseal()
                checked = dict(checked, passed=False, errors=[dict(path='docs/coverage.json',
                    error='Current step or final plan integration has missing source-backed claims', missing=missing)])
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
                capacity = self._planning_capacity(packet, allowance-(control.requests-started_requests), requirement_ids=pending)
                instructions = (
                    'Plan the supplied task from its actual requirements and current project. '
                    'Inspect as needed using read-only tools. Return JSON with steps: each step has '
                    'exactly id (nonempty string, max 80 characters), goal (nonempty string, max 2000), '
                    'requirement_ids (array of unique supplied ID strings), depends_on (array of unique '
                    'step ID strings), acceptance (nonempty string, max 2000). No extra step fields, '
                    'including file lists or status. IDs must be unique; dependencies '
                    'must form a DAG. Cover every pending requirement using only its supplied ID. '
                    'Every step, including foundation, integration and verification, must have nonempty '
                    'requirement_ids linking it to the pending requirements it implements or checks. '
                    'Each step must fit the supplied request/output limits and leave structurally valid source. '
                    'Use resource_capacity.next_step_implementation_allowance for the next implementation, '
                    'with its author reserve separate; hard_step_request_limit is not the current allowance. '
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
                if self.step_behavior_gate is not None:
                    instructions += (' Before committing each step, the controller authors and freezes bounded '
                        'public-requirement diagnostics and executes them alongside all previously accepted '
                        'journeys. Each step needs a supported browser observation for every linked requirement; '
                        f'limit each step to at most {behavior_checks.MAX_CASES} requirement IDs. '
                        'One request is reserved for authoring; unused implementation requests permit corrections. '
                        'These checks are sampled development evidence, not complete or formal acceptance.')
                    if getattr(self.step_behavior_gate, 'retain_unverified', False) is True:
                        instructions += (' Candidate delivery is enabled. Unsupported public constraints remain '
                            'explicitly unverified. Structurally valid construction checkpoints can be retained '
                            'without claiming requirement success. They are not runnable until the final real '
                            'build/start/browser check. Build an integrated runnable baseline early, and preserve '
                            'all public requirements and reference assets for independent evaluation.')
                context = json.dumps(dict(pending_requirement_ids=pending, completed_steps=completed,
                                          planning_evidence=planning,
                                          feedback=failure, max_steps=capacity['max_steps'], resource_capacity=capacity,
                                          total_requests_remaining=allowance-(control.requests-started_requests),
                                          step_request_limit=capacity['next_step_request_allowance'],
                                          response_output_limit=self.output_reserve_tokens), ensure_ascii=False)
                messages = self._messages(packet, 'plan', instructions)
                messages.append(dict(role='user', content=context))
                return self._run(packet, tools, stage='competition_implementation', mode='design',
                    instructions=instructions, max_tokens=self.output_reserve_tokens,
                    request_allowance=budget, initial_messages=messages,
                    final_validator=lambda value: validate_plan(value, pending, capacity['max_steps'],
                        planning['requirement_dependencies'], exhausted_scope=failure.get('requirement_ids', ())
                        if isinstance(failure, dict) and failure.get('reason') == 'request_allowance' else ()))

            def execute(step, budget):
                gate = self.step_behavior_gate
                if gate is not None:
                    budget = max(0, budget - gate.request_reserve(step))
                current = source_digest(snapshot_project(tools.project_dir))
                instruction = json.dumps(dict(current_source_sha256=current,
                    step_requests_remaining=budget, controller_feedback=feedback,
                    note='Execute this step using the current workspace snapshot. Read omitted or changed files as needed. Preserve completed steps.'), ensure_ascii=False)
                make_packet = self.step_packet_factory
                scoped = make_packet(step.requirement_ids) if make_packet else packet
                return self._react(scoped, tools, step=step, feedback=instruction, request_allowance=budget)

            def repair(step, diagnostic, budget):
                make_packet = self.step_packet_factory
                scoped = make_packet(step.requirement_ids) if make_packet else packet
                return self._react(scoped, tools, step=step, repair_feedback=diagnostic, request_allowance=budget)

            runner = PlanExecutor(control, tools, planner, execute,
                                  lambda step: self._validate_step(tools, step=step),
                                  self.step_behavior_gate, before_closeout=lambda: self.reserve_validation_steps(
                                      1, stage='competition_implementation'), budget_planner=self.budget_planner,
                                  repairer=repair if control.policy.enabled else None)
            result = runner.run(requirements, allowance, feedback,
                                whole_task=self._manifest(packet).get('whole_task') is True)
            if result.get('reason') in SOURCE_SAFETY_STOPS:
                raise ExecutionStopped(result['reason'])
            return result
        except ExecutionStopped as error:
            if error.reason in SOURCE_SAFETY_STOPS:
                raise
            return dict(status='stopped', reason=error.reason, turns=control.requests - started_requests)

    def review_sources(self, packet, tools, **kwargs):
        # A review is a bounded ReAct pass, not another nested planning tree.
        started_requests = self.execution_control.requests
        try:
            return self._react(packet, tools, review=True, **kwargs)
        except ExecutionStopped as error:
            if error.reason in SOURCE_SAFETY_STOPS:
                raise
            return dict(status='stopped', reason=error.reason, turns=self.execution_control.requests - started_requests)

    def repair_sources(self, packet, tools, *, feedback, request_allowance):
        """Repair observed development defects without rebuilding the task's plan."""
        if type(request_allowance) is not int or request_allowance < 0:
            raise ValueError('request allowance must be a nonnegative integer')
        return self.review_sources(packet, tools, request_allowance=request_allowance,
            feedback=('Repair only requirement violations supported by this development diagnostic '
                      'and current source. Trace affected dependencies and preserve other behavior. '
                      'The controller will revalidate the resulting source; a static check is not '
                      'proof that the reported behavior is fixed.\n' + feedback))

    def author_behavior_checks(self, packet, tools, ir, request_allowance):
        """Read-only authoring shares all request, token and protocol limits."""
        started = self.execution_control.requests
        contracts = behavior_checks.requirement_contracts(ir)
        unresolved = {ref: row['unresolved'] for ref, row in contracts.items() if row['unresolved']}
        if unresolved or not contracts:
            return dict(status='incomplete', reason='requirement_constraints_unresolved', unresolved=unresolved, turns=0)
        try:
            instructions = behavior_checks.INSTRUCTIONS
            messages = self._messages(packet, 'behavior_diagnostics', instructions)
            messages.append(dict(role='user', content='PUBLIC REQUIREMENT BINDINGS\n' + json.dumps(
                behavior_checks.requirement_bindings(ir),
                ensure_ascii=False)))
            result = self._run(packet, tools, stage='competition_audit', mode='audit',
                instructions=instructions, max_tokens=self.output_reserve_tokens,
                request_allowance=request_allowance, initial_messages=messages,
                final_validator=lambda value: behavior_checks.validate_plan(value, ir))
            result['turns'] = self.execution_control.requests - started
            return result
        except ExecutionStopped as error:
            if error.reason in SOURCE_SAFETY_STOPS:
                raise
            return dict(status='stopped', reason=error.reason, turns=self.execution_control.requests - started)

    def reserve_validation_steps(self, count, *, diagnostic=False, stage='competition_audit'):
        """Charge controller checks before execution; never refund on failure."""
        if type(count) is not int or count < 1:
            raise ValueError('invalid validation step reservation')
        control = self.execution_control
        control.check_diagnostics() if diagnostic else control.check()
        used = getattr(self.session, 'tool_calls_used', 0)
        if control.tools + count > control.limits.tools or used + count > self.max_tool_calls:
            if diagnostic:
                raise ExecutionStopped('execution_tool_limit')
            control.stop('execution_tool_limit')
        if self.campaign_budget is not None:
            self.campaign_budget.record_tools(stage, count, diagnostic=diagnostic)
        control.tools += count
        self.session.tool_calls_used = used + count
        self._append_log(dict(event='validation_step_reservation', stage=stage, count=count, total_tools=control.tools))

    def _messages(self, packet, stage, instructions):
        instructions += (
            '\nSOURCE application/ is a display namespace; edit paths are project-relative. '
            'Use this stage\'s current_workspace and subsequent tool edits as current source. '
            'Inspect files when the workspace observation is absent or insufficient. '
            'Choose edit scope from dependencies and output limits. '
            'Batch dependent edits that can be decided together, then validate once. Use update_coverage '
            'for incremental source-backed claims supported by the current step, not the whole future plan. '
            f'Each update_coverage call allows at most {COVERAGE_UPDATE_SCHEMA["properties"]["items"]["maxItems"]} items. '
            'Split larger updates into separate tool calls within the per-response tool limit; each arguments '
            'value must contain exactly one JSON object, never concatenated objects. Put validate_source last. '
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

    def _call_tool(self, tools, name, arguments, packet, stage, *, stage_context=None):
        control = self.execution_control
        files = control.source(tools.project_dir) if hasattr(tools, 'project_dir') else {}
        state = source_digest(files)
        reading = stage_context is not None and name in READ_TOOLS
        if stage_context is not None:
            stage_context.new_information = False
        if not reading:
            control.action(name, arguments, state)
        receipts = stage_context.source_read_receipts if stage_context is not None else None
        read_key = None
        if receipts is not None and name in {'read_file', 'read_files', 'list_files', 'search_text'}:
            read_key = json.dumps([name, arguments, state], sort_keys=True, ensure_ascii=False)
            prior = receipts.get(read_key)
            if prior is not None:
                reference = json.dumps(dict(kind='source_observation_reference', tool=name,
                    arguments=arguments, source_sha256=state, result_sha256=prior['sha256'],
                    unchanged=True, content_location='earlier successful tool result in this stage'), ensure_ascii=False)
                saved = prior['bytes'] - len(reference.encode('utf-8'))
                if saved > 0:
                    control.action(name, arguments, state)
                    self._append_log(dict(event='source_read_reused', tool=name, avoided_utf8_bytes=saved,
                                          executed=False, new_information=False))
                    return reference
        if name == 'read_file' and arguments.get('start_line', 1) == 1 and arguments.get('end_line') is None:
            path = tools._resolve(arguments['path']).relative_to(tools.project_dir).as_posix()
            digest = hashlib.sha256(files[path]).hexdigest() if path in files else None
            if digest and (stage_context.visible_source_hashes if stage_context is not None else {}).get(path) == digest:
                control.action(name, arguments, state)
                self._append_log(dict(event='source_read_reused', path=path, source_sha256=digest,
                                      executed=False, new_information=False))
                return json.dumps(dict(kind='source_observation_reference', path=path, sha256=digest,
                    unchanged=True, content_location='complete current_workspace content in this stage',
                    note='Use the already supplied complete content. A targeted line range remains available if needed.'))
        if name == 'read_files':
            paths = [tools._resolve(path).relative_to(tools.project_dir).as_posix() for path in arguments['paths']]
            visible = stage_context.visible_source_hashes if stage_context is not None else {}
            if paths and all(path in files and visible.get(path) == hashlib.sha256(files[path]).hexdigest() for path in paths):
                reference = json.dumps(dict(kind='source_observation_reference', paths=paths,
                    source_sha256=state, unchanged=True, content_location='complete current_workspace content in this stage'))
                saved = sum(len(files[path]) for path in paths) - len(reference.encode('utf-8'))
                if saved > 0:
                    control.action(name, arguments, state)
                    self._append_log(dict(event='source_read_reused', tool=name, avoided_utf8_bytes=saved,
                                          executed=False, new_information=False))
                    return reference
        checkpoint = SourceTransaction(tools, control) if name in {'write_files', 'replace_text', 'update_coverage'} else None
        try:
            result = (json.dumps(self._validate_step(tools, step=stage_context.step if stage_context is not None else None)) if name == 'validate_source'
                      else tools.call(name, arguments) if name == 'update_coverage'
                      else super()._call_tool(tools, name, arguments, packet, stage))
            if checkpoint:
                changed_files = control.source(tools.project_dir, changed_by_controller=True)
            control.check()
            if checkpoint:
                checkpoint.commit()
                if stage_context is not None and name == 'write_files':
                    for row in arguments['files']:
                        stage_context.observations.file(tools, changed_files, row['path'], row['content'])
        except BaseException:
            if checkpoint:
                # A tool may have failed after writing only part of its batch.
                # The controller owns that batch; restore it before any next action.
                control.source(tools.project_dir, changed_by_controller=True)
                checkpoint.rollback()
            if reading:
                control.action(name, arguments, state)
            raise
        if reading:
            stage_context.new_information = stage_context.observations.observe(
                name, arguments, result, tools=tools, files=files)
            # Only redundant observations consume the repeat allowance. A new
            # stage must receive real content again; shared resource use remains.
            if not stage_context.new_information:
                control.action(name, arguments, state)
            self._append_log(dict(event='source_information', tool=name,
                                  new_information=stage_context.new_information, executed=True))
        raw = result.encode('utf-8')
        digest = hashlib.sha256(raw).hexdigest()
        self._append_log(dict(event='react_observation', tool=name, source_sha256=state, result_sha256=digest))
        if read_key is not None:
            receipts[read_key] = dict(sha256=digest, bytes=len(raw))
        if stage_context is not None and name == 'validate_source' and json.loads(result).get('passed'):
            stage_context.completed = True
        return result

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


def runtime_install_succeeded(result):
    """Only a stage reached after installation is evidence that its tree is reusable."""
    return result.stage in {'build', 'start', 'probe', 'playwright', 'tests'}


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
        if relative and relative[0] not in {'.arc', '.git', 'node_modules', 'self-tests'} and not any(path.iterdir()):
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


def review_schedule(ir, completed_steps, evidence, request_budget, batch_limit):
    """Cover each item once, grouping by completed work and structural hints."""
    items = {row['id']: row for row in ir.get('work_items', [])
             if isinstance(row, dict) and isinstance(row.get('id'), str)}
    if not items or request_budget <= 0 or batch_limit <= 0:
        return []
    groups, assigned = [], set()
    for step in completed_steps:
        refs = list(dict.fromkeys(ref for ref in step.get('requirement_ids', [])
                                 if isinstance(ref, str) and ref in items and ref not in assigned))
        if refs:
            groups.append(refs)
            assigned.update(refs)
    groups.extend([ref] for ref in items if ref not in assigned)
    files = {ref: set() for ref in items}
    related = {}
    for ref, row in items.items():
        related[ref] = {dep for dep in row.get('depends_on', []) if isinstance(dep, str)}
        if isinstance(row.get('parent'), str):
            related[ref].add(row['parent'])
    for claim in evidence.get('shared_source_claims', []):
        for ref in claim['requirement_ids']:
            if ref in files:
                files[ref].add(claim['path'])
    maximum = min(len(groups), math.ceil(request_budget / batch_limit))
    buckets = []
    for refs in groups:
        ref_set = set(refs)
        owned = set().union(*(files[ref] for ref in refs))
        links = set().union(*(related[ref] for ref in refs))
        if len(buckets) < maximum:
            buckets.append(dict(refs=list(refs), ids=ref_set, files=owned, related=links))
            continue
        # Direct dependencies/parents take precedence over broad shared files.
        # Balance ties so a common entrypoint does not absorb every later item.
        def affinity(index):
            bucket = buckets[index]
            shared = owned & bucket['files']
            return (bool(ref_set & bucket['related'] or links & bucket['ids']),
                    bool(shared), -len(bucket['refs']), len(shared), -index)
        target = buckets[max(range(len(buckets)), key=affinity)]
        target['refs'].extend(refs)
        target['ids'].update(refs)
        target['files'].update(owned)
        target['related'].update(links)
    remaining = min(request_budget, len(buckets) * batch_limit)
    schedule = []
    for index, bucket in enumerate(buckets):
        share = min(batch_limit, math.ceil(remaining / (len(buckets) - index)))
        schedule.append(dict(requirement_ids=bucket['refs'], request_allowance=share))
        remaining -= share
    return schedule


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
            scope=('build_http_and_authored_diagnostics' if runtime.get('authored_checks') else 'build_and_http')
                if input_policy == 'requirements_only' else 'supplied_tests_and_runtime',
            authored_checks=runtime.get('authored_checks')),
        formal_behavior=dict(status='UNRUN', evaluated_cases=None, passed_cases=None))


def ir_prompt_summary(ir, starter_layout):
    """Layout, persistent groups, and parsed elements. The task title is omitted."""
    document = ir if isinstance(ir, dict) else {}
    runtime = document.get('runtime') if isinstance(document.get('runtime'), dict) else {}
    lines = [
        'TASK IR SUMMARY',
        'Use the full public requirements as the authority. Multiplicity, persistence, grouping and auth '
        'recognition are incomplete extraction hints; omitted or false flags do not negate stated behavior '
        'or prescribe a container role.',
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
            f"persistent={element.get('persistent') is True} page={element.get('page')} "
            f"within={json.dumps(element.get('within'), ensure_ascii=False)}"
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


def review_slice(ir, work_item):
    """Public IR evidence for one source-review target."""
    document = ir if isinstance(ir, dict) else {}
    work_id = work_item.get('id')
    elements = [
        {
            'role': element.get('role'),
            'name': element.get('name'),
            'group': element.get('group'),
            'persistent': element.get('persistent') is True,
            'multiple': element.get('multiple') is True,
            'page': element.get('page'),
            'within': element.get('within'),
            'evidence': element.get('evidence'),
            'occurrences': element.get('occurrences', []),
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
        'work_item': {key: work_item.get(key) for key in ('id', 'name', 'text', 'depends_on', 'page')},
        'parsed_ui_elements': elements,
        'persistent_groups': groups,
        'scenarios': scenarios,
        'auth': document.get('auth'),
    }
    return payload


def review_group_feedback(ir, items):
    # The full task packet already carries INTERACTION_CONVENTIONS once.
    return ('Review these related requirements together against the current source. '
            'Trace shared state and dependent behaviors end to end; batch related fixes and validate once. '
            'Inspect every supplied review target within the allowance; source review is not behavioral acceptance. '
            'Preserve repeated public UI names and roles; distinguish their page, container and required count. '
            'For public HTTP APIs, compare method, full path including query, request fields and types, '
            'response fields and types, and status with current source; a shared display name is not identity. '
            'HTTP availability probes do not verify JSON shapes; keep unsupported type checks unverified. '
            'Do not redo unchanged source reads or turn this review into a new implementation plan. '
            'IR SLICES\n' + json.dumps([review_slice(ir, item) for item in items], ensure_ascii=False))


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
    if not (set(signals) - {'probes'}) and getattr(result, 'failures', None):
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
            signals[name] = {key: block.get(key) for key in ('passed', 'failed', 'skipped', 'failures')}
    return {'signals': signals, 'diagnostics': getattr(result, 'diagnostics', None) or {},
            'api_type_status': 'not_checked'}


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


class StepBehaviorGate:
    """Freeze public diagnostics once per requirement; recheck all accepted journeys.

    Conservatively rechecking the cumulative suite avoids guessing which routes,
    shared components or server state a source edit can affect. A failed or empty
    check cannot commit a step. These observations are not formal acceptance.
    """
    def __init__(self, app, model, tools, settings, ir, make_packet, directory):
        self.app, self.model, self.tools = Path(app), model, tools
        self.settings, self.ir, self.make_packet = settings, ir, make_packet
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.deadline = min(monotonic() + settings.runtime_seconds, model.execution_control.deadline)
        self.frozen, self.accepted, self.collections = {}, set(), {}
        self.checks = 0
        self.retain_unverified = settings.delivery_policy == 'runnable_candidate'

    def can_retain(self, checked):
        # Only semantic uncertainty or an observed test failure is construction
        # progress. Harness, integrity and resource failures never qualify.
        return (checked.get('reason') == 'requirement_constraints_unresolved'
                or (checked.get('failure_kind') == 'behavior'
                    and checked.get('reason') == 'behavior_check_failed'
                    and checked.get('result', {}).get('stage') == 'tests'))

    @property
    def authored_checks(self):
        return self.collections[tuple(sorted(self.accepted))] if self.accepted else None

    def request_reserve(self, step):
        if not self.verification_available(step):
            return 0  # Capability is known before spending an author request.
        return int(bool(set(step.requirement_ids) - self.frozen.keys()))

    def verification_available(self, step):
        if not self.retain_unverified:
            return True
        contracts = behavior_checks.requirement_contracts(self.ir)
        return not any(ref not in contracts or contracts[ref]['unresolved'] for ref in step.requirement_ids)

    def __call__(self, step, allowance):
        control = self.model.execution_control
        control.check()
        refs = set(step.requirement_ids)
        # Validate before scoping: malformed global structure must not escape
        # through row.get(), while unrelated pending semantics cannot block this step.
        contracts = behavior_checks.requirement_contracts(self.ir)
        selected_refs = refs | self.accepted
        unresolved = {ref: (contracts[ref]['unresolved'] if ref in contracts else ['public_requirement_missing'])
                      for ref in selected_refs
                      if ref not in contracts or contracts[ref]['unresolved']}
        if unresolved:
            return dict(passed=False, failure_kind='verification', reason='requirement_constraints_unresolved',
                        unresolved=unresolved)
        missing = refs - self.frozen.keys()
        if missing:
            if allowance <= 0:
                return dict(passed=False, failure_kind='verification', reason='behavior_author_allowance')
            scoped = dict(self.ir,
                work_items=[row for row in self.ir.get('work_items', []) if row['id'] in missing],
                scenarios=[row for row in self.ir.get('scenarios', []) if row.get('work_item') in missing])
            authored = self.model.author_behavior_checks(self.make_packet(sorted(missing)), self.tools, scoped,
                min(allowance, behavior_checks.AUTHOR_REQUESTS))
            control.check()
            if authored.get('status') != 'complete':
                reason = authored.get('reason')
                if reason in {'self_test_reserve', 'self_test_time_reserve'}:
                    return dict(passed=False, failure_kind='verification', reason=reason,
                                detail='behavior_author_incomplete', author_status=reason)
                return dict(passed=False, failure_kind='verification', reason='behavior_author_incomplete',
                            author_status=authored.get('reason'))
            plan = {'cases': authored.get('cases')}
            try:
                behavior_checks.validate_plan(plan, scoped)
                constraint_evidence = behavior_checks.plan_coverage(plan, scoped)
                bound = set(constraint_evidence['covered'])
            except ValueError as error:
                return dict(passed=False, failure_kind='verification', reason='invalid_behavior_plan', detail=str(error))
            # Store canonical code by requirement; only executable suites need files.
            for ref in sorted(bound):
                subset = {'cases': [case for case in plan['cases'] if case['requirement_id'] == ref]}
                self.frozen[ref] = dict(source=behavior_checks.compile_plan(subset, dict(self.ir,
                    work_items=[row for row in self.ir.get('work_items', []) if row['id'] == ref],
                    scenarios=[row for row in self.ir.get('scenarios', []) if row.get('work_item') == ref])).encode('utf-8'),
                                        cases=subset['cases'])
        # Retain valid partial author output as a frozen attempt, without
        # treating its missing requirements as executed or accepted.
        selected = tuple(sorted(self.accepted | (refs & self.frozen.keys())))
        if selected not in self.collections:
            directory = self.directory / f'suite-{len(self.collections) + 1}'
            cases, files = [], {}
            for index, ref in enumerate(selected):
                item = self.frozen[ref]
                files[f'journey-{index + 1}.spec.cjs'] = item['source']
                cases.extend(item['cases'])
            files['plan.json'] = json.dumps({'cases': cases}, ensure_ascii=False, indent=2).encode('utf-8')
            hashes = behavior_checks.write_frozen_suite(directory, files)
            self.collections[selected] = dict(directory=directory, hashes=hashes,
                plan={'cases': cases}, cases=len(cases))
        suite = self.collections[selected]
        if missing := refs - self.frozen.keys():
            return dict(passed=False, failure_kind='verification', reason='behavior_requirements_missing',
                        missing=sorted(missing))
        if not behavior_checks.verify_frozen(suite['directory'], suite['hashes']):
            return dict(passed=False, failure_kind='verification', reason='authored_suite_changed')
        deadline = min(self.deadline, self.model.request_deadline)
        if deadline - monotonic() < RUNTIME_MIN_SECONDS:
            return dict(passed=False, failure_kind='verification', reason='behavior_wall_budget')
        self.model.reserve_validation_steps(sum(len(case['steps']) for case in suite['plan']['cases']))
        source = source_digest(control.source(self.app))
        self.checks += 1
        work = Path(tempfile.mkdtemp(prefix='arc-step-check-'))
        try:
            # Build and start commands can mutate dependencies without changing manifests.
            # Each step therefore gets a fresh install as well as fresh source and data.
            sync_project_copy(self.app, work / 'project')
            result = run_runtime_check(work / 'project', None, suites=[('authored', suite['directory'])],
                work_dir=work / 'check', playwright_cache=Path(tempfile.gettempdir()) / PLAYWRIGHT_CACHE_NAME,
                deadline=deadline, timeouts=dict(test=self.settings.runtime_test_timeout_ms / 1000))
            control.source(self.app)
            unchanged = behavior_checks.verify_frozen(suite['directory'], suite['hashes'])
            passed = (unchanged and result.status == 'passed' and result.stage == 'tests'
                and result.total == suite['cases'] and result.passed == suite['cases']
                and result.failed == result.skipped == result.precondition == 0
                and not api_failures(result.api_probes))
            checked = dict(passed=passed, reason=None if passed else 'behavior_check_failed',
                scope='synthetic_diagnostics_only', source_sha256=source, requirement_ids=list(selected),
                constraint_evidence=behavior_checks.plan_coverage(suite['plan'], dict(self.ir, work_items=[r for r in self.ir.get('work_items', []) if r['id'] in selected])),
                current_requirement_ids=sorted(refs), regression_requirement_ids=sorted(self.accepted),
                suite_hashes=suite['hashes'], result=result.to_feedback())
            if not passed:
                observed_failure = (unchanged and (result.status in {'build_failed', 'start_failed'}
                    or (result.stage == 'tests' and result.status == 'failed' and result.failed > 0)
                    or api_failures(result.api_probes)))
                checked['failure_kind'] = 'behavior' if observed_failure else 'verification'
        except ExecutionStopped:
            raise
        except Exception as error:
            checked = dict(passed=False, failure_kind='verification', reason='behavior_harness_error',
                           detail=f'{type(error).__name__}: {str(error)[:500]}')
        finally:
            safe_rmtree(work)
        (self.directory / f'check-{self.checks}.json').write_text(json.dumps(checked, ensure_ascii=False, indent=2), encoding='utf-8')
        return checked

    def quarantine(self):
        """Retain unverified source outside the deliverable; never count it as accepted."""
        files = self.model.execution_control.source(self.app)
        directory = Path(tempfile.mkdtemp(prefix='unverified-source-', dir=self.directory))
        apply_snapshot(directory, files, before={})
        return str(directory)

    def accept(self, step, checked):
        self.accepted.update(step.requirement_ids)


def run_runtime_loop(app, tests_dir, settings, *, model, tools, make_packet, record, validate, task_id, report_path, ir,
                     model_calls_allowed=lambda: True, authored_checks=None, review_after_pass=None,
                     runtime_deadline=None, report=None):
    """Check, repair and re-check the project on a scratch copy.

    An optional controller-frozen authored suite contains public-requirement
    diagnostics only. Otherwise, without supplied tests this is HTTP probes.
    Authored failures are fallible observations, never official acceptance.
    A full tie keeps the later round. A worse rank restores the best snapshot. A missing browser does not
    count as skipped: probe failures are repaired, and otherwise one coverage review runs before stopping.
    """
    initial = dict(task_id=task_id, enabled=True, skipped=None, rounds=[], final_status=None, last_round_status=None,
                  best_round=None, stopped=None, wall_budget_seconds=settings.runtime_seconds, max_rounds=settings.runtime_rounds,
                  repair_minimum_seconds=repair_minimum_seconds(settings))
    if report is None:
        report = initial
    else:
        report.update(initial)
    deadline = monotonic() + settings.runtime_seconds
    if runtime_deadline is not None:
        deadline = min(deadline, runtime_deadline)
    control = getattr(model, 'execution_control', None)
    if control is not None:
        deadline = min(deadline, control.deadline)
    requested_model_calls = model_calls_allowed
    model_calls_allowed = lambda: not (control is not None and control.stopped) and requested_model_calls()
    work_root = Path(tempfile.mkdtemp(prefix='arc-runtime-'))
    copy_dir, check_dir = work_root/'project', work_root/'check'
    cache = Path(tempfile.gettempdir())/PLAYWRIGHT_CACHE_NAME
    best = None
    checked_source = None
    review_attempted = False
    installed_manifests = None
    if authored_checks:
        report['authored_checks'] = dict(scope='synthetic_diagnostics_only', hashes=authored_checks['hashes'],
            cases=authored_checks['cases'], frozen=True)

    def write_report(current_source=None):
        if report.get('stopped') == 'unavailable_after_review':
            report['final_status'] = 'unavailable'
        elif best is not None:
            report['final_status'] = best['status']
            report['best_round'] = best['round']
        if report.get('stopped') != 'unavailable_after_review' and best is not None:
            report['best_round'] = best['round']
        if report['rounds']:
            report['last_round_status'] = report['rounds'][-1]['status']
        report['checked_source_sha256'] = checked_source
        report['source_sha256'] = current_source if current_source is not None else source_digest(snapshot_project(app))
        if best is not None and checked_source != report['source_sha256']:
            report['final_status'] = 'unverified'
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')

    def restore_best(entry, action):
        nonlocal checked_source
        if control is not None:
            control.source(app)  # Detect external edits before any restoration write.
        try:
            if best is not None and restore_snapshot(app, best['files']):
                # Restoration requires a fresh check; historical success is not
                # evidence that the restored candidate ran in this world.
                checked_source = None
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
            if control is not None and control.stopped:
                if control.stopped in SOURCE_SAFETY_STOPS:
                    raise ExecutionStopped(control.stopped)
                try:
                    control.check_diagnostics()
                except ExecutionStopped as stop:
                    report['stopped'] = stop.reason
                    if best is not None:
                        restore_best(report['rounds'][-1], 'restored_after_execution_stopped')
                    break
                report['model_stop_reason'] = control.stopped
            remaining = deadline - monotonic()
            if remaining < RUNTIME_MIN_SECONDS:
                report['stopped'] = 'wall_budget'
                if best is not None:
                    restore_best(report['rounds'][-1], 'restored_before_unrun_check')
                break
            if authored_checks and not behavior_checks.verify_frozen(authored_checks['directory'], authored_checks['hashes']):
                report['stopped'] = 'authored_suite_changed'
                report['final_status'] = 'unverified'
                best = None
                break
            if authored_checks and hasattr(model, 'reserve_validation_steps'):
                try:
                    # A token ledger may stop requests before RunControl does.
                    # These operations are read-only regardless of model state.
                    model.reserve_validation_steps(sum(len(case['steps']) for case in authored_checks['plan']['cases']),
                                                 diagnostic=True)
                except (ExecutionStopped, CampaignBudgetStopped) as stop:
                    report['stopped'] = stop.reason
                    if best is not None:
                        restore_best(report['rounds'][-1], 'restored_before_unrun_check')
                    break
            try:
                checking_files = control.source(app) if control is not None else snapshot_project(app)
                checking_source = source_digest(checking_files)
                if (control is not None and not control.stopped
                        and getattr(getattr(control, 'policy', None), 'enabled', False)):
                    control.set_task_state('BUILD', reason='runtime build and launch checks',
                                           source_sha256=checking_source)
                manifests = manifest_digests(checking_files)
                del checking_files
                sync_project_copy(app, copy_dir, installed_manifests=installed_manifests)
                suites = [('input', Path(tests_dir))] if tests_dir is not None else []
                if authored_checks:
                    suites.append(('authored', Path(authored_checks['directory'])))
                result = run_runtime_check(copy_dir, None, suites=suites, work_dir=check_dir, playwright_cache=cache,
                                           deadline=deadline, timeouts=dict(test=settings.runtime_test_timeout_ms / 1000))
                if control is not None:
                    control.source(app)
            except ExecutionStopped:
                raise  # Safety stops must never become a harness error followed by rollback.
            except Exception as error:  # the optional check must never discard a static-valid delivery
                entry = dict(round=number, status='harness_error', error=f'{type(error).__name__}: {str(error)[:300]}',
                             signals={}, diagnostics={})
                report['rounds'].append(entry)
                logging.warning('PLATFORM_RUNTIME_CHECK %s', json.dumps(dict(task_id=task_id, **entry)))
                restore_best(entry, 'restored_best' if best is not None and number > 1 else 'skipped')
                report['stopped'] = 'harness_error'
                break
            if authored_checks and not behavior_checks.verify_frozen(authored_checks['directory'], authored_checks['hashes']):
                report['stopped'] = 'authored_suite_changed'
                report['final_status'] = 'unverified'
                best = None
                break
            install_ok = runtime_install_succeeded(result)
            if (result.stage == 'tests' and control is not None and not control.stopped
                    and getattr(getattr(control, 'policy', None), 'enabled', False)):
                control.set_task_state('TEST', reason='authored runtime tests executed',
                                       source_sha256=checking_source)
                if authored_checks and result.total > 0 and result.status in {'passed', 'failed'}:
                    control.record_verification(stage='TEST', passed=result.status == 'passed',
                        source_sha256=checking_source, evidence_id=f'final-runtime-{number}',
                        requirement_ids=[case['requirement_id'] for case in authored_checks['plan']['cases']],
                        failure_kind='behavior' if result.status == 'failed' else None)
            checked_source = checking_source
            installed_manifests = manifests if install_ok else None
            entry = dict(round=number, status=result.status, stage=result.stage, passed=result.passed, failed=result.failed,
                         skipped=result.skipped, precondition=result.precondition, total=result.total,
                          source_sha256=checking_source,
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
                    report['stopped'] = (control.stopped if control is not None and control.stopped
                                         else 'model_budget_exhausted')
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
            if authored_checks:
                feedback += ('\nAUTHOR DIAGNOSTIC BOUNDARY: These frozen journeys were generated from public '
                    'requirements and may contain wrong selectors or assumptions. Reconcile each failure with '
                    'its quoted requirement and current source before editing. Fix only independently evidenced '
                    'requirement violations; do not alter the suite, invent behavior, add test-specific branches '
                    'or treat a passing journey as full acceptance. If the diagnostic is wrong, leave the '
                    'application faithful to requirements and report the limitation.\n' +
                    json.dumps(authored_checks['plan'], ensure_ascii=False))
            if best is None or checking_source == best['source_sha256'] or rank >= best['rank']:
                # A fresh observation of the same candidate supersedes its
                # historical rank, including a previously passing result.
                best = dict(rank=rank, round=number, status=result.status, files=snapshot_project(app),
                            feedback=feedback, source_sha256=checking_source)
            else:
                restore_best(entry, f"restored_round_{best['round']}")
                if checked_source is None:
                    # Re-run the restored source before any further repair or
                    # acceptance. The existing round/time cap still applies.
                    if number >= settings.runtime_rounds:
                        report['stopped'] = 'restored_candidate_unverified'
                    write_report()
                    continue
            write_report()
            if control is not None and control.stopped:
                # Never turn a read-only observation into a model retry or repair.
                report['stopped'] = control.stopped
                break
            if result.status == 'passed':
                if (review_after_pass is not None and not review_attempted
                        and number < settings.runtime_rounds and model_calls_allowed()
                        and deadline - monotonic() >= repair_minimum_seconds(settings)):
                    review_attempted = True
                    entry['source_review'] = 'started_after_runtime_pass'
                    write_report()
                    original_deadline = control.deadline if control is not None else None
                    try:
                        if control is not None:
                            control.deadline = min(control.deadline, deadline)
                        review_after_pass()
                    finally:
                        if control is not None:
                            control.deadline = original_deadline
                    current_source = source_digest(control.source(app) if control is not None else snapshot_project(app))
                    changed = current_source != checked_source
                    entry['source_review'] = 'changed_recheck_required' if changed else 'unchanged'
                    write_report(current_source)
                    if changed:
                        continue
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
                report['stopped'] = (control.stopped if control is not None and control.stopped
                                     else 'model_budget_exhausted')
                break
            try:
                tools.unseal()
                repair = model.repair_sources(make_packet(), tools, request_allowance=settings.repair_request_allowance,
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
    parser.add_argument('--pause-before-request', type=int,
                        help='Pause the live owner before this logical request; no process restart is supported')
    parser.add_argument('--pause-on-api-error', action='store_true',
                        help='Supervised live pause after an eligible error; consumes the original API wait budget')
    handoff = parser.add_mutually_exclusive_group()
    handoff.add_argument('--handoff-final-verification', action='store_true',
                        help='Exit 75 after cleanly handing off complete frozen final verification')
    handoff.add_argument('--resume-final-verification', type=Path,
                        help='Continue this clean handoff once, with no model calls or source repairs')
    parser.add_argument('--checkpoint-sha256', help='Trusted digest returned by the original handoff')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    app = Path(args.output_dir).resolve()
    if root == app or root in app.parents or app in root.parents:
        raise ValueError('submission and editable project must be separate')
    return run_owned(args)


def run_owned(args):
    from .verification_handoff import publish_ready, resume
    app = Path(args.output_dir).resolve()
    try:
        with ProjectLease(app) as lease:
            args.project_lease = lease
            return resume(args) if getattr(args, 'resume_final_verification', None) else generate(args)
    except VerificationHandoff as handoff:
        # A lease-release failure replaces the exception and cannot publish readiness.
        publish_ready(handoff)
        print('FINAL_VERIFICATION_HANDOFF ' + json.dumps(dict(checkpoint=str(handoff.path),
            sha256=handoff.sha256, exit_code=75)), flush=True)
        return 75


@dataclass
class GenerationEvidence:
    """Evidence retained across early exits until the controller exports self-tests."""
    artifacts: object = None
    required_ids: tuple = ()
    requirements_sha256: str | None = None
    static: dict = field(default_factory=dict)
    runtime_report: dict = field(default_factory=dict)
    authored: dict | None = None
    suites: list = field(default_factory=list)
    step_gate: object = None
    missing: list = field(default_factory=list)
    runtime: object = None
    source_ready: bool = False
    behavior_enabled: bool = False
    runtime_enabled: bool = False
    candidate: dict | None = None
    promotion_before: dict | None = None
    promotion_after: dict | None = None
    promotion_control: object = None
    control: object = None
    delivery_policy: str = 'verified'
    delivery: dict | None = None
    delivery_permit: object = None


def _mark_generation_failed(evidence, app, reason):
    control = evidence.control
    if isinstance(control, RunControl) and control.task_state != 'STOPPED':
        control.set_task_state('STOPPED', reason='generation_incomplete')
    try:
        if evidence.runtime is None:
            from arcbench_agent_runtime import AgentRuntime
            evidence.runtime = AgentRuntime.from_env(project_dir=str(app))
        evidence.runtime.events.mark_run_failed(reason)
    except Exception as caught:
        logging.error('PLATFORM_FAILURE_EVENT_UNAVAILABLE %s', type(caught).__name__)


def generate(args):
    app = Path(args.output_dir).resolve()
    evidence = GenerationEvidence()
    error = None
    try:
        app.mkdir(parents=True, exist_ok=True)
        _generate(args, evidence)
    except VerificationHandoff:
        raise  # No failure export or completion event may mutate the frozen handoff.
    except BaseException as caught:
        error = f'{type(caught).__name__}: {str(caught)[:500]}'
        logging.error('PLATFORM_GENERATION_INCOMPLETE %s', json.dumps(dict(error=error)))
        _rollback_promotion(evidence, app)
    return finish_generation(evidence, app, error)


def finish_generation(evidence, app, error=None):
    """One acceptance/export gate for generation and frozen final verification."""
    try:
        # Early input/configuration failures still get a visible report. On the
        # normal path initialization follows scaffold detection, before model work.
        if evidence.artifacts is None:
            app.mkdir(parents=True, exist_ok=True)
            evidence.artifacts = initialize_self_test(app)
        source = source_digest(snapshot_project(app)) if evidence.source_ready else ''
        suites = list(evidence.suites)
        if evidence.step_gate is not None:
            suites.extend(evidence.step_gate.collections.values())
        report = finalize_self_test(evidence.artifacts, required_ids=evidence.required_ids,
            static=evidence.static, runtime_report=evidence.runtime_report, authored=evidence.authored,
            source_sha256=source, missing_coverage=evidence.missing,
            behavior_enabled=evidence.behavior_enabled, runtime_enabled=evidence.runtime_enabled,
            authored_suites=suites, requirements_sha256=evidence.requirements_sha256, error=error,
            candidate=evidence.candidate)
    except Exception as caught:
        _rollback_promotion(evidence, app)
        logging.error('PLATFORM_SELF_TEST_EXPORT_FAILED %s', type(caught).__name__)
        _mark_generation_failed(evidence, app, 'Self-test evidence could not be exported safely')
        return 1
    if evidence.delivery_policy == 'runnable_candidate' and evidence.delivery is not None:
        from .candidate_delivery import finish_candidate_delivery
        return finish_candidate_delivery(evidence, app, report, error)
    if not report['accepted']:
        _rollback_promotion(evidence, app)
        if evidence.candidate and evidence.candidate.get('status') != 'promoted':
            record_delivery_failure(evidence.artifacts, source_digest(snapshot_project(app)), evidence.candidate)
        logging.warning('PLATFORM_SELF_TEST_INCOMPLETE %s', json.dumps(report['reasons']))
        _mark_generation_failed(evidence, app, 'Self-tests incomplete: ' + ', '.join(report['reasons']))
        return 1
    control = evidence.control
    if control is not None and getattr(getattr(control, 'policy', None), 'enabled', False):
        try:
            control.record_verification(stage='TEST', passed=True, source_sha256=source,
                evidence_id='final_authored_tests', requirement_ids=evidence.required_ids)
            control.finish_task(source_sha256=source, required_checks=['final_authored_tests'])
        except (ExecutionStopped, ValueError) as stopped:
            _rollback_promotion(evidence, app)
            record_delivery_failure(evidence.artifacts, source_digest(snapshot_project(app)), evidence.candidate,
                                    reason='controller_completion_rejected')
            _mark_generation_failed(evidence, app, 'Controller rejected completion: ' +
                                    str(getattr(stopped, 'reason', type(stopped).__name__)))
            return 1
    # Successful generation is emitted only after exact tests and the passing
    # source-bound report are safely visible. Formal evaluation remains separate.
    for requirement_id in evidence.required_ids:
        evidence.runtime.events.mark_implementation_done(requirement_id, 'Public-requirement self-tests passed; formal tests pending')
    evidence.runtime.events.mark_run_completed('Source and complete authored self-tests exported; local self-tests passed; formal evaluation pending')
    return 0


def _rollback_promotion(evidence, app):
    """Restore the prior delivery only if its promoted bytes remain untouched."""
    if evidence.promotion_before is None:
        return
    try:
        current = snapshot_project(app)
        if current == evidence.promotion_before:
            pass  # The snapshot writer already restored a partially applied promotion.
        elif current != evidence.promotion_after:
            raise RuntimeError('delivery changed after promotion')
        else:
            apply_snapshot(app, evidence.promotion_before, before=current)
        if snapshot_project(app) != evidence.promotion_before:
            raise RuntimeError('delivery restoration mismatch')
        if evidence.promotion_control is not None:
            evidence.promotion_control.source(app, changed_by_controller=True)
        if evidence.candidate is not None:
            evidence.candidate['status'] = 'rolled_back'
    except Exception as error:
        if evidence.candidate is not None:
            evidence.candidate['status'] = 'rollback_failed'
        if evidence.promotion_control is not None:
            try:
                evidence.promotion_control.stop('rollback_failed')
            except ExecutionStopped:
                pass  # Persist the hard stop while allowing the failure report to export.
        logging.error('PLATFORM_PROMOTION_ROLLBACK_FAILED %s', type(error).__name__)
    finally:
        evidence.promotion_before = None


def _generate(args, evidence):
    from arcbench_agent_runtime import AgentRuntime
    app = Path(args.output_dir).resolve()
    settings = PlatformSettings.from_env()
    evidence.delivery_policy = settings.delivery_policy
    if settings.delivery_policy == 'runnable_candidate' and (
            not settings.runtime_check or not settings.behavior_check
            or settings.execution_mode != 'plan_execute' or settings.control_policy != 'adaptive'
            or getattr(args, 'handoff_final_verification', False)):
        raise ValueError('Candidate delivery requires adaptive plan execution and both verification layers')
    evidence.behavior_enabled, evidence.runtime_enabled = settings.behavior_check, settings.runtime_check
    if settings.input_policy != 'requirements_only':
        raise ValueError('Generation requires public requirements and self-authored tests; provided_tests is not permitted')
    inputs = resolve_task_inputs(Path(args.requirements).resolve(), include_tests=False)
    if any(is_test_path(path.relative_to(app).as_posix()) for path in app.rglob('*')):
        raise ValueError('Requirements-only generation needs a project without evaluation tests; use a separate source-only workspace')
    if any(path.name.casefold() == 'self-tests' for path in app.iterdir()):
        raise ValueError('Use a fresh output directory; existing self-tests must be preserved')
    evidence.source_ready = True
    task, req = inputs.task_id, inputs.requirement_dir
    starter = detect_runtime(app)
    inject_scaffold = starter.get('layout') == 'empty'
    if inject_scaffold:
        prepare_scaffold(app)
    evidence.artifacts = initialize_self_test(app)
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
    bundle = load_task_bundle(**bundle_kwargs(inputs), include_tests=False)
    evidence.required_ids = tuple(bundle.index.atomic_ids)
    evidence.requirements_sha256 = bundle.source_hash
    reports = app/'.arc'/'platform-budget'/task
    reports.mkdir(parents=True, exist_ok=True)
    contracts = behavior_checks.requirement_contracts(ir)
    (reports/'verification-capabilities.json').write_text(json.dumps(dict(
        delivery_policy=settings.delivery_policy,
        unresolved={ref: row['unresolved'] for ref, row in contracts.items() if row['unresolved']},
        scope='public_input_capability_not_application_score'), ensure_ascii=False, indent=2), encoding='utf-8')
    if not os.environ.get('MODEL', '').strip():
        raise ValueError('MODEL must be supplied explicitly by the runner')
    endpoint = os.environ.get('OPENAI_BASE_URL', '').strip()
    if endpoint and '://' not in endpoint:
        endpoint = 'https://' + endpoint.lstrip('/')
        os.environ['OPENAI_BASE_URL'] = endpoint
    print(f"PLATFORM_AGENT task={task} model={os.environ['MODEL'].strip()} endpoint_host={urlsplit(endpoint).hostname or '-'}", flush=True)
    budget = PlatformTokenBudget(reports/'tokens.json', model=os.environ['MODEL'].strip(),
        endpoint=endpoint, limit=settings.token_limit or inputs.token_limit, settings=settings)
    budget.protect_self_test_requests(1)
    session = ModelClient(max_tool_calls=settings.max_tool_calls)
    from .platform_budget_policy import BudgetPlanner
    control_policy = ControlPolicy(enabled=settings.control_policy == 'adaptive',
                                   recovery_attempts=settings.recovery_attempt_limit)
    planner = BudgetPlanner() if settings.budget_policy == 'adaptive' else None
    model = PlatformGenerationModel(session, context_window_tokens=settings.context_window_tokens,
        output_reserve_tokens=settings.max_output_tokens, request_allowance=settings.request_allowance,
        audit_log=lambda row: append_jsonl(reports/'context.jsonl',row), campaign_budget=budget,
        execution_mode=settings.execution_mode, execution_limits=settings.execution_limits(),
        reasoning_effort=settings.reasoning_effort, workspace_context_bytes=settings.workspace_context_bytes,
        control_policy=control_policy, budget_planner=planner)
    evidence.control = getattr(model, 'execution_control', None)
    # All cutoffs are inside the original hard deadline. Holding one full author
    # call improves its chance; it cannot guarantee coverage for arbitrary tasks.
    shared_diagnostic_deadline = model.execution_control.deadline
    if (settings.execution_mode == 'plan_execute' and settings.runtime_check and settings.behavior_check
            and isinstance(model.execution_control, RunControl)):
        shared_diagnostic_deadline = min(shared_diagnostic_deadline, monotonic() + settings.runtime_seconds)
    available_seconds = min(settings.execution_seconds, max(0, shared_diagnostic_deadline - monotonic()))
    local_test_seconds = min(settings.runtime_seconds, max(RUNTIME_MIN_SECONDS, available_seconds * .25))
    author_seconds = available_seconds * .1
    author_deadline = shared_diagnostic_deadline - local_test_seconds
    model.request_deadline = author_deadline - author_seconds
    model.prospective_request_admission = settings.delivery_policy == 'runnable_candidate'
    (reports/'self-test-budget.json').write_text(json.dumps(dict(
        protected_author_requests=1, protected_author_tokens=budget.state['self_test_reserve_tokens'],
        token_reserve_unfunded=budget.state['token_reserve_unfunded'],
        local_test_seconds=local_test_seconds, author_seconds=author_seconds,
        implementation_deadline=model.request_deadline, author_deadline=author_deadline,
        shared_diagnostic_deadline=shared_diagnostic_deadline,
        hard_deadline=model.execution_control.deadline), indent=2), encoding='utf-8')
    logging.info('PLATFORM_MODEL_POLICY %s', json.dumps(dict(
        reasoning_effort=settings.reasoning_effort, max_output_tokens=settings.max_output_tokens,
        execution_mode=settings.execution_mode, token_limit=budget.state['limit'])))
    tools = StaticProjectTools(app, include_tests=False)
    tools.public_tests = inputs.tests_dir
    runtime = AgentRuntime.from_env(project_dir=str(app))
    evidence.runtime = runtime
    runtime.traceability.init_db()
    runtime.traceability.store_requirement_tree(json.loads(json.dumps(bundle.to_dict()['requirements'])))
    runtime.events.mark_run_started('Source generation with static validation and runtime check on a scratch copy; formal evaluation follows')
    if getattr(args, 'pause_before_request', None) is not None or getattr(args, 'pause_on_api_error', False):
        from .execution_checkpoint import ApiErrorPause, LivePause, digest, encoded
        from .verification_handoff import code_identity
        def pause_identity():
            return dict(requirements=load_task_bundle(**bundle_kwargs(inputs), include_tests=False).source_hash,
                agent_code=digest(encoded(code_identity())), configuration=digest(encoded(dict(
                    settings=budget.settings, model=session.model, endpoint_sha256=digest(session.base_url.encode())))),
                phase_deadline=model.request_deadline)
        if getattr(args, 'pause_before_request', None) is not None:
            model.live_pause = LivePause(reports/'live-pause', app, getattr(args, 'project_lease', None),
                pause_identity, runtime.events, args.pause_before_request)
        if getattr(args, 'pause_on_api_error', False):
            model.error_pause = ApiErrorPause(reports/'api-error-pause', app, getattr(args, 'project_lease', None),
                pause_identity, runtime.events)
    contract = global_contract_schema(bundle)
    execution_contract = platform_contract(settings.input_policy)
    contract['execution_contract'] = execution_contract
    contract['input_policy'] = settings.input_policy
    contract['evaluation_boundary'] = ('Implement the complete supplied requirements, including behavior not sampled by tests. '
        'Evaluation tests and their results are withheld until the source is frozen; do not seek or infer evaluator internals. '
        'Build and HTTP probes are development diagnostics, not behavioral acceptance.')
    if inject_scaffold:
        contract['scaffold_guide'] = SCAFFOLD_GUIDE
        contract['seed_rules'] = seed_instructions()
    else:
        contract['scaffold_guide'] = ('The starter layout is already present. Retain the supplied project layout, its build and start '
                                      'scripts, and its own startup data mechanism. Only declared prerequisite data is seeded.')
    seed_observations = collect_seed_evidence(inputs.requirements_path, inputs.tests_dir)
    seed_evidence = json.dumps(seed_observations, ensure_ascii=False)
    seed_summary = seed_hint_summary(seed_observations)
    work = build_work_plan(bundle)
    all_ids = tuple(bundle.index.atomic_ids)
    actions = []
    def packet(selection, *, scoped=False):
        selection = tuple(selection)
        whole = set(selection) == set(all_ids)
        files = {} if scoped and settings.workspace_context_bytes else snapshot_project(app)
        paths = []
        for path, data in files.items():
            try: data.decode('utf-8')
            except UnicodeError: continue
            paths.append(path)
        result = assemble_context(bundle,selection,app,settings.context_chars,global_contract=contract,source_paths=paths)
        for path in req.glob('*.md'):
            if is_test_path(path.name):
                continue
            result.repository.add_evidence('evidence/platform-'+path.name,path.read_text(encoding='utf-8'))
        result.repository.add_evidence('evidence/platform-contract.json',json.dumps(execution_contract))
        result.repository.add_evidence('evidence/seed-evidence.json',seed_evidence)
        if scoped:
            return replace(result, text=result.text + INTERACTION_CONVENTIONS + '\n' + seed_summary,
                           manifest=dict(result.manifest, whole_task=False))
        # Deliver unchanged sources once in a stable prefix. Small tasks fit
        # completely; larger ones retain explicit offsets for read_context.
        sources = [(record.relative_path, record.raw_bytes) for record in bundle.files
                   if record.relative_path.endswith(('.yaml','.md','.ts','.js','.json'))
                   and record.relative_path != 'requirements/requirements.md']
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
        visible_ranges = []
        for name,data in sources:
            try: content=data.decode('utf-8')
            except UnicodeError: continue
            take = min(len(content),max(0,remaining//3)) if len(data)>remaining else len(content)
            visible=content[:take]
            visible_ranges.append(dict(path=name, sha256=hashlib.sha256(data).hexdigest(), start=0, end=take))
            complete = complete and take == len(content)
            text += (f'\nSOURCE {name} sha256={hashlib.sha256(data).hexdigest()} '
                     f'characters={len(content)} provided=[0,{take}) complete={take==len(content)}\n'+visible+'\n')
            remaining -= len(visible.encode('utf-8'))
        logging.info('SOURCE_PACKET %s',json.dumps(dict(task_id=task,selection=list(selection),whole_task=whole,
            all_text_visible=complete,source_files=len(sources),utf8_bytes=len(text.encode('utf-8')),
            dependency_locks='preserved_without_repeated_payload',requirement_mirror='excluded_duplicate')))
        return replace(result,text=(WHOLE_TASK_PREFIX if complete else PARTIAL_TASK_PREFIX)+text,
                       manifest=dict(result.manifest, whole_task=whole, visible_source_ranges=visible_ranges))
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
            if control.clock() >= model.request_deadline:
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
            if settings.delivery_policy == 'runnable_candidate':
                return dict(result, status='stopped', reason='coverage_incomplete', missing_coverage=missing)
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
    author_allowance = (min(behavior_checks.AUTHOR_REQUESTS, review_request_budget(len(ir.get('work_items') or [])))
        if settings.behavior_check and settings.runtime_check
        and hasattr(model, 'author_behavior_checks') else 0)

    def review_batches(completed_steps):
        current = load_task_ir(app, ir)
        items = {item['id']: item for item in current.get('work_items') or []
                 if isinstance(item, dict) and isinstance(item.get('id'), str)}
        schedule = review_schedule(current, completed_steps, planning_evidence(app, list(items)),
                                   max(0, review_request_budget(len(items)) - author_allowance), settings.review_request_allowance)
        audit = dict(claim_scope='source_review_only', source_claims_are_verified=False,
                     groups=[dict(group, status='pending') for group in schedule],
                     unscheduled_requirement_ids=sorted(set(items) - {
                         ref for group in schedule for ref in group['requirement_ids']}))
        def save_review():
            (reports/'review-schedule.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
        save_review()
        try:
            for group in audit['groups']:
                if not budget_open():
                    break
                refs = group['requirement_ids']
                group['status'] = 'running'
                save_review()
                tools.unseal()
                reviewed = model.review_sources(packet(all_ids), tools, request_allowance=group['request_allowance'],
                    feedback=review_group_feedback(current, [items[ref] for ref in refs]))
                group.update(status=reviewed.get('status', 'unknown'), reason=reviewed.get('reason'),
                             requests_used=reviewed.get('turns'))
                record('locator_review', ','.join(refs), dict(reviewed, review_requirement_ids=refs,
                       claim_scope='source_review_only'), allow_delivery=True, optional=True)
                save_review()
                if reviewed.get('reason') in ALLOCATION_STOP_REASONS:
                    break
        finally:
            for group in audit['groups']:
                if group['status'] in {'pending', 'running'}:
                    group.update(status='skipped' if group['status'] == 'pending' else 'interrupted',
                                 reason='review_budget_or_execution_stopped')
            save_review()
    model.step_packet_factory = lambda refs: packet(refs, scoped=True)
    step_gate = None
    if (settings.runtime_check and settings.behavior_check
            and getattr(model, 'execution_mode', None) == 'plan_execute'
            and isinstance(getattr(model, 'execution_control', None), RunControl)):
        step_gate = StepBehaviorGate(app, model, tools, settings, load_task_ir(app, ir), model.step_packet_factory,
                                    reports / 'step-behavior')
        step_gate.deadline = min(step_gate.deadline, shared_diagnostic_deadline)
        model.step_behavior_gate = step_gate
        evidence.step_gate = step_gate
    for item in work:
        for req_id in item.requirement_ids:
            runtime.events.mark_implementation_started(req_id,'Implement requirement in shared application')
    whole_packet = packet(all_ids)
    if whole_packet.text.startswith(WHOLE_TASK_PREFIX):
        result = implement_until_covered(all_ids, 'all-atomic-requirements', settings.implement_request_allowance, None)
    else:
        # Sources do not fit one packet: implement flow by flow in dependency
        # order, each batch seeing the narrowed tests plus the current application.
        logging.info('PLATFORM_BATCHING %s',json.dumps(dict(task_id=task,items=len(work),
            implement_request_allowance=settings.implement_request_allowance,minimum_per_batch=MIN_BATCH_REQUESTS)))
        # Batch shares are soft ceilings. Charge physical requests actually spent,
        # including planning and transport recovery, so unused shares remain available.
        pool = settings.implement_request_allowance
        result = dict(status='complete')
        for position, item in enumerate(work):
            if pool <= 0:
                logging.info('PLATFORM_BATCHING %s',json.dumps(dict(task_id=task,stopped='request_pool_exhausted',item=item.id)))
                break
            share = min(pool, max(min(MIN_BATCH_REQUESTS, pool), math.ceil(pool / (len(work) - position))))
            before_requests = budget.state['requests']
            result = implement_until_covered(item.requirement_ids, item.id, share, item.requirement_ids)
            spent = budget.state['requests'] - before_requests
            if not 0 <= spent <= share:
                raise RuntimeError('Implementation exceeded its physical request share')
            pool -= spent
            logging.info('PLATFORM_BATCH_BUDGET %s', json.dumps(dict(item=item.id, allocated=share,
                requests_used=spent, released=share-spent, requests_remaining=pool)))
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
    delivery_app, delivery_tools = app, tools
    candidate_files = None
    if (settings.delivery_policy == 'verified'
            and result.get('reason') in {'self_test_reserve', 'self_test_time_reserve'}
            and result.get('candidate_source') and step_gate is not None):
        candidate_path = Path(result['candidate_source']).resolve()
        if not candidate_path.is_relative_to(step_gate.directory.resolve()):
            raise ValueError('candidate outside controller quarantine')
        candidate_files = snapshot_project(candidate_path)
        base_files = model.execution_control.source(delivery_app)
        candidate_hash = source_digest(candidate_files)
        base_hash = source_digest(base_files)
        if (candidate_hash != result.get('candidate_source_sha256')
                or base_hash != result.get('candidate_base_sha256')):
            raise ValueError('candidate or accepted source changed after rollback')
        evidence.candidate = dict(status='quarantined', source_sha256=candidate_hash,
                                  base_source_sha256=base_hash, reason=result['reason'])
        app = candidate_path
        tools = StaticProjectTools(app, include_tests=False)
        tools.public_tests = inputs.tests_dir
        model.execution_control.source(app)
    if (settings.delivery_policy == 'runnable_candidate'
            and result.get('reason') in PHASE_BUDGET_STOPS and result.get('candidate_source')
            and step_gate is not None):
        from .candidate_delivery import check_candidate_delivery
        evidence.delivery = check_candidate_delivery(app, evidence, model, settings,
            ir=load_task_ir(app, ir), reports=reports, generation_result=result,
            deadline=shared_diagnostic_deadline, assert_input_integrity=bundle.assert_integrity)
        bundle.assert_integrity()
        return
    checked = validate()
    evidence.static = checked
    if (candidate_files is None and not checked['passed'] and budget_open()
            and result.get('reason') not in ALLOCATION_STOP_REASONS):
        tools.unseal()
        result = model.repair_sources(packet(all_ids),tools,request_allowance=settings.repair_request_allowance,
            feedback=STATIC_REPAIR_FEEDBACK+json.dumps(checked))
        record('static_repair','all-atomic-requirements',result,allow_delivery=True)
        checked = validate()
    (reports/'static-validation.json').write_text(json.dumps(checked,indent=2),encoding='utf-8')
    if not checked['passed']: raise RuntimeError('Source is not ready for platform build: '+json.dumps(checked['errors']))
    if settings.delivery_policy == 'runnable_candidate':
        from .candidate_delivery import check_candidate_delivery
        evidence.missing = missing_coverage(app, load_task_ir(app, ir))
        evidence.delivery = check_candidate_delivery(app, evidence, model, settings,
            ir=load_task_ir(app, ir), reports=reports, generation_result=result,
            deadline=shared_diagnostic_deadline, assert_input_integrity=bundle.assert_integrity)
        bundle.assert_integrity()
        return
    # Without runtime capability this remains explicitly source-only review.
    # Otherwise defer optional reviews until a runnable baseline exists, inside
    # the same runtime round/time allocation so changed source is rechecked.
    if (not settings.runtime_check or not shutil.which('node')) and budget_open() and result.get('reason') not in ALLOCATION_STOP_REASONS:
        review_batches(result.get('completed_steps') or [])
    if result.get('status')!='complete':
        logging.warning('Delivering structurally valid candidate after bounded generation stopped: %s; functional acceptance remains UNVERIFIED',result.get('reason'))
    model.request_deadline = author_deadline
    budget.protect_self_test_requests(0)
    authored = step_gate.authored_checks if step_gate is not None else None
    authored_parts = [authored] if authored else []
    covered = {case['requirement_id'] for suite in authored_parts for case in suite['plan']['cases']}
    required = set(all_ids)
    missing_ids = [ref for ref in all_ids if ref not in covered]
    author_audit = dict(status='not_run', scope='synthetic_diagnostics_only', formal_acceptance=False,
                        request_allowance=author_allowance, batches=[], reused_requirement_ids=sorted(covered))
    remaining_author_calls = author_allowance
    if covered - required:
        author_audit.update(status='invalid', reason='authored_requirement_scope_mismatch')
    else:
        for offset in range(0, len(missing_ids), behavior_checks.MAX_CASES):
            refs = missing_ids[offset:offset + behavior_checks.MAX_CASES]
            if not remaining_author_calls or not budget_open():
                author_audit.update(status='incomplete', reason='author_budget_or_time_unavailable')
                break
            public_ir = load_task_ir(app, ir)
            scoped_ir = dict(public_ir,
                work_items=[row for row in public_ir.get('work_items', []) if row['id'] in refs],
                scenarios=[row for row in public_ir.get('scenarios', []) if row.get('work_item') in refs])
            authored_result = model.author_behavior_checks(packet(refs, scoped=True), tools, scoped_ir, remaining_author_calls)
            record('behavior_author', ','.join(refs), authored_result, optional=True)
            used = authored_result.get('turns', 0)
            remaining_author_calls -= max(1, used if type(used) is int else 1)
            audit = dict(requirement_ids=refs, status=authored_result.get('status'),
                reason=authored_result.get('reason'), requests_used=used)
            author_audit['batches'].append(audit)
            if authored_result.get('status') != 'complete':
                author_audit.update(status='incomplete', reason='author_incomplete')
                break
            plan = {'cases': authored_result.get('cases')}
            directory = reports / 'final-author' / f'batch-{len(author_audit["batches"])}'
            try:
                behavior_checks.validate_plan(plan, scoped_ir)
                hashes = behavior_checks.freeze_plan(plan, scoped_ir, directory)
            except (ValueError, OSError) as error:
                author_audit.update(status='invalid', reason=str(error)[:500])
                break
            part = dict(directory=directory, hashes=hashes, plan=plan, cases=len(plan['cases']))
            evidence.suites.append(part)
            authored_parts.append(part)
            constraint_evidence = behavior_checks.plan_coverage(plan, scoped_ir)
            audit['constraint_evidence'] = constraint_evidence
            bound = set(constraint_evidence['covered'])
            covered.update(bound)
            if bound != set(refs):
                author_audit.update(status='incomplete', reason='author did not cover every requested atomic requirement')
                break
    # Execute valid frozen cases even when later authoring fails. These remain
    # partial diagnostics: the final acceptance gate still requires every ID.
    final_constraint_evidence = behavior_checks.plan_coverage(
        {'cases': [case for part in authored_parts for case in part['plan']['cases']]},
        dict(ir, work_items=[row for row in ir.get('work_items', []) if row['id'] in required]))
    author_audit['constraint_evidence'] = final_constraint_evidence
    suite_complete = covered == required and bool(authored_parts) and final_constraint_evidence['complete']
    if authored_parts and not covered - required:
        if len(authored_parts) == 1:
            authored = authored_parts[0]
        else:
            files, cases = {}, []
            for index, part in enumerate(authored_parts, 1):
                if not behavior_checks.verify_frozen(part['directory'], part['hashes']):
                    raise ValueError('frozen authored suite changed before final collection')
                for name in part['hashes']:
                    if name != 'plan.json':
                        files[f'part-{index}-{name}'] = (part['directory'] / name).read_bytes()
                cases.extend(part['plan']['cases'])
            plan = {'cases': cases}
            files['plan.json'] = json.dumps(plan, ensure_ascii=False, indent=2).encode('utf-8')
            directory = reports / 'final-authored-suite'
            hashes = behavior_checks.write_frozen_suite(directory, files)
            authored = dict(directory=directory, hashes=hashes, plan=plan, cases=len(cases))
            evidence.suites.append(authored)
        author_audit.update(hashes=authored['hashes'], cases=authored['cases'])
        if suite_complete:
            author_audit['status'] = 'frozen'
    else:
        authored = None
    if not suite_complete:
        author_audit.setdefault('reason', 'authored_requirements_incomplete')
        author_audit['status'] = 'incomplete'
    author_audit['missing_requirement_ids'] = sorted(required - covered)
    evidence.authored = authored
    (reports/'behavior-author.json').write_text(json.dumps(author_audit, ensure_ascii=False, indent=2), encoding='utf-8')
    runtime_report = dict(task_id=task, enabled=settings.runtime_check, skipped=None, rounds=[], final_status=None,
        suite_complete=suite_complete, missing_requirement_ids=sorted(required - covered),
        scope='synthetic_diagnostics_only', formal_acceptance=False)
    evidence.runtime_report = runtime_report
    if not settings.runtime_check:
        runtime_report['skipped'] = 'disabled_by_settings'
    elif not settings.behavior_check:
        runtime_report['skipped'] = 'behavior_disabled_by_settings'
    elif authored is None:
        runtime_report['skipped'] = 'self_test_suite_incomplete'
    elif not shutil.which('node'):
        runtime_report['skipped'] = 'node_unavailable'
    if runtime_report['skipped']:
        logging.info('PLATFORM_RUNTIME_CHECK %s', json.dumps(dict(
            task_id=task, skipped=runtime_report['skipped'], signals={}, diagnostics={})))
        (reports/'runtime-check.json').write_text(json.dumps(runtime_report,indent=2),encoding='utf-8')
    else:
        if getattr(args, 'handoff_final_verification', False):
            from .verification_handoff import prepare
            evidence.static = validate()
            evidence.missing = missing_coverage(app, load_task_ir(app, ir))
            prepare(args, evidence, settings=settings, model=model, task=task,
                ir=load_task_ir(app, ir), deadline=step_gate.deadline if step_gate else model.execution_control.deadline,
                reports=reports, result=result)
        runtime_report = run_runtime_loop(app, None, settings, model=model, tools=tools,
            make_packet=lambda: packet(all_ids), record=record, validate=validate, task_id=task,
            report_path=reports/'runtime-check.json', ir=load_task_ir(app, ir),
            model_calls_allowed=lambda: candidate_files is None and suite_complete and budget_open(),
            authored_checks=authored,
            report=evidence.runtime_report,
            runtime_deadline=step_gate.deadline if step_gate is not None else model.execution_control.deadline,
            review_after_pass=(lambda: review_batches(result.get('completed_steps') or []))
                if candidate_files is None and suite_complete and result.get('reason') not in ALLOCATION_STOP_REASONS else None)
        evidence.runtime_report = runtime_report
        final = validate()
        evidence.static = final
        (reports/'static-validation.json').write_text(json.dumps(final,indent=2),encoding='utf-8')
        if not final['passed']:
            raise RuntimeError('Source is not ready for platform build after runtime repair: '+json.dumps(final['errors']))
    control = getattr(model, 'execution_control', None)
    if control is not None and control.stopped in {'rollback_failed', 'concurrent_source_change'}:
        raise ExecutionStopped(control.stopped)
    final = validate()
    evidence.static = final
    (reports/'static-validation.json').write_text(json.dumps(final, indent=2), encoding='utf-8')
    if not final.get('passed'):
        raise RuntimeError('Final delivery source is invalid after review or rollback')
    final_ir = load_task_ir(app, ir)
    final_missing = missing_coverage(app, final_ir)
    evidence.missing = final_missing
    if final_missing:
        logging.warning('PLATFORM_COVERAGE %s', json.dumps(dict(task_id=task,
            status='incomplete', item='final_delivery', missing=final_missing), ensure_ascii=False))
    if candidate_files is not None:
        # Exercise the same export and acceptance contract before touching the
        # accepted delivery. The temporary report also tests suite export I/O.
        with tempfile.TemporaryDirectory(prefix='arc-candidate-contract-') as temporary:
            preflight = finalize_self_test(initialize_self_test(Path(temporary)),
                required_ids=evidence.required_ids, static=final, runtime_report=runtime_report,
                authored=authored, source_sha256=source_digest(snapshot_project(app)),
                missing_coverage=final_missing, behavior_enabled=evidence.behavior_enabled,
                runtime_enabled=evidence.runtime_enabled,
                authored_suites=[*evidence.suites, *step_gate.collections.values()],
                requirements_sha256=evidence.requirements_sha256)
        control = model.execution_control
        current_candidate = control.source(app)
        if current_candidate != candidate_files:
            raise ValueError('quarantined candidate changed during verification')
        if preflight['accepted']:
            control.check()
            current_delivery = control.source(delivery_app)
            if current_delivery != base_files:
                raise ValueError('accepted source changed before promotion')
            evidence.promotion_before = current_delivery
            evidence.promotion_after = candidate_files
            evidence.promotion_control = control
            apply_snapshot(delivery_app, candidate_files, before=current_delivery)
            if control.source(delivery_app, changed_by_controller=True) != candidate_files:
                raise ValueError('candidate promotion source mismatch')
            evidence.candidate['status'] = 'promoted'
        else:
            evidence.candidate.update(status='unverified', reasons=preflight['reasons'])
        app, tools = delivery_app, delivery_tools
        final_ir = load_task_ir(app, ir)
        if not preflight['accepted']:
            final = validate()
            evidence.static = final
            evidence.missing = missing_coverage(app, load_task_ir(app, ir))
    delivery_source = source_digest(snapshot_project(app))
    verification = verification_summary(app, final_ir, final, runtime_report,
                                        input_policy=settings.input_policy, source_sha256=delivery_source)
    (reports/'verification.json').write_text(json.dumps(verification, indent=2), encoding='utf-8')
    (reports/'generation-boundary.json').write_text(json.dumps(dict(
        input_policy=settings.input_policy, requirement_source_sha256=bundle.source_hash,
        delivered_source_sha256=delivery_source, evaluation_tests_visible=False,
        behavioral_acceptance='UNRUN',
        formal_status='UNRUN'), indent=2), encoding='utf-8')
    logging.info('PLATFORM_DELIVERY %s',json.dumps(dict(task_id=task,input_policy=settings.input_policy,
        source_sha256=delivery_source,formal_status='PENDING',budget=budget.state,
        runtime_check=dict(skipped=runtime_report.get('skipped'),final_status=runtime_report.get('final_status'),
                           rounds=len(runtime_report.get('rounds') or [])))))
    return None


def append_jsonl(path,row):
    with path.open('a',encoding='utf-8') as stream: stream.write(json.dumps(row,ensure_ascii=False)+'\n')

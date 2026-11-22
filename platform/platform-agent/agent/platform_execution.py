"""Task-independent execution limits, validated plans and source transactions.

One controller owns the project. Source rollback does not refund requests,
tokens, tool calls or elapsed time, and never restores runtime metadata.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import logging
import math
from pathlib import Path
import time
from uuid import uuid4

from .platform_settings import MAX_RUN_SECONDS
from .sandbox_files import apply_snapshot, snapshot_project

COVERAGE_DELIVERY_STOPS = frozenset({'coverage_storage_failed', 'coverage_capacity_exceeded'})
CONTROLLER_STATE_STOPS = COVERAGE_DELIVERY_STOPS | {'coverage_state_invalid'}
MODEL_ONLY_STOPS = COVERAGE_DELIVERY_STOPS | frozenset({'truncated', 'model_request_failed', 'request_interrupted_usage_unknown',
    'model_terminated', 'execution_request_limit', 'platform_token_allocation',
    'platform_request_allocation', 'request_allowance', 'context_limit',
    'repeated_action', 'no_progress', 'source_oscillation', 'replan_limit', 'tool_batch_limit',
    'verification_blocked', 'invalid_plan', 'planning_truncated', 'recovery_exhausted',
    'invalid_json', 'invalid_json_shape', 'invalid_tool_batch', 'model_incomplete', 'empty_response'})
SOURCE_SAFETY_STOPS = frozenset({'rollback_failed', 'concurrent_source_change'})
PHASE_BUDGET_STOPS = frozenset({'self_test_reserve', 'self_test_time_reserve'})
SOURCE_CLAIMS_DRAFT = 'source_claims_incomplete'
SOURCE_CLAIMS_DRAFT_STOPS = frozenset({'replan_limit', 'request_allowance'})
EXECUTION_STOP_REASONS = frozenset({'execution_deadline', 'execution_request_limit', 'execution_tool_limit',
    'tool_batch_limit', 'repeated_action', 'source_oscillation', 'no_progress', 'replan_limit',
    'request_interrupted_usage_unknown', 'model_request_failed', 'model_terminated', 'verification_blocked',
    'truncated', 'recovery_exhausted'})
BUDGET_STOP_REASONS = EXECUTION_STOP_REASONS | PHASE_BUDGET_STOPS | {'platform_token_allocation', 'platform_request_allocation',
    'request_allowance', 'budget_insufficient', 'context_limit', 'inspection_limit'}
ALLOCATION_STOP_REASONS = EXECUTION_STOP_REASONS | {'platform_token_allocation', 'platform_request_allocation'}
CAMPAIGN_DIAGNOSTIC_STOPS = frozenset({'request_usage_unknown', 'request_interrupted_usage_unknown'})
QUALITY_WARNINGS = frozenset({'quality_stagnation', 'quality_unknown'})


def planning_stopped(result):
    """Only a classified planning failure can enter the candidate-only exit."""
    return (result.get('status') == 'stopped' and result.get('reason') in {
                'invalid_plan', 'planning_truncated', 'invalid_json', 'invalid_json_shape',
                'invalid_tool_batch', 'model_incomplete', 'empty_response'}
            and result.get('phase') == 'planning')


def behavior_verification_status(checked):
    """Describe controller evidence without granting new execution allowance.

    Only the two existing final-verification reservation stops are deferred.
    A failed observed behavior outranks a simultaneous budget reason. Unknown
    outcomes and unavailable/invalid harnesses are blocked, never failed tests.
    """
    stops = [checked.get(key) for key in ('reason', 'author_status')]
    if any(reason in (BUDGET_STOP_REASONS | SOURCE_SAFETY_STOPS |
            {'usage_unknown', 'usage_unknown_or_out_of_bounds', 'request_usage_unknown', 'budget_exhausted', 'total_token_budget'})
            - PHASE_BUDGET_STOPS for reason in stops):
        return 'BLOCKED'
    if checked.get('passed') is False and checked.get('failure_kind') == 'behavior':
        return 'FAIL'
    phase_stop = any(checked.get(key) in PHASE_BUDGET_STOPS
                     for key in ('reason', 'author_status'))
    if phase_stop:
        return ('DEFERRED' if checked.get('passed') is False
                and checked.get('failure_kind') in {'verification', 'phase_stop'} else 'BLOCKED')
    if (checked.get('passed') is True and not checked.get('failure_kind')
            and not checked.get('reason') and not checked.get('author_status')):
        return 'PASS'
    return 'BLOCKED'


def diagnostics_allowed(reason, *, campaign=False):
    """Classify a stop without bypassing tool, time or pending-usage accounting.

    Durable campaign stops have a narrower vocabulary than execution stops.
    Unknown reasons fail closed in either domain; a permitted diagnostic never
    reopens model requests or releases their existing reservation.
    """
    allowed = CAMPAIGN_DIAGNOSTIC_STOPS if campaign else MODEL_ONLY_STOPS
    return reason is None or reason in allowed


class ExecutionStopped(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class CandidateCloseout:
    source_sha256: str
    stop_reason: str | None
    stop_epoch: int
    requests: int
    fallback_source_sha256: str | None = None


@dataclass(frozen=True)
class ControlPolicy:
    """Soft windows never replace the immutable execution limits."""
    enabled: bool = False
    recovery_attempts: int = 1
    warning_turns: int = 3
    quality_window: int = 6
    recovery_requests: int | None = None
    probation_requests: int | None = None

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError('control policy enabled must be boolean')
        for name, value in vars(self).items():
            if name in {'recovery_requests', 'probation_requests'} and value is None:
                continue
            if name != 'enabled' and (type(value) is not int or value < (0 if name == 'recovery_attempts' else 1)):
                raise ValueError('invalid control policy: ' + name)


class ProjectLease:
    """Exclude concurrent controllers without touching an empty application's layout.

    A crash leaves the lease in place; callers must investigate before removing
    it. Automatically expiring an active writer's lease could corrupt sources.
    """
    def __init__(self, project):
        root = Path(project).resolve()
        self.path = root.parent / ('.' + root.name + '.arc-generation.lock')
        self.identity = uuid4().hex

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open('x', encoding='utf-8') as handle:
                handle.write(self.identity)
        except FileExistsError:
            raise ExecutionStopped('project_already_owned') from None
        return self

    def __exit__(self, *error):
        if self.path.is_symlink() or self.path.read_text(encoding='utf-8') != self.identity:
            raise ExecutionStopped('project_lease_changed')
        self.path.unlink()


@dataclass(frozen=True)
class ExecutionLimits:
    requests: int | None = None
    tools: int = 2000
    seconds: int = MAX_RUN_SECONDS
    batch_tools: int = 16
    repeated_actions: int = 3
    idle_turns: int = 6
    plan_steps: int = 24
    plan_requests: int | None = None
    step_requests: int | None = None
    replans: int = 1
    protocol_repairs: int = 2
    inspection_turns: int = 3

    def __post_init__(self):
        for name, value in vars(self).items():
            if name in {'requests', 'plan_requests', 'step_requests'} and value is None:
                continue
            if type(value) is not int or value < (0 if name in {'replans', 'protocol_repairs'} else 1):
                raise ValueError(f'invalid execution limit: {name}')


def request_horizon(value):
    """Use infinity only for internal arithmetic on an unconfigured request limit."""
    return math.inf if value is None else value


def reported_requests(value):
    return None if value == math.inf else value


def source_digest(files):
    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        digest.update(name.encode('utf-8') + b'\0' + hashlib.sha256(data).digest())
    return digest.hexdigest()


def construction_digest(files):
    """Hash implementation progress, separately from complete source integrity.

    Public inputs, coverage claims, documentation and tests cannot buy another
    construction window. Runtime source, assets, seeds, dependency manifests
    and deployment scripts remain included. This is still activity evidence,
    never proof of behavior or requirement acceptance.
    """
    def implementation_path(name):
        parts = name.casefold().split('/')
        basename = parts[-1]
        if parts[0] in {'docs', 'requirements'}:
            return False
        if any(part in {'test', 'tests', 'self-tests', '__tests__', '__snapshots__',
                        'test-results', 'playwright-report', 'coverage'} for part in parts[:-1]):
            return False
        if ('.test.' in basename or '.spec.' in basename
                or basename.startswith(('test_', 'conftest.'))
                or basename.split('.')[0] in {'readme', 'changelog', 'license', 'licence',
                                             'copying', 'authors', 'contributors', 'contributing'}
                or basename.endswith(('.md', '.rst', '.adoc'))):
            return False
        return True
    return source_digest({name: data for name, data in files.items() if implementation_path(name)})


def validate_arguments(value, schema, path='$'):
    """Validate the JSON-schema subset used by the exposed local tools."""
    kind = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'boolean': bool}
    if kind in types and type(value) is not types[kind]:
        raise ValueError(f'{path}: tool argument must be {kind}')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{path}: tool argument is outside enum')
    if kind == 'object':
        properties = schema.get('properties', {})
        if not set(schema.get('required', [])) <= value.keys():
            missing = sorted(set(schema.get('required', [])) - value.keys())
            raise ValueError(f'{path}: required tool argument is absent: {", ".join(missing)}')
        if schema.get('additionalProperties') is False and not value.keys() <= properties.keys():
            raise ValueError(f'{path}: unexpected tool argument; use only declared properties')
        for key, item in value.items():
            if key in properties:
                validate_arguments(item, properties[key], f'{path}.{key}')
    elif kind == 'array':
        minimum, maximum = schema.get('minItems', 0), schema.get('maxItems', 1000000)
        if not minimum <= len(value) <= maximum:
            raise ValueError(f'{path}: tool array size is outside bounds; received {len(value)} items, '
                             f'allowed {minimum}..{maximum}. Split oversized work into bounded calls.')
        for index, item in enumerate(value):
            validate_arguments(item, schema.get('items', {}), f'{path}[{index}]')
    elif kind == 'integer':
        if value < schema.get('minimum', value) or value > schema.get('maximum', value):
            raise ValueError(f'{path}: tool integer is outside bounds; '
                             f'minimum={schema.get("minimum", "unspecified")}, '
                             f'maximum={schema.get("maximum", "unspecified")}')


class RunControl:
    """A sticky circuit breaker shared by every stage of one generation."""
    def __init__(self, limits=None, *, clock=time.monotonic, emit=None, policy=None):
        self.limits = limits or ExecutionLimits()
        self.clock, self.emit = clock, emit or (lambda event: None)
        self.deadline = clock() + self.limits.seconds
        self.requests = self.tools = self.replans = self.idle = self.protocol_repairs = 0
        self.service_failed_requests = 0
        self.protected_final_tools = 0
        self.external_tool_capacity = None
        self.productive_replans = 0
        self._replan_progress_requirements = set()
        self._replan_progress_sources = set()
        self.stopped = None
        self._stop_epoch = 0
        self._candidate_closeout = None
        self._candidate_closeout_fallback = False
        self.actions = Counter()
        self.states = Counter()
        self.expected_sources = {}
        self.plan_progress = None
        self.prior_retained_requirement_ids = frozenset()
        self.retained_requirement_ids = frozenset()
        self.initial_empty_step_alternated = False
        self.policy = policy or ControlPolicy()
        self.task_state, self.control_state = 'PLAN', 'RUNNING'
        self.current_source = None
        self.verifications = {}
        self.recovery_count = self.source_recovery_count = 0
        self.truncation_replacements = 0
        self.quality_idle = self.warning_age = 0
        self.fault_reason = self.warning_reason = None
        self.quality_known = False
        self.verification_available = True
        self._passed_requirements, self._failed_requirements = set(), set()
        self._observed_scenarios = set()
        self._probation_requests = 0

    @property
    def work_requests(self):
        """Physical requests excluding confirmed service failures that yielded no model decision."""
        return self.requests - self.service_failed_requests

    def remaining_physical_requests(self):
        return max(0, request_horizon(self.limits.requests) - self.requests)

    @property
    def control_recovery_available(self):
        return self.recovery_count - self.truncation_replacements < self.policy.recovery_attempts

    def _transition(self, state, reason):
        previous = self.control_state
        self.control_state = state
        self.emit(dict(event='control_state_transition', previous=previous, control_state=state,
                       task_state=self.task_state, reason=reason, source_sha256=self.current_source,
                       requests=self.requests, tools=self.tools, recoveries=self.recovery_count))

    def set_task_state(self, state, *, reason, source_sha256=None):
        transitions = {'PLAN': {'CODE', 'BUILD', 'TEST'}, 'CODE': {'PLAN', 'BUILD', 'TEST'},
                       'BUILD': {'PLAN', 'CODE', 'TEST'}, 'TEST': {'PLAN', 'CODE', 'BUILD'},
                       'COMPLETE': set(), 'READY_FOR_EVALUATION': set(), 'STOPPED': set()}
        if state not in transitions or (state != self.task_state and state not in transitions[self.task_state]
                                        and state != 'STOPPED'):
            raise ValueError('illegal task state transition: ' + self.task_state + ' -> ' + str(state))
        if state != 'STOPPED' and self.control_state == 'TERMINATED':
            raise ExecutionStopped(self.stopped or self.fault_reason or 'execution_terminated')
        previous = self.task_state
        self.task_state = state
        if source_sha256 is not None:
            self.current_source = source_sha256
        self.emit(dict(event='task_state_transition', previous=previous, task_state=state,
                       control_state=self.control_state, reason=reason, source_sha256=self.current_source))

    def permit(self, action='task'):
        if self.stopped:
            raise ExecutionStopped(self.stopped)
        if self.clock() >= self.deadline:
            self.stop('execution_deadline')
        if self._candidate_closeout is not None and action in {'task', 'request', 'write'}:
            raise ExecutionStopped('candidate_closeout_sealed')
        if self.task_state in {'COMPLETE', 'READY_FOR_EVALUATION', 'STOPPED'} and action in {'task', 'request', 'write'}:
            raise ExecutionStopped('task_' + self.task_state.lower())
        if self.policy.enabled:
            if self.control_state in {'CIRCUIT_OPEN', 'TERMINATED'}:
                raise ExecutionStopped(self.fault_reason or 'execution_terminated')
            if self.control_state == 'RECOVERING' and action != 'recovery':
                raise ExecutionStopped('recovery_action_required')
            probation_limit = self.policy.probation_requests
            if (self.control_state == 'PROBATION' and self.task_state != 'PLAN' and probation_limit is not None
                    and self._probation_requests >= probation_limit
                    and (action == 'request' or action == 'write'
                         and self._probation_requests > probation_limit)):
                self._trip(self.fault_reason or 'probation_exhausted')
        return True

    def soft_request_allowance(self, proposed, *, stage=None):
        self.permit()
        if self.control_state == 'PROBATION' and (stage or self.task_state) == 'PLAN':
            return proposed  # Planning still spends the shared hard budgets.
        if self.policy.enabled and self.control_state in {'WARNING', 'PROBATION'}:
            cap = self.policy.probation_requests if self.control_state == 'PROBATION' else self.policy.recovery_requests
            if cap is None:
                return proposed
            if self.control_state == 'PROBATION':
                cap -= self._probation_requests
            return max(0, min(proposed, cap))
        return proposed

    def _trip(self, reason):
        self.fault_reason = reason
        if reason == 'activity_stagnation':
            self.emit_cost(dict(event='activity_stagnation_limit', source_sha256=self.current_source,
                consecutive_idle_turns=self.idle, idle_turn_limit=self.limits.idle_turns,
                warning_age=self.warning_age, warning_turn_limit=self.policy.warning_turns,
                requests=self.requests, tools=self.tools))
        if not self.control_recovery_available:
            self.stop('recovery_exhausted')
        self._transition('CIRCUIT_OPEN', reason)
        raise ExecutionStopped(reason)

    def _warn(self, reason):
        if self.control_state == 'PROBATION':
            self._trip(reason)
        if self.control_state == 'RUNNING':
            self.warning_reason, self.warning_age = reason, 0
            self._transition('WARNING', reason)
        elif self.control_state == 'WARNING':
            self.warning_age += 1
            if self.warning_age >= self.policy.warning_turns:
                self._trip(self.warning_reason)

    def recover(self, action, callback, *, source_changed=False):
        """Apply a bounded checkpoint rollback or an accounted transport change."""
        warning_rollback = (self.control_state == 'WARNING' and self.warning_reason in QUALITY_WARNINGS
                            and action == 'source_rollback' and source_changed)
        if (not self.policy.enabled or self.stopped
                or not (self.control_state == 'CIRCUIT_OPEN' or warning_rollback)
                or action not in {'source_rollback', 'model_transport'}
                or action == 'model_transport' and (self.fault_reason != 'model_transport' or source_changed)):
            raise ExecutionStopped(self.stopped or 'recovery_not_permitted')
        if self.clock() >= self.deadline:
            self.stop('execution_deadline')
        if not self.control_recovery_available:
            self.stop('recovery_exhausted')
        if warning_rollback:
            self.fault_reason = self.warning_reason
            self._transition('CIRCUIT_OPEN', self.fault_reason)
        self.recovery_count += 1
        if action == 'source_rollback':
            self.source_recovery_count += 1
        self._transition('RECOVERING', action)
        try:
            result = callback()
        except BaseException:
            if self.stopped is None:
                self.stop('recovery_failed')
            raise
        if self.clock() >= self.deadline:
            self.stop('execution_deadline')
        self.permit('recovery')
        if source_changed:
            self.verifications.clear()
            self.set_task_state('BUILD', reason='recovery_source_changed', source_sha256=self.current_source)
        self._probation_requests = 0
        self._transition('PROBATION', self.fault_reason)
        return result

    def record_verification(self, *, stage, passed, source_sha256, evidence_id,
                            failure_kind=None, requirement_ids=(), failed_ids=None, evidence_kind='runtime', closeout=None):
        if (stage not in {'PLAN', 'BUILD', 'TEST'} or type(passed) is not bool
                or not source_sha256 or not evidence_id or evidence_kind not in {'runtime', 'static'}):
            raise ValueError('verification requires a stage, boolean result, source and evidence identity')
        if closeout is not None:
            self.check_candidate_closeout(closeout)
            if (stage != 'BUILD' or evidence_kind != 'runtime' or requirement_ids or failed_ids
                    or source_sha256 != self.candidate_closeout_source(closeout)):
                raise ValueError('candidate closeout only records exact-source runtime readiness')
        else:
            self.permit('verification')
        if self.current_source is not None and source_sha256 != self.current_source:
            raise ValueError('verification source does not match current source')
        self.current_source = source_sha256
        receipt = dict(stage=stage, passed=passed, source_sha256=source_sha256,
                       evidence_id=evidence_id, failure_kind=failure_kind,
                       requirement_ids=list(requirement_ids), failed_ids=list(failed_ids or ()), evidence_kind=evidence_kind)
        if closeout is not None:
            receipt['closeout_stop_epoch'] = closeout.stop_epoch
        self.verifications[evidence_id] = receipt
        self.emit(dict(event='verification_evidence', **receipt))
        if closeout is not None or not self.policy.enabled:
            return receipt
        refs = set(requirement_ids)
        runtime_test = stage == 'TEST' and evidence_kind == 'runtime'
        gain = runtime_test and passed and (bool(refs - self._passed_requirements)
                                             or bool(self._failed_requirements & refs))
        if stage == 'PLAN' and passed:
            self.idle = 0
        if runtime_test:
            self.quality_known = True
            if passed:
                self._passed_requirements.update(refs)
                self._failed_requirements.difference_update(refs)
            else:
                self._failed_requirements.update(refs if failed_ids is None else failed_ids)
        if gain:
            self.quality_idle = 0
        elif runtime_test:
            self.quality_idle += 1
        recovery_verified = passed and stage in {'BUILD', 'TEST'} and evidence_kind == 'runtime'
        if self.fault_reason in QUALITY_WARNINGS:
            recovery_verified = recovery_verified and stage == 'TEST' and bool(refs)
        if self.control_state == 'PROBATION' and recovery_verified:
            self.idle = self.quality_idle = self.warning_age = 0
            self.warning_reason = self.fault_reason = None
            self._transition('RUNNING', 'recovery_verified')
        elif self.control_state == 'WARNING' and (
                (self.warning_reason == 'activity_stagnation' and passed and evidence_kind == 'runtime')
                or (self.warning_reason in QUALITY_WARNINGS and gain)):
            self.warning_age = 0
            self.warning_reason = None
            self._transition('RUNNING', 'warning_verified')
        elif runtime_test and (self.control_state == 'PROBATION' and not passed
                                 or self.quality_idle >= self.policy.quality_window):
            self._warn('quality_stagnation')
        return receipt

    def observe_retained_scenario(self, step, checked, source_sha256):
        """Credit a new runtime observation only on the retained construction source."""
        observation = checked.get('selected_scenario') or {}
        if (not self.policy.enabled or checked.get('reason') != 'requirement_constraints_unresolved'
                or observation.get('diagnostic_only') is not True or observation.get('status') != 'passed'
                or observation.get('source_sha256') != source_sha256
                or self.current_source != source_sha256 or observation.get('requirement_id') not in step.requirement_ids
                or not observation.get('scenario_id')):
            return
        identity = (observation['requirement_id'], observation['scenario_id'])
        if identity in self._observed_scenarios:
            return
        self._observed_scenarios.add(identity)
        if self.control_state == 'WARNING' and self.warning_reason == 'quality_unknown' and not self.quality_known:
            self.quality_idle = self.warning_age = 0
            self.warning_reason = None
            self._transition('RUNNING', 'retained_public_scenario_observed')
        elif self.control_state == 'RUNNING' and not self.quality_known:
            self.quality_idle = 0

    def finish_task(self, *, source_sha256, required_checks):
        self.permit('verification')
        if (not required_checks or source_sha256 != self.current_source
                or any(not self.verifications.get(identity, {}).get('passed')
                       or self.verifications[identity]['source_sha256'] != source_sha256
                       for identity in required_checks)):
            self.stop('required_verification_incomplete')
        if self.policy.enabled and self.control_state not in {'RUNNING', 'WARNING'}:
            self.stop('required_verification_incomplete')
        previous = self.task_state
        self.task_state = 'COMPLETE'
        self.emit(dict(event='task_state_transition', previous=previous, task_state='COMPLETE',
                       control_state=self.control_state, reason='required_checks_passed', source_sha256=source_sha256))

    def finish_candidate(self, *, source_sha256, evidence_id):
        """Close construction without claiming complete requirement verification."""
        self.permit('verification')
        receipt = self.verifications.get(evidence_id, {})
        if (source_sha256 != self.current_source or receipt.get('source_sha256') != source_sha256
                or receipt.get('stage') != 'BUILD' or receipt.get('evidence_kind') != 'runtime'
                or receipt.get('passed') is not True or receipt.get('requirement_ids')
                or self.control_state not in {'RUNNING', 'WARNING'}):
            self.stop('candidate_readiness_incomplete')
        previous = self.task_state
        self.task_state = 'READY_FOR_EVALUATION'
        self.emit(dict(event='task_state_transition', previous=previous, task_state=self.task_state,
                       control_state=self.control_state, reason='runnable_candidate_only',
                       source_sha256=source_sha256, requirements_verified=False))

    def stop(self, reason):
        self._stop_epoch += 1
        if self.stopped is None or reason in SOURCE_SAFETY_STOPS:
            self.stopped = reason
            self.task_state = 'STOPPED'
            self._transition('TERMINATED', reason)
            self.emit(dict(event='execution_stopped', reason=reason,
                           requests=self.requests, tools=self.tools, replans=self.replans,
                           productive_replans=self.productive_replans,
                           unproductive_replans=self.replans - self.productive_replans))
        raise ExecutionStopped(self.stopped)

    def check(self):
        self.permit()

    def check_diagnostics(self):
        """Model-only stops do not authorize more edits or requests, but permit bounded checks."""
        if not diagnostics_allowed(self.stopped):
            raise ExecutionStopped(self.stopped)
        if self.clock() >= self.deadline:
            raise ExecutionStopped('execution_deadline')

    def begin_candidate_closeout(self, *, source_sha256, fallback_source_sha256=None):
        """Seal model work; preserve a pre-existing model stop for bounded diagnostics."""
        self.check_diagnostics()
        if self._candidate_closeout is not None:
            raise ExecutionStopped('candidate_closeout_already_started')
        if fallback_source_sha256 is not None and (
                not isinstance(fallback_source_sha256, str) or not fallback_source_sha256
                or fallback_source_sha256 == source_sha256):
            raise ValueError('candidate fallback must name a distinct prebound source')
        if self.stopped is None:
            self.permit('verification')
            self.set_task_state('BUILD', reason='candidate_readiness_check')
        permit = CandidateCloseout(source_sha256, self.stopped, self._stop_epoch, self.requests,
                                   fallback_source_sha256)
        self._candidate_closeout = permit
        self.emit(dict(event='candidate_closeout_started', source_sha256=source_sha256,
                       original_stop=self.stopped, requests=self.requests, tools=self.tools))
        return permit

    def candidate_closeout_source(self, permit):
        self.check_candidate_closeout(permit)
        return permit.fallback_source_sha256 if self._candidate_closeout_fallback else permit.source_sha256

    def select_candidate_fallback(self, permit, *, source_sha256):
        """One sealed switch to the baseline bound before any candidate probe."""
        self.check_candidate_closeout(permit)
        if (self._candidate_closeout_fallback or permit.fallback_source_sha256 is None
                or source_sha256 != permit.fallback_source_sha256):
            raise ExecutionStopped('candidate_fallback_not_permitted')
        self._candidate_closeout_fallback = True
        self.emit(dict(event='candidate_closeout_fallback', source_sha256=source_sha256,
                       rejected_source_sha256=permit.source_sha256, requests=self.requests, tools=self.tools))

    def check_candidate_closeout(self, permit):
        self.check_diagnostics()
        if (permit is not self._candidate_closeout or permit.stop_epoch != self._stop_epoch
                or permit.stop_reason != self.stopped or permit.requests != self.requests):
            raise ExecutionStopped(self.stopped or 'candidate_closeout_changed')

    def finish_candidate_closeout(self, permit, *, source_sha256, evidence_id):
        self.check_candidate_closeout(permit)
        if source_sha256 != self.candidate_closeout_source(permit):
            raise ExecutionStopped('candidate_source_mismatch')
        receipt = self.verifications.get(evidence_id, {})
        if (receipt.get('closeout_stop_epoch') != permit.stop_epoch or receipt.get('passed') is not True
                or receipt.get('stage') != 'BUILD' or receipt.get('evidence_kind') != 'runtime'
                or receipt.get('source_sha256') != source_sha256 or receipt.get('requirement_ids')):
            raise ExecutionStopped('candidate_readiness_incomplete')
        if self.stopped is None:
            self.finish_candidate(source_sha256=source_sha256, evidence_id=evidence_id)
        else:
            # A separate delivery receipt cannot rewrite STOPPED/TERMINATED,
            # reset quality debt, or manufacture requirement verification.
            self.emit(dict(event='candidate_delivery_closed', source_sha256=source_sha256,
                original_stop=self.stopped, task_state=self.task_state, control_state=self.control_state,
                requirements_verified=False, requests=self.requests, tools=self.tools))

    def cost_snapshot(self):
        return self.requests, self.tools, self.clock()

    def cost_since(self, start, end=None):
        requests, tools, started = start
        final_requests, final_tools, finished = self.cost_snapshot() if end is None else end
        return dict(requests_start=requests, requests_end=final_requests, requests=final_requests - requests,
                    tools_start=tools, tools_end=final_tools, tools=final_tools - tools, seconds=finished - started)

    @contextmanager
    def measure_phase(self, phases, name):
        start = self.cost_snapshot()
        try:
            yield
        finally:
            phases[name] = self.cost_since(start)

    def emit_cost(self, event):
        # Accounting is observational. A failing audit sink must not replace
        # a controller stop, alter a committed outcome, or interrupt rollback.
        try:
            self.emit(event)
        except Exception as error:
            logging.warning('Cost observation unavailable: %s', type(error).__name__)

    def request(self):
        self.permit('request')
        if not self.remaining_physical_requests():
            self.stop('execution_request_limit')
        self.requests += 1
        if self.control_state == 'PROBATION' and self.task_state != 'PLAN':
            self._probation_requests += 1

    def repair_protocol(self):
        """Share correction attempts across plans, steps and later stages."""
        self.check()
        if self.protocol_repairs >= self.limits.protocol_repairs:
            return False
        self.protocol_repairs += 1
        self.emit(dict(event='protocol_repair', count=self.protocol_repairs))
        return True

    def batch(self, calls):
        self.check()
        if len(calls) > self.limits.batch_tools:
            self.stop('tool_batch_limit')
        if self.tools + len(calls) > self.limits.tools:
            self.stop('execution_tool_limit')
        if self.protected_final_tools:
            available = self.limits.tools - self.tools
            if self.external_tool_capacity is not None:
                external, reason = self.external_tool_capacity
                if len(calls) > external:
                    raise ExecutionStopped(reason)
                available = min(available, external)
            if len(calls) + self.protected_final_tools > available:
                raise ExecutionStopped('self_test_reserve')
        self.tools += len(calls)

    def action(self, name, arguments, state):
        self.check()
        signature = hashlib.sha256(json.dumps([name, arguments, state],
            sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        if self.actions[signature] >= self.limits.repeated_actions:
            self.stop('repeated_action')
        self.actions[signature] += 1

    def observe(self, before, after, *, new_evidence=False, verification_pending=False, information_only=False):
        self.check()
        progress = before != after or new_evidence
        self.idle = 0 if progress else self.idle + 1
        if before != after:
            self.states[after] += 1
            if self.states[after] > self.limits.repeated_actions:
                self.stop('source_oscillation')
        self.current_source = after or self.current_source
        if self.policy.enabled:
            # Quiet BUILD/TEST processes are not failed CODE turns. Quality is
            # unknown until actual checks; a write or novel read cannot prove it.
            if self.task_state in {'PLAN', 'CODE'}:
                if progress and self.control_state == 'WARNING' and self.warning_reason == 'activity_stagnation':
                    self.warning_reason, self.warning_age = None, 0
                    self._transition('RUNNING', 'activity_resumed')
                # Necessary context acquisition consumes the hard budgets but
                # is not an attempted implementation or a quality improvement.
                # Do not reset accrued quality debt or a quality warning.
                if (self.task_state == 'CODE' and self.verification_available
                        and not (information_only and new_evidence and before == after)):
                    self.quality_idle += 1
                if self.idle >= self.limits.idle_turns:
                    self._warn('activity_stagnation')
                # Retained quality debt alone does not consume the new CODE/TEST
                # recovery attempt. A failed test still trips it.
                elif (self.task_state == 'CODE' and self.control_state != 'PROBATION'
                      and self.verification_available
                      and self.quality_idle >= self.policy.quality_window
                      and not verification_pending):
                    self._warn('quality_stagnation' if self.quality_known else 'quality_unknown')
                elif self.control_state == 'WARNING' and not verification_pending:
                    self.warning_age += 1
                    if self.warning_age >= self.policy.warning_turns:
                        self._trip(self.warning_reason)
            return
        if self.idle >= self.limits.idle_turns:
            self.stop('no_progress')

    def check_planning_resources(self):
        self.check()
        if self.remaining_physical_requests() <= 0:
            self.stop('execution_request_limit')
        if self.tools >= self.limits.tools:
            self.stop('execution_tool_limit')

    def replan(self, *, committed_progress=()):
        """Admit planning without erasing cumulative attempts or resource use.

        Only PlanExecutor supplies these already committed, source-changing
        receipts. A new step ID, rolled-back edit, repeated requirement or
        previously credited source does not create another productive attempt.
        """
        self.check_planning_resources()
        new_refs, new_sources, full_sources = set(), set(), set()
        if self.policy.enabled:
            for receipt in committed_progress:
                refs = receipt.get('requirement_ids')
                source = receipt.get('implementation_source_sha256')
                full_source = receipt.get('source_sha256')
                if ('slice_parent' in receipt or receipt.get('source_changed') is not True or not isinstance(refs, (list, tuple))
                        or not refs or any(not isinstance(ref, str) or not ref for ref in refs)
                        or not isinstance(source, str) or not source
                        or not isinstance(full_source, str) or not full_source
                        or source in self._replan_progress_sources or source in new_sources):
                    continue
                unseen = set(refs) - self._replan_progress_requirements - new_refs
                if unseen:
                    new_refs.update(unseen)
                    new_sources.add(source)
                    full_sources.add(full_source)
        productive = bool(new_refs and new_sources)
        used = self.replans - self.productive_replans if self.policy.enabled else self.replans
        if not productive and used >= self.limits.replans:
            self.stop('replan_limit')
        self.replans += 1
        if productive:
            self.productive_replans += 1
            self._replan_progress_requirements.update(new_refs)
            self._replan_progress_sources.update(new_sources)
        self.emit(dict(event='replan_admitted', replans=self.replans,
            productive_replans=self.productive_replans,
            unproductive_replans=self.replans - self.productive_replans,
            productive=productive, new_requirement_ids=sorted(new_refs),
            committed_source_sha256s=sorted(full_sources), progress_source_sha256s=sorted(new_sources)))

    def source(self, root, *, changed_by_controller=False):
        root = Path(root).resolve()
        files = snapshot_project(root)
        digest = source_digest(files)
        previous = self.expected_sources.get(str(root))
        if previous is not None and previous != digest and not changed_by_controller:
            self.stop('concurrent_source_change')
        self.expected_sources[str(root)] = digest
        self.current_source = digest
        return files


class SourceTransaction:
    """In-memory checkpoint under the existing bounded, single-writer file policy."""
    def __init__(self, tools, control):
        self.tools, self.control = tools, control
        self.root = Path(tools.project_dir)
        self.before = control.source(self.root)
        self.finished = False

    def implementation_changed(self):
        from .task_bundle import is_test_path
        current = self.control.source(self.root)
        return any(current.get(path) != self.before.get(path) for path in current.keys() | self.before.keys()
                   if not path.casefold().startswith(('docs/', 'requirements/')) and not is_test_path(path))

    def quarantine(self, save, *, stop_reason=None, **identity):
        files = self.control.source(self.root)
        if stop_reason in {'truncated', SOURCE_CLAIMS_DRAFT}:
            # Coverage claims alone cannot turn the untouched starter into a candidate.
            from .coverage_store import LEDGER_PATH
            paths = (files.keys() | self.before.keys()) - {LEDGER_PATH}
            if stop_reason == SOURCE_CLAIMS_DRAFT:
                from .task_bundle import is_test_path
                paths = (name for name in paths if not name.casefold().startswith(('docs/', 'requirements/'))
                         and not is_test_path(name))
            if not any(files.get(name) != self.before.get(name) for name in paths):
                return {}
        try:
            location = save()
        except ExecutionStopped:
            raise
        except Exception as error:
            if stop_reason:
                raise RuntimeError(f'{stop_reason} candidate capture failed: {error}') from error
            raise
        if stop_reason:
            identity['candidate_stop_reason'] = stop_reason
        return dict(candidate_source=location, candidate_source_sha256=source_digest(files),
                    candidate_base_sha256=source_digest(self.before), candidate_acceptance='unverified', **identity)

    def commit(self):
        self.control.check()
        self.committed_files = self.control.source(self.root)
        digest = source_digest(self.committed_files)
        self.finished = True
        return digest

    def rollback(self):
        if self.finished:
            return
        if self.control.stopped == 'concurrent_source_change':
            # An uncoordinated writer owns the new bytes; never overwrite them.
            raise ExecutionStopped('concurrent_source_change')
        try:
            current = self.control.source(self.root)
            if current != self.before:
                apply_snapshot(self.root, self.before, before=current)
                self.tools.source_revision = getattr(self.tools, 'source_revision', 0) + 1
            if snapshot_project(self.root) != self.before:
                raise RuntimeError('restored source differs from checkpoint')
            self.control.source(self.root, changed_by_controller=True)
            if hasattr(self.tools, 'unseal'):
                self.tools.unseal()
            self.finished = True
            self.control.emit(dict(event='source_rollback', source_sha256=source_digest(self.before)))
        except ExecutionStopped:
            raise
        except BaseException:
            self.control.stop('rollback_failed')


@dataclass(frozen=True)
class PlanStep:
    id: str
    goal: str
    requirement_ids: tuple[str, ...]
    depends_on: tuple[str, ...]
    acceptance: str


def _plan_id_preview(value):
    """Bound escaped diagnostic text without restricting the identifier itself."""
    preview = value[:80]
    while len(json.dumps(preview, ensure_ascii=True)) > 82:
        preview = preview[:-1]
    rendered = json.dumps(preview, ensure_ascii=True)
    return rendered if preview == value else rendered + ' (truncated)'


def _refits_deferred_scope(step, deferred):
    return len(step.requirement_ids) < len(deferred) or deferred.isdisjoint(step.requirement_ids)


def validate_plan(document, requirement_ids, limit, requirement_dependencies=None, *, exhausted_scope=(), deferred_scope=()):
    """Reject malformed plans, invented requirements, cycles and omitted goals."""
    expected = set(requirement_ids)
    rows = document.get('steps') if isinstance(document, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= limit:
        raise ValueError('plan must contain a bounded nonempty steps list')
    steps, covered = {}, set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'id', 'goal', 'requirement_ids', 'depends_on', 'acceptance'}:
            raise ValueError('plan step must have exactly id, goal, requirement_ids, depends_on, acceptance; no other fields')
        for field, maximum in (('id', 80), ('goal', 2000), ('acceptance', 2000)):
            if not isinstance(row[field], str) or not row[field].strip() or len(row[field]) > maximum:
                raise ValueError(f'invalid plan {field}')
        for field in ('requirement_ids', 'depends_on'):
            values = row[field]
            if (not isinstance(values, list) or len(values) > max(limit, len(expected))
                    or any(not isinstance(item, str) or not item for item in values)
                    or len(set(values)) != len(values)):
                raise ValueError(f'invalid plan {field}')
        identity = row['id']
        if identity in steps or identity in row['depends_on']:
            raise ValueError('duplicate or self-dependent step')
        if not set(row['requirement_ids']) <= expected:
            raise ValueError('plan invents requirement identifiers')
        if expected and not row['requirement_ids']:
            raise ValueError('step is not grounded in a requirement')
        covered.update(row['requirement_ids'])
        steps[identity] = PlanStep(identity, row['goal'], tuple(row['requirement_ids']),
                                   tuple(row['depends_on']), row['acceptance'])
    if covered != expected:
        raise ValueError('plan omits requirements')
    deferred = set(deferred_scope)
    if deferred and not any(not step.depends_on and _refits_deferred_scope(step, deferred)
                            for step in steps.values()):
        raise ValueError('plan repeats the deferred requirement scope; provide a smaller or independent ready step '
                         'for the current request window and retain all other work')
    for step_index, step in enumerate(steps.values()):
        unknown = [(index, ref) for index, ref in enumerate(step.depends_on) if ref not in steps]
        if unknown:
            dependency_index, ref = unknown[0]
            sample = ', '.join(_plan_id_preview(identity) for identity in list(steps)[:4])
            detail = (
                'plan references an unknown dependency: '
                f'steps[{step_index}].depends_on[{dependency_index}] must name an id defined in this returned steps array. '
                'Requirement IDs and completed_steps IDs are valid only if also defined in the current steps. '
                f'step_id={_plan_id_preview(step.id)}; unknown_dependency={_plan_id_preview(ref)}; '
                f'unknown_dependency_count={len(unknown)}; current_step_ids=[{sample}]; '
                f'total_current_steps={len(steps)}; omitted_current_step_ids={max(0, len(steps) - 4)}')
            raise ValueError(detail[:1000])
    ordered, done = [], set()
    while len(done) < len(steps):
        ready = [step for step in steps.values() if step.id not in done and set(step.depends_on) <= done]
        if not ready:
            raise ValueError('cyclic plan')
        for step in ready:
            ordered.append(step)
            done.add(step.id)
    # Explicit requirement prerequisites must be in this step or an ancestor.
    # Completed/out-of-scope requirements are not invented as new plan nodes.
    ancestors = {}
    exhausted = set(exhausted_scope)
    for step in ordered:
        reachable = set(step.depends_on)
        for dependency in step.depends_on:
            reachable.update(ancestors[dependency])
        ancestors[step.id] = reachable
        prior = {ref for identity in reachable for ref in steps[identity].requirement_ids}
        available = set(step.requirement_ids) | prior
        if len(exhausted) > 1 and exhausted <= set(step.requirement_ids):
            if not exhausted & prior:
                raise ValueError('plan repeats exhausted scope; complete a smaller prerequisite step first')
        for ref in step.requirement_ids:
            required = set((requirement_dependencies or {}).get(ref, ())) & expected
            if not required <= available:
                raise ValueError('plan omits declared requirement dependency: ' + ref)
    return ordered


@dataclass
class StepOutcome:
    """Separate model completion from controller evidence."""
    execution: dict
    static_validation: dict | None = None
    behavior_validation: dict | None = None
    closed_by_controller: bool = False

    @property
    def validation(self):
        if self.behavior_validation is not None:
            return self.behavior_validation
        if self.static_validation is not None:
            return self.static_validation
        reported = self.execution.get('validation')
        return reported if isinstance(reported, dict) else {'passed': False}


class PlanExecutor:
    """Sequential plan/execute/replan. Successful steps survive a later failure."""
    def __init__(self, control, tools, planner, executor, validator, behavior_validator=None,
                 *, before_closeout=None, budget_planner=None, repairer=None, phase_deadline=None, tools_remaining=None,
                 quarantine_source=None, requests_remaining=None, repair_request_limit=None, adaptive_progress=None,
                 can_fund_repair=None, has_independent_followup=None):
        self.control, self.tools = control, tools
        self.planner, self.executor, self.validator = planner, executor, validator
        self.behavior_validator = behavior_validator
        self.before_closeout = before_closeout
        self.budget_planner = budget_planner
        self.repairer = repairer
        self.quarantine_source = quarantine_source or getattr(behavior_validator, 'quarantine', None)
        self.phase_deadline = phase_deadline
        self.tools_remaining = tools_remaining
        self.requests_remaining = requests_remaining
        self.repair_request_limit = repair_request_limit
        self.can_fund_repair = can_fund_repair
        self.has_independent_followup = has_independent_followup
        self.adaptive_progress = (control.policy.enabled and (adaptive_progress if adaptive_progress is not None
                                  else not getattr(budget_planner, 'fixed_order', False)))

    def _preflight_pending(self, steps, rows, done):
        """Evaluate plan prerequisites and public requirement contracts separately."""
        runnable, fully_bound = [], []
        for step, row in zip(steps, rows):
            check = self.behavior_validator.contract_preflight(step)
            check['pending_step_dependencies'] = [ref for ref in step.depends_on if ref not in done]
            row['verification_preflight'] = check
            if not check['pending_step_dependencies'] and check['runnable_requirement_ids']:
                runnable.append(step.id)
                if not check['unresolved'] and not check['blocked_dependencies']:
                    fully_bound.append(step.id)
        return runnable, fully_bound

    def _split_initial_step(self, step, tail, *, requests_left, implementation_reserve):
        """Allow one batch of reads before the final partition, within the original step ceiling."""
        plan_allowance = min(2, request_horizon(self.control.limits.plan_requests),
                             requests_left - implementation_reserve)
        if plan_allowance < 1:
            return None
        reserved_ids = {step.id, *(item.id for item in tail)}
        refs = list(step.requirement_ids)
        max_steps = self.control.limits.plan_steps - len(tail)
        feedback = dict(reason='initial_step_empty_truncation', step=step.id, requirement_ids=refs,
            goal=step.goal, acceptance=step.acceptance, reserved_step_ids=sorted(reserved_ids),
            max_steps=max_steps, step_request_limit=reported_requests(requests_left - plan_allowance))
        document = self.planner(refs, [], feedback, plan_allowance)
        if document.get('status') != 'complete':
            reason = document.get('reason') or 'planning_failed'
            if reason in PHASE_BUDGET_STOPS:
                raise ExecutionStopped(reason)
            if reason in ALLOCATION_STOP_REASONS | SOURCE_SAFETY_STOPS | CONTROLLER_STATE_STOPS | {
                    'usage_unknown', 'usage_unknown_or_out_of_bounds', 'request_usage_unknown',
                    'budget_exhausted', 'total_token_budget'}:
                self.control.stop(reason)
            self.control.emit(dict(event='initial_step_split_declined', step=step.id, reason=reason))
            return None
        try:
            children = validate_plan(document, refs, max_steps)
            child_ids = tuple(child.id for child in children)
            if reserved_ids.intersection(child_ids):
                raise ValueError('split reuses an existing step id')
            if len(children) < 2 or sum(len(child.requirement_ids) for child in children) != len(refs):
                raise ValueError('split must partition the original scope into smaller steps')
        except ValueError as error:
            self.control.emit(dict(event='initial_step_split_declined', step=step.id,
                reason='invalid_plan', detail=str(error)[:1000]))
            return None
        updated_tail = [replace(item, depends_on=tuple(ref for parent in item.depends_on
            for ref in (child_ids if parent == step.id else (parent,)))) for item in tail]
        return child_ids, [*children, *updated_tail]

    def run(self, requirement_ids, allowance, feedback=None, *, whole_task=False):
        from .platform_budget_policy import BudgetPlanner
        renewal_policy = self.budget_planner if callable(getattr(self.budget_planner, 'renew', None)) else BudgetPlanner()
        self.control.retained_requirement_ids = self.control.prior_retained_requirement_ids
        started = self.control.requests
        work_started = self.control.work_requests
        phase_ceiling = work_started + request_horizon(allowance)
        phase_refs = list(dict.fromkeys(requirement_ids))
        pending = list(phase_refs)
        completed, constructed, history = [], [], []
        local_failures = []
        blocked_requirements = set()
        failed = None
        candidate = {}
        truncated_refs = set()
        slice_parents = set()
        slice_children = {}
        slice_ceilings = {}
        next_slice = None
        awaiting_alternative = False
        refit_step_ids = set()
        budget_scope = uuid4().hex
        previous_final_tools = self.control.protected_final_tools
        strict_preflight = (getattr(self.behavior_validator, 'retain_unverified', None) is False
                            and callable(getattr(self.behavior_validator, 'contract_preflight', None)))

        def remaining():
            phase = phase_ceiling - self.control.work_requests
            return min(phase, self.requests_remaining()) if self.requests_remaining is not None else phase

        def observation_key(step):
            return f'{budget_scope}:{len(history)}:{step.id}'

        def cost_profile(step):
            profile = getattr(self.behavior_validator, 'cost_profile', None)
            if callable(profile):
                return ('behavior', *profile(step))
            return ('custom_behavior' if self.behavior_validator else 'static', len(step.requirement_ids),
                    minimum_requests(step))

        def finish_attempt(step, outcome, start, end, phases, disposition, stop_reason):
            attempts = plan[index].setdefault('attempts', [])
            cost = dict(self.control.cost_since(start, end), run=budget_scope, revision=len(history),
                        step=step.id, attempt=len(attempts) + 1, disposition=disposition, phases=phases)
            if stop_reason:
                cost['stop_reason'] = stop_reason
            attempts.append(dict(outcome, cost=cost))
            self.control.emit_cost(dict(event='attempt_cost', **cost))

        def minimum_requests(step):
            reserve = getattr(self.behavior_validator, 'request_reserve', None)
            return 1 + (reserve(step) if callable(reserve) else 0)

        def step_capacity(*, planning_requests=0, request_ceiling=None):
            remaining_requests = min(remaining(), self.control.remaining_physical_requests())
            if request_ceiling is not None:
                remaining_requests = min(remaining_requests, request_ceiling - self.control.work_requests)
            ceiling = min(request_horizon(self.control.limits.step_requests), max(0, remaining_requests - planning_requests))
            return self.control.soft_request_allowance(ceiling, stage='CODE')

        def isolatable(failure):
            if (not self.adaptive_progress or self.control.stopped or truncated_refs or refit_step_ids
                    or step.id in slice_ceilings
                    or self.control.control_state not in {'RUNNING', 'WARNING'}):
                return False
            reason = failure.get('reason')
            if reason in (ALLOCATION_STOP_REASONS | PHASE_BUDGET_STOPS | SOURCE_SAFETY_STOPS
                          | CONTROLLER_STATE_STOPS | {'usage_unknown', 'usage_unknown_or_out_of_bounds', 'request_usage_unknown',
                                                      'budget_exhausted', 'total_token_budget'}):
                return False
            checked = failure.get('validation') or {}
            return (reason in {'request_allowance', 'inspection_limit', 'context_limit',
                               'static_validation_failed', 'step_behavior_unverified',
                               'requirement_constraints_unresolved', 'behavior_requirements_missing'}
                    or checked.get('failure_kind') == 'behavior')

        def result(status, reason=None, **context):
            if failed is not None and reason in CONTROLLER_STATE_STOPS:
                context.setdefault('detail', failed.get('detail', ''))
            if reason == 'verification_blocked' and failed and failed.get('phase') == 'verification_preflight':
                context.update(phase=failed['phase'], detail=failed['reason'])
            if reason == 'truncated' and candidate.get('candidate_stop_reason') != reason:
                candidate.clear()  # A previous failure's draft is not this interrupted step.
            if (reason in SOURCE_CLAIMS_DRAFT_STOPS and failed
                    and failed.get('candidate_stop_reason') == SOURCE_CLAIMS_DRAFT
                    and failed.get('candidate_base_sha256') == self.control.current_source):
                candidate.update((key, value) for key, value in failed.items() if key.startswith('candidate_'))
            if failed is not None:
                context.setdefault('triggering_failure', failed)
            return dict(status=status, reason=reason, turns=self.control.requests - started,
                        work_turns=self.control.work_requests - work_started,
                        completed_steps=completed, candidate_steps=constructed, plan_history=history,
                        pending_requirement_ids=pending,
                        local_failures=list(local_failures),
                        blocked_requirement_ids=sorted(blocked_requirements.intersection(pending)),
                        replans=self.control.replans, productive_replans=self.control.productive_replans,
                        unproductive_replans=self.control.replans - self.control.productive_replans,
                        phase_requests_remaining=reported_requests(max(0, remaining())),
                        global_requests_remaining=reported_requests(self.control.remaining_physical_requests()),
                        acceptance='synthetic_step_diagnostics' if self.behavior_validator else 'static_only',
                        task_state=self.control.task_state, control_state=self.control.control_state, **candidate, **context)

        def quarantine(checkpoint, **identity):
            receipt = checkpoint.quarantine(self.quarantine_source, **identity)
            candidate.update(receipt)
            return receipt

        def pending_work():
            return list(dict.fromkeys(ref for row in plan
                if row['status'] not in {'completed', 'constructed_unverified'} and 'replacement_step_ids' not in row
                for ref in row['requirement_ids'])) + deferred

        adaptive_slices = (self.control.policy.enabled and self.budget_planner is not None
                    and not getattr(self.budget_planner, 'fixed_order', False)
                    and getattr(self.behavior_validator, 'retain_unverified', False) is True
                    and callable(getattr(self.behavior_validator, 'public_step_slice', None)))

        def may_slice(parent):
            return (adaptive_slices
                    and (parent.id, parent.requirement_ids) not in slice_parents and parent.id not in slice_children
                    and len(steps) < self.control.limits.plan_steps)

        def insert_slice(parent, position, capacity, reason, runway=None):
            nonlocal next_slice
            if not may_slice(parent) or capacity < 2:
                return False
            slice_parents.add((parent.id, parent.requirement_ids))
            ids = {s.id for s in steps}
            number = 1
            while (child_id := f'{parent.id[:64]}-slice-{number}') in ids:
                number += 1
            ceiling = self.control.work_requests + capacity
            self.control.plan_progress = None
            prepared = self.behavior_validator.public_step_slice(
                parent, self.control.retained_requirement_ids, child_id, allowance=capacity)
            if prepared is None:
                return False
            child, scenario = prepared
            residual = replace(parent, depends_on=(*parent.depends_on, child.id))
            steps[position:position+1] = [child, residual]
            row = plan[position]
            row.update(depends_on=list(residual.depends_on), status='pending', slice_child=child.id)
            plan.insert(position, dict(id=child.id, goal=child.goal, acceptance=child.acceptance,
                requirement_ids=list(child.requirement_ids), depends_on=list(child.depends_on),
                status='pending', slice_parent=parent.id, scenario_id=scenario))
            slice_children[child.id] = (parent.id, scenario)
            slice_ceilings[parent.id] = slice_ceilings[child.id] = ceiling
            next_slice = child.id
            self.control.emit(dict(event='public_step_slice', parent_step=parent.id, step=child.id,
                requirement_ids=list(child.requirement_ids), scenario_id=scenario, reason=reason, runway=runway))
            return True

        def scheduling_stop(reason, **context):
            if truncated_refs or refit_step_ids:
                # Seal generation before closeout so an unmet step cannot reappear
                # in a new plan or a whole-task static repair. Recheck the active baseline.
                self.control.stop('truncated')
            return result('stopped', reason, **context)

        try:
            while True:
                self.control.plan_progress = None
                self.control.check()
                self.control.set_task_state('PLAN', reason='plan_requested')
                if strict_preflight and self.budget_planner is not None:
                    reserve = getattr(self.behavior_validator, 'retained_closeout_tools', None)
                    if callable(reserve):
                        self.control.protected_final_tools = reserve(pending)
                if remaining() < 2:
                    return result('stopped', 'request_allowance')
                planned_window = step_capacity(planning_requests=1)
                deferred_scope = set(failed['requirement_ids']) if failed and failed['reason'] == 'step_window_changed' else set()
                planning_start = self.control.cost_snapshot()
                try:
                    document = self.planner(pending, completed + constructed, feedback,
                        min(request_horizon(self.control.limits.plan_requests), remaining() - 1))
                    if document.get('status') != 'complete':
                        return result('stopped', document.get('reason', 'planning_failed'),
                                      phase='planning', detail=document.get('detail', ''))
                    try:
                        # The production planner attaches this scope after validating its response.
                        # Requirements outside it stay in the task until a later window handles them.
                        window = document.get('plan_window_requirement_ids', pending)
                        if (not isinstance(window, list) or (pending and not window)
                                or any(not isinstance(ref, str) for ref in window)
                                or len(set(window)) != len(window) or not set(window).issubset(pending)):
                            raise ValueError('invalid planning window')
                        window_set = set(window)
                        deferred = [ref for ref in pending if ref not in window_set]
                        steps = validate_plan(document, window, self.control.limits.plan_steps,
                            exhausted_scope=failed['requirement_ids'] if failed and failed['reason'] == 'request_allowance' else (),
                            deferred_scope=deferred_scope)
                    except ValueError as error:
                        return result('stopped', 'invalid_plan', phase='planning', detail=str(error)[:1000])
                finally:
                    self.control.emit_cost(dict(event='planning_cost', run=budget_scope, revision=len(history)+1,
                        purpose='replan' if history else 'plan', **self.control.cost_since(planning_start)))
                plan = [dict(id=s.id, goal=s.goal, requirement_ids=list(s.requirement_ids),
                             depends_on=list(s.depends_on), acceptance=s.acceptance,
                             status='pending') for s in steps]
                if strict_preflight:
                    propose = getattr(self.behavior_validator, 'prepare_semantic_proposal', None)
                    if callable(propose):
                        propose(window, max(0, remaining() - 1))
                history.append(plan)
                self.control.emit(dict(event='plan_validated', steps=plan))
                if self.control.policy.enabled and self.control.control_state != 'PROBATION':
                    self.control.record_verification(stage='PLAN', passed=True,
                        source_sha256=source_digest(self.control.source(self.tools.project_dir)),
                        evidence_id='plan-' + str(len(history)))
                failed = None
                revision_failures = []
                refit_step_ids.clear()
                refit_ceiling = None
                slice_children.clear()
                slice_ceilings.clear()
                next_slice = None
                index = 0
                while index < len(steps):
                    step = steps[index]
                    # Construction makes source available for an actual setup check.
                    # A scenario slice alone never supplies its whole parent requirement.
                    self.control.retained_requirement_ids = self.control.prior_retained_requirement_ids | frozenset(
                        ref for row in completed + constructed if 'slice_parent' not in row
                        for ref in row['requirement_ids'])
                    # Model step IDs are local to this validated revision.
                    done = [item['id'] for item in plan[:index]
                            if item['status'] in {'completed', 'constructed_unverified'}]
                    step_ceiling = refit_ceiling if refit_step_ids else None
                    request_share = None
                    selected_id = step.id
                    ready = [item.id for item in steps[index:] if set(item.depends_on) <= set(done)
                             and truncated_refs.isdisjoint(item.requirement_ids)
                             and (not refit_step_ids or item.id in refit_step_ids)]
                    if revision_failures and not ready and not truncated_refs and not refit_step_ids:
                        for row in plan[index:]:
                            missing = sorted(set(row['depends_on']) - set(done))
                            row.update(status='blocked', blocked_dependencies=missing)
                            blocked_requirements.update(row['requirement_ids'])
                            self.control.emit(dict(event='plan_step_blocked', step=row['id'],
                                requirement_ids=row['requirement_ids'], blocked_dependencies=missing))
                        pending = pending_work()
                        break
                    if revision_failures and selected_id not in ready and ready:
                        selected_id = ready[0]
                    if truncated_refs and not ready:
                        return scheduling_stop('coverage_incomplete')
                    preferred = []
                    if strict_preflight:
                        self.control.check()
                        ready, preferred = self._preflight_pending(steps[index:], plan[index:], set(done))
                        if not ready:
                            reason = 'requirement_dependencies_pending'
                            for row in plan[index:]:
                                check = row['verification_preflight']
                                if check['unresolved'] and not check['pending_step_dependencies']:
                                    reason = 'requirement_constraints_unresolved'
                                    row['status'] = 'blocked_before_implementation'
                                else:
                                    row['status'] = 'waiting_for_dependencies'
                                row['verification_status'] = 'BLOCKED'
                            failed = dict(phase='verification_preflight', reason=reason)
                            self.control.emit(dict(event='verification_preflight_blocked', reason=reason,
                                                   pending_requirement_ids=pending))
                            self.control.stop('verification_blocked')
                    if strict_preflight or self.budget_planner is not None:
                        capacity = step_capacity(request_ceiling=step_ceiling)
                        ready = [item.id for item in steps[index:]
                                 if item.id in ready and minimum_requests(item) <= min(capacity,
                                     slice_ceilings.get(item.id, phase_ceiling) - self.control.work_requests)
                                 and (index != 0 or not deferred_scope or _refits_deferred_scope(item, deferred_scope))]
                        if not ready:
                            return scheduling_stop('budget_insufficient')
                        preferred = [ref for ref in ready if ref in preferred]
                        selected_id = (preferred or ready)[0]
                    if self.budget_planner is not None:
                        actions = []
                        profiles = {}
                        time_pressure = {}
                        deadline = min(self.control.deadline, self.phase_deadline()) if self.phase_deadline else self.control.deadline
                        review_scope = (self.control.policy.enabled and not getattr(self.budget_planner, 'fixed_order', False)
                                        and self.control.control_state in {'WARNING', 'PROBATION'}
                                        and capacity < planned_window)
                        for item in steps[index:]:
                            action = dict(id=item.id, dependencies=list(item.depends_on),
                                observation_key=observation_key(item), minimum_requests=minimum_requests(item),
                                requirement_ids=list(item.requirement_ids), estimated_cost=self.control.limits.step_requests or minimum_requests(item),
                                request_ceiling=capacity, expected_gain=float(len(item.requirement_ids)),
                                confidence=.25, risk=0., required=True)
                            if item.id in ready:
                                profiles[item.id] = cost_profile(item)
                                sample = self.budget_planner.estimate_cost(profiles[item.id])
                                selected_probe = getattr(self.behavior_validator, 'selected_scenario_probe', None)
                                probe = selected_probe(item) if callable(selected_probe) else None
                                scenario_id = probe.get('scenario_id') if isinstance(probe, dict) else None
                                optional_observer = getattr(self.behavior_validator, 'can_schedule_observation', None)
                                if ((strict_preflight or probe or (callable(optional_observer) and optional_observer(item, capacity)))
                                        and callable(getattr(self.behavior_validator, 'step_budget', None))):
                                    action.update(self.behavior_validator.step_budget(item))
                                action.update(sample)
                                if (adaptive_slices and item.id not in slice_children and (completed or constructed)
                                        and callable(getattr(self.behavior_validator, 'step_slice_runway', None))):
                                    request_seconds, check_seconds = self.behavior_validator.step_slice_runway(item)
                                    runway = self.budget_planner.step_runway(profiles[item.id],
                                        remaining_seconds=max(0, deadline - self.control.clock()),
                                        request_seconds=request_seconds, check_seconds=check_seconds)
                                    if runway['fits'] is False:
                                        time_pressure[item.id] = runway
                                        action['estimated_seconds'] = max(action.get('estimated_seconds') or 0,
                                                                          runway['required_seconds'])
                                if review_scope:
                                    origin = sample.get('estimate_source')
                                    closed = (isinstance(origin, dict)
                                              and origin.get('requirement_ids') == sorted(item.requirement_ids)
                                              and origin.get('scenario_id') == scenario_id
                                              and origin.get('disposition') in {'committed', 'constructed_unverified'})
                                    # A measured closure can support a grouped step. With no such
                                    # sample prefer a ready smaller unit, or ask the planner to refit.
                                    action['review_scope'] = (len(item.requirement_ids) > 1 and
                                                              (not closed or sample['estimated_cost'] > capacity))
                                action['scenario_id'] = scenario_id
                            actions.append(action)
                        tools_left = self.control.limits.tools - self.control.tools
                        if self.tools_remaining is not None:
                            tools_left = min(tools_left, self.tools_remaining())
                        resources = dict(requests=min(remaining(), self.control.remaining_physical_requests()),
                                         hard_requests=self.control.remaining_physical_requests(),
                                         tools=tools_left, seconds=max(0, deadline - self.control.clock()))
                        resources['can_replan_scope'] = (not refit_step_ids
                                                         and self.control.replans < self.control.limits.replans
                                                         and resources['requests'] >= 2)
                        # A partially checkable step cannot commit under strict policy. Run
                        # an affordable, fully checkable independent step first when possible.
                        first = [next_slice] if next_slice in ready else preferred or ready
                        decision = self.budget_planner.select(actions, remaining=resources, completed=done, allowed=first)
                        if decision['action_id'] is None and first != ready:
                            decision = self.budget_planner.select(actions, remaining=resources, completed=done, allowed=ready)
                        eligible_score = next((r['score'] for r in decision['trace']
                                               if r['action_id'] == decision['action_id']), float('-inf'))
                        constrained = sorted((r for r in decision['trace'] if r['action_id'] in time_pressure
                                              and r['limiting_resource'] == 'seconds' and r['score'] >= eligible_score),
                                             key=lambda r: -r['score'])
                        reconsider = False
                        for row in constrained:
                            if next_slice:
                                break
                            position = next(i for i in range(index, len(steps)) if steps[i].id == row['action_id'])
                            parent = steps[position]
                            started_requests = self.control.requests
                            inserted = insert_slice(parent, position, capacity, 'implementation_time', time_pressure[parent.id])
                            if inserted or self.control.requests != started_requests:
                                # A declined proposal may still have spent a request and wall time.
                                reconsider = True
                                break
                        if reconsider:
                            continue
                        self.control.emit(dict(event='budget_decision', planned_step_window=reported_requests(planned_window),
                                               step_request_limit=reported_requests(capacity), **decision))
                        if decision['action_id'] is None:
                            if decision.get('reason') == 'step_window_changed':
                                if truncated_refs:
                                    return scheduling_stop('coverage_incomplete')
                                deferred_row = next(row for row in plan[index:] if row['id'] == decision['deferred_action_id'])
                                deferred_row['status'] = 'deferred_for_replan'
                                failed = dict(step=deferred_row['id'], reason='step_window_changed',
                                              requirement_ids=deferred_row['requirement_ids'], goal=deferred_row['goal'],
                                              acceptance=deferred_row['acceptance'], depends_on=deferred_row['depends_on'],
                                              planned_step_window=reported_requests(planned_window), step_request_limit=reported_requests(capacity),
                                              control_state=self.control.control_state, validation={})
                                deferred_row['scope_review'] = failed
                                self.control.emit(dict(event='step_scope_review', **failed))
                                break
                            cost_budget_exhausted = any(row['limiting_resource'] in {'tools', 'seconds'}
                                for row in decision['trace'] if row['action_id'] in ready)
                            return scheduling_stop(decision.get('reason') or 'budget_insufficient',
                                          cost_budget_exhausted=cost_budget_exhausted)
                        selected_id = decision['action_id']
                        next_slice = None
                        request_share = decision['request_share']
                        chosen = next(row for row in actions if row['id'] == selected_id)
                        if 'protected_closeout' in chosen:
                            self.control.protected_final_tools = chosen['protected_closeout']['tools']
                    if selected_id != step.id:
                        selected = next(pos for pos in range(index, len(steps)) if steps[pos].id == selected_id)
                        steps[index], steps[selected] = steps[selected], steps[index]
                        plan[index], plan[selected] = plan[selected], plan[index]
                        step = steps[index]
                    pending = pending_work()
                    self.control.check()
                    if remaining() <= 0:
                        return scheduling_stop('request_allowance')
                    if step.id in slice_ceilings:
                        step_ceiling = min(step_ceiling or phase_ceiling, slice_ceilings[step.id])
                    step_allowance = step_capacity(request_ceiling=step_ceiling)
                    if request_share is not None and self.control.limits.step_requests is not None:
                        step_allowance = min(step_allowance, max(0, int(request_share)))
                    if step_allowance < minimum_requests(step):
                        return scheduling_stop('budget_insufficient')
                    can_alternate = (self.control.policy.enabled and self.budget_planner is not None
                        and getattr(self.behavior_validator, 'retain_unverified', False) is True)
                    independent = any(item.id in ready and item.id != step.id
                        and set(step.requirement_ids).isdisjoint(item.requirement_ids) for item in steps[index+1:])
                    can_split_initial = (len(step.requirement_ids) > 1 and len(steps) < self.control.limits.plan_steps
                        and self.control.replans < self.control.limits.replans and step_allowance >= 4)
                    self.control.plan_progress = dict(
                        revision=len(history), current_step=step.id,
                        can_isolate_truncation=bool((completed or constructed) and can_alternate and len(ready) > 1),
                        can_refit_public_slice=may_slice(step) and step_allowance >= 2,
                        can_alternate_initial_empty_step=bool(index == 0 and len(history) == 1 and can_alternate
                            and not self.control.initial_empty_step_alternated and (independent or can_split_initial)),
                        request_ceiling=reported_requests(min(phase_ceiling, step_ceiling) if step_ceiling is not None else phase_ceiling),
                        phase_requirement_ids=list(phase_refs),
                        whole_task=whole_task is True,
                        remaining_steps=sum('replacement_step_ids' not in row for row in plan) - len(done),
                        remaining_requirement_count=len(pending),
                        completed_step_ids=done,
                        unverified_step_ids=[item['id'] for item in plan[:index]
                                             if item['status'] == 'constructed_unverified'],
                        unresolved_steps=[row['id'] for row in plan[:index] if row['status'] not in {
                            'completed', 'constructed_unverified'} and 'replacement_step_ids' not in row],
                        remaining_steps_summary=[dict(id=row['id'], requirement_ids=list(row['requirement_ids']),
                            depends_on=list(row['depends_on']), status=row['status']) for row in plan
                            if row['status'] not in {'completed', 'constructed_unverified'}
                            and 'replacement_step_ids' not in row])
                    if deferred:
                        self.control.plan_progress['plan_window_requirement_ids'] = window
                    if ('plan_window_requirement_ids' in document or refit_step_ids
                            or (awaiting_alternative and self.control.initial_empty_step_alternated)):
                        self.control.plan_progress['compact_source_claims'] = True
                    if awaiting_alternative:
                        # The previous draft remains in its own transaction's history.
                        # It cannot be offered as the current step's source on failure.
                        candidate.clear()
                        awaiting_alternative = False
                    plan[index]['status'] = 'running'
                    plan[index]['initial_request_allowance'] = reported_requests(step_allowance)
                    attempt_start = self.control.cost_snapshot()
                    step_start = attempt_start
                    step_work_started = self.control.work_requests
                    checkpoint = None
                    disposition = 'interrupted'
                    phases = {}
                    outcome = StepOutcome({})
                    eligible = False
                    initial_empty = False
                    public_truncation = False
                    behavior_validation_active = False
                    verification_interruption = None
                    try:
                        capability = getattr(self.behavior_validator, 'verification_available', None)
                        self.control.verification_available = (capability(step) is not False) if callable(capability) else True
                        self.control.emit(dict(event='step_verification_capability', step=step.id,
                                               available=self.control.verification_available))
                        checkpoint = SourceTransaction(self.tools, self.control)
                        self.control.set_task_state('CODE', reason='step_execution')
                        repair_allowance = 0
                        for attempt in range(2):
                            if attempt:
                                repair_start = self.control.cost_snapshot()
                                finish_attempt(step, vars(outcome), attempt_start, repair_start, phases, 'sent_for_repair', None)
                                attempt_start = repair_start
                                phases = {}
                            outcome = StepOutcome({})
                            slice_observed = step.id not in slice_children
                            plan[index].pop('verification_status', None)
                            attempt_work_started = self.control.work_requests
                            with self.control.measure_phase(phases, 'repair' if attempt else 'implementation'):
                                outcome.execution = (self.executor(step, step_allowance) if attempt == 0 else
                                                     self.repairer(step, repair_diagnostic, repair_allowance))
                            self.control.check()
                            terminal = outcome.execution.get('reason')
                            if terminal in {'settled_step_truncation', 'settled_initial_empty_step', 'settled_public_step_truncation'}:
                                outcome.execution['reason'] = 'truncated'
                                initial_empty = terminal == 'settled_initial_empty_step'
                                public_truncation = ((initial_empty or terminal == 'settled_public_step_truncation')
                                                     and may_slice(step) and not checkpoint.implementation_changed())
                                if (terminal == 'settled_public_step_truncation' and not public_truncation
                                        and not self.control.plan_progress['can_isolate_truncation']):
                                    self.control.stop('truncated')
                                if initial_empty:
                                    self.control.initial_empty_step_alternated = True
                                awaiting_alternative = True
                                raise ExecutionStopped('truncated')
                            closeout = (self.before_closeout is not None
                                and outcome.execution.get('reason') in {'request_allowance', 'verification_due'}
                                and self.control.work_requests > attempt_work_started)
                            eligible = outcome.execution.get('status') == 'complete' or closeout
                            if outcome.execution.get('engineering_feedback'):
                                eligible = closeout = False
                            if closeout:
                                self.before_closeout()
                            if eligible:
                                self.control.set_task_state('BUILD', reason='step_static_validation')
                                with self.control.measure_phase(phases, 'static_validation'):
                                    outcome.static_validation = self.validator(step)
                                self.control.check()
                                self.control.record_verification(stage='BUILD',
                                    passed=outcome.static_validation.get('passed') is True,
                                    source_sha256=source_digest(self.control.source(self.tools.project_dir)),
                                    evidence_id=f'step-{len(history)}-{step.id}-static', evidence_kind='static')
                            if closeout:
                                outcome.closed_by_controller = bool(outcome.static_validation.get('passed'))
                                self.control.emit(dict(event='forced_step_validation', step=step.id,
                                                       validation=outcome.static_validation))
                            static_passed = bool(outcome.static_validation and outcome.static_validation.get('passed'))
                            if static_passed and self.behavior_validator:
                                self.control.set_task_state('TEST', reason='step_behavior_validation')
                                behavior_validation_active = True
                                with self.control.measure_phase(phases, 'behavior_validation'):
                                    outcome.behavior_validation = self.behavior_validator(step, min(remaining(),
                                        step_allowance - (self.control.work_requests - step_work_started)))
                                self.control.check()
                                if step.id in slice_children:
                                    validation = outcome.behavior_validation
                                    observation = validation.get('selected_scenario') or {}
                                    slice_observed = (observation.get('status') in {'passed', 'observed'}
                                        and observation.get('scenario_id') == slice_children[step.id][1]
                                        and observation.get('requirement_id') in step.requirement_ids
                                        and observation.get('source_sha256') == self.control.current_source)
                                    if (not slice_observed and not validation.get('review_observation')
                                            and validation.get('reason') in {None, 'requirement_constraints_unresolved'}):
                                        outcome.behavior_validation = dict(validation, passed=False, failure_kind='verification',
                                            reason=observation.get('reason') or 'slice_observation_unavailable')
                                verification_status = behavior_verification_status(outcome.behavior_validation)
                                outcome.behavior_validation = dict(outcome.behavior_validation,
                                    verification_status=verification_status, passed=verification_status == 'PASS',
                                    failure_kind=('verification' if verification_status == 'BLOCKED'
                                                  else outcome.behavior_validation.get('failure_kind')))
                                behavior_validation_active = False
                                plan[index]['verification_status'] = verification_status
                                self.control.emit(dict(event='step_verification_outcome', step=step.id,
                                    verification_status=verification_status,
                                    reason=outcome.behavior_validation.get('reason'),
                                    failure_kind=outcome.behavior_validation.get('failure_kind'),
                                    requests=self.control.requests, source_recoveries=self.control.source_recovery_count,
                                    replans=self.control.replans))
                                if (outcome.behavior_validation.get('passed') is True
                                        or outcome.behavior_validation.get('failure_kind') == 'behavior'):
                                    self.control.record_verification(stage='TEST',
                                        passed=outcome.behavior_validation.get('passed') is True,
                                        source_sha256=source_digest(self.control.source(self.tools.project_dir)),
                                        evidence_id=f'step-{len(history)}-{step.id}-behavior',
                                        failure_kind=outcome.behavior_validation.get('failure_kind'),
                                        requirement_ids=outcome.behavior_validation.get('requirement_ids', step.requirement_ids),
                                        failed_ids=outcome.behavior_validation.get('failed_requirement_ids'))
                            checked = outcome.validation
                            repairable = (eligible and (
                                outcome.static_validation is not None and outcome.static_validation.get('passed') is False
                                or outcome.behavior_validation is not None
                                and (outcome.behavior_validation.get('verification_status') == 'FAIL'
                                     or outcome.behavior_validation.get('review_observation') is True)))
                            if (attempt or self.repairer is None or self.repair_request_limit == 0 or not repairable
                                    or self.control.control_state not in {'RUNNING', 'WARNING'}):
                                break
                            if self.can_fund_repair is not None and not self.can_fund_repair():
                                self.control.emit(dict(event='step_repair_unavailable', step=step.id,
                                                       reason='request_not_admissible'))
                                break
                            # One defect-directed correction inside this transaction.
                            # Adaptive shares may renew after observable source progress;
                            # every physical request still spends the phase/global ledgers.
                            # Legacy keeps the original step envelope.
                            reserve = (self.behavior_validator.request_reserve(step)
                                       if self.behavior_validator is not None else 0)
                            spent = self.control.work_requests - step_work_started
                            runway = min(remaining(), self.control.remaining_physical_requests(),
                                slice_ceilings.get(step.id, phase_ceiling) - self.control.work_requests)
                            if step_ceiling is not None:
                                runway = min(runway, step_ceiling - self.control.work_requests)
                            repair_window = request_horizon(self.repair_request_limit)
                            if self.adaptive_progress:
                                repair_window = min(repair_window, request_horizon(self.control.policy.recovery_requests))
                            observed_defect = (outcome.static_validation and outcome.static_validation.get('passed') is False
                                or checked.get('verification_status') == 'FAIL')
                            if self.adaptive_progress and observed_defect:
                                other_minima = [minimum_requests(item) for item in steps[index + 1:]
                                                if item.id in ready]
                                tools_left = self.control.limits.tools - self.control.tools
                                if self.tools_remaining is not None:
                                    tools_left = min(tools_left, self.tools_remaining())
                                deadline = self.control.deadline
                                if self.phase_deadline is not None:
                                    deadline = min(deadline, self.phase_deadline())
                                # Without an explicit correction limit a bounded initial
                                # share can be renewed once at its own size. An unbounded
                                # existing share already permits any funded correction.
                                quantum = step_allowance if repair_window == math.inf else repair_window
                                renewal = renewal_policy.renew(current_share=reported_requests(step_allowance),
                                    spent=spent, remaining=dict(requests=reported_requests(max(0, runway)),
                                        tools=tools_left-self.control.protected_final_tools,
                                        seconds=deadline-self.control.clock()),
                                    request_quantum=reported_requests(quantum + reserve), minimum_requests=1 + reserve,
                                    protected_requests=min(other_minima, default=0),
                                    source_progress=construction_digest(self.control.source(self.tools.project_dir))
                                                    != construction_digest(checkpoint.before))
                                step_allowance = request_horizon(renewal['request_share'])
                                plan[index].setdefault('request_renewals', []).append(renewal)
                                self.control.emit(dict(event='step_request_renewal', step=step.id,
                                    observed_defect=checked.get('failure_kind') or 'static_validation', **renewal))
                                if renewal['reason'] == 'hard_resource_exhausted':
                                    break
                            plan[index]['request_allowance'] = reported_requests(step_allowance)
                            available = min(runway, step_allowance - spent)
                            repair_allowance = self.control.soft_request_allowance(
                                min(repair_window, max(0, available - reserve)), stage='CODE')
                            if repair_allowance <= 0:
                                break
                            repair_diagnostic = dict(requirement_ids=list(step.requirement_ids),
                                static_validation=outcome.static_validation,
                                behavior_validation=outcome.behavior_validation)
                            self.control.emit(dict(event='step_repair_started', step=step.id,
                                request_allowance=reported_requests(repair_allowance), requests_used=self.control.requests - step_start[0],
                                work_requests_used=self.control.work_requests - step_work_started,
                                original_step_allowance=plan[index]['initial_request_allowance'],
                                current_step_allowance=reported_requests(step_allowance), source_sha256=self.control.current_source))
                            self.control.set_task_state('CODE', reason='observed_step_defect')
                        checked = outcome.validation
                        retain_unverified = getattr(self.behavior_validator, 'retain_unverified', False) is True
                        retain = (retain_unverified and eligible and static_passed and slice_observed
                                  and (step.id not in slice_children or checked.get('reason') == 'requirement_constraints_unresolved')
                                  and self.behavior_validator.can_retain(checked))
                        if not eligible or not static_passed or not (checked.get('passed') or retain):
                            original_stop = next((checked.get(key) for key in sorted(('reason', 'author_status'),
                                key=lambda key: checked.get(key) in PHASE_BUDGET_STOPS)
                                if checked.get(key) in BUDGET_STOP_REASONS | SOURCE_SAFETY_STOPS
                                | {'usage_unknown', 'usage_unknown_or_out_of_bounds', 'request_usage_unknown',
                                   'budget_exhausted', 'total_token_budget'}
                                if checked.get(key) not in PHASE_BUDGET_STOPS
                                or checked.get('verification_status') in {None, 'DEFERRED'}), None)
                            phase_reason = original_stop if original_stop in PHASE_BUDGET_STOPS else None
                            reason = (original_stop or checked.get('reason') or 'step_behavior_unverified' if outcome.behavior_validation is not None
                                      else outcome.execution.get('reason') or 'static_validation_failed')
                            failed = dict(step=step.id, requirement_ids=list(step.requirement_ids),
                                          reason=reason,
                                          detail=str(outcome.execution.get('detail') or '')[:1000], validation=checked)
                            static = outcome.static_validation or {}
                            observation = static.get('selected_scenario')
                            if checked.get('failure_kind') in {'verification', 'phase_stop'} or phase_reason:
                                failed.update(quarantine(checkpoint))
                            elif (closeout and reason == 'request_allowance' and original_stop is None
                                  and outcome.execution.get('status') == 'stopped'
                                  and retain_unverified and self.quarantine_source is not None
                                  and (remaining() < 2 or self.control.replans >= self.control.limits.replans)
                                  and static.get('passed') is False and static.get('source_claims_only') is True
                                  and (observation is None or isinstance(observation, dict)
                                       and observation.get('status') == 'passed')):
                                # Retain the draft with this failure. Publish its identity only after
                                # rollback and a stop without requests for another plan and edit.
                                try:
                                    failed.update(checkpoint.quarantine(self.quarantine_source,
                                        stop_reason=SOURCE_CLAIMS_DRAFT, candidate_step=step.id,
                                        candidate_revision=len(history), candidate_attempt=attempt + 1))
                                except ExecutionStopped:
                                    raise
                                except (OSError, RuntimeError) as error:
                                    failed['candidate_capture_error'] = type(error.__cause__ or error).__name__
                            plan[index]['failure'] = failed
                        else:
                            with self.control.measure_phase(phases, 'commit'):
                                committed_source = checkpoint.commit()
                            disposition = 'committed' if checked.get('passed') else 'constructed_unverified'
                    except BaseException as error:
                        if isinstance(error, ExecutionStopped) and behavior_validation_active:
                            observed = outcome.behavior_validation
                            checked = dict(observed) if isinstance(observed, dict) else dict(
                                passed=False, failure_kind='verification')
                            checked['reason'] = error.reason
                            try:
                                status = behavior_verification_status(checked)
                                if isinstance(observed, dict) and behavior_verification_status(observed) == 'BLOCKED':
                                    status = 'BLOCKED'  # A later phase yield cannot erase an earlier hard stop.
                            except Exception:
                                status = 'BLOCKED'  # Diagnostics must never mask the original interruption.
                            if status == 'PASS':
                                status = 'BLOCKED'  # Even a reasonless interruption is not acceptance.
                            verification_interruption = dict(passed=False, reason=error.reason,
                                failure_kind='behavior' if status == 'FAIL' else 'verification',
                                verification_status=status, interrupted=True)
                            if isinstance(observed, dict):
                                verification_interruption['observed_validation'] = dict(observed)
                        failure = dict(step=step.id, reason=getattr(error, 'reason', type(error).__name__))
                        plan[index]['failure'] = failure
                        if isinstance(error, ExecutionStopped) and error.reason == 'truncated':
                            candidate.clear()
                            if checkpoint is not None and self.quarantine_source is not None:
                                failure.update(quarantine(checkpoint, stop_reason=error.reason,
                                    candidate_step=step.id, candidate_revision=len(history),
                                    candidate_attempt=len(plan[index].get('attempts', [])) + 1))
                        elif (isinstance(error, ExecutionStopped) and error.reason in MODEL_ONLY_STOPS | PHASE_BUDGET_STOPS
                                and eligible and outcome.static_validation
                                and outcome.static_validation.get('passed') and self.behavior_validator):
                            failure.update(quarantine(checkpoint))
                        if awaiting_alternative and isinstance(error, ExecutionStopped) and error.reason == 'truncated':
                            if not public_truncation:
                                truncated_refs.update(step.requirement_ids)
                        elif (isinstance(error, ExecutionStopped) and self.control.stopped is None
                                and self.control.control_state == 'CIRCUIT_OPEN'):
                            failed = dict(failure, requirement_ids=list(step.requirement_ids), validation=outcome.validation)
                        else:
                            raise
                    finally:
                        # Decorate only the report snapshot. Recovery and budget
                        # observation must keep using the original live outcome.
                        reported_outcome = vars(outcome).copy()
                        if verification_interruption is not None:
                            reported_outcome['behavior_validation'] = verification_interruption
                            plan[index]['verification_status'] = verification_interruption['verification_status']
                        plan[index]['outcome'] = reported_outcome
                        try:
                            if checkpoint is not None and not checkpoint.finished:
                                try:
                                    with self.control.measure_phase(phases, 'rollback'):
                                        warning_rollback = (self.control.control_state == 'WARNING'
                                            and self.control.warning_reason in QUALITY_WARNINGS
                                            and self.control.control_recovery_available)
                                        if (not awaiting_alternative and self.control.stopped is None
                                                and (self.control.control_state == 'CIRCUIT_OPEN' or warning_rollback)):
                                            self.control.recover('source_rollback', checkpoint.rollback, source_changed=True)
                                        else:
                                            checkpoint.rollback()
                                except ExecutionStopped as rollback_error:
                                    plan[index].update(
                                        status='rollback_failed' if rollback_error.reason == 'rollback_failed' else 'rollback_blocked',
                                        rollback_reason=rollback_error.reason)
                                    raise
                                plan[index]['status'] = 'rolled_back'
                        finally:
                            step_record = plan[index]
                            if step_record['status'].startswith('rollback') or step_record['status'] == 'rolled_back':
                                disposition = step_record['status']
                            reason = step_record.get('rollback_reason') or step_record.get('failure', {}).get('reason')
                            end = self.control.cost_snapshot()
                            finish_attempt(step, reported_outcome, attempt_start, end, phases, disposition, reason)
                            if self.budget_planner is not None:
                                source = dict(observation_key=observation_key(step), attempts=len(step_record['attempts']),
                                              disposition=disposition, profile=profiles[step.id],
                                              requirement_ids=sorted(step.requirement_ids), scenario_id=chosen['scenario_id'])
                                measured = self.control.cost_since(step_start, end)
                                implementations = [a['cost']['phases']['implementation']['seconds']
                                    for a in step_record['attempts'] if 'implementation' in a['cost']['phases']]
                                if implementations:
                                    measured['construction_seconds'] = sum(implementations)
                                self.budget_planner.observe_cost(profiles[step.id], source, measured)
                    if self.budget_planner is not None:
                        behavior = outcome.behavior_validation
                        self.budget_planner.observe(observation_key(step), requests=self.control.requests - step_start[0],
                            verified_gain=(float(len(step.requirement_ids)) if behavior and behavior.get('passed') else 0.)
                                if behavior and (behavior.get('passed') or behavior.get('failure_kind') == 'behavior') else None,
                            passed=behavior.get('passed') is True
                                if behavior and (behavior.get('passed') or behavior.get('failure_kind') == 'behavior') else None)
                    if awaiting_alternative:
                        self.control.emit(dict(event='plan_step_truncated', step=step.id, revision=len(history),
                                               requirement_ids=list(step.requirement_ids),
                                               restored_source_sha256=self.control.current_source))
                        if public_truncation:
                            capacity = step_capacity(request_ceiling=step_work_started + step_allowance)
                            if insert_slice(step, index, capacity, 'zero_write_truncation'):
                                awaiting_alternative = False
                                continue
                            truncated_refs.update(step.requirement_ids)
                        if initial_empty and not independent:
                            refit_ceiling = step_work_started + step_allowance
                            requests_left = min(remaining(), refit_ceiling - self.control.work_requests,
                                                self.control.remaining_physical_requests())
                            tools_left = self.tools_remaining() if self.tools_remaining is not None else (
                                self.control.limits.tools - self.control.tools)
                            if (not can_split_initial or requests_left < 2
                                    or tools_left <= self.control.protected_final_tools):
                                self.control.stop('truncated')
                            self.control.plan_progress = None
                            self.control.set_task_state('PLAN', reason='initial_step_empty_truncation')
                            self.control.replan()
                            split_start = self.control.cost_snapshot()
                            try:
                                split = self._split_initial_step(step, steps[index+1:], requests_left=requests_left,
                                                                 implementation_reserve=minimum_requests(step))
                            finally:
                                self.control.emit_cost(dict(event='planning_cost', run=budget_scope, revision=len(history),
                                    purpose='initial_step_split', **self.control.cost_since(split_start)))
                            if split is None:
                                self.control.stop('truncated')
                            child_ids, replacements = split
                            refit_step_ids.update(child_ids)
                            plan[index]['replacement_step_ids'] = list(child_ids)
                            steps[index+1:] = replacements
                            plan[index+1:] = [dict(id=item.id, goal=item.goal, requirement_ids=list(item.requirement_ids),
                                depends_on=list(item.depends_on), acceptance=item.acceptance, status='pending')
                                for item in replacements]
                            truncated_refs.difference_update(step.requirement_ids)
                            pending = pending_work()
                            self.control.emit(dict(event='initial_step_split', step=step.id,
                                replacement_step_ids=list(child_ids), request_ceiling=reported_requests(refit_ceiling)))
                            if self.control.control_state != 'PROBATION':
                                self.control.record_verification(stage='PLAN', passed=True,
                                    source_sha256=source_digest(self.control.source(self.tools.project_dir)),
                                    evidence_id=f'plan-{len(history)}-initial-step-split')
                        index += 1
                        continue
                    if failed is not None:
                        if (outcome.execution.get('engineering_feedback')
                                and failed['reason'] in {'request_allowance', 'model_incomplete'} and not isolatable(failed)):
                            return result('stopped', failed['reason'], phase='implementation',
                                          detail=outcome.execution['engineering_feedback'])
                        pending = pending_work()
                        if isolatable(failed):
                            recorded = dict(failed, revision=len(history),
                                            source_sha256=self.control.current_source)
                            revision_failures.append(recorded)
                            local_failures.append(recorded)
                            self.control.emit(dict(event='plan_step_failure_isolated', **recorded))
                            # The failed transaction was rolled back. Independent
                            # validated rows can still make useful progress, while
                            # its dependents remain unavailable in this revision.
                            failed = None
                            index += 1
                            continue
                        break
                    if candidate and candidate.get('candidate_base_sha256') != committed_source:
                        # A quarantined branch was based on an older checkpoint.
                        # Later independent commits must remain the delivery
                        # base. Retain the old receipt in its failure/history, but
                        # do not hand a stale branch to candidate closeout.
                        self.control.emit(dict(event='candidate_reference_superseded',
                            candidate_source_sha256=candidate.get('candidate_source_sha256'),
                            candidate_base_sha256=candidate.get('candidate_base_sha256'),
                            current_source_sha256=committed_source))
                        candidate.clear()
                    verified = checked.get('passed') is True
                    destination = completed if verified else constructed
                    plan[index]['status'] = 'completed' if verified else 'constructed_unverified'
                    refit_step_ids.discard(step.id)
                    progress_source = construction_digest(checkpoint.committed_files)
                    destination.append(dict(id=step.id, revision=len(history), goal=step.goal,
                        requirement_ids=list(step.requirement_ids), source_sha256=committed_source,
                        implementation_source_sha256=progress_source,
                        source_changed=progress_source != construction_digest(checkpoint.before),
                        acceptance='step_verified' if verified else 'construction_only'))
                    if step.id in slice_children:
                        destination[-1]['slice_parent'], destination[-1]['scenario_id'] = slice_children[step.id]
                    if self.behavior_validator:
                        destination[-1]['behavior_validation'] = checked
                    if outcome.closed_by_controller:
                        destination[-1]['closed_by_controller'] = True
                    pending = pending_work()
                    blocked_requirements.intersection_update(pending)
                    self.control.emit(dict(event='plan_step_completed' if verified else 'plan_step_constructed',
                                           **destination[-1]))
                    if not verified and retain:
                        self.control.observe_retained_scenario(step, checked, committed_source)
                    # Publishing accepted diagnostics is post-commit bookkeeping.
                    # A failure here stops execution, but cannot relabel verified,
                    # committed source as rolled back or omit its completed record.
                    if self.behavior_validator and verified:
                        self.behavior_validator.accept(step, checked)
                    index += 1
                isolated_revision = failed is None and bool(revision_failures)
                if isolated_revision:
                    failed = dict(revision_failures[0], local_failures=list(revision_failures),
                                  blocked_requirement_ids=sorted(blocked_requirements.intersection(pending)))
                if failed is None:
                    if truncated_refs:
                        self.control.stop('truncated')
                    if deferred:
                        # Every row in this window reached its normal disposition; pending_work()
                        # already carried the untouched requirements forward.
                        feedback = None
                        continue
                    if pending:
                        return result('stopped', 'dependency_blocked')
                    return result('complete')
                if failed['reason'] in PHASE_BUDGET_STOPS:
                    # The phase has yielded resources to final verification.
                    # Replanning cannot expand its share and must not turn this
                    # local stop into a global replan-limit stop.
                    return result('stopped', failed['reason'])
                if failed['reason'] in ALLOCATION_STOP_REASONS | SOURCE_SAFETY_STOPS | CONTROLLER_STATE_STOPS | {
                        'usage_unknown', 'usage_unknown_or_out_of_bounds', 'request_usage_unknown',
                        'budget_exhausted', 'total_token_budget'}:
                    self.control.stop(failed['reason'])
                if failed['validation'].get('failure_kind') == 'verification':
                    self.control.stop('verification_blocked')
                if truncated_refs or refit_step_ids:
                    self.control.stop('truncated')
                # Replan only unmet work; a failed dependent step can share IDs
                # with an earlier step, so use the remaining steps rather than
                # subtracting IDs covered by earlier successful edits.
                pending = pending_work()
                self.control.check_planning_resources()
                if remaining() < 2:
                    return result('stopped', 'request_allowance')
                # Only a revision whose remaining failures were all isolated may
                # yield. Preserve any hard stop and the shared replan allowance.
                if (isolated_revision and self.has_independent_followup
                        and self.has_independent_followup(tuple(pending))):
                    return result('stopped', failed['reason'], phase='implementation',
                                  yielded_to_independent_window=True)
                progress = [item for item in completed + constructed
                            if self.adaptive_progress and item['revision'] == len(history) and item.get('source_changed')]
                self.control.replan(committed_progress=progress)
                feedback = failed
        except ExecutionStopped as error:
            return result('stopped', error.reason)
        finally:
            self.control.plan_progress = None
            self.control.retained_requirement_ids = self.control.prior_retained_requirement_ids
            self.control.verification_available = True
            self.control.protected_final_tools = previous_final_tools
            self.control.external_tool_capacity = None

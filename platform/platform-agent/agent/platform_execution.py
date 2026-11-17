"""Task-independent execution limits, validated plans and source transactions.

One controller owns the project. Source rollback does not refund requests,
tokens, tool calls or elapsed time, and never restores runtime metadata.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4

from .sandbox_files import apply_snapshot, snapshot_project

MODEL_ONLY_STOPS = frozenset({'model_request_failed', 'request_interrupted_usage_unknown',
    'model_terminated', 'execution_request_limit', 'platform_token_allocation',
    'platform_request_allocation', 'request_allowance', 'context_limit',
    'repeated_action', 'no_progress', 'source_oscillation', 'replan_limit', 'tool_batch_limit', 'verification_blocked'})
SOURCE_SAFETY_STOPS = frozenset({'rollback_failed', 'concurrent_source_change'})
PHASE_BUDGET_STOPS = frozenset({'self_test_reserve', 'self_test_time_reserve'})
EXECUTION_STOP_REASONS = frozenset({'execution_deadline', 'execution_request_limit', 'execution_tool_limit',
    'tool_batch_limit', 'repeated_action', 'source_oscillation', 'no_progress', 'replan_limit',
    'request_interrupted_usage_unknown', 'model_request_failed', 'model_terminated', 'verification_blocked'})
BUDGET_STOP_REASONS = EXECUTION_STOP_REASONS | PHASE_BUDGET_STOPS | {'platform_token_allocation', 'platform_request_allocation',
    'request_allowance', 'budget_insufficient', 'context_limit', 'inspection_limit'}
ALLOCATION_STOP_REASONS = EXECUTION_STOP_REASONS | {'platform_token_allocation', 'platform_request_allocation'}
CAMPAIGN_DIAGNOSTIC_STOPS = frozenset({'request_usage_unknown', 'request_interrupted_usage_unknown'})
QUALITY_WARNINGS = frozenset({'quality_stagnation', 'quality_unknown'})


def behavior_verification_status(checked):
    """Describe controller evidence without granting new execution allowance.

    Only the two existing final-verification reservation stops are deferred.
    A failed observed behavior outranks a simultaneous budget reason. Unknown
    outcomes and unavailable/invalid harnesses are blocked, never failed tests.
    """
    stops = [checked.get(key) for key in ('reason', 'author_status')]
    if any(reason in (BUDGET_STOP_REASONS | SOURCE_SAFETY_STOPS |
            {'usage_unknown', 'request_usage_unknown', 'budget_exhausted', 'total_token_budget'})
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
    recovery_requests: int = 2
    probation_requests: int = 4

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError('control policy enabled must be boolean')
        for name, value in vars(self).items():
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
    requests: int = 400
    tools: int = 2000
    seconds: int = 3600
    batch_tools: int = 16
    repeated_actions: int = 3
    idle_turns: int = 6
    plan_steps: int = 24
    plan_requests: int = 4
    step_requests: int = 12
    replans: int = 1
    protocol_repairs: int = 2
    inspection_turns: int = 3

    def __post_init__(self):
        for name, value in vars(self).items():
            if type(value) is not int or value < (0 if name in {'replans', 'protocol_repairs'} else 1):
                raise ValueError(f'invalid execution limit: {name}')


def source_digest(files):
    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        digest.update(name.encode('utf-8') + b'\0' + hashlib.sha256(data).digest())
    return digest.hexdigest()


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
        self.stopped = None
        self._stop_epoch = 0
        self._candidate_closeout = None
        self._candidate_closeout_fallback = False
        self.actions = Counter()
        self.states = Counter()
        self.expected_sources = {}
        self.plan_progress = None
        self.policy = policy or ControlPolicy()
        self.task_state, self.control_state = 'PLAN', 'RUNNING'
        self.current_source = None
        self.verifications = {}
        self.recovery_count = self.quality_idle = self.warning_age = 0
        self.fault_reason = self.warning_reason = None
        self.quality_known = False
        self.verification_available = True
        self._passed_requirements, self._failed_requirements = set(), set()
        self._probation_requests = 0

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
            if (self.control_state == 'PROBATION' and self.task_state != 'PLAN'
                    and self._probation_requests >= self.policy.probation_requests
                    and (action == 'request' or action == 'write'
                         and self._probation_requests > self.policy.probation_requests)):
                self._trip(self.fault_reason or 'probation_exhausted')
        return True

    def soft_request_allowance(self, proposed, *, stage=None):
        self.permit()
        if self.control_state == 'PROBATION' and (stage or self.task_state) == 'PLAN':
            return proposed  # Planning has its own bound and still spends the shared hard budgets.
        if self.policy.enabled and self.control_state in {'WARNING', 'PROBATION'}:
            cap = self.policy.probation_requests if self.control_state == 'PROBATION' else self.policy.recovery_requests
            if self.control_state == 'PROBATION':
                cap -= self._probation_requests
            return max(0, min(proposed, cap))
        return proposed

    def _trip(self, reason):
        self.fault_reason = reason
        if self.recovery_count >= self.policy.recovery_attempts:
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
        if self.recovery_count >= self.policy.recovery_attempts:
            self.stop('recovery_exhausted')
        if warning_rollback:
            self.fault_reason = self.warning_reason
            self._transition('CIRCUIT_OPEN', self.fault_reason)
        self.recovery_count += 1
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
                            failure_kind=None, requirement_ids=(), failed_ids=(), evidence_kind='runtime', closeout=None):
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
                       requirement_ids=list(requirement_ids), failed_ids=list(failed_ids), evidence_kind=evidence_kind)
        if closeout is not None:
            receipt['closeout_stop_epoch'] = closeout.stop_epoch
        self.verifications[evidence_id] = receipt
        self.emit(dict(event='verification_evidence', **receipt))
        if closeout is not None or not self.policy.enabled:
            return receipt
        refs, failures = set(requirement_ids), set(failed_ids)
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
                self._failed_requirements.update(failures or refs)
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
                           requests=self.requests, tools=self.tools, replans=self.replans))
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

    def request(self):
        self.permit('request')
        if self.requests >= self.limits.requests:
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
                # Necessary context acquisition consumes the hard budgets but
                # is not an attempted implementation or a quality improvement.
                # Do not reset accrued quality debt or an existing warning.
                if (self.task_state == 'CODE' and self.verification_available
                        and not (information_only and new_evidence and before == after)):
                    self.quality_idle += 1
                if self.idle >= self.limits.idle_turns:
                    self._warn('activity_stagnation')
                # The retained quality debt cannot immediately consume the new
                # bounded CODE/TEST recovery window. A failed test still trips it.
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

    def replan(self):
        self.check()
        if self.replans >= self.limits.replans:
            self.stop('replan_limit')
        self.replans += 1

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

    def commit(self):
        self.control.check()
        digest = source_digest(self.control.source(self.root))
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


def validate_plan(document, requirement_ids, limit, requirement_dependencies=None, *, exhausted_scope=()):
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
    if any(not set(step.depends_on) <= steps.keys() for step in steps.values()):
        raise ValueError('plan references an unknown dependency')
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
                 *, before_closeout=None, budget_planner=None, repairer=None):
        self.control, self.tools = control, tools
        self.planner, self.executor, self.validator = planner, executor, validator
        self.behavior_validator = behavior_validator
        self.before_closeout = before_closeout
        self.budget_planner = budget_planner
        self.repairer = repairer

    def run(self, requirement_ids, allowance, feedback=None, *, whole_task=False):
        started = self.control.requests
        phase_refs = list(dict.fromkeys(requirement_ids))
        pending = list(phase_refs)
        completed, constructed, history = [], [], []
        failed = None
        candidate = {}
        budget_scope = uuid4().hex

        def remaining():
            return allowance - (self.control.requests - started)

        def observation_key(step):
            return f'{budget_scope}:{len(history)}:{step.id}'

        def minimum_requests(step):
            reserve = getattr(self.behavior_validator, 'request_reserve', None)
            return 1 + (reserve(step) if callable(reserve) else 0)

        def step_capacity():
            ceiling = min(self.control.limits.step_requests, remaining(),
                          self.control.limits.requests - self.control.requests)
            return self.control.soft_request_allowance(max(0, ceiling), stage='CODE')

        def result(status, reason=None):
            return dict(status=status, reason=reason, turns=self.control.requests - started,
                        completed_steps=completed, candidate_steps=constructed, plan_history=history,
                        pending_requirement_ids=pending,
                        acceptance='synthetic_step_diagnostics' if self.behavior_validator else 'static_only',
                        task_state=self.control.task_state, control_state=self.control.control_state, **candidate)

        def quarantine(checkpoint):
            receipt = dict(candidate_source=self.behavior_validator.quarantine(),
                candidate_source_sha256=source_digest(self.control.source(self.tools.project_dir)),
                candidate_base_sha256=source_digest(checkpoint.before), candidate_acceptance='unverified')
            candidate.update(receipt)
            return receipt

        try:
            while True:
                self.control.plan_progress = None
                self.control.check()
                self.control.set_task_state('PLAN', reason='plan_requested')
                if remaining() < 2:
                    return result('stopped', 'request_allowance')
                document = self.planner(pending, completed + constructed, feedback,
                    min(self.control.limits.plan_requests, remaining() - 1))
                if document.get('status') != 'complete':
                    return result('stopped', document.get('reason', 'planning_failed'))
                try:
                    steps = validate_plan(document, pending, self.control.limits.plan_steps,
                        exhausted_scope=failed['requirement_ids'] if failed and failed['reason'] == 'request_allowance' else ())
                except ValueError as error:
                    return result('stopped', 'invalid_plan: ' + str(error))
                plan = [dict(id=s.id, goal=s.goal, requirement_ids=list(s.requirement_ids),
                             depends_on=list(s.depends_on), acceptance=s.acceptance,
                             status='pending') for s in steps]
                history.append(plan)
                self.control.emit(dict(event='plan_validated', steps=plan))
                if self.control.policy.enabled and self.control.control_state != 'PROBATION':
                    self.control.record_verification(stage='PLAN', passed=True,
                        source_sha256=source_digest(self.control.source(self.tools.project_dir)),
                        evidence_id='plan-' + str(len(history)))
                failed = None
                for index, step in enumerate(steps):
                    # Model step IDs are local to this validated revision.
                    done = [item['id'] for item in plan[:index]
                            if item['status'] in {'completed', 'constructed_unverified'}]
                    request_share = None
                    if self.budget_planner is not None:
                        ready = [item.id for item in steps[index:] if set(item.depends_on) <= set(done)]
                        capacity = step_capacity()
                        ready = [item.id for item in steps[index:]
                                 if item.id in ready and minimum_requests(item) <= capacity]
                        if not ready:
                            return result('stopped', 'budget_insufficient')
                        actions = [dict(id=item.id, dependencies=list(item.depends_on),
                            observation_key=observation_key(item), minimum_requests=minimum_requests(item),
                            requirement_ids=list(item.requirement_ids), estimated_cost=self.control.limits.step_requests,
                            expected_gain=float(len(item.requirement_ids)), confidence=.25, risk=0., required=True)
                            for item in steps[index:]]
                        decision = self.budget_planner.select(actions, remaining=dict(requests=min(remaining(),
                            self.control.limits.requests - self.control.requests),
                            tools=self.control.limits.tools - self.control.tools,
                            seconds=max(0, self.control.deadline - self.control.clock())), completed=done, allowed=ready)
                        self.control.emit(dict(event='budget_decision', **decision))
                        if decision['action_id'] is None:
                            return result('stopped', decision.get('reason') or 'budget_insufficient')
                        selected = next(pos for pos in range(index, len(steps)) if steps[pos].id == decision['action_id'])
                        steps[index], steps[selected] = steps[selected], steps[index]
                        plan[index], plan[selected] = plan[selected], plan[index]
                        step = steps[index]
                        request_share = decision['request_share']
                    pending = list(dict.fromkeys(ref for s in steps[index:] for ref in s.requirement_ids))
                    self.control.check()
                    if remaining() <= 0:
                        return result('stopped', 'request_allowance')
                    self.control.plan_progress = dict(
                        revision=len(history), current_step=step.id,
                        request_ceiling=started + allowance,
                        phase_requirement_ids=list(phase_refs),
                        whole_task=whole_task is True,
                        remaining_steps=len(steps) - index,
                        remaining_requirement_count=len(pending),
                        completed_step_ids=done,
                        unverified_step_ids=[item['id'] for item in plan[:index]
                                             if item['status'] == 'constructed_unverified'],
                        remaining_steps_summary=[dict(id=item.id,
                            requirement_ids=list(item.requirement_ids),
                            depends_on=list(item.depends_on)) for item in steps[index:]])
                    step_allowance = step_capacity()
                    if request_share is not None:
                        step_allowance = min(step_allowance, max(0, int(request_share)))
                    if step_allowance < minimum_requests(step):
                        return result('stopped', 'budget_insufficient')
                    plan[index]['status'] = 'running'
                    capability = getattr(self.behavior_validator, 'verification_available', None)
                    self.control.verification_available = (capability(step) is not False) if callable(capability) else True
                    self.control.emit(dict(event='step_verification_capability', step=step.id,
                                           available=self.control.verification_available))
                    checkpoint = SourceTransaction(self.tools, self.control)
                    step_started = self.control.requests
                    self.control.set_task_state('CODE', reason='step_execution')
                    outcome = StepOutcome({})
                    eligible = False
                    behavior_validation_active = False
                    verification_interruption = None
                    try:
                        repair_allowance = 0
                        for attempt in range(2):
                            outcome = StepOutcome({})
                            plan[index].pop('verification_status', None)
                            attempt_started = self.control.requests
                            outcome.execution = (self.executor(step, step_allowance) if attempt == 0 else
                                                 self.repairer(step, repair_diagnostic, repair_allowance))
                            self.control.check()
                            closeout = (self.before_closeout is not None
                                and outcome.execution.get('reason') in {'request_allowance', 'verification_due'}
                                and self.control.requests > attempt_started)
                            eligible = outcome.execution.get('status') == 'complete' or closeout
                            if closeout:
                                self.before_closeout()
                            if eligible:
                                self.control.set_task_state('BUILD', reason='step_static_validation')
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
                                outcome.behavior_validation = self.behavior_validator(step, min(remaining(),
                                    step_allowance - (self.control.requests - step_started)))
                                self.control.check()
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
                                    requests=self.control.requests, source_recoveries=self.control.recovery_count,
                                    replans=self.control.replans))
                                if (outcome.behavior_validation.get('passed') is True
                                        or outcome.behavior_validation.get('failure_kind') == 'behavior'):
                                    self.control.record_verification(stage='TEST',
                                        passed=outcome.behavior_validation.get('passed') is True,
                                        source_sha256=source_digest(self.control.source(self.tools.project_dir)),
                                        evidence_id=f'step-{len(history)}-{step.id}-behavior',
                                        failure_kind=outcome.behavior_validation.get('failure_kind'),
                                        requirement_ids=step.requirement_ids)
                            plan[index].setdefault('attempts', []).append(vars(outcome).copy())
                            checked = outcome.validation
                            repairable = (eligible and (
                                outcome.static_validation is not None and outcome.static_validation.get('passed') is False
                                or outcome.behavior_validation is not None
                                and outcome.behavior_validation.get('verification_status') == 'FAIL'))
                            if (attempt or self.repairer is None or not repairable
                                    or self.control.control_state not in {'RUNNING', 'WARNING'}):
                                break
                            # One defect-directed correction inside this transaction and its
                            # original physical-request envelope. No new retry/replan budget.
                            reserve = (self.behavior_validator.request_reserve(step)
                                       if self.behavior_validator is not None else 0)
                            available = min(remaining(), step_allowance - (self.control.requests - step_started),
                                            self.control.limits.requests - self.control.requests)
                            repair_allowance = self.control.soft_request_allowance(
                                min(self.control.policy.recovery_requests, max(0, available - reserve)), stage='CODE')
                            if repair_allowance <= 0:
                                break
                            repair_diagnostic = dict(requirement_ids=list(step.requirement_ids),
                                static_validation=outcome.static_validation,
                                behavior_validation=outcome.behavior_validation)
                            self.control.emit(dict(event='step_repair_started', step=step.id,
                                request_allowance=repair_allowance, requests_used=self.control.requests - step_started,
                                original_step_allowance=step_allowance, source_sha256=self.control.current_source))
                            self.control.set_task_state('CODE', reason='observed_step_defect')
                        checked = outcome.validation
                        retain = (getattr(self.behavior_validator, 'retain_unverified', False) is True
                                  and eligible and static_passed
                                  and self.behavior_validator.can_retain(checked))
                        if not eligible or not static_passed or not (checked.get('passed') or retain):
                            original_stop = next((checked.get(key) for key in sorted(('reason', 'author_status'),
                                key=lambda key: checked.get(key) in PHASE_BUDGET_STOPS)
                                if checked.get(key) in BUDGET_STOP_REASONS | SOURCE_SAFETY_STOPS
                                | {'usage_unknown', 'request_usage_unknown', 'budget_exhausted', 'total_token_budget'}
                                if checked.get(key) not in PHASE_BUDGET_STOPS
                                or checked.get('verification_status') in {None, 'DEFERRED'}), None)
                            phase_reason = original_stop if original_stop in PHASE_BUDGET_STOPS else None
                            reason = (original_stop or 'step_behavior_unverified' if outcome.behavior_validation is not None
                                      else outcome.execution.get('reason') or 'static_validation_failed')
                            failed = dict(step=step.id, requirement_ids=list(step.requirement_ids),
                                          reason=reason,
                                          detail=str(outcome.execution.get('detail') or '')[:1000], validation=checked)
                            if checked.get('failure_kind') in {'verification', 'phase_stop'} or phase_reason:
                                failed.update(quarantine(checkpoint))
                            plan[index]['failure'] = failed
                        else:
                            committed_source = checkpoint.commit()
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
                        if (isinstance(error, ExecutionStopped) and error.reason in MODEL_ONLY_STOPS | PHASE_BUDGET_STOPS
                                and eligible and outcome.static_validation
                                and outcome.static_validation.get('passed') and self.behavior_validator):
                            failure.update(quarantine(checkpoint))
                        plan[index]['failure'] = failure
                        if (isinstance(error, ExecutionStopped) and self.control.stopped is None
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
                        if not checkpoint.finished:
                            try:
                                warning_rollback = (self.control.control_state == 'WARNING'
                                    and self.control.warning_reason in QUALITY_WARNINGS
                                    and self.control.recovery_count < self.control.policy.recovery_attempts)
                                if (self.control.stopped is None
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
                    if self.budget_planner is not None:
                        behavior = outcome.behavior_validation
                        self.budget_planner.observe(observation_key(step), requests=self.control.requests - step_started,
                            verified_gain=(float(len(step.requirement_ids)) if behavior and behavior.get('passed') else 0.)
                                if behavior and (behavior.get('passed') or behavior.get('failure_kind') == 'behavior') else None,
                            passed=behavior.get('passed') is True
                                if behavior and (behavior.get('passed') or behavior.get('failure_kind') == 'behavior') else None)
                    if failed is not None:
                        break
                    verified = checked.get('passed') is True
                    destination = completed if verified else constructed
                    plan[index]['status'] = 'completed' if verified else 'constructed_unverified'
                    destination.append(dict(id=step.id, revision=len(history), goal=step.goal,
                        requirement_ids=list(step.requirement_ids), source_sha256=committed_source,
                        acceptance='step_verified' if verified else 'construction_only'))
                    if self.behavior_validator:
                        destination[-1]['behavior_validation'] = checked
                    if outcome.closed_by_controller:
                        destination[-1]['closed_by_controller'] = True
                    pending = list(dict.fromkeys(ref for remaining_step in steps[index + 1:]
                                                 for ref in remaining_step.requirement_ids))
                    self.control.emit(dict(event='plan_step_completed' if verified else 'plan_step_constructed',
                                           **destination[-1]))
                    # Publishing accepted diagnostics is post-commit bookkeeping.
                    # A failure here stops execution, but cannot relabel verified,
                    # committed source as rolled back or omit its completed record.
                    if self.behavior_validator and verified:
                        self.behavior_validator.accept(step, checked)
                if failed is None:
                    pending = []
                    return result('complete')
                if failed['reason'] in PHASE_BUDGET_STOPS:
                    # The phase has yielded resources to final verification.
                    # Replanning cannot expand its share and must not turn this
                    # local stop into a global replan-limit stop.
                    return result('stopped', failed['reason'])
                if failed['reason'] in ALLOCATION_STOP_REASONS | SOURCE_SAFETY_STOPS | {
                        'usage_unknown', 'request_usage_unknown', 'budget_exhausted', 'total_token_budget'}:
                    self.control.stop(failed['reason'])
                if failed['validation'].get('failure_kind') == 'verification':
                    self.control.stop('verification_blocked')
                # Replan only unmet work; a failed dependent step can share IDs
                # with an earlier step, so use the remaining steps rather than
                # subtracting IDs covered by earlier successful edits.
                pending = list(dict.fromkeys(ref for s in steps[index:] for ref in s.requirement_ids))
                self.control.replan()
                feedback = failed
        except ExecutionStopped as error:
            return result('stopped', error.reason)
        finally:
            self.control.plan_progress = None
            self.control.verification_available = True

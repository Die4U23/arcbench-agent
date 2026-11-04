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


class ExecutionStopped(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


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
    def __init__(self, limits=None, *, clock=time.monotonic, emit=None):
        self.limits = limits or ExecutionLimits()
        self.clock, self.emit = clock, emit or (lambda event: None)
        self.deadline = clock() + self.limits.seconds
        self.requests = self.tools = self.replans = self.idle = self.protocol_repairs = 0
        self.stopped = None
        self.actions = Counter()
        self.states = Counter()
        self.expected_sources = {}
        self.plan_progress = None

    def stop(self, reason):
        if self.stopped is None:
            self.stopped = reason
            self.emit(dict(event='execution_stopped', reason=reason,
                           requests=self.requests, tools=self.tools, replans=self.replans))
        raise ExecutionStopped(self.stopped)

    def check(self):
        if self.stopped:
            raise ExecutionStopped(self.stopped)
        if self.clock() >= self.deadline:
            self.stop('execution_deadline')

    def request(self):
        self.check()
        if self.requests >= self.limits.requests:
            self.stop('execution_request_limit')
        self.requests += 1

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

    def observe(self, before, after, *, new_evidence=False):
        self.check()
        progress = before != after or new_evidence
        self.idle = 0 if progress else self.idle + 1
        if before != after:
            self.states[after] += 1
            if self.states[after] > self.limits.repeated_actions:
                self.stop('source_oscillation')
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
        self.control.source(self.root)
        self.finished = True

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
            self.control.stopped = 'rollback_failed'
            raise ExecutionStopped('rollback_failed')


@dataclass(frozen=True)
class PlanStep:
    id: str
    goal: str
    requirement_ids: tuple[str, ...]
    depends_on: tuple[str, ...]
    acceptance: str


def validate_plan(document, requirement_ids, limit, requirement_dependencies=None):
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
    for step in ordered:
        reachable = set(step.depends_on)
        for dependency in step.depends_on:
            reachable.update(ancestors[dependency])
        ancestors[step.id] = reachable
        available = set(step.requirement_ids)
        for identity in reachable:
            available.update(steps[identity].requirement_ids)
        for ref in step.requirement_ids:
            required = set((requirement_dependencies or {}).get(ref, ())) & expected
            if not required <= available:
                raise ValueError('plan omits declared requirement dependency: ' + ref)
    return ordered


class PlanExecutor:
    """Sequential plan/execute/replan. Successful steps survive a later failure."""
    def __init__(self, control, tools, planner, executor, validator):
        self.control, self.tools = control, tools
        self.planner, self.executor, self.validator = planner, executor, validator

    def run(self, requirement_ids, allowance, feedback=None):
        started = self.control.requests
        phase_refs = list(dict.fromkeys(requirement_ids))
        pending = list(phase_refs)
        completed, history = [], []

        def remaining():
            return allowance - (self.control.requests - started)

        def result(status, reason=None):
            return dict(status=status, reason=reason, turns=self.control.requests - started,
                        completed_steps=completed, plan_history=history,
                        pending_requirement_ids=pending, acceptance='static_only')

        try:
            while True:
                self.control.plan_progress = None
                self.control.check()
                if remaining() < 2:
                    return result('stopped', 'request_allowance')
                document = self.planner(pending, completed, feedback,
                    min(self.control.limits.plan_requests, remaining() - 1))
                if document.get('status') != 'complete':
                    return result('stopped', document.get('reason', 'planning_failed'))
                try:
                    steps = validate_plan(document, pending, self.control.limits.plan_steps)
                except ValueError as error:
                    return result('stopped', 'invalid_plan: ' + str(error))
                plan = [dict(id=s.id, goal=s.goal, requirement_ids=list(s.requirement_ids),
                             depends_on=list(s.depends_on), acceptance=s.acceptance,
                             status='pending') for s in steps]
                history.append(plan)
                self.control.emit(dict(event='plan_validated', steps=plan))
                failed = None
                for index, step in enumerate(steps):
                    pending = list(dict.fromkeys(ref for s in steps[index:] for ref in s.requirement_ids))
                    self.control.check()
                    if remaining() <= 0:
                        return result('stopped', 'request_allowance')
                    plan[index]['status'] = 'running'
                    self.control.plan_progress = dict(
                        revision=len(history), current_step=step.id,
                        request_ceiling=started + allowance,
                        phase_requirement_ids=list(phase_refs),
                        remaining_steps=len(steps) - index,
                        remaining_requirement_count=len(pending),
                        completed_step_ids=[item['id'] for item in completed],
                        remaining_steps_summary=[dict(id=item.id,
                            requirement_ids=list(item.requirement_ids),
                            depends_on=list(item.depends_on)) for item in steps[index:]])
                    checkpoint = SourceTransaction(self.tools, self.control)
                    try:
                        outcome = self.executor(step, min(self.control.limits.step_requests, remaining()))
                        self.control.check()
                        checked = self.validator() if outcome.get('status') == 'complete' else {'passed': False}
                        if outcome.get('status') != 'complete' or not checked.get('passed'):
                            checkpoint.rollback()
                            failed = dict(step=step.id, reason=outcome.get('reason') or 'static_validation_failed',
                                          detail=str(outcome.get('detail') or '')[:1000], validation=checked)
                            plan[index].update(status='rolled_back', failure=failed)
                            break
                        checkpoint.commit()
                    except BaseException as error:
                        checkpoint.rollback()
                        plan[index].update(status='rolled_back', failure=dict(
                            step=step.id, reason=getattr(error, 'reason', type(error).__name__)))
                        raise
                    plan[index]['status'] = 'completed'
                    completed.append(dict(id=step.id, goal=step.goal, requirement_ids=list(step.requirement_ids),
                                          source_sha256=source_digest(snapshot_project(self.tools.project_dir))))
                    self.control.emit(dict(event='plan_step_completed', **completed[-1]))
                if failed is None:
                    pending = []
                    return result('complete')
                # Replan only unmet work; a failed dependent step can share IDs
                # with an earlier step, so use the remaining steps rather than
                # subtracting IDs covered by earlier successful edits.
                pending = list(dict.fromkeys(ref for s in steps[index:] for ref in s.requirement_ids))
                self.control.replan()
                feedback = json.dumps(failed, ensure_ascii=False)
        except ExecutionStopped as error:
            return result('stopped', error.reason)
        finally:
            self.control.plan_progress = None

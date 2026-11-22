"""Runnable candidate evidence, separate from complete requirement acceptance."""
from pathlib import Path
from typing import NamedTuple
import json
import tempfile

from . import behavior_checks
from .campaign_budget import CampaignBudgetStopped
from .platform_execution import (ExecutionStopped, PHASE_BUDGET_STOPS, SOURCE_CLAIMS_DRAFT,
                                 SOURCE_CLAIMS_DRAFT_STOPS, diagnostics_allowed, source_digest)
from .platform_runtime_check import api_failures, run_runtime_check, tool
from .platform_scaffold import has_only_starter_runtime
from .sandbox_files import apply_snapshot, snapshot_project


SMOKE_STEPS = 5
SMOKE = '''const { test, expect } = require('@playwright/test');
test('candidate root responds and renders in Chromium', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  const response = await page.goto('/', { waitUntil: 'load' });
  expect(response && response.status()).toBeGreaterThanOrEqual(200);
  expect(response.status()).toBeLessThan(400);
  await expect(page.locator('body')).toBeVisible();
  await expect.poll(async () => page.locator('body').evaluate(body =>
    !!body.innerText.trim() || !!body.querySelector('canvas,svg,img,input,button,video')
  )).toBe(true);
  await page.waitForTimeout(250);
  expect(errors).toEqual([]);
});
'''

# Optional feedback for the next implementation turn, collected from the existing browser.
ROOT_VIEW_TIMEOUT_MS = 250
ROOT_VIEW_OBSERVATION = '''
test.afterEach(async ({ page }, info) => {
  if (info.status !== 'passed') return;
  try {
    const tree = await page.locator('body').ariaSnapshot({ timeout: %d });
    // Retain enough UTF-16 units for the controller to read 3073 Unicode characters.
    if (tree.trim()) require('node:fs').writeFileSync(info.outputPath('rendered-root.txt'),
      tree.slice(0, 2 * 3073), 'utf8');
  } catch { /* The optional observation does not decide the smoke result. */ }
});
''' % ROOT_VIEW_TIMEOUT_MS


class _DeliverySource(NamedTuple):
    selection: str
    path: Path
    files: dict[str, bytes]
    source_sha256: str


class _ChangedScenarioReplay(ValueError):
    pass


def is_source_claim_draft(settings, result):
    return bool(settings.delivery_policy == 'runnable_candidate' and result.get('reason') in SOURCE_CLAIMS_DRAFT_STOPS
                and result.get('candidate_stop_reason') == SOURCE_CLAIMS_DRAFT and result.get('candidate_source'))


def engineering_checkpoint(evidence, settings, result):
    if settings.delivery_policy != 'runnable_candidate':
        return None
    reason = result.get('reason')
    if (not diagnostics_allowed(reason) and reason not in PHASE_BUDGET_STOPS | {'coverage_incomplete'}
            and not (reason == 'model_incomplete' and result.get('phase') == 'implementation')):
        return None
    return getattr(evidence.step_gate, 'runnable_checkpoint', None)


def check_candidate_delivery(app, evidence, model, settings, *, ir, reports, generation_result, deadline,
                             assert_input_integrity=lambda: None):
    """No model calls. Build/start/check one exact source with frozen diagnostics."""
    control = model.execution_control
    reason = generation_result.get('reason')
    if reason == 'truncated' or control.stopped == 'truncated':
        if reason != control.stopped:
            raise ExecutionStopped(control.stopped or 'truncation_stop_mismatch')
        assert_input_integrity()
        attempts = []
        try:
            report = _check_deferred_candidate(app, evidence, model, settings, ir=ir, reports=reports,
                generation_result=generation_result, deadline=deadline,
                assert_input_integrity=assert_input_integrity, attempts=attempts)
        except (ExecutionStopped, CampaignBudgetStopped) as error:
            if error.reason not in {'execution_deadline', 'execution_tool_limit', 'platform_tool_allocation',
                                    'candidate_diagnostic_time_unavailable'}:
                raise
            if evidence.promotion_before is not None:
                from .platform_generation import _rollback_promotion
                _rollback_promotion(evidence, app)
                if control.stopped != 'truncated':
                    raise ExecutionStopped(control.stopped)
            report = _candidate_unready(evidence, model, attempts, error.reason)
        directory = reports / 'candidate-readiness'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return report
    control.check_diagnostics()
    assert_input_integrity()
    if (is_source_claim_draft(settings, generation_result) or engineering_checkpoint(evidence, settings, generation_result)
            or (reason in PHASE_BUDGET_STOPS and generation_result.get('candidate_source') and evidence.step_gate is not None)):
        return _check_deferred_candidate(app, evidence, model, settings, ir=ir, reports=reports,
            generation_result=generation_result, deadline=deadline,
            assert_input_integrity=assert_input_integrity)
    source = source_digest(control.source(app))
    permit = control.begin_candidate_closeout(source_sha256=source)
    evidence.delivery_permit = permit
    directory = reports / 'candidate-readiness'
    report = _check_source(app, evidence, model, settings, ir=ir, directory=directory,
        generation_result=generation_result, deadline=deadline, permit=permit)
    assert_input_integrity()
    return report


def _replay_owners(collections):
    """Map each frozen step suite to the suite that exercises all its checks on this source."""
    signatures = []
    for suite in collections:
        cases_by_ref = {}
        for case in suite['plan']['cases']:
            cases_by_ref.setdefault(case['requirement_id'], []).append(case)
        files = dict(suite['hashes'])
        files.pop('plan.json')
        # StepBehaviorGate.suite_for writes one spec per sorted requirement. Its index
        # changes when a preceding requirement joins the suite; the script bytes must match.
        members = set()
        for index, ref in enumerate(sorted(cases_by_ref), 1):
            spec = files.pop(f'journey-{index}.spec.cjs')
            members.add(('spec', ref, spec, json.dumps(cases_by_ref[ref], sort_keys=True)))
        members.update(('asset', name, digest) for name, digest in files.items())
        signatures.append(members)
    largest_first = sorted(range(len(signatures)), key=lambda i: -len(signatures[i]))
    return [next(i for i in largest_first if required <= signatures[i]) for required in signatures]


def _candidate_checks(evidence):
    collections = list(evidence.step_gate.collections.values()) if evidence.step_gate else []
    owners = _replay_owners(collections)
    selected = sorted(set(owners))
    steps = SMOKE_STEPS + sum(len(case['steps']) for i in selected for case in collections[i]['plan']['cases'])
    return collections, owners, selected, steps


def _check_replay_bytes(replays):
    for replay in replays:
        directory = replay['directory']
        spec = directory / 'scenario.spec.cjs'
        try:
            intact = set(directory.iterdir()) == {spec} and spec.read_bytes() == replay['source']
        except OSError:
            intact = False
        if not intact:
            raise _ChangedScenarioReplay('baseline scenario suite changed')


def _required_replays(collections, selected, replays, source, baseline):
    rows = {'candidate_smoke': dict(expected_cases=1)}
    for i in selected:
        rows[f'authored_{i}'] = dict(expected_cases=len(collections[i]['plan']['cases']))
    for i, replay in enumerate(replays):
        rows[f'baseline_scenario_{i}'] = dict(expected_cases=1, scenario_id=replay['scenario_id'],
            requirement_id=replay['requirement_id'], operations=replay['operations'])
    for row in rows.values():
        row.update(status='not_run', source_sha256=source, baseline_source_sha256=baseline)
    return rows


def _retained_slices(result):
    return [step for key in ('completed_steps', 'candidate_steps') for step in result.get(key, ())
            if step.get('slice_parent') and step.get('scenario_id')]


def _require_runtime_window(deadline, control, attempt):
    from .platform_generation import RUNTIME_MIN_SECONDS, monotonic
    remaining = min(deadline, control.deadline) - monotonic()
    if remaining <= 0 or (not attempt.get('time_reserved_before_draft') and remaining < RUNTIME_MIN_SECONDS):
        raise ExecutionStopped('candidate_diagnostic_time_unavailable')


def _check_source(app, evidence, model, settings, *, ir, directory, generation_result, deadline, permit,
                  attempt=None, baseline_source=None, replays=()):
    """Probe only the currently selected source under an already sealed permit."""
    control = model.execution_control
    files = control.source(app)
    source = source_digest(files)
    if source != control.candidate_closeout_source(permit):
        raise ValueError('candidate probe source is not selected by closeout')
    if has_only_starter_runtime(files):
        row = attempt if attempt is not None else dict(selection='baseline', source_sha256=source, ready=False)
        report = _candidate_unready(evidence, model, [row], 'neutral_scaffold_unchanged', generation_result)
        report['reserved_validation_steps'] = 0
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return report
    smoke = directory / 'smoke'
    smoke_hashes = behavior_checks.write_frozen_suite(smoke, {'candidate.spec.cjs': SMOKE.encode('utf-8')})
    evidence.suites.append(dict(directory=smoke, hashes=smoke_hashes, scope='runnability_only'))
    collections, owners, selected, validation_steps = _candidate_checks(evidence)
    for suite in collections:
        if not behavior_checks.verify_frozen(suite['directory'], suite['hashes']):
            raise ValueError('candidate authored suite changed before execution')
    suites = [('candidate_smoke', smoke)] + [(f'authored_{i}', collections[i]['directory']) for i in selected]
    if baseline_source is not None:
        required = attempt['required_replays']
        _check_replay_bytes(replays)
        for i, replay in enumerate(replays):
            suites.append((f'baseline_scenario_{i}', replay['directory']))
            validation_steps += replay['operations']
    if attempt is not None:
        _require_runtime_window(deadline, control, attempt)
    model.reserve_validation_steps(validation_steps, diagnostic=True)
    if attempt is not None:
        attempt.update(runtime={'status': 'started'}, reserved_validation_steps=validation_steps)
    result = run_candidate_suites(app, suites, directory, settings, min(deadline, control.deadline))
    feedback = result.to_feedback()
    if attempt is not None:
        attempt['runtime'] = feedback
    control.check_candidate_closeout(permit)
    if source_digest(control.source(app)) != source:
        raise ValueError('candidate source changed during readiness check')
    if not behavior_checks.verify_frozen(smoke, smoke_hashes) or any(
            not behavior_checks.verify_frozen(s['directory'], s['hashes']) for s in collections):
        raise ValueError('candidate diagnostics changed during readiness check')
    ready = smoke_passed(feedback)
    if baseline_source is not None:
        _check_replay_bytes(replays)
        expected = sum(row['expected_cases'] for row in required.values())
        for name, row in required.items():
            signal = feedback.get('signals', {}).get(name, {})
            passed = (type(signal.get('passed')) is int and signal['passed'] == row['expected_cases']
                      and signal.get('failed') == signal.get('skipped') == 0 and signal.get('failures') == [])
            row.update(signal=signal, status='passed' if passed else 'failed' if signal.get('failed') else 'unavailable')
        counts = feedback.get('counts', {})
        ready = (ready and feedback['status'] == 'passed' and feedback.get('browser_cases_executed') == expected
                 and counts.get('passed') == counts.get('total') == expected
                 and counts.get('failed') == counts.get('skipped') == counts.get('precondition') == 0
                 and not api_failures(result.api_probes) and all(row['status'] == 'passed' for row in required.values()))
    _, unresolved, blocked = behavior_checks.VerificationScope(ir).select(evidence.required_ids)
    report = dict(version=1, scope='runnable_candidate_not_full_verification', ready=ready,
        status='READY_FOR_EVALUATION' if ready else 'NOT_READY', formal_status='UNRUN',
        source_sha256=source, requirements_sha256=evidence.requirements_sha256,
        static_validation=evidence.static, runtime=feedback, smoke_hashes=smoke_hashes,
        reserved_validation_steps=validation_steps,
        generation_status=generation_result.get('status'), generation_stop_reason=generation_result.get('reason'),
        generation_stop_detail=generation_result.get('stop_detail') or generation_result.get('detail'),
        generation_stop_phase=generation_result.get('phase'),
        pending_requirement_ids=generation_result.get('pending_requirement_ids', []),
        blocked_requirement_ids=generation_result.get('blocked_requirement_ids', []),
        local_failures=generation_result.get('local_failures', []),
        original_controller_stop=permit.stop_reason,
        model_requests_closed=True,
        usage_accounting=dict(model.campaign_budget.state) if model.campaign_budget is not None else None,
        missing_source_coverage=evidence.missing,
        requirements={ref: dict(status='not_fully_verified',
            unresolved=unresolved.get(ref, []), blocked_dependencies=blocked.get(ref, []))
            for ref in evidence.required_ids},
        retained_steps=generation_result.get('candidate_steps', []),
        known_diagnostic_suites=[dict(name=f'authored_{i}', replayed_in=f'authored_{owners[i]}', hashes=s['hashes'],
            requirement_ids=sorted({c['requirement_id'] for c in s['plan']['cases']}))
            for i, s in enumerate(collections)],
        full_verification_accepted=False)
    if baseline_source is not None:
        report['required_replays'] = required
    for step in _retained_slices(generation_result):
        origin = step['source_sha256']
        for ref in step['requirement_ids']:
            match = next((row for row in report.get('required_replays', {}).values()
                          if row.get('requirement_id') == ref and row.get('scenario_id') == step['scenario_id']
                          and row['baseline_source_sha256'] == origin), None)
            status = 'observed_on_same_source' if origin == source else 'not_rechecked'
            if match and match['status'] in {'passed', 'failed'}:
                status = 'replayed_passed' if match['status'] == 'passed' else 'replayed_failed'
            report.setdefault('retained_slice_observations', []).append(dict(
                step=step['id'], requirement_id=ref, scenario_id=step['scenario_id'], status=status,
                origin_source_sha256=origin, checked_source_sha256=None if status == 'not_rechecked' else source))
    (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def run_candidate_suites(app, suites, directory, settings, deadline):
    from .platform_generation import sync_project_copy, safe_rmtree, PLAYWRIGHT_CACHE_NAME
    scratch = Path(tempfile.mkdtemp(prefix='arc-candidate-check-'))
    try:
        sync_project_copy(app, scratch / 'project')
        return run_runtime_check(scratch / 'project', suites=suites, work_dir=directory / 'execution',
            playwright_cache=Path(tempfile.gettempdir()) / PLAYWRIGHT_CACHE_NAME,
            deadline=deadline, timeouts={'test': settings.runtime_test_timeout_ms / 1000})
    finally:
        safe_rmtree(scratch)


def smoke_passed(feedback):
    signal = feedback.get('signals', {}).get('candidate_smoke', {})
    return (feedback.get('stage') == 'tests' and feedback.get('status') in {'passed', 'failed'}
            and type(signal.get('passed')) is int and signal['passed'] == 1
            and signal.get('failed') == signal.get('skipped') == 0 and signal.get('failures') == [])


def _candidate_unready(evidence, model, attempts, reason, generation_result=None):
    row = attempts[-1] if attempts else {}
    runtime = row.setdefault('runtime', dict(status='not_run', reason=reason))
    return dict(version=1, scope='runnable_candidate_not_full_verification', ready=False,
        status='NOT_READY', formal_status='UNRUN', full_verification_accepted=False,
        generation_status=(generation_result or {}).get('status', 'stopped'),
        generation_stop_reason=(generation_result or {}).get('reason', 'truncated'),
        original_controller_stop=model.execution_control.stopped, model_requests_closed=True,
        source_sha256=row.get('source_sha256'), requirements_sha256=evidence.requirements_sha256,
        static_validation=row.get('static_validation'), runtime=runtime,
        closeout_reason=reason, selected_source=None, selection_attempts=attempts,
        requirements={ref: dict(status='not_fully_verified') for ref in evidence.required_ids},
        usage_accounting=dict(model.campaign_budget.state) if model.campaign_budget is not None else None)


def _check_deferred_candidate(app, evidence, model, settings, *, ir, reports,
                              generation_result, deadline, assert_input_integrity, attempts=None):
    """At most two independently validated, prebound sources under the original permit."""
    from .platform_generation import RUNTIME_MIN_SECONDS, load_task_ir, monotonic
    from .platform_static import missing_coverage, validate_source
    control = model.execution_control
    truncated = generation_result.get('reason') == 'truncated'
    claims_draft = is_source_claim_draft(settings, generation_result)
    checkpoint = engineering_checkpoint(evidence, settings, generation_result)
    control.check_diagnostics()
    if claims_draft and generation_result['reason'] == 'request_allowance' and control.stopped is None:
        try:
            control.stop('request_allowance')
        except ExecutionStopped as stopped:
            if stopped.reason != 'request_allowance':
                raise
    if claims_draft and control.stopped != generation_result['reason']:
        raise ExecutionStopped(control.stopped or 'candidate_stop_mismatch')
    if (control.stopped != ('truncated' if truncated else None)
            and not ((checkpoint or claims_draft) and control.stopped == generation_result.get('reason'))):
        raise ExecutionStopped(control.stopped or 'truncation_stop_mismatch')
    if attempts is None:
        attempts = []
    base_files = control.source(app)
    base_hash = source_digest(base_files)
    retained_base = (base_hash in generation_result.get('previous_step_sources', ())
        or base_hash == generation_result.get('committed_source_sha256')
        or any(step.get('source_sha256') == base_hash
               for key in ('completed_steps', 'candidate_steps') for step in generation_result.get(key, ())))
    baseline = _DeliverySource('baseline', app, base_files, base_hash)
    baseline_available = retained_base or (not truncated and (not checkpoint or generation_result.get('status') == 'complete'))
    if checkpoint and has_only_starter_runtime(base_files):
        baseline_available = False
    quarantine = (evidence.step_gate.directory if evidence.step_gate is not None else reports/'step-behavior').resolve()

    def retained_source(label, path, expected):
        path = Path(path).resolve()
        if path == quarantine or not path.is_relative_to(quarantine):
            raise ValueError('candidate outside controller quarantine')
        files = control.source(path)
        if source_digest(files) != expected:
            raise ValueError('candidate or accepted source changed after rollback')
        return _DeliverySource(label, path, files, expected)

    candidate = None
    candidate_source = generation_result.get('candidate_source')
    if candidate_source and (claims_draft or truncated or generation_result.get('reason') in PHASE_BUDGET_STOPS):
        label = 'source_claim_draft' if claims_draft else 'truncated' if truncated else 'deferred'
        original = retained_source(label, candidate_source,
                                   generation_result.get('candidate_source_sha256'))
        if base_hash != generation_result.get('candidate_base_sha256'):
            raise ValueError('candidate or accepted source changed after rollback')
        if claims_draft and (not retained_base or not baseline_available):
            # A first, rolled-back step cannot displace the existing engineering checkpoint.
            claims_draft = False
        elif original.source_sha256 != base_hash:
            candidate = original
        if label != 'source_claim_draft' or claims_draft:
            evidence.candidate = dict(status='quarantined', source_sha256=original.source_sha256,
                base_source_sha256=base_hash, reason=generation_result['reason'])
            if truncated or claims_draft:
                evidence.candidate['acceptance'] = 'unverified'
                for key in ('step', 'attempt', 'revision'):
                    if 'candidate_' + key in generation_result:
                        evidence.candidate[key] = generation_result['candidate_' + key]
    choices = [candidate] if candidate else []
    if baseline_available:
        choices.append(baseline)
    if checkpoint and not claims_draft and len(choices) < 2:
        saved = retained_source('engineering_checkpoint', checkpoint['path'], checkpoint['source_sha256'])
        if all(choice.source_sha256 != saved.source_sha256 for choice in choices):
            choices.append(saved)
    if not choices:
        return _candidate_unready(evidence, model, attempts, 'no_truncated_candidate')
    first = dict(selection=choices[0].selection, source_sha256=choices[0].source_sha256, ready=False)
    attempts.append(first)
    if truncated:
        if not settings.runtime_check:
            return _candidate_unready(evidence, model, attempts, 'runtime_disabled_by_settings')
        for executable in ('node', 'npm'):
            if not tool(executable):
                return _candidate_unready(evidence, model, attempts, executable + '_unavailable')
    permit = control.begin_candidate_closeout(source_sha256=choices[0].source_sha256,
        fallback_source_sha256=choices[1].source_sha256 if len(choices) == 2 else None)
    evidence.delivery_permit = permit

    def guard():
        control.check_candidate_closeout(permit)
        assert_input_integrity()
        if control.source(app) != base_files or any(control.source(s.path) != s.files for s in choices if s.path != app):
            raise ValueError('candidate or baseline changed during closeout selection')

    def probe(path, row, probe_deadline, **checks):
        guard()
        _require_runtime_window(probe_deadline, control, row)
        model.reserve_validation_steps(1, diagnostic=True)
        static = validate_source(path, include_test_evidence=False)
        row['static_validation'] = static
        if (truncated or checkpoint or claims_draft) and not static.get('passed'):
            row['runtime'] = dict(status='not_run', reason='static_validation_failed')
        (reports/'candidate-selection.json').write_text(json.dumps(attempts, indent=2), encoding='utf-8')
        if not static.get('passed'):
            guard()
            return None
        evidence.static = static
        selected_ir = load_task_ir(path, ir)
        evidence.missing = missing_coverage(path, selected_ir)
        report = _check_source(path, evidence, model, settings, ir=selected_ir,
            directory=reports/'candidate-readiness'/row['selection'], generation_result=generation_result,
            deadline=probe_deadline, permit=permit, attempt=row if truncated or checkpoint or claims_draft else None,
            **checks)
        guard()
        if (checks or row.get('time_reserved_before_draft')) and monotonic() >= min(probe_deadline, control.deadline):
            row['reason'] = 'candidate_diagnostic_time_unavailable'
            report.update(ready=False, status='NOT_READY', closeout_reason=row['reason'])
            (reports/'candidate-readiness'/row['selection']/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        row.update(ready=report['ready'], runtime=report['runtime'],
                   reserved_validation_steps=report['reserved_validation_steps'])
        (reports/'candidate-selection.json').write_text(json.dumps(attempts, indent=2), encoding='utf-8')
        return report

    baseline_slices = {(step['scenario_id'], ref) for step in _retained_slices(generation_result)
        if step['source_sha256'] == base_hash
        for ref in step.get('requirement_ids', ())}
    baseline_time_reserved = False
    for index, (identity, path, files, digest) in enumerate(choices):
        row = first
        if index:
            control.select_candidate_fallback(permit, source_sha256=digest)
            row = dict(selection=identity, source_sha256=digest, ready=False)
            if baseline_time_reserved:
                row['time_reserved_before_draft'] = True
            attempts.append(row)
        claims_selection = identity == 'source_claim_draft'
        if claims_selection or (digest != base_hash and baseline_slices):
            # An observed public slice is a source-bound regression check. Its
            # success on a replacement never upgrades the original requirement.
            replays = [r for r in evidence.step_gate.passed_scenario_replays(base_hash)
                       if (r['scenario_id'], r['requirement_id']) in baseline_slices
                       or (claims_selection and r.get('kind') != 'retained_public_slice')]
            fallback = any(choice.source_sha256 == base_hash for choice in choices[index+1:])
            collections, _, selected, validation_steps = _candidate_checks(evidence)
            baseline_cost = 1 + validation_steps
            draft_cost = baseline_cost + sum(item['operations'] for item in replays)
            protected = max(baseline_cost, control.protected_final_tools) if fallback else 0
            fallback_seconds = RUNTIME_MIN_SECONDS if fallback else 0
            draft_deadline = min(deadline, control.deadline) - fallback_seconds
            row.update(baseline_source_sha256=base_hash, fallback_reserved_tools=protected,
                       fallback_reserved_seconds=fallback_seconds, draft_validation_steps=draft_cost,
                       required_replays=_required_replays(collections, selected, replays, digest, base_hash))
            observed = set(baseline_slices)
            if claims_selection:
                observed.update((d['scenario_id'], d['requirement_id'])
                    for d in getattr(evidence.step_gate, 'scenario_diagnostics', ())
                    if d['status'] == 'passed' and d['source_sha256'] == base_hash)
            available = {(r['scenario_id'], r['requirement_id']) for r in replays}
            if not observed <= available:
                row['runtime'] = dict(status='not_run', reason='incomplete_baseline_scenario_replays',
                    missing=[dict(scenario_id=sid, requirement_id=ref) for sid, ref in sorted(observed-available)])
                report = None
            elif model._remaining_tools() < draft_cost + protected or draft_deadline - monotonic() < RUNTIME_MIN_SECONDS:
                row['runtime'] = dict(status='not_run', reason='baseline_closeout_reserve')
                report = None
            else:
                baseline_time_reserved = fallback
                try:
                    report = probe(path, row, draft_deadline, baseline_source=base_hash, replays=replays)
                except _ChangedScenarioReplay as error:
                    guard()
                    row.update(ready=False, reason='baseline_scenario_suite_changed')
                    row.setdefault('runtime', dict(status='not_run', reason=str(error)))
                    for name, signal in row['runtime'].get('signals', {}).items():
                        if name in row['required_replays']:
                            row['required_replays'][name].update(status='unavailable', signal=signal, reason=row['reason'])
                    report = None
                except (ExecutionStopped, CampaignBudgetStopped) as error:
                    if error.reason not in {'candidate_diagnostic_time_unavailable', 'execution_deadline',
                                            'execution_tool_limit', 'platform_tool_allocation'}:
                        raise
                    guard()
                    row.update(ready=False, reason=error.reason)
                    row.setdefault('runtime', dict(status='not_run', reason=error.reason))
                    report = None
        else:
            report = probe(path, row, deadline)
        if report is not None and report['ready']:
            break
        if evidence.candidate is not None:
            evidence.candidate['status'] = 'not_delivered'
    if report is None:
        if truncated or checkpoint or claims_draft:
            reason = row.get('reason') or row.get('runtime', {}).get('reason') or 'static_validation_failed'
            return _candidate_unready(evidence, model, attempts, reason, generation_result)
        raise ValueError('candidate and baseline are not statically valid')
    if report['ready'] and path != app:
        guard()
        if identity == 'engineering_checkpoint':
            evidence.candidate = dict(status='quarantined', source_sha256=digest,
                base_source_sha256=base_hash, reason=identity, acceptance='unverified')
        if truncated and settings.delivery_policy == 'verified':
            evidence.candidate['status'] = 'checked_unverified'
        else:
            evidence.promotion_before = base_files
            evidence.promotion_after = files
            evidence.promotion_control = control
            apply_snapshot(app, files, before=base_files)
            if control.source(app, changed_by_controller=True) != files:
                raise ValueError('candidate promotion source mismatch')
            assert_input_integrity()
            control.check_candidate_closeout(permit)
            evidence.candidate['status'] = 'promoted'
    elif path == app and not candidate and evidence.candidate is not None:
        evidence.candidate['status'] = 'unchanged'
    control.source(app)
    report['selection_attempts'] = attempts
    report['selected_source'] = identity if report['ready'] or not (truncated or checkpoint or claims_draft) else None
    report['usage_accounting'] = dict(model.campaign_budget.state) if model.campaign_budget is not None else None
    return report


def finish_candidate_delivery(evidence, app, self_test_report, error):
    """A successful Stage 2 means the evidenced candidate was exported, never PASS."""
    from .platform_generation import _mark_generation_failed, _rollback_promotion
    from .platform_self_test import attach_candidate_delivery, record_delivery_failure, verify_exported_suites
    delivery = dict(evidence.delivery)

    def attach_export_report():
        exported = delivery['exported_source_sha256']
        delivery['scenario_diagnostics'] = [
            row for row in getattr(evidence.step_gate, 'scenario_diagnostics', ())
            if exported and row.get('source_sha256') == exported
        ]
        attach_candidate_delivery(evidence.artifacts, delivery)

    failures = []
    if error:
        failures.append('generation_error')
    source = source_digest(snapshot_project(app))
    delivery['exported_source_sha256'] = source
    delivery['export_integrity_at_delivery'] = False
    check_export = delivery.get('ready') or delivery.get('generation_stop_reason') != 'truncated'
    if check_export and delivery.get('source_sha256') != source:
        failures.append('candidate_source_mismatch')
    if delivery.get('ready') is not True:
        failures.append('candidate_readiness_not_passed')
    if any(not row.get('integrity') or not row.get('exported') for row in self_test_report.get('suites', [])):
        failures.append('candidate_diagnostic_export_incomplete')
    if evidence.control is None or evidence.runtime is None:
        failures.append('candidate_controller_missing')
    try:
        verify_exported_suites(evidence.artifacts, self_test_report)
        delivery['export_integrity_at_delivery'] = True
        if not failures:
            control = evidence.control
            control.record_verification(stage='BUILD', passed=True, source_sha256=source,
                evidence_id='candidate_runtime_readiness', evidence_kind='runtime', closeout=evidence.delivery_permit)
            control.finish_candidate_closeout(evidence.delivery_permit, source_sha256=source,
                evidence_id='candidate_runtime_readiness')
        delivery['delivery_failures'] = failures
        delivery['ready'] = not failures
        delivery['status'] = 'READY_FOR_EVALUATION' if not failures else 'NOT_READY'
        delivery['full_verification_accepted'] = self_test_report['accepted'] is True
        delivery['controller_task_state'] = evidence.control.task_state if evidence.control else None
        delivery['controller_stop_reason'] = evidence.control.stopped if evidence.control else None
        if failures:
            _rollback_promotion(evidence, app)
            delivery['exported_source_sha256'] = source_digest(snapshot_project(app))
            if evidence.candidate is not None:
                record_delivery_failure(evidence.artifacts, delivery['exported_source_sha256'], evidence.candidate)
        attach_export_report()
        if failures:
            _mark_generation_failed(evidence, app, 'Candidate not ready: ' + ', '.join(failures))
            return 1
        # Requirement completion events remain absent when full acceptance is false.
        evidence.runtime.events.mark_run_completed(
            'Runnable candidate exported with failures and unverified requirements; '
            'complete requirement acceptance is recorded separately; formal evaluation pending')
        return 0
    except Exception as caught:
        _rollback_promotion(evidence, app)
        delivery.update(ready=False, status='NOT_READY', delivery_failures=[
            *failures, str(getattr(caught, 'reason', type(caught).__name__))],
            delivery_error=str(caught)[:500])
        try:
            delivery['exported_source_sha256'] = source_digest(snapshot_project(app))
            if evidence.candidate is not None:
                record_delivery_failure(evidence.artifacts, delivery['exported_source_sha256'], evidence.candidate)
            attach_export_report()
        except Exception:
            pass  # An ownership/export failure cannot authorize a second writer.
        _mark_generation_failed(evidence, app, 'Candidate export rejected: ' + type(caught).__name__)
        return 1

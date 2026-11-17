"""Runnable candidate evidence, separate from complete requirement acceptance."""
from pathlib import Path
import json
import tempfile

from . import behavior_checks
from .platform_execution import ExecutionStopped, PHASE_BUDGET_STOPS, source_digest
from .platform_runtime_check import run_runtime_check
from .sandbox_files import apply_snapshot, snapshot_project


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


def check_candidate_delivery(app, evidence, model, settings, *, ir, reports, generation_result, deadline,
                             assert_input_integrity=lambda: None):
    """No model calls. Build/start/check one exact source with frozen diagnostics."""
    control = model.execution_control
    control.check_diagnostics()
    assert_input_integrity()
    if (generation_result.get('reason') in PHASE_BUDGET_STOPS
            and generation_result.get('candidate_source') and evidence.step_gate is not None):
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


def _check_source(app, evidence, model, settings, *, ir, directory, generation_result, deadline, permit):
    """Probe only the currently selected source under an already sealed permit."""
    from .platform_generation import sync_project_copy, safe_rmtree, PLAYWRIGHT_CACHE_NAME
    control = model.execution_control
    source = source_digest(control.source(app))
    if source != control.candidate_closeout_source(permit):
        raise ValueError('candidate probe source is not selected by closeout')
    smoke = directory / 'smoke'
    smoke_hashes = behavior_checks.write_frozen_suite(smoke, {'candidate.spec.cjs': SMOKE.encode('utf-8')})
    evidence.suites.append(dict(directory=smoke, hashes=smoke_hashes, scope='runnability_only'))
    collections = list(evidence.step_gate.collections.values()) if evidence.step_gate else []
    for suite in collections:
        if not behavior_checks.verify_frozen(suite['directory'], suite['hashes']):
            raise ValueError('candidate authored suite changed before execution')
    suites = [('candidate_smoke', smoke)] + [(f'authored_{i}', s['directory']) for i, s in enumerate(collections)]
    # Navigation, HTTP status, body visibility, rendered content and page errors
    # are five bounded observations; authored operations retain their own count.
    validation_steps = 5 + sum(len(case['steps']) for suite in collections for case in suite['plan']['cases'])
    model.reserve_validation_steps(validation_steps, diagnostic=True)
    scratch = Path(tempfile.mkdtemp(prefix='arc-candidate-check-'))
    try:
        sync_project_copy(app, scratch / 'project')
        result = run_runtime_check(scratch / 'project', suites=suites, work_dir=directory / 'execution',
            playwright_cache=Path(tempfile.gettempdir()) / PLAYWRIGHT_CACHE_NAME,
            deadline=min(deadline, control.deadline), timeouts={'test': settings.runtime_test_timeout_ms / 1000})
    finally:
        safe_rmtree(scratch)
    control.check_candidate_closeout(permit)
    if source_digest(control.source(app)) != source:
        raise ValueError('candidate source changed during readiness check')
    if not behavior_checks.verify_frozen(smoke, smoke_hashes) or any(
            not behavior_checks.verify_frozen(s['directory'], s['hashes']) for s in collections):
        raise ValueError('candidate diagnostics changed during readiness check')
    feedback = result.to_feedback()
    signal = feedback.get('signals', {}).get('candidate_smoke', {})
    ready = (result.stage == 'tests' and result.status in {'passed', 'failed'}
             and type(signal.get('passed')) is int and signal['passed'] == 1
             and signal.get('failed') == signal.get('skipped') == 0 and signal.get('failures') == [])
    contracts = behavior_checks.requirement_contracts(ir)
    report = dict(version=1, scope='runnable_candidate_not_full_verification', ready=ready,
        status='READY_FOR_EVALUATION' if ready else 'NOT_READY', formal_status='UNRUN',
        source_sha256=source, requirements_sha256=evidence.requirements_sha256,
        static_validation=evidence.static, runtime=feedback, smoke_hashes=smoke_hashes,
        reserved_validation_steps=validation_steps,
        generation_status=generation_result.get('status'), generation_stop_reason=generation_result.get('reason'),
        generation_stop_detail=generation_result.get('detail'), generation_stop_phase=generation_result.get('phase'),
        original_controller_stop=permit.stop_reason,
        model_requests_closed=True,
        usage_accounting=dict(model.campaign_budget.state) if model.campaign_budget is not None else None,
        missing_source_coverage=evidence.missing,
        requirements={ref: dict(status='not_fully_verified',
            unresolved=contracts.get(ref, {}).get('unresolved', ['public_requirement_missing']))
            for ref in evidence.required_ids},
        retained_steps=generation_result.get('candidate_steps', []),
        known_diagnostic_suites=[dict(name=f'authored_{i}', hashes=s['hashes'],
            requirement_ids=sorted({c['requirement_id'] for c in s['plan']['cases']}))
            for i, s in enumerate(collections)],
        full_verification_accepted=False)
    (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def _check_deferred_candidate(app, evidence, model, settings, *, ir, reports,
                              generation_result, deadline, assert_input_integrity):
    """One candidate probe and, if funded, one prebound baseline fallback."""
    from .platform_generation import RUNTIME_MIN_SECONDS, load_task_ir, monotonic
    from .platform_static import missing_coverage, validate_source
    control = model.execution_control
    # A local phase yield does not authorize recovery from any controller stop.
    if control.stopped is not None:
        raise ExecutionStopped(control.stopped)
    base_files = control.source(app)
    base_hash = source_digest(base_files)
    candidate = Path(generation_result['candidate_source']).resolve()
    quarantine = evidence.step_gate.directory.resolve()
    if candidate == quarantine or not candidate.is_relative_to(quarantine):
        raise ValueError('candidate outside controller quarantine')
    candidate_files = control.source(candidate)
    candidate_hash = source_digest(candidate_files)
    if (candidate_hash != generation_result.get('candidate_source_sha256')
            or base_hash != generation_result.get('candidate_base_sha256')):
        raise ValueError('candidate or accepted source changed after rollback')
    same_source = candidate_hash == base_hash
    permit = control.begin_candidate_closeout(source_sha256=candidate_hash,
        fallback_source_sha256=None if same_source else base_hash)
    evidence.delivery_permit = permit
    evidence.candidate = dict(status='quarantined', source_sha256=candidate_hash,
        base_source_sha256=base_hash, reason=generation_result['reason'])
    attempts = []

    def guard():
        control.check_candidate_closeout(permit)
        assert_input_integrity()
        if control.source(app) != base_files or control.source(candidate) != candidate_files:
            raise ValueError('candidate or baseline changed during closeout selection')

    def probe(path, identity):
        guard()
        if min(deadline, control.deadline) - monotonic() < RUNTIME_MIN_SECONDS:
            raise ExecutionStopped('candidate_diagnostic_time_unavailable')
        model.reserve_validation_steps(1, diagnostic=True)
        static = validate_source(path, include_test_evidence=False)
        row = dict(selection=identity, source_sha256=source_digest(control.source(path)),
                   static_validation=static, ready=False)
        attempts.append(row)
        (reports/'candidate-selection.json').write_text(json.dumps(attempts, indent=2), encoding='utf-8')
        if not static.get('passed'):
            guard()
            return None
        evidence.static = static
        selected_ir = load_task_ir(path, ir)
        evidence.missing = missing_coverage(path, selected_ir)
        report = _check_source(path, evidence, model, settings, ir=selected_ir,
            directory=reports/'candidate-readiness'/identity, generation_result=generation_result,
            deadline=deadline, permit=permit)
        guard()
        row.update(ready=report['ready'], runtime=report['runtime'],
                   reserved_validation_steps=report['reserved_validation_steps'])
        (reports/'candidate-selection.json').write_text(json.dumps(attempts, indent=2), encoding='utf-8')
        return report

    report = probe(app if same_source else candidate, 'baseline' if same_source else 'deferred')
    if same_source:
        if report is None:
            raise ValueError('unchanged deferred source is not statically valid')
        evidence.candidate['status'] = 'unchanged'
        control.source(app)
    elif report is not None and report['ready']:
        guard()
        evidence.promotion_before = base_files
        evidence.promotion_after = candidate_files
        evidence.promotion_control = control
        apply_snapshot(app, candidate_files, before=base_files)
        if control.source(app, changed_by_controller=True) != candidate_files:
            raise ValueError('candidate promotion source mismatch')
        assert_input_integrity()
        control.check_candidate_closeout(permit)
        evidence.candidate['status'] = 'promoted'
    else:
        guard()
        control.select_candidate_fallback(permit, source_sha256=base_hash)
        evidence.candidate['status'] = 'not_delivered'
        report = probe(app, 'baseline')
        if report is None:
            raise ValueError('candidate and baseline are not statically valid')
        # guard() observes both sources and leaves current_source on the candidate.
        control.source(app)
    report['selection_attempts'] = attempts
    report['selected_source'] = 'deferred' if evidence.candidate['status'] == 'promoted' else 'baseline'
    report['usage_accounting'] = dict(model.campaign_budget.state) if model.campaign_budget is not None else None
    return report


def finish_candidate_delivery(evidence, app, self_test_report, error):
    """A successful Stage 2 means the evidenced candidate was exported, never PASS."""
    from .platform_generation import _mark_generation_failed, _rollback_promotion
    from .platform_self_test import attach_candidate_delivery, record_delivery_failure, verify_exported_suites
    delivery = dict(evidence.delivery)
    failures = []
    if error:
        failures.append('generation_error')
    source = source_digest(snapshot_project(app))
    delivery['exported_source_sha256'] = source
    delivery['export_integrity_at_delivery'] = False
    if delivery.get('source_sha256') != source:
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
        attach_candidate_delivery(evidence.artifacts, delivery)
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
            attach_candidate_delivery(evidence.artifacts, delivery)
        except Exception:
            pass  # An ownership/export failure cannot authorize a second writer.
        _mark_generation_failed(evidence, app, 'Candidate export rejected: ' + type(caught).__name__)
        return 1

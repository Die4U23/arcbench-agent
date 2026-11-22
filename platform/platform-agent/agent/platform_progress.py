"""Bounded continuation from observed application progress and physical usage.

The caller owns request admission, application hashing, and coverage observation.
This module never changes source, coverage claims, budget counters, or verification
results. A coverage-complete return is not a claim of independent behavior testing.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from math import inf


LOCAL_CONTINUATION_REASONS = frozenset({
    'request_allowance', 'coverage_incomplete', 'static_validation_failed',
    'step_behavior_unverified', 'model_incomplete', 'empty_response',
})
_HISTORY_FIELDS = ('completed_steps', 'candidate_steps', 'plan_history', 'local_failures')


def _counter(request_count):
    count = request_count()
    if type(count) is not int or count < 0:
        raise ValueError('request_count must return a nonnegative physical request count')
    return count


def _observe(observe):
    observed = observe()
    if not isinstance(observed, Mapping):
        raise ValueError('observe must return a mapping')
    observed = deepcopy(dict(observed))
    digest = observed.get('source_sha256')
    if not isinstance(digest, str) or not digest:
        raise ValueError('observe must provide the application source digest')
    missing = observed.get('missing')
    if (not isinstance(missing, list) or any(
            not isinstance(item, Mapping)
            or not all(isinstance(item.get(key), str) and item[key] for key in ('kind', 'ref'))
            for item in missing)):
        raise ValueError('observe.missing must contain stable kind/ref identities')
    pending = observed.get('pending_requirement_ids')
    if not isinstance(pending, list) or any(not isinstance(ref, str) or not ref for ref in pending):
        raise ValueError('observe.pending_requirement_ids must contain requirement IDs')
    observed['pending_requirement_ids'] = list(dict.fromkeys(pending))
    return observed


def _state_key(observed):
    # Reasons, ordering, duplicate rows and claim prose must not evade the guard.
    identities = tuple(sorted({(item['kind'], item['ref']) for item in observed['missing']}))
    return observed['source_sha256'], identities


def _history_entry(item, round_number):
    if isinstance(item, Mapping):
        return dict(deepcopy(item), progress_round=round_number)
    if isinstance(item, list):
        return [_history_entry(value, round_number) for value in item]
    return dict(value=deepcopy(item), progress_round=round_number)


def run_progress_rounds(requirement_ids, allowance, *, implement, observe, request_count,
                        can_continue, on_result, feedback_builder, work_request_count=None):
    """Implement, inspect, and continue only locally recoverable source progress.

    Callbacks are synchronous. ``implement(ids, remaining, feedback)`` returns
    the implementation result; ``request_count()`` reads the monotonic shared
    physical ledger. An optional ``work_request_count`` discounts only service
    attempts that provided no model response when charging the local share.
    A ``None`` share uses the controller's token and time admission.
    ``observe()`` returns ``source_sha256``, ``missing`` rows
    with kind/ref identities, and their owning ``pending_requirement_ids``.
    The digest must exclude coverage bookkeeping and other controller artifacts.

    ``on_result(result)`` records each round with measured ``turns`` and its
    continuation decision. ``feedback_builder(observation, result)`` prepares
    the next round's feedback. All callbacks except implement are read-only;
    in particular, they must not issue model requests. Callback exceptions are
    deliberately not swallowed, including source-safety and controller errors.
    """
    if allowance is not None and (type(allowance) is not int or allowance < 0):
        raise ValueError('allowance must be a nonnegative physical request count')
    if isinstance(requirement_ids, (str, bytes)):
        raise ValueError('requirement_ids must be a sequence of requirement IDs')
    selection = list(dict.fromkeys(requirement_ids))
    if any(not isinstance(ref, str) or not ref for ref in selection):
        raise ValueError('requirement_ids must contain nonempty strings')
    authorized = set(selection)
    limit = inf if allowance is None else allowance

    def remaining_share(used):
        return None if allowance is None else max(0, allowance - used)

    started = _counter(request_count)
    work_started = _counter(work_request_count) if work_request_count else started
    previous_work = work_started

    def work_at(physical, previous):
        count = _counter(work_request_count) if work_request_count else physical
        if count < previous or count - work_started > physical - started:
            raise ValueError('work request ledger is inconsistent with physical requests')
        return count
    previous_count = started
    observed = _observe(observe)
    seen = {_state_key(observed)}
    history = {field: [] for field in _HISTORY_FIELDS}
    rounds = []
    feedback = None
    last = {}
    effective_reason = None

    def finish(status, reason, code, message, *, source_changed=False):
        ending = _counter(request_count)
        if ending < previous_count:
            raise ValueError('physical request counter decreased; continuation cannot refund requests')
        used = ending - started
        work_used = work_at(ending, previous_work) - work_started
        if work_used > limit:
            status, reason, code = 'stopped', 'request_allowance_exceeded', 'request_allowance_exceeded'
            message = 'The measured work requests exceed this implementation allowance.'
        result = deepcopy(last)
        result.update(status=status, reason=reason, turns=used, work_turns=work_used,
                      physical_requests_start=started, physical_requests_end=ending,
                      request_allowance=allowance, requests_remaining=remaining_share(work_used),
                      missing_coverage=deepcopy(observed['missing']),
                      pending_requirement_ids=list(observed['pending_requirement_ids']),
                      progress_rounds=deepcopy(rounds), **deepcopy(history))
        if last.get('status') == 'stopped':
            result['pending_requirement_ids'] = list(dict.fromkeys(
                result['pending_requirement_ids'] + list(last.get('pending_requirement_ids') or [])))
        if status == 'complete':
            result.pop('stop_detail', None)
        else:
            detail = dict(code=code, message=message, round=len(rounds),
                          implementation_reason=last.get('reason'), source_changed=source_changed,
                          requests_used=used, requests_remaining=remaining_share(work_used),
                          source_sha256=observed['source_sha256'], missing_count=len(observed['missing']),
                          pending_requirement_ids=list(result['pending_requirement_ids']))
            if last.get('stop_detail') is not None:
                detail['implementation_stop_detail'] = deepcopy(last['stop_detail'])
            result['stop_detail'] = detail
        return result

    if not selection:
        if not observed['missing'] and not observed['pending_requirement_ids']:
            return finish('complete', None, 'nothing_pending', 'No requirements are pending.')
        return finish('stopped', 'coverage_incomplete', 'pending_scope_unresolved',
                      'Missing work exists, but no implementation requirement scope was supplied.')

    while True:
        before_count = _counter(request_count)
        if before_count < previous_count:
            raise ValueError('physical request counter decreased; continuation cannot refund requests')
        before_work = work_at(before_count, previous_work)
        remaining = remaining_share(before_work - work_started)
        if remaining == 0:
            return finish('stopped', effective_reason or 'request_allowance',
                          'request_allowance_exhausted', 'The implementation request allowance is exhausted.')
        if not can_continue():
            return finish('stopped', effective_reason or 'continuation_blocked',
                          'continuation_blocked', 'The shared controller did not authorize another round.')

        before = observed
        raw = implement(list(selection), remaining, feedback)
        after_count = _counter(request_count)
        if after_count < before_count:
            raise ValueError('physical request counter decreased during implementation')
        previous_count = after_count
        after_work = work_at(after_count, before_work)
        if after_work - before_work > after_count - before_count:
            raise ValueError('work request cost exceeds physical requests in the round')
        previous_work = after_work
        if not isinstance(raw, Mapping):
            raise ValueError('implement must return a result mapping')
        last = deepcopy(dict(raw))
        observed = _observe(observe)
        spent = after_count - before_count
        work_used = after_work - work_started
        remaining = remaining_share(work_used)
        changed = before['source_sha256'] != observed['source_sha256']
        state = _state_key(observed)
        repeated = state in seen
        seen.add(state)
        pending = list(observed['pending_requirement_ids'])
        if last.get('status') == 'stopped':
            pending = list(dict.fromkeys(pending + list(last.get('pending_requirement_ids') or [])))
        outstanding = bool(observed['missing'] or pending)
        implementation_reason = last.get('reason')
        reason = implementation_reason or ('coverage_incomplete' if last.get('status') == 'complete'
                                           else 'model_incomplete')

        decision, code, message = 'stop', '', ''
        if work_used > limit:
            reason, code = 'request_allowance_exceeded', 'request_allowance_exceeded'
            message = 'Implementation consumed more work requests than its supplied allowance.'
        elif last.get('cost_budget_exhausted'):
            code = 'nonlocal_stop'
            message = 'The remaining time or tools cannot fund the selected work.'
        elif last.get('status') == 'stopped' and last.get('yielded_to_independent_window') is True:
            code = 'independent_window_available'
            message = 'The failed work remains pending. The scheduler can consider an independent window.'
        elif implementation_reason and implementation_reason not in LOCAL_CONTINUATION_REASONS:
            code = 'nonlocal_stop'
            message = 'The implementation stop is outside the local continuation policy; preserve it for the controller.'
        elif last.get('status') == 'complete' and not implementation_reason and not outstanding:
            decision, reason, code = 'complete', None, 'coverage_complete'
            message = 'Implementation is complete and the observer reports no missing or pending work.'
        elif reason not in LOCAL_CONTINUATION_REASONS:
            code = 'nonlocal_stop'
            message = 'The stop reason is not eligible for another local implementation round.'
        elif last.get('status') == 'stopped' and last.get('blocked_requirement_ids'):
            code = 'unresolved_step_failures'
            message = 'Failed transactions and their dependent work remain pending.'
        elif spent == 0:
            code = 'no_physical_request_progress'
            message = 'No physical request was consumed; another round could repeat without reducing its allowance.'
        elif not changed:
            code = 'no_application_source_progress'
            message = 'Application source did not change; coverage declarations alone do not authorize continuation.'
        elif repeated:
            code = 'repeated_source_and_missing_state'
            message = 'This application source and set of missing identities were already observed; stop oscillation.'
        elif not pending:
            code = 'pending_scope_unresolved'
            message = 'Missing or incomplete work has no resolved owning requirement; do not retry the whole task.'
        elif not set(pending) <= authorized:
            code = 'pending_scope_outside_selection'
            message = 'Observed pending requirements extend beyond this implementation scope.'
        elif remaining == 0:
            code = 'request_allowance_exhausted'
            message = 'The implementation request allowance is exhausted; completed progress remains available.'
        elif not can_continue():
            code = 'continuation_blocked'
            message = 'The shared controller did not authorize another round.'
        else:
            decision, code = 'continue', 'source_progress_with_pending_work'
            message = 'Changed application source and measured request capacity permit a bounded pending-scope round.'

        effective_reason = reason
        round_number = len(rounds) + 1
        for field in _HISTORY_FIELDS:
            values = last.get(field, [])
            if not isinstance(values, list):
                raise ValueError('implementation result ' + field + ' must be a list')
            history[field].extend(_history_entry(item, round_number) for item in values)
        round_result = deepcopy(last)
        round_result.update(progress_round=round_number, reported_turns=last.get('turns'), turns=spent,
                            reported_status=last.get('status'), reported_reason=implementation_reason,
                            status='complete' if decision == 'complete' else 'stopped', reason=reason,
                            missing_coverage=deepcopy(observed['missing']),
                            pending_requirement_ids=list(pending),
                            progress_decision=decision, progress_detail=dict(code=code, message=message),
                            physical_requests_before=before_count, physical_requests_after=after_count,
                            work_requests_used=after_work - before_work,
                            requests_remaining=remaining, requirement_ids=list(selection),
                            source_before_sha256=before['source_sha256'],
                            source_sha256=observed['source_sha256'], source_changed=changed)
        rounds.append(deepcopy(round_result))
        on_result(deepcopy(round_result))
        if decision != 'continue':
            return finish('complete' if decision == 'complete' else 'stopped', reason, code, message,
                          source_changed=changed)
        feedback = feedback_builder(deepcopy(observed), deepcopy(round_result))
        selection = list(pending)

"""Deterministic soft allocation from public requirements and measured checks.

This policy ranks only controller-permitted, dependency-ready work. Its costs
are estimates, never request admission: the shared ledger still reserves every
full request and owns all hard resource limits.
"""
from __future__ import annotations

import math

from .platform_execution import reported_requests, request_horizon


class BudgetPlanner:
    def __init__(self, *, fixed_order=False):
        self.fixed_order = fixed_order
        self.observations = {}
        # One planner per task. A comparable workload can inform cost, while
        # its gain and acceptance remain local to the original observation key.
        self.cost_samples = {}
        self.completed_cost_samples = {}

    def observe_cost(self, profile, source, cost):
        sample = dict(estimated_cost=cost['requests'], estimated_tools=cost['tools'],
            estimated_seconds=cost['seconds'], estimate_source=source)
        self.cost_samples[profile] = sample
        if source.get('disposition') in {'committed', 'constructed_unverified'}:
            self.completed_cost_samples[profile] = dict(sample, construction_seconds=cost.get('construction_seconds'))

    def _completed_estimate(self, profile):
        if profile in self.completed_cost_samples:
            return self.completed_cost_samples[profile]
        # Width is the requirement count. Retain the verification workload and
        # optional observation kind identical. Reuse the nearest narrower
        # completion without assuming that cost grows linearly with width.
        narrower = (other for other in self.completed_cost_samples
                    if other[0] == profile[0] and other[2:] == profile[2:] and other[1] < profile[1])
        peer = max(narrower, key=lambda other: other[1], default=None)
        if peer is None:
            return {}
        sample = self.completed_cost_samples[peer]
        origin = dict(sample['estimate_source'], kind='comparable_step',
                      sample_width=peer[1], target_width=profile[1])
        return dict(sample, estimate_source=origin)

    def estimate_cost(self, profile):
        return self.cost_samples.get(profile) or self._completed_estimate(profile)

    def step_runway(self, profile, *, remaining_seconds, request_seconds, check_seconds, allow_repair=True):
        """Compare an edit, public check and one feedback-driven repair/recheck with the implementation window."""
        sample = self._completed_estimate(profile)
        construction = sample.get('construction_seconds')
        elapsed = None
        if construction is None:
            # A whole-step measurement includes its own checks. Compare it
            # with the phase estimate as a floor, rather than summing both.
            elapsed = sample.get('estimated_seconds')
            construction = request_seconds
            basis = 'request_runway'
        else:
            basis = 'implementation_phase'
        if construction is not None and request_seconds is not None:
            construction = max(construction, request_seconds)
        origin = dict(sample.get('estimate_source', {}), construction_basis=basis if construction is not None else 'unknown')
        if elapsed is not None:
            origin['elapsed_step_seconds'] = elapsed
        repair = 0.0
        if allow_repair:
            repair = None if request_seconds is None or check_seconds is None else request_seconds + check_seconds
        required = None
        if construction is not None and check_seconds is not None and repair is not None:
            required = construction + check_seconds + repair
            if elapsed is not None:
                required = max(required, elapsed)
        return dict(fits=None if required is None or remaining_seconds is None else required <= remaining_seconds,
                    required_seconds=required, remaining_seconds=remaining_seconds, construction_seconds=construction,
                    check_seconds=check_seconds, repair_seconds=repair, estimate_source=origin)

    def observe(self, action_id, *, requests, verified_gain=None, passed=None):
        if type(requests) is not int or requests < 0:
            raise ValueError('requests must be a nonnegative measured count')
        if verified_gain is not None and (not math.isfinite(verified_gain) or verified_gain < 0):
            raise ValueError('verified gain must be nonnegative or unknown')
        old = self.observations.get(action_id, {})
        self.observations[action_id] = dict(
            requests=max(requests, old.get('requests', 0)),
            verified_gain=verified_gain, passed=passed,
            no_gain=(old.get('no_gain', 0) + 1 if verified_gain == 0 else
                     old.get('no_gain', 0) if verified_gain is None else 0))

    def renew(self, *, current_share, spent, remaining, request_quantum,
              minimum_requests=1, protected_requests=0, source_progress=False):
        """Reallocate one bounded correction window without refunding any use.

        A changed candidate and an observed defect are admission inputs from
        the controller, not evidence that the requirement passed. The caller
        owns the one-correction limit. Phase/global request admission, token
        reservations, time and source-oscillation guards remain authoritative.
        """
        for name, value, minimum in (('spent', spent, 0), ('minimum_requests', minimum_requests, 1),
                                     ('protected_requests', protected_requests, 0),
                                     ('request_quantum', request_quantum, 1), ('current_share', current_share, 0),
                                     ('remaining requests', remaining.get('requests', 0), 0)):
            if value is None and name in {'request_quantum', 'current_share', 'remaining requests'}:
                continue
            if type(value) is not int or value < minimum:
                raise ValueError(name + ' must be a valid request count')
        quantum = request_horizon(request_quantum)
        requests = request_horizon(remaining.get('requests', 0))
        if minimum_requests > quantum or type(source_progress) is not bool:
            raise ValueError('invalid renewal admission')
        available = max(0, request_horizon(current_share) - spent)
        decision = dict(request_share=current_share, added_requests=0,
                        requests_spent=spent, existing_requests_remaining=reported_requests(available),
                        requested_window=request_quantum, protected_requests=protected_requests,
                        source_progress=source_progress, quality='unverified')
        if any(remaining.get(resource) is not None and remaining[resource] <= 0
               for resource in ('tokens', 'tools', 'seconds')):
            return dict(decision, reason='hard_resource_exhausted')
        if not source_progress:
            return dict(decision, reason='no_source_progress')
        if available >= quantum:
            return dict(decision, reason='existing_share_sufficient')
        spare = max(0, requests - protected_requests - available)
        extra = min(quantum - available, spare)
        if available + extra < minimum_requests or extra <= 0:
            return dict(decision, reason='protected_request_capacity')
        return dict(decision, request_share=reported_requests(current_share + extra), added_requests=reported_requests(extra),
                    reason='observed_progress_correction')

    def select(self, actions, *, remaining, completed=(), allowed=None):
        rows = list(actions)
        by_id = {row['id']: row for row in rows}
        if len(by_id) != len(rows):
            raise ValueError('budget action IDs must be unique')
        completed = set(completed)
        allowed = set(by_id) if allowed is None else set(allowed)
        requests = max(0, request_horizon(remaining.get('requests', 0)))
        hard_requests = request_horizon(remaining.get('hard_requests', requests))
        traces, eligible = [], []
        for row in rows:
            identity = row['id']
            observation_key = row.get('observation_key', identity)
            prior = self.observations.get(observation_key, {})
            minimum = row.get('minimum_requests', 1)
            if type(minimum) is not int or minimum < 1:
                raise ValueError('minimum requests must be a positive integer')
            cost = max(minimum, math.ceil(float(row.get('estimated_cost', 1))), prior.get('requests', 0))
            gain = row.get('expected_gain')
            confidence = float(row.get('confidence', 0))
            risk = float(row.get('risk', 0))
            if (gain is not None and (not math.isfinite(gain) or gain < 0)
                    or not 0 <= confidence <= 1 or not 0 <= risk <= 1):
                raise ValueError('invalid gain, confidence or risk estimate')
            required = bool(row.get('required', True))
            reason = None
            limiting_resource = None
            if identity in completed:
                reason = 'already_completed'
            elif identity not in allowed:
                reason = 'controller_disallowed'
            elif not set(row.get('dependencies', ())) <= completed:
                reason = 'dependency_pending'
            elif min(requests, request_horizon(row.get('request_ceiling', requests)), hard_requests
                     - row.get('protected_closeout', {}).get('requests', 0)) < minimum:
                reason, limiting_resource = 'budget_insufficient', 'requests'
            else:
                for resource in ('tokens', 'tools', 'seconds'):
                    available = remaining.get(resource)
                    estimate = row.get('estimated_' + resource)
                    if resource == 'tools' and available is not None:
                        available -= row.get('protected_closeout', {}).get('tools', 0)
                        if row.get('minimum_validation_tools'):
                            estimate = max(estimate or 0, row['minimum_validation_tools'])
                    if resource == 'seconds' and row.get('minimum_runtime_seconds'):
                        estimate = max(estimate or 0, row['minimum_runtime_seconds'])
                    if available is not None and (available <= 0 or
                            estimate is not None and estimate > available):
                        reason, limiting_resource = 'budget_insufficient', resource
                        break
            # A prerequisite carries a fraction of the public requirement gain
            # it unlocks. This prevents cheap leaves crowding out a foundation.
            dependents = {identity}
            while True:
                expanded = dependents | {other['id'] for other in rows
                    if other['id'] not in completed and set(other.get('dependencies', ())) & dependents}
                if expanded == dependents:
                    break
                dependents = expanded
            unlocked = sum(max(0, float(by_id[key].get('expected_gain') or 0))
                           for key in dependents - {identity})
            measured = prior.get('verified_gain')
            estimate = gain if measured is None else measured
            effective_confidence = confidence if measured is None else 1.
            # Unknown quality gets a bounded probe, not a fabricated zero or
            # guaranteed pass. All numbers here are declared heuristics.
            utility = ((1 if estimate is None else estimate * effective_confidence) + .25 * unlocked)
            utility *= (1 - risk) / (1 + prior.get('no_gain', 0))
            score = utility / cost
            if reason is None and not required and estimate is not None and utility <= 0:
                reason = 'low_expected_gain'
            item = dict(action_id=identity, observation_key=observation_key, estimated_requests=cost,
                        minimum_requests=minimum, estimated_tools=row.get('estimated_tools'),
                        estimated_seconds=row.get('estimated_seconds'), estimate_source=row.get('estimate_source'),
                        limiting_resource=limiting_resource, expected_gain=gain,
                        verified_gain=measured, quality='unknown' if measured is None else 'measured',
                        confidence=effective_confidence, risk=risk, dependency_gain=unlocked,
                        score=score, required=required, reason=reason,
                        estimate_is_guarantee=False)
            traces.append(item)
            if 'review_scope' in row:
                item['review_scope'] = row['review_scope']
            if 'protected_closeout' in row:
                item['protected_closeout'] = row['protected_closeout']
                item['minimum_runtime_seconds'] = row.get('minimum_runtime_seconds')
                item['minimum_validation_tools'] = row.get('minimum_validation_tools')
            if reason is None:
                eligible.append(item)
        if not eligible:
            reason = ('budget_insufficient' if any(t['reason'] == 'budget_insufficient' for t in traces)
                      else 'no_eligible_action')
            return dict(action_id=None, request_share=0, reason=reason, trace=traces)
        if any(item.get('review_scope') for item in eligible):
            fitted = [item for item in eligible if not item.get('review_scope')]
            if not fitted and remaining.get('can_replan_scope'):
                deferred = max(eligible, key=lambda item: item['score'])
                return dict(action_id=None, deferred_action_id=deferred['action_id'], request_share=0,
                            reason='step_window_changed', trace=traces)
            eligible = fitted or eligible
        selected = eligible[0] if self.fixed_order else max(eligible, key=lambda row: row['score'])
        # A selected action must have enough allowance for its minimum useful
        # unit (for example, implementation plus authoring). Preserve other
        # eligible minima only where affordable; never turn every action into
        # a zero-progress dispatch. These shares do not touch the hard ledger.
        minimum = selected['minimum_requests']
        other_minima = sum(row['minimum_requests'] for row in eligible if row is not selected)
        share = max(minimum, requests - other_minima)
        action = by_id[selected['action_id']]
        share = min(share, request_horizon(action.get('request_ceiling', selected['estimated_requests'])),
                    hard_requests - action.get('protected_closeout', {}).get('requests', 0))
        if selected['expected_gain'] is None and selected['verified_gain'] is None:
            share = min(share, max(2, minimum))
        return dict(action_id=selected['action_id'], request_share=None if self.fixed_order else reported_requests(share),
                    reason='selected', trace=traces)

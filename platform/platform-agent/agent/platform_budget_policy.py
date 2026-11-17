"""Deterministic soft allocation from public requirements and measured checks.

This policy ranks only controller-permitted, dependency-ready work. Its costs
are estimates, never request admission: the shared ledger still reserves every
full request and owns all hard resource limits.
"""
from __future__ import annotations

import math


class BudgetPlanner:
    def __init__(self):
        self.observations = {}

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

    def select(self, actions, *, remaining, completed=(), allowed=None):
        rows = list(actions)
        by_id = {row['id']: row for row in rows}
        if len(by_id) != len(rows):
            raise ValueError('budget action IDs must be unique')
        completed = set(completed)
        allowed = set(by_id) if allowed is None else set(allowed)
        requests = max(0, int(remaining.get('requests', 0)))
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
            if identity in completed:
                reason = 'already_completed'
            elif identity not in allowed:
                reason = 'controller_disallowed'
            elif not set(row.get('dependencies', ())) <= completed:
                reason = 'dependency_pending'
            elif requests < minimum:
                reason = 'budget_insufficient'
            else:
                for resource in ('tokens', 'tools', 'seconds'):
                    available = remaining.get(resource)
                    estimate = row.get('estimated_' + resource)
                    if available is not None and (available <= 0 or
                            estimate is not None and estimate > available):
                        reason = 'budget_insufficient'
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
                        minimum_requests=minimum, expected_gain=gain,
                        verified_gain=measured, quality='unknown' if measured is None else 'measured',
                        confidence=effective_confidence, risk=risk, dependency_gain=unlocked,
                        score=score, required=required, reason=reason,
                        estimate_is_guarantee=False)
            traces.append(item)
            if reason is None:
                eligible.append(item)
        if not eligible:
            reason = ('budget_insufficient' if any(t['reason'] == 'budget_insufficient' for t in traces)
                      else 'no_eligible_action')
            return dict(action_id=None, request_share=0, reason=reason, trace=traces)
        selected = max(eligible, key=lambda row: row['score'])
        # A selected action must have enough allowance for its minimum useful
        # unit (for example, implementation plus authoring). Preserve other
        # eligible minima only where affordable; never turn every action into
        # a zero-progress dispatch. These shares do not touch the hard ledger.
        minimum = selected['minimum_requests']
        other_minima = sum(row['minimum_requests'] for row in eligible if row is not selected)
        share = max(minimum, requests - other_minima)
        share = min(share, selected['estimated_requests'])
        if selected['expected_gain'] is None and selected['verified_gain'] is None:
            share = min(share, max(2, minimum))
        return dict(action_id=selected['action_id'], request_share=share,
                    reason='selected', trace=traces)

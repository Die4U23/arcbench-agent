"""Publish local requirement evidence without changing verification verdicts."""
from __future__ import annotations

import re

from .candidate_checkpoint import PUBLIC_CHECK
from .requirements import walk_requirement_ids
from .ticketbooking_contract import PUBLIC_CASES
from .ticketbooking_probes import JOURNEY_PROBES


def mark_subtree_progress(runtime, subtree, event: str, message: str) -> None:
    """A planning/implementation turn covers the descendants in its scope."""
    for node_id in dict.fromkeys(walk_requirement_ids(subtree)):
        getattr(runtime.events, event)(node_id, message)


def _expected_cases() -> dict[str, str]:
    # Reuse the protected case sources; no duplicated count or acceptance oracle.
    titles = re.findall(r'''\btest\(\s*(['"])(.*?)\1''',
                        '\n'.join(source for _, source in PUBLIC_CASES) + '\n' + JOURNEY_PROBES)
    result = {}
    for _, title in titles:
        match = re.match(r'^(REQ-[A-Za-z0-9._-]+):', title)
        if match:
            result[title] = match[1]
    return result


def _complete_public_cases(result) -> dict[str, list[tuple[str, str]]]:
    checks = [check for check in result.checks if check.name == PUBLIC_CHECK]
    if len(checks) != 1 or not checks[0].case_statuses:
        return {}
    expected = _expected_cases()
    observed = {}
    for full_title, status in checks[0].case_statuses:
        # Vitest may prefix fullName with a suite name. Match the whole known
        # title at a word boundary, never a requirement's numeric prefix.
        full_title = full_title.strip()
        matches = [title for title in expected
                   if full_title == title or full_title.endswith(' ' + title)]
        if len(matches) != 1 or matches[0] in observed or status not in {'passed', 'failed'}:
            return {}
        observed[matches[0]] = status
    if not expected or observed.keys() != expected.keys():
        return {}
    grouped = {}
    for title, status in observed.items():
        grouped.setdefault(expected[title], []).append((title, status))
    return grouped


def local_requirement_statuses(tree, result) -> tuple[dict[str, bool | None], dict]:
    """A complete case report can prove assertions despite a runner-level error.

    Such evidence never changes CheckResult/VerificationResult or ROOT's full
    verification verdict. Explicit requirement failures still take precedence.
    """
    cases = _complete_public_cases(result)
    statuses = {}

    def visit(node):
        node_id = str(node.get('id') or node.get('req_id') or '').strip()
        raw_children = node.get('children', [])
        children = [child for child in raw_children if isinstance(child, dict)] if isinstance(raw_children, list) else []
        child_statuses = [visit(child) for child in children]
        related = [check for check in result.checks if node_id in check.requirement_ids]
        failed = any(not check.passed and not check.output.startswith('Not executed:')
                     for check in related)
        unrun = any(check.output.startswith('Not executed:') for check in related)
        own_cases = cases.get(node_id, [])
        own = (all(status == 'passed' for _, status in own_cases) if own_cases else None)
        if node_id == 'ROOT':
            status = result.passed
        elif failed or own is False or any(value is False for value in child_statuses):
            status = False
        elif unrun:
            status = None
        elif result.passed:
            status = True
        elif children:
            # A folder with its own scenarios needs its own evidence as well.
            own_required = bool(node.get('scenarios'))
            status = (True if all(value is True for value in child_statuses)
                      and (not own_required or own is True) else None)
        else:
            status = own
        if node_id:
            statuses[node_id] = status
        return status

    visit(tree)
    return statuses, cases


def record_local_verification(runtime, tree, result) -> None:
    statuses, cases = local_requirement_statuses(tree, result)
    for node_id, status in statuses.items():
        if node_id == 'ROOT':
            message = ('Local whole-project verification ' + ('passed' if status else 'did not pass')
                       + '; separate from per-requirement assertions; formal Stage 3 acceptance pending')
        else:
            count = len(cases.get(node_id, []))
            message = (f'Local requirement evidence: {count} directly mapped reconstructed jsdom assertions; '
                       'folders aggregate their children; formal Stage 3 acceptance pending. '
                       f'Whole-project local verification passed={result.passed}')
        if status is True:
            runtime.events.mark_test_passed(node_id, message)
        elif status is False:
            failures = [check.name for check in result.checks
                        if not check.passed and (node_id == 'ROOT' or node_id in check.requirement_ids)]
            failed_cases = [title for title, value in cases.get(node_id, []) if value == 'failed']
            runtime.events.mark_test_failed(node_id, message + '; failed checks/cases: '
                                            + ', '.join([*failures, *failed_cases]))
        else:
            # Unknown on a later verification must not leave an older green/red
            # node behind. Preserve unfinished design/implementation progress.
            prior = runtime.traceability.get_requirement_state(node_id)
            if (isinstance(prior, dict) and prior.get('phase') == 'test'
                    and prior.get('state') in {'PASSED', 'FAILED'}):
                runtime.traceability.set_requirement_state(node_id, 'IMPLEMENTED', phase='implement')
        test_id = f'TEST-{node_id}'
        runtime.traceability.upsert_test(test_id=test_id, req_id=node_id,
                                        type='INTEGRATION', passed=status)
        runtime.traceability.set_test_pass_status(test_id, status)

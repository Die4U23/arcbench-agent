"""Bounded repairs of complete behavioral reports, retaining the full task context."""
from __future__ import annotations

import re

from .candidate_checkpoint import PUBLIC_CHECK
from .browser_contract import BROWSER_CASE, BROWSER_CHECK
from .booking_completion import CASE as COMPLETION_CASE
from .station_units import CASES as LOCATION_CASES
from .ticketbooking_contract import PUBLIC_CASES
from .verify import VerificationResult


def published_feedback_patterns(lines: list[str]) -> str:
    """Expose unchanged case predicates when generic count failures hide them."""
    literal = r'/(?:\\.|[^/\r\n])+/[dgimsuvy]*'
    evidence = []
    for _, source in PUBLIC_CASES:
        title_match = re.search(r'''test\(\s*(['"])(.*?)\1''', source)
        if not title_match:
            continue
        title = title_match[2]
        if not any(line.startswith(title + ':') and 'expected 0 to be greater than 0' in line
                   for line in lines):
            continue
        patterns = re.findall(r'expectVisibleFeedback\(\s*page\s*,\s*(' + literal + ')', source)
        if 'currentCase.expected' in source:
            patterns.extend(re.findall(r'expected\s*:\s*(' + literal + ')', source))
        patterns = list(dict.fromkeys(patterns))
        if patterns:
            evidence.append(title + '\n' + '\n'.join(patterns))
    if not evidence:
        return ''
    return ('Published feedback expressions from unchanged failing case sources '
            '(candidate predicates, not evidence that every loop input was reached):\n' +
            '\n\n'.join(evidence) + '\nCompare the observed alert with each applicable expression. '
            'Distinguish an empty required field from a nonempty value below its minimum; '
            'keep both the required-field message and boundary validation accurate.')


def failed_behavior_ids(result: VerificationResult) -> tuple[str, ...]:
    public = next((check for check in result.checks if check.name == PUBLIC_CHECK), None)
    if public is None or not public.case_statuses:
        return ()
    # Infrastructure failures require whole-project diagnosis before routing a
    # behavioral repair. Never infer a leaf from an incomplete runner report.
    if any(not check.passed and check.name in {
        'frontend: npm run build', 'web template structure', 'project dependencies',
        'frontend JavaScript undefined names'
    } for check in result.checks):
        return ()
    failed_ids = set()
    for title, status in public.case_statuses:
        # Vitest fullName can prepend a space for an empty ancestor title.
        # Normalize only for routing; retain the complete report's case identity.
        title = title.strip()
        if status == 'failed' and title.startswith('REQ-') and ':' in title:
            failed_ids.add(title.split(':', 1)[0].strip())
    native = next((check for check in result.checks if check.name == BROWSER_CHECK), None)
    if native is not None and not native.passed and not native.output.startswith('Not executed:'):
        # The probe's static requirement set also includes registration. Once
        # it actually reached booking and failed its exact Name textbox, route
        # that observed blocker first rather than sorting it behind registration
        # and the merged additional station checks. Keep every other identity.
        original_failed = dict(native.case_statuses).get(BROWSER_CASE) == 'failed'
        booking_name_failure = (native.output.startswith('search and booking with real CSS and backend:')
            and 'get_by_role("textbox"' in native.output and '^name$' in native.output)
        native_ids = native.requirement_ids
        if (dict(native.case_statuses).get(BROWSER_CASE) == 'passed'
                and dict(native.case_statuses).get(COMPLETION_CASE) == 'failed'):
            native_ids = ('REQ-3.2', *(node for node in native_ids if node != 'REQ-3.2'))
        if original_failed and booking_name_failure and 'REQ-3.2' in native_ids:
            native_ids = ('REQ-3.2', *(node for node in native_ids if node != 'REQ-3.2'))
        # A native registration blocker can invalidate many later journeys even
        # when reconstructed jsdom has passed their setup steps.
        # Native evidence changes priority, not the complete set of repair
        # obligations. Otherwise exhausted native families can hide an untried
        # public failure and make the controller stop the whole repair loop.
        return tuple(dict.fromkeys((*native_ids, *sorted(failed_ids))))
    return tuple(sorted(failed_ids))


def focused_subtree(tree: dict, requirement_id: str) -> dict | None:
    pending = [tree]
    while pending:
        node = pending.pop()
        if node.get('id') == requirement_id:
            # Copy the wrapper, not the immutable task. ModelClient uses the leaf
            # id to select the published cases while preserving cross-module context.
            return dict(node, project_requirements=tree)
        pending.extend(child for child in node.get('children', []) if isinstance(child, dict))
    return None


def _native_failure_families(native) -> set[str]:
    statuses = dict(native.case_statuses)
    if statuses.get(BROWSER_CASE) != 'passed':
        return set(native.requirement_ids)
    families = set()
    if statuses.get(COMPLETION_CASE) == 'failed':
        families.add('REQ-3.2')
    if any(statuses.get(case) == 'failed' for case in LOCATION_CASES):
        families.update(('REQ-2.1', 'REQ-2.2'))
    return families


def repair_guidance(result: VerificationResult, requirement_id: str | None) -> str:
    native = next((check for check in result.checks if check.name == BROWSER_CHECK), None)
    scope_note = (f'Active repair scope: {requirement_id}. Resolve this family first in this slice. '
                  'The complete report also contains other families for regression context; '
                  'do not switch to their repairs unless evidence shows they block this family. '
                  'An unrelated generated self-test failure does not by itself block this family. '
                  'Leave its source and test repairs for their own slice; do not rewrite a passing '
                  'search or booking display during an authentication feedback repair. '
                  'Keep related cross-file edits atomic and rerun the complete protected regression.\n\n'
                  if requirement_id else '')
    native_note = scope_note
    if (native is not None and not native.passed and not native.output.startswith('Not executed:')
            and (not requirement_id or requirement_id in _native_failure_families(native))):
        native_note += ('Actual Chromium verification failure (original probe and additional local checks, not Stage 3):\n'
                       + native.output + '\nRepair this first blocking native interaction before later '
                       'jsdom failures. getByLabel with an anchored expression uses associated label '
                       'text; a radio accessible role name alone does not establish that exact label '
                       'selector matches. Inspect whitespace and explicit aria-label in the rendered '
                       'control. Keep the published selector and original probe unchanged.\n\n')
        if dict(native.case_statuses).get(COMPLETION_CASE) == 'failed':
            native_note += ('The completion probe continues beyond review to confirmation and document reload. '
                            'Fix every reported stage, not only its first assertion. Compare the actual '
                            'backend response keys with the success UI and restoration predicate; verify '
                            'both identifier type and field name. Persist the owned selected journey and '
                            'confirmed reference across document reload using the real API contract. '
                            'Do not repeat the journey summary in review or success, and do not hide '
                            'required passenger details to avoid duplicate matches.\n\n')
        if 'individual visible named location' in native.output:
            native_note += ('The location aliases in the requirement identify supported city/station '
                            'names. Render departure and arrival as separate readable named units, '
                            'not a slash-alias/arrow paragraph. Keep each normalized city substring '
                            'unique for the unchanged public partial-text assertions; do not repeat '
                            'city text in both criteria and station nodes. Preserve the selected '
                            'train, date, times, and ownership when changing the display.\n\n')
    public = next((check for check in result.checks if check.name == PUBLIC_CHECK), None)
    if public is None or public.passed:
        return native_note
    lines = [line for line in public.output.splitlines()
             if line.startswith('REQ-') and (not requirement_id or line.startswith(requirement_id + ':'))]
    if not lines:
        return native_note
    notes = [native_note] if native_note else []
    notes += [
        'Repair one failure family atomically, then rerun the complete protected regression. '
        'Preserve previously passing cases; a restored candidate discards the attempted edits, '
        'so reread current source rather than repeating an old replacement.',
        'Focus cases:\n' + '\n'.join(lines),
    ]
    text = '\n'.join(lines)
    visibility_lines = [line for line in lines if re.search(
        r'\bStrict visibility (?:text|role|label)\b', line)]
    visibility_counts = {int(count) for count in re.findall(
        r'\bexpected \+?(\d+) to be 1\b', '\n'.join(visibility_lines))}
    if visibility_lines:
        notes.append('Inspect the exact published expression and actual rendered DOM before assigning '
                     'a cause. A zero match and multiple matches require different diagnosis. '
                     'Do not hide required data or change selectors.')
    if 0 in visibility_counts:
        notes.append('Zero matching locator nodes: inspect the active route, session readiness, '
                     'conditional rendering and early returns, then any real API error or validation '
                     'feedback. A missing heading or booking number can be a blocked flow rather '
                     'than a wording defect; establish which interaction was reached.')
    if any('Strict visibility text' in line and r'\b' in line and
           re.search(r'\bexpected \+?0 to be 1\b', line) for line in visibility_lines):
        notes.append('A whole-word text expression can match zero nodes even when a longer alias '
                     'contains the same letters. Compare the exact expression with the rendered text '
                     'before changing routing or authentication. For journey endpoints, use the '
                     'required route\'s canonical city value in one separate visible location node; '
                     'do not append a slash/station alias or merge it with time/route text. Satisfy '
                     'both the whole-word city expression and the anchored individual-location '
                     'check together, rather than alternating between two failing representations.')
    if any(count > 1 for count in visibility_counts):
        notes.append('Multiple matching locator nodes: inspect every rendered match and accessible '
                     'name. For text locators, include city substrings inside station names, headings '
                     'and success notices. A substring '
                     'or regex can match distinct facts even when their full text differs. Reread '
                     'frontend/src/components/SearchResults.jsx and JourneySummary.jsx, plus their '
                     'current call sites or equivalents. Keep required city, station, train and date '
                     'facts visible in one authoritative summary using the selected journey fields.')
    if any(re.search(r'\bexpected \+?\d+ to be 1\b', line) and
           'Strict visibility' not in line and 'Strict locator' not in line for line in lines):
        notes.append('A plain numeric count assertion has no locator description. Inspect the failing '
                     'assertion before assigning a DOM cause. For a locator count, inspect rendered '
                     'instances through entry, review and success: a shared summary component can '
                     'still be mounted by both a page and its child. Reread the current booking page, '
                     'review and confirmation components; do not infer success from one call site.')
    families = {line.split(':', 1)[0] for line in lines}
    if 'REQ-1.2' in families:
        notes.append('Locate the first blocked interaction in the published login sequence. It may '
                     'be registration or sign-out setup before credential submission. Reread the '
                     'actual header, auth provider/session API and router entry sources, such as '
                     'frontend/src/Header.jsx, AuthContext.jsx, api.js and main.jsx, or their current '
                     'equivalents. Compare the current DOM, user state, stored token and route after '
                     'sign-out and session restoration. State updater functions must be pure; '
                     'storage changes or other setters inside an updater can produce different '
                     'results when development StrictMode invokes it again. Treat a development/'
                     'production scheduling difference as a diagnostic hypothesis, not an official '
                     'case result. Do not invent an extra Login link or classify the title as a '
                     'credentials failure without reaching the login request.')
    if 'REQ-2.1' in families:
        notes.append('After the first search-summary fix, check every remaining published expression, '
                     'including the date. Visible example dates, result headings and train-card '
                     'dates can all match the same substring locator. Inspect actual current '
                     'SearchForm, HomePage and result-card sources and keep required facts visible; '
                     'a comment saying the criteria appear once does not establish DOM uniqueness.')
    if 'REQ-2.2' in families:
        notes.append('The published train-selection cases search and select a train without first '
                     'signing in. Inspect frontend/src/pages/HomePage.jsx and BookingPage.jsx, or '
                     'their current equivalents, for the selected-journey display and authentication '
                     'branches. Preserve the public train summary for that guest flow while retaining '
                     'authentication for booking submission and private saved results; preserve the '
                     'passing signed-out submission rejection.')
    if families & {'REQ-3.1', 'REQ-3.2'}:
        notes.append('Trace the real booking flow from form validation through the review snapshot, '
                     'confirmation request and saved-record reload. Locate and reread the actual '
                     'booking page, auth/booking API and backend routes/validator sources, such as '
                     'frontend/src/BookingPage.jsx, api.js and backend/src/app.js, or their current '
                     'equivalents. Compare token propagation with the actual backend credential '
                     'contract; trace terms acceptance and selected-journey data through every stage. '
                     'A signed-in header does not prove the booking API is authenticated, and '
                     'client-side validation does not prove the confirmation payload retains terms '
                     'acceptance. Inspect actual backend errors and compare the complete nested '
                     'payload schema, including the required journey/date fields: a top-level field '
                     'does not satisfy a required nested field. Use the current source field names '
                     'rather than a historical candidate schema. Read actual API feedback before '
                     'changing success text. Preserve '
                     'owner/journey isolation and stale-request cancellation.')
        notes.append('After confirmation succeeds, follow the remaining published sequence. Check '
                     'whether the success heading and booking-number notice both match the same '
                     'success expression. Verify that storage helpers are actually called to save '
                     'the confirmed reference and restore the owned record from the real API on '
                     'reload; imports, helper definitions and a generated flow id alone do not '
                     'connect that lifecycle. Preserve the selected journey across reload, and '
                     'avoid restoring another owner or a newly selected flow\'s old confirmation.')
    if 'REQ-3.2' in families:
        notes.append('The published route/seat combination sequence registers another account in '
                     'the same browser without an intervening sign-out. Keep registration reachable '
                     'while signed in and verify session replacement and owned-result isolation. '
                     'Do not add a sign-out step to the public case to bypass this transition.')
    notes.append('Each failure reports the first blocking assertion in its case; later interactions '
                 'may not have run. The case title alone does not identify the failing subsystem. '
                 'After repairing the observed cause, exercise the remaining published sequence '
                 'against the real App/API and rerun the complete protected regression. Do not '
                 'claim confirmation, reload or journey replacement from an earlier summary check.')
    label_failure = 'Strict locator label' in text or 'Strict visibility label' in text
    if 'textbox' in text or 'No unique' in text or label_failure:
        notes.append('Check the actual accessible role and full name of the failing control. '
                     'A Passenger name label does not match the required Name textbox; a '
                     'Nationality select is not a textbox. Use the original requirement and '
                     'published interaction contract to choose semantic controls and associated '
                     'labels. Keep the real form handler, validation and authenticated API '
                     'submission connected; a render-only form with preventDefault is unfinished.')
    if label_failure:
        notes.append('Inspect the associated label text as well as the accessible name; a control '
                     'may exist even when an exact label query matches none. getByLabelText may '
                     'include a required-marker asterisk inside the label even when its span has '
                     'aria-hidden="true". Preserve the visible label and required validation; give '
                     'the real control an explicit aria-label matching the required name, or render '
                     'the marker with CSS outside the DOM label text. Do not change the published '
                     'selector or adapter, and rerun the complete protected regression.')
    if 'expected 0 to be greater than 0' in text:
        notes.append('The visible feedback expression matched no message. Compare actual validation '
                     'wording against the case expression for each input, while retaining the same '
                     'numeric and format rules. A message saying 12-128 or between 2 and 100 does '
                     'not say at least, invalid, or too short. Also inspect native form validation '
                     'and the submit handler; do not weaken validation to satisfy feedback.')
        patterns = published_feedback_patterns(lines)
        if patterns:
            notes.append(patterns)
            if ('already registered' in text.casefold() and
                    any(line.startswith('REQ-1.1:') and 'duplicate' in line.casefold()
                        for line in lines)):
                notes.append('The observed duplicate-account alert says already registered. '
                             'The unchanged duplicate username/email expressions require the '
                             'phrases already exists, duplicate, taken or conflict; registered '
                             'is not any of those phrases. Correct the real API/UI feedback '
                             'while continuing to reject the duplicate and leave no session. '
                             'Do not change the published expression or loosen uniqueness validation.')
    notes.append('Generated self-tests may assume an exact text node where the public case uses a '
                 'substring or regex. Inspect their helpers, API mocks and actual rendered DOM. '
                 'Preserve meaningful assertions and original requirements; do not redesign '
                 'production behavior merely to satisfy an incompatible mock or text expectation.')
    return '\n\n'.join(notes)

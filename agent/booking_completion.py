"""Additional real-browser confirmation/reload evidence; published tests unchanged."""
import json
from pathlib import Path
import re
import uuid

from .browser_contract import check_booking_controls
from .verify import CheckResult


NAME = 'Native booking completion (additional local check, not Stage3)'
CASE = 'local Chromium: confirmed passenger details and booking survive reload'
BASE = 'http://127.0.0.1:3000'


def completion_result(passed, output):
    return CheckResult(NAME, passed, 0 if passed else 1, output,
                       () if passed else ('REQ-3.2',),
                       ((CASE, 'passed' if passed else 'failed'),))


def _visible_one(page, pattern, description):
    from playwright.sync_api import expect
    locator = page.get_by_text(re.compile(pattern, re.I))
    expect(locator, description).to_have_count(1, timeout=3000)
    expect(locator, description).to_be_visible(timeout=3000)
    return locator


def check_confirmed_details(page):
    number = _visible_one(page, r'booking number|order number', 'One saved booking number')
    text = number.inner_text()
    match = re.search(r'(?:booking|order) number\s*:\s*(\S+)', text, re.I)
    if not match:
        raise ValueError('A concrete saved booking number is required: ' + text[:300])
    for pattern, description in (
        (r'Chromium Passenger', 'Confirmed passenger name from the real booking response'),
        (r'123456789', 'Confirmed passenger ID'),
        (r'Vietnam', 'Confirmed nationality'),
        (r'Business Class', 'Confirmed ticket class'),
        (r'Sun, May 31', 'One selected date after confirmation'),
    ):
        _visible_one(page, pattern, description)
    return match[1]


def exercise_completion(page):
    """Collect review errors, then continue to expose later persistence defects."""
    failures = []
    try:
        _visible_one(page, r'Sun, May 31', 'One selected date during review')
    except Exception as exc:
        failures.append('Review summary: ' + str(exc))
    page.get_by_role('button', name=re.compile(r'^confirm$', re.I)).click()
    number = None
    try:
        number = check_confirmed_details(page)
    except Exception as exc:
        failures.append('Confirmation response/details: ' + str(exc))
    page.reload()
    try:
        restored = check_confirmed_details(page)
        if number is not None and restored != number:
            raise ValueError(f'Reload changed booking number from {number} to {restored}')
    except Exception as exc:
        failures.append('Reload persistence: ' + str(exc))
    if failures:
        raise ValueError('\n\n'.join(failures))


def verify_booking_completion(protected: Path):
    from playwright.sync_api import sync_playwright
    from .visual_acceptance import _block_external_requests, _ensure_chromium

    stage = 'browser startup'
    result = completion_result(False, 'Completion probe did not execute')
    try:
        with sync_playwright() as playwright:
            browser = _ensure_chromium(playwright)
            try:
                context = browser.new_context()
                _block_external_requests(context, BASE)
                page = context.new_page()
                page.set_default_timeout(3000)
                page.set_default_navigation_timeout(10000)
                try:
                    stage = 'isolated registration'
                    page.goto(BASE)
                    page.get_by_role('link', name=re.compile('register', re.I)).click()
                    username = 'bc-' + uuid.uuid4().hex[:18]
                    for pattern, value in (
                        (r'^name$', 'Chromium Passenger'), (r'passport number', 'P123456789'),
                        (r'passport expiration date', '2035-12-31'), (r'date of birth', '1995-06-15'),
                        (r'^username$', username), (r'^password$', 'Valid-password-123!'),
                        (r'confirm password', 'Valid-password-123!'),
                        (r'email address', username + '@example.test'),
                    ):
                        page.get_by_label(re.compile(pattern, re.I)).fill(value)
                    page.get_by_label(re.compile('nationality', re.I)).select_option(label='Vietnam')
                    page.get_by_label(re.compile('^male$', re.I)).check()
                    page.get_by_role('checkbox', name=re.compile('terms of service|privacy policy|agree', re.I)).check()
                    page.get_by_role('button', name=re.compile('next step', re.I)).click()
                    page.get_by_text(username, exact=True).wait_for(state='visible')
                    stage = 'selected booking and review'
                    page.goto(BASE)
                    for label, value in (('from', 'Shanghai'), ('to', 'Beijing'), ('date', 'Sun, May 31')):
                        page.get_by_label(re.compile('^' + label + '$', re.I)).fill(value)
                    page.get_by_role('button', name=re.compile('search', re.I)).click()
                    page.get_by_role('button', name=re.compile(r'book\s+G532|G532\s+book', re.I)).click()
                    check_booking_controls(page)
                    stage = 'confirmation and document reload'
                    exercise_completion(page)
                    result = completion_result(True, 'Real confirmation displayed the passenger, ID, nationality, '
                        'ticket class and one selected date; the same booking survived document reload. '
                        'Additional local evidence, not official acceptance.')
                except Exception as exc:
                    evidence = ''
                    try:
                        evidence = page.locator('body').aria_snapshot()
                        (protected/'booking-completion-accessibility.txt').write_text(evidence, encoding='utf-8')
                        page.screenshot(path=str(protected/'booking-completion.png'))
                    except Exception:
                        pass
                    result = completion_result(False, stage + ': ' + str(exc) + '\nObserved DOM: ' + evidence[:3000])
            finally:
                browser.close()
    except Exception as exc:
        result = completion_result(False, 'Native completion environment: ' + str(exc))
    (protected/'booking-completion.json').write_text(json.dumps({
        'passed': result.passed, 'output': result.output, 'case_statuses': result.case_statuses,
        'formal_acceptance': False}, ensure_ascii=False, indent=2), encoding='utf-8')
    return result

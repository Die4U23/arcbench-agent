"""Additional native location-display checks; original public tests unchanged."""
import json
from pathlib import Path
import re

from .verify import CheckResult

NAME = 'Native individual journey locations (additional local check, not Stage3)'
ROUTES = (
    ('Shanghai', 'Beijing', 'G532', r'^(?:Shanghai|ShanghaiHongqiao)$', r'^(?:Beijing|BeijingNan)$'),
    ('Beijing', 'Tianjin', 'G561', r'^(?:Beijing|BeijingNan)$', r'^Tianjin$'),
)
CASES = tuple('local Chromium named locations: ' + route[2] for route in ROUTES)


def check_locations(page, origin_pattern: str, destination_pattern: str):
    for label, pattern in (('departure', origin_pattern), ('arrival', destination_pattern)):
        location = page.get_by_text(re.compile(pattern, re.I))
        count = location.count()
        if count != 1:
            raise ValueError(f'{label} location must be one individual visible named location '
                             f'matching {pattern}; found {count}. A combined slash-alias/arrow '
                             'route paragraph does not establish an individual location unit.')
        location.wait_for(state='visible', timeout=3000)


def unavailable(message):
    return CheckResult(NAME, False, 1, message, ('REQ-2.1', 'REQ-2.2'),
                       tuple((case, 'failed') for case in CASES))


def verify_station_units(protected: Path):
    from .sandbox_context import in_worker
    if not in_worker():
        return unavailable("Browser probes require an isolated verification session")
    from playwright.sync_api import sync_playwright
    from .visual_acceptance import _block_external_requests, _ensure_chromium

    statuses, details = [], []
    try:
        with sync_playwright() as playwright:
            browser = _ensure_chromium(playwright)
            try:
                context = browser.new_context()
                _block_external_requests(context, 'http://127.0.0.1:3000')
                page = context.new_page()
                page.set_default_timeout(3000)
                page.set_default_navigation_timeout(10000)
                for case, route in zip(CASES, ROUTES):
                    origin, destination, train, origin_pattern, destination_pattern = route
                    stage = 'search result'
                    try:
                        page.goto('http://127.0.0.1:3000')
                        for label, value in (('from', origin), ('to', destination), ('date', 'Sun, May 31')):
                            page.get_by_label(re.compile('^'+label+'$', re.I)).fill(value)
                        page.get_by_role('button', name=re.compile('search', re.I)).click()
                        action = page.get_by_role('button', name=re.compile(r'book\s+'+train+'|'+train+r'\s+book', re.I))
                        action.wait_for(state='visible')
                        check_locations(page, origin_pattern, destination_pattern)
                        stage = 'selected booking entry'
                        action.click()
                        page.get_by_text(train, exact=True).wait_for(state='visible')
                        check_locations(page, origin_pattern, destination_pattern)
                        statuses.append((case, 'passed'))
                    except Exception as exc:
                        statuses.append((case, 'failed'))
                        details.append(case + ' / ' + stage + ': ' + str(exc))
                        try:
                            page.screenshot(path=str(protected/(train+'-station-units.png')))
                            snapshot = page.locator('body').aria_snapshot()
                            (protected/(train+'-station-units-accessibility.txt')).write_text(snapshot, encoding='utf-8')
                            details.append('Observed DOM: ' + snapshot[:3000])
                        except Exception:
                            pass
            finally:
                browser.close()
    except Exception as exc:
        result = unavailable('Native station environment: '+str(exc))
    else:
        passed = len(statuses) == len(CASES) and all(status == 'passed' for _, status in statuses)
        result = CheckResult(NAME, passed, 0 if passed else 1,
            '\n\n'.join(details) if details else 'Separate named locations verified in both published routes and booking entries; additional local evidence only.',
            () if passed else ('REQ-2.1', 'REQ-2.2'), tuple(statuses))
    (protected/'station-units.json').write_text(json.dumps({
        'passed':result.passed,'output':result.output,'case_statuses':result.case_statuses,
        'formal_acceptance':False},indent=2),encoding='utf-8')
    return result

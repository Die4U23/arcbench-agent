"""Bounded real Chromium journey guard; additional local evidence, not Stage 3."""
from __future__ import annotations

import json
from pathlib import Path
import re
import uuid

from .verify import CheckResult

BROWSER_CHECK = "Chromium registration and booking controls (local guard, not Stage 3)"
BROWSER_CASE = "local Chromium: exact booking Name remains accessible with real CSS"


def browser_guard_result(passed: bool, output: str) -> CheckResult:
    return CheckResult(BROWSER_CHECK, passed, 0 if passed else 1, output,
                       () if passed else ("REQ-1.1", "REQ-3.2"),
                       ((BROWSER_CASE, "passed" if passed else "failed"),))


def check_booking_controls(page) -> None:
    """Use the published exact Name selector, rather than querying DOM text."""
    name = page.get_by_role("textbox", name=re.compile(r"^name$", re.I))
    name.fill("Chromium Passenger")
    page.get_by_role("textbox", name=re.compile(r"id number", re.I)).fill("123456789")
    page.get_by_role("textbox", name=re.compile(r"nationality", re.I)).fill("Vietnam")
    page.get_by_role("combobox", name=re.compile(r"ticket class", re.I)).select_option(label="Business Class")
    page.get_by_role("combobox", name=re.compile(r"ticket type", re.I)).select_option(label="Adult")
    page.get_by_role("checkbox", name=re.compile(r"terms of service|privacy policy|agree", re.I)).check()
    page.get_by_role("button", name=re.compile(r"place order", re.I)).click()
    page.get_by_role("button", name=re.compile(r"^confirm$", re.I)).wait_for(state="visible")


def verify_browser_contract(protected: Path) -> CheckResult:
    from .sandbox_context import in_worker
    if not in_worker():
        return browser_guard_result(False, "Browser probes require an isolated verification session")
    # This function must be called only while the caller's owned backend is alive.
    from playwright.sync_api import sync_playwright
    from .visual_acceptance import _block_external_requests, _ensure_chromium

    browser = None
    context = None
    page = None
    stage = "Chromium startup"
    base = "http://127.0.0.1:3000"
    try:
        with sync_playwright() as playwright:
            try:
                browser = _ensure_chromium(playwright)
                context = browser.new_context()
                _block_external_requests(context, base)
                page = context.new_page()
                page.set_default_timeout(3000)
                page.set_default_navigation_timeout(10000)
                stage = "registration using published accessible selectors"
                page.goto(base)
                page.get_by_role("link", name=re.compile(r"register", re.I)).click()
                username = "cg-" + uuid.uuid4().hex[:18]
                for pattern, value in ((r"^name$", "Chromium Passenger"),
                        (r"passport number", "P123456789"), (r"passport expiration date", "2035-12-31"),
                        (r"date of birth", "1995-06-15"), (r"^username$", username),
                        (r"^password$", "Valid-password-123!"), (r"confirm password", "Valid-password-123!"),
                        (r"email address", username + "@example.test")):
                    page.get_by_label(re.compile(pattern, re.I)).fill(value)
                page.get_by_label(re.compile(r"nationality", re.I)).select_option(label="Vietnam")
                page.get_by_label(re.compile(r"^male$", re.I)).check()
                page.get_by_role("checkbox", name=re.compile(r"terms of service|privacy policy|agree", re.I)).check()
                page.get_by_role("button", name=re.compile(r"next step", re.I)).click()
                page.get_by_text(username, exact=True).wait_for(state="visible")
                stage = "search and booking with real CSS and backend"
                page.goto(base)
                for label, value in (("from", "Shanghai"), ("to", "Beijing"), ("date", "Sun, May 31")):
                    page.get_by_label(re.compile("^" + label + "$", re.I)).fill(value)
                page.get_by_role("button", name=re.compile(r"search", re.I)).click()
                page.get_by_role("button", name=re.compile(r"book\s+G532|G532\s+book", re.I)).click()
                check_booking_controls(page)
                result = browser_guard_result(True,
                    "Real Chromium registration, owned backend search, exact Name and booking review passed. "
                    "One additional local guard; official 30-case evaluation remains pending.")
            except Exception as exc:
                evidence = []
                if page is not None:
                    for filename, capture in (("chromium-guard.png", lambda path: page.screenshot(path=str(path))),
                            ("chromium-guard-accessibility.txt", lambda path: path.write_text(
                                page.locator("body").aria_snapshot(), encoding="utf-8"))):
                        try:
                            capture(protected / filename)
                            evidence.append(filename)
                        except Exception:
                            pass
                result = browser_guard_result(False, f"{stage}: {exc}\nEvidence: {', '.join(evidence)}")
            finally:
                if context is not None:
                    context.close()
                if browser is not None:
                    browser.close()
    except Exception as exc:
        result = browser_guard_result(False, f"Chromium environment/cleanup failed: {exc}")
    (protected / "chromium-guard.json").write_text(json.dumps({
        "passed": result.passed, "output": result.output, "case_statuses": result.case_statuses,
        "formal_acceptance": "not executed by this guard"}, ensure_ascii=False, indent=2), encoding="utf-8")
    return result

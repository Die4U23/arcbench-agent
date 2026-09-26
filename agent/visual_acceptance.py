from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .verify import CheckResult, VerificationResult


MANIFEST_NAME = "arcbench-visual-acceptance.json"
REPORT_RELATIVE_PATH = Path("artifacts/visual-acceptance/report.json")
SCREENSHOT_RELATIVE_DIR = Path("artifacts/visual-acceptance/screenshots")
INTERACTION_ACTIONS = {"click", "fill", "check", "uncheck", "select_option"}
FLOW_ACTIONS = INTERACTION_ACTIONS | {"navigate"}
SUPPORTED_ACTIONS = FLOW_ACTIONS | {
    "expect_visible",
    "expect_hidden",
    "expect_navigation",
    "expect_text",
    "expect_value",
    "expect_checked",
    "expect_unchecked",
    "wait_for_url",
}
MAX_CASES = 12
MAX_STEPS_PER_FLOW = 80
SERVER_TIMEOUT_SECONDS = 30
BROWSER_INSTALL_TIMEOUT_SECONDS = 240


def collect_visual_references(tree: dict[str, Any]) -> list[str]:
    references: list[str] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        values = node.get("visual_reference", [])
        if isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            for value in values:
                reference = str(value).strip()
                if reference and reference not in seen:
                    references.append(reference)
                    seen.add(reference)
        children = node.get("children", [])
        if isinstance(children, list):
            for child in children:
                walk(child)

    walk(tree)
    return references


def _validate_viewport(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError(f"{label}.viewport must be an object")
    dimensions: dict[str, int] = {}
    for key, minimum, maximum in (("width", 320, 2560), ("height", 240, 2160)):
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, int) or not minimum <= number <= maximum:
            raise ValueError(f"{label}.viewport.{key} must be an integer from {minimum} to {maximum}")
        dimensions[key] = number
    return dimensions


def _validate_route(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        raise ValueError(f"{label}.route must be a local path beginning with one slash")
    if "\\" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{label}.route contains invalid characters")
    return value


def validate_manifest(payload: Any, references: list[str]) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Visual acceptance manifest must be an object with version 1")
    cases = payload.get("cases")
    flows = payload.get("flows")
    if not isinstance(cases, list) or not cases or len(cases) > MAX_CASES:
        raise ValueError(f"Manifest cases must contain 1 to {MAX_CASES} entries")
    if not isinstance(flows, list) or not flows:
        raise ValueError("Manifest flows must contain at least one browser interaction flow")

    case_references: list[str] = []
    case_names: set[str] = set()
    for index, case in enumerate(cases):
        label = f"cases[{index}]"
        if not isinstance(case, dict):
            raise ValueError(f"{label} must be an object")
        name = case.get("name")
        reference = case.get("reference")
        if not isinstance(name, str) or not name.strip() or name in case_names:
            raise ValueError(f"{label}.name must be non-empty and unique")
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError(f"{label}.reference must identify a task image")
        case_names.add(name)
        case_references.append(reference)
        _validate_route(case.get("route"), label)
        case["viewport"] = _validate_viewport(case.get("viewport"), label)
        expectations = case.get("visual_expectations")
        if not isinstance(expectations, list) or not expectations or any(
            not isinstance(item, str) or not item.strip() for item in expectations
        ):
            raise ValueError(f"{label}.visual_expectations must contain observable expectations")
        if "target_selector" in case and not isinstance(case["target_selector"], str):
            raise ValueError(f"{label}.target_selector must be a CSS selector string")
        if "full_page" in case and not isinstance(case["full_page"], bool):
            raise ValueError(f"{label}.full_page must be a boolean")
        setup = case.get("setup")
        if setup is not None:
            if not isinstance(setup, dict):
                raise ValueError(f"{label}.setup must be an object")
            _validate_route(setup.get("route"), f"{label}.setup")
            _validate_steps(setup.get("steps"), f"{label}.setup")

    if sorted(case_references) != sorted(references):
        missing = sorted(set(references) - set(case_references))
        extra = sorted(set(case_references) - set(references))
        duplicate = sorted({ref for ref in case_references if case_references.count(ref) > 1})
        details = []
        if missing:
            details.append(f"unmapped references: {missing}")
        if extra:
            details.append(f"unknown references: {extra}")
        if duplicate:
            details.append(f"duplicate references: {duplicate}")
        raise ValueError("Every task image must be mapped to exactly one visual case (" + "; ".join(details) + ")")

    flow_names: set[str] = set()
    for index, flow in enumerate(flows):
        label = f"flows[{index}]"
        if not isinstance(flow, dict):
            raise ValueError(f"{label} must be an object")
        name = flow.get("name")
        if not isinstance(name, str) or not name.strip() or name in flow_names:
            raise ValueError(f"{label}.name must be non-empty and unique")
        flow_names.add(name)
        _validate_route(flow.get("route"), label)
        flow["viewport"] = _validate_viewport(
            flow.get("viewport") or cases[0]["viewport"], label
        )
        covers = flow.get("covers")
        if not isinstance(covers, list) or not covers or any(not isinstance(s, str) or not s.strip() for s in covers):
            raise ValueError(f"{label}.covers must list selectors for actionable controls to exercise")
        if len(set(covers)) != len(covers):
            raise ValueError(f"{label}.covers must not contain duplicate selectors")
        steps = _validate_steps(flow.get("steps"), label)
        action_selectors = {
            step["selector"] for step in steps
            if step["action"] in INTERACTION_ACTIONS
        }
        uncovered_selectors = sorted(set(covers) - action_selectors)
        if uncovered_selectors:
            raise ValueError(f"{label}.covers contains controls with no exercised action: {uncovered_selectors}")

    return payload


def _validate_steps(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_STEPS_PER_FLOW:
        raise ValueError(f"{label}.steps must contain 1 to {MAX_STEPS_PER_FLOW} actions or assertions")
    has_action = False
    awaiting_assertion = False
    for step_index, step in enumerate(value):
        step_label = f"{label}.steps[{step_index}]"
        if not isinstance(step, dict) or step.get("action") not in SUPPORTED_ACTIONS:
            raise ValueError(f"{step_label}.action is not supported")
        action = step["action"]
        if action in {"wait_for_url", "navigate"}:
            expected_url = step.get("value")
            if not isinstance(expected_url, str) or not expected_url.startswith("/") or expected_url.startswith("//"):
                raise ValueError(f"{step_label}.value must be a local path")
        elif action not in {"wait_for_url", "navigate", "expect_navigation"}:
            selector = step.get("selector")
            if not isinstance(selector, str) or not selector.strip():
                raise ValueError(f"{step_label}.selector is required")
        if action in {"fill", "select_option", "expect_text", "expect_value", "navigate"}:
            if not isinstance(step.get("value"), str):
                raise ValueError(f"{step_label}.value is required")
        if action in FLOW_ACTIONS:
            if awaiting_assertion:
                raise ValueError(f"{step_label} must follow an observable assertion for the previous interaction")
            has_action = True
            awaiting_assertion = True
        elif action.startswith("expect_") or action == "wait_for_url":
            if not awaiting_assertion:
                raise ValueError(f"{step_label} must observe the immediately preceding interaction")
            awaiting_assertion = False
    if not has_action:
        raise ValueError(f"{label}.steps must perform at least one real interaction")
    if awaiting_assertion:
        raise ValueError(f"{label}.steps must assert an observable result after its final interaction")
    return value


def _read_manifest(project_dir: Path, references: list[str]) -> dict[str, Any]:
    manifest_path = project_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"Visual references are present but {MANIFEST_NAME} is missing. Add cases for every image and browser flows with observable assertions."
        )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {MANIFEST_NAME}: {exc}") from exc
    return validate_manifest(payload, references)


def _find_package_root(project_dir: Path) -> Path:
    package_files: list[Path] = []
    ignored_dirs = {".git", ".arc", "node_modules", "dist", "build", ".venv", "venv"}
    for current, dirs, files in os.walk(project_dir):
        relative = Path(current).relative_to(project_dir)
        dirs[:] = [directory for directory in dirs if directory not in ignored_dirs]
        if len(relative.parts) > 2:
            dirs[:] = []
        if "package.json" in files:
            package_files.append(Path(current) / "package.json")
    package_files.sort(key=lambda path: (len(path.relative_to(project_dir).parts), str(path)))
    for package_file in package_files:
        try:
            payload = json.loads(package_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        scripts = payload.get("scripts") if isinstance(payload, dict) else None
        if isinstance(scripts, dict) and "start" in scripts:
            return package_file.parent
    raise ValueError("Visual browser checks require a package.json with a start script")


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_server(project_dir: Path, task_dir: Path, port: int, log_path: Path) -> subprocess.Popen[Any]:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise RuntimeError("npm was not found; cannot start the generated application for visual checks")
    env = os.environ.copy()
    env.update(
        {
            "PORT": str(port),
            "HOST": "127.0.0.1",
            "HOSTNAME": "127.0.0.1",
            "ARCBENCH_TASK_DIR": str(task_dir.resolve()),
        }
    )
    log_file = log_path.open("wb")
    try:
        if os.name == "nt" and npm.lower().endswith((".cmd", ".bat")):
            process = subprocess.Popen(
                f'"{npm}" run start',
                cwd=project_dir,
                env=env,
                shell=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        else:
            process = subprocess.Popen(
                [npm, "run", "start"],
                cwd=project_dir,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
    except Exception:
        log_file.close()
        raise
    process._arcbench_log_file = log_file  # type: ignore[attr-defined]
    return process


def _stop_server(process: subprocess.Popen[Any]) -> None:
    if os.name == "nt" and process.poll() is None:
        taskkill = shutil.which("taskkill")
        if taskkill:
            subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    log_file = getattr(process, "_arcbench_log_file", None)
    if log_file:
        log_file.close()


def _wait_for_server(process: subprocess.Popen[Any], port: int, log_path: Path) -> str:
    url = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + SERVER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Generated app exited before becoming ready. Server log: {log_path}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status < 500:
                    return url
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.25)
    raise TimeoutError(f"Generated app did not respond within {SERVER_TIMEOUT_SECONDS}s. Server log: {log_path}")


def _ensure_chromium(playwright: Any) -> Any:
    try:
        return playwright.chromium.launch(headless=True)
    except Exception as first_error:
        install = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=BROWSER_INSTALL_TIMEOUT_SECONDS,
            check=False,
        )
        if install.returncode != 0:
            details = (install.stdout + "\n" + install.stderr).strip()
            raise RuntimeError(
                "Headless Chromium is unavailable and automatic browser setup failed. "
                "Install Playwright's Chromium browser in the Runner environment. "
                + details[-4000:]
            ) from first_error
        try:
            return playwright.chromium.launch(headless=True)
        except Exception as exc:
            raise RuntimeError(f"Could not launch headless Chromium after setup: {exc}") from exc


def _snapshot_expected_state(page: Any, assertion: dict[str, Any]) -> dict[str, Any]:
    action = assertion["action"]
    if action == "wait_for_url":
        return {"url": page.url}
    if action == "expect_navigation":
        return {"url": page.url, "time_origin": page.evaluate("performance.timeOrigin")}
    locator = page.locator(assertion["selector"])
    exists = locator.count() > 0
    visible = exists and locator.is_visible()
    snapshot: dict[str, Any] = {"exists": exists, "visible": visible}
    if action in {"expect_text", "expect_visible", "expect_hidden"} and exists:
        snapshot["text"] = locator.inner_text(timeout=2_000) if visible else ""
    if action == "expect_value" and exists:
        snapshot["value"] = locator.input_value(timeout=2_000)
    if action in {"expect_checked", "expect_unchecked"} and exists:
        snapshot["checked"] = locator.is_checked()
    return snapshot


def _run_interaction(page: Any, step: dict[str, Any], base_url: str) -> None:
    action = step["action"]
    if action == "navigate":
        page.goto(base_url.rstrip("/") + step["value"], wait_until="domcontentloaded", timeout=15_000)
        return
    locator = page.locator(step["selector"])
    if action == "click":
        locator.click(timeout=5_000)
    elif action == "fill":
        locator.fill(step["value"], timeout=5_000)
    elif action == "check":
        locator.check(timeout=5_000)
    elif action == "uncheck":
        locator.uncheck(timeout=5_000)
    elif action == "select_option":
        locator.select_option(step["value"], timeout=5_000)


def _run_assertion(page: Any, step: dict[str, Any], base_url: str, before: dict[str, Any]) -> None:
    action = step["action"]
    if action == "wait_for_url":
        expected_url = base_url.rstrip("/") + step["value"]
        if before.get("url") == expected_url:
            raise AssertionError(f"URL assertion did not expect a state change to {expected_url}")
        page.wait_for_url(expected_url, timeout=10_000)
        return
    if action == "expect_navigation":
        current_time_origin = page.evaluate("performance.timeOrigin")
        if before.get("url") == page.url and before.get("time_origin") == current_time_origin:
            raise AssertionError("Expected the interaction to cause a document navigation or reload")
        return

    locator = page.locator(step["selector"])
    exists = locator.count() > 0
    visible = exists and locator.is_visible()
    if action == "expect_visible":
        if not visible or before.get("visible"):
            raise AssertionError(f"Expected {step['selector']} to become visible after the interaction")
    elif action == "expect_hidden":
        if visible or not before.get("visible"):
            raise AssertionError(f"Expected {step['selector']} to become hidden after the interaction")
    elif action == "expect_text":
        actual = locator.inner_text(timeout=5_000) if visible else ""
        prior = before.get("text", "")
        if step["value"] in prior or step["value"] not in actual:
            raise AssertionError(f"Expected new text {step['value']!r} in {step['selector']}; got {actual!r}")
    elif action == "expect_value":
        actual = locator.input_value(timeout=5_000)
        if actual == before.get("value") or actual != step["value"]:
            raise AssertionError(f"Expected {step['selector']} value to change to {step['value']!r}; got {actual!r}")
    elif action == "expect_checked":
        if before.get("checked") is not False or not locator.is_checked():
            raise AssertionError(f"Expected {step['selector']} to become checked")
    elif action == "expect_unchecked":
        if before.get("checked") is not True or locator.is_checked():
            raise AssertionError(f"Expected {step['selector']} to become unchecked")


def _run_steps(page: Any, steps: list[dict[str, Any]], base_url: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    index = 0
    while index < len(steps):
        step = steps[index]
        if step["action"] in FLOW_ACTIONS:
            assertion = steps[index + 1]
            try:
                before = _snapshot_expected_state(page, assertion)
                _run_interaction(page, step, base_url)
                _run_assertion(page, assertion, base_url, before)
                results.append(
                    {
                        "action": step["action"],
                        "selector": step.get("selector"),
                        "value": step.get("value") if step["action"] == "navigate" else None,
                        "result": "passed",
                        "assertion": assertion,
                    }
                )
            except Exception as exc:
                exc.completed_steps = results  # type: ignore[attr-defined]
                raise
            index += 2
        else:
            raise ValueError(f"Expected a browser interaction at step {index}")
    return results


def _assert_control_coverage(page: Any, flow: dict[str, Any]) -> list[str]:
    selectors = flow["covers"]
    # Each selector must identify one visible control, not hide untested matches in a broad selector.
    for selector in selectors:
        locator = page.locator(selector)
        count = locator.count()
        if count != 1:
            raise AssertionError(f"Coverage selector {selector!r} must match exactly one control; matched {count}")
        if not locator.is_visible() or not locator.is_enabled():
            raise AssertionError(f"Coverage selector {selector!r} must identify a visible, enabled control")
    uncovered = page.evaluate(
        """(selectors) => {
          const candidates = [...document.querySelectorAll('body *')].filter((el) => {
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            const semanticAction = el.matches(
              'a[href],button,input:not([type="hidden"]),select,textarea,[role="button"],[role="link"],[tabindex]'
            );
            const actionParent = el.parentElement && el.parentElement.closest(
              'a[href],button,[role="button"],[role="link"]'
            );
            const labelParent = el.parentElement && el.parentElement.closest('label');
            if (actionParent || el.tagName === 'LABEL' || (labelParent && !semanticAction)) return false;
            const actionLikeClass = /(?:^|[-_])(?:button|btn|link|menu|dropdown|clickable|tab|nav__item)(?:$|[-_])/i.test(el.className || '');
            const actionLike = semanticAction || typeof el.onclick === 'function' ||
              el.hasAttribute('onclick') || actionLikeClass || style.cursor === 'pointer';
            return actionLike && style.display !== 'none' && style.visibility !== 'hidden' &&
              rect.width > 0 && rect.height > 0 && !el.disabled && el.getAttribute('aria-hidden') !== 'true';
          });
          const covered = new Set();
          for (const selector of selectors) {
            try { document.querySelectorAll(selector).forEach((el) => covered.add(el)); }
            catch (error) { throw new Error(`Invalid coverage selector: ${selector}`); }
          }
          return candidates.filter((el) => !covered.has(el)).map((el) => ({
            tag: el.tagName.toLowerCase(),
            id: el.id || null,
            name: el.getAttribute('name'),
            label: el.getAttribute('aria-label') || el.innerText || el.value || '',
            href: el.getAttribute('href')
          }));
        }""",
        selectors,
    )
    return [json.dumps(item, ensure_ascii=False) for item in uncovered]


def _block_external_requests(context: Any, base_url: str) -> None:
    local_origin = urlsplit(base_url)

    def guard(route: Any) -> None:
        requested = urlsplit(route.request.url)
        if requested.scheme in {"data", "blob", "about"} or (
            requested.scheme == local_origin.scheme
            and requested.netloc == local_origin.netloc
        ):
            route.continue_()
        else:
            route.abort()

    context.route("**/*", guard)


def verify_visual_acceptance(
    project_dir: Path,
    task_dir: Path,
    references: list[str],
    model: Any,
) -> VerificationResult:
    if not references:
        return VerificationResult(True, ())

    checks: list[CheckResult] = []
    report: dict[str, Any] = {"version": 1, "cases": [], "flows": []}
    artifact_dir = project_dir / SCREENSHOT_RELATIVE_DIR
    artifact_dir.mkdir(parents=True, exist_ok=True)
    report_path = project_dir / REPORT_RELATIVE_PATH
    try:
        manifest = _read_manifest(project_dir, references)
        package_dir = _find_package_root(project_dir)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is required for screenshot and browser interaction acceptance") from exc

        port = _reserve_port()
        server_log = artifact_dir.parent / "server.log"
        process = _start_server(package_dir, task_dir, port, server_log)
        try:
            base_url = _wait_for_server(process, port, server_log)
            with sync_playwright() as playwright:
                browser = _ensure_chromium(playwright)
                try:
                    for index, case in enumerate(manifest["cases"], 1):
                        context = browser.new_context(viewport=case["viewport"], device_scale_factor=1)
                        saved_screenshot_path: Path | None = None
                        try:
                            _block_external_requests(context, base_url)
                            page = context.new_page()
                            setup = case.get("setup")
                            setup_results = []
                            if setup:
                                page.goto(base_url.rstrip("/") + setup["route"], wait_until="domcontentloaded", timeout=15_000)
                                setup_results = _run_steps(page, setup["steps"], base_url)
                            page.goto(base_url.rstrip("/") + case["route"], wait_until="domcontentloaded", timeout=15_000)
                            page.wait_for_timeout(300)
                            if case.get("target_selector"):
                                target = page.locator(case["target_selector"])
                                target.wait_for(state="visible", timeout=5_000)
                                screenshot = target.screenshot(animations="disabled", timeout=10_000)
                            else:
                                screenshot = page.screenshot(
                                    full_page=case.get("full_page", False),
                                    animations="disabled",
                                    timeout=15_000,
                                )
                            safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in case["name"]).strip("-")[:80]
                            screenshot_path = artifact_dir / f"{index:02d}-{safe_name or 'visual-case'}.png"
                            screenshot_path.write_bytes(screenshot)
                            saved_screenshot_path = screenshot_path
                            review = model.review_visual_case(
                                reference=case["reference"],
                                reference_dir=task_dir,
                                case=case,
                                screenshot=screenshot,
                            )
                            passed = review["verdict"] == "pass"
                            case_result = {
                                "name": case["name"],
                                "reference": case["reference"],
                                "route": case["route"],
                                "viewport": case["viewport"],
                                "screenshot": screenshot_path.relative_to(project_dir).as_posix(),
                                "setup_steps": setup_results,
                                "verdict": review["verdict"],
                                "issues": review["issues"],
                                "matched": review["matched"],
                            }
                            report["cases"].append(case_result)
                            summary = json.dumps(case_result, ensure_ascii=False)
                            checks.append(CheckResult(f"visual reference: {case['name']}", passed, 0 if passed else 1, summary))
                        except Exception as exc:
                            case_result = {
                                "name": case["name"],
                                "reference": case["reference"],
                                "route": case["route"],
                                "verdict": "repair",
                                "issues": [str(exc)],
                            }
                            if saved_screenshot_path is not None:
                                case_result["screenshot"] = saved_screenshot_path.relative_to(project_dir).as_posix()
                            report["cases"].append(case_result)
                            checks.append(CheckResult(f"visual reference: {case['name']}", False, 1, str(exc)))
                        finally:
                            context.close()

                    for flow in manifest["flows"]:
                        context = browser.new_context(viewport=flow["viewport"], device_scale_factor=1)
                        step_results: list[dict[str, Any]] = []
                        uncovered: list[str] = []
                        try:
                            _block_external_requests(context, base_url)
                            page = context.new_page()
                            page.goto(base_url.rstrip("/") + flow["route"], wait_until="domcontentloaded", timeout=15_000)
                            page.wait_for_timeout(200)
                            uncovered = _assert_control_coverage(page, flow)
                            step_results = _run_steps(page, flow["steps"], base_url)
                            if uncovered:
                                raise AssertionError("Visible actionable controls missing from flow.covers: " + "; ".join(uncovered))
                            flow_result = {
                                "name": flow["name"],
                                "route": flow["route"],
                                "covers": flow["covers"],
                                "steps": step_results,
                                "uncovered_controls": uncovered,
                                "passed": True,
                            }
                            report["flows"].append(flow_result)
                            checks.append(CheckResult(f"browser interaction: {flow['name']}", True, 0, "All declared actions and assertions passed; visible actionable controls were covered."))
                        except Exception as exc:
                            step_results = getattr(exc, "completed_steps", step_results)
                            flow_result = {
                                "name": flow["name"],
                                "route": flow["route"],
                                "covers": flow["covers"],
                                "steps": step_results,
                                "uncovered_controls": uncovered,
                                "passed": False,
                                "error": str(exc),
                            }
                            report["flows"].append(flow_result)
                            checks.append(CheckResult(f"browser interaction: {flow['name']}", False, 1, str(exc)))
                        finally:
                            context.close()
                finally:
                    browser.close()
        finally:
            _stop_server(process)
    except Exception as exc:
        checks.append(CheckResult("visual and browser acceptance", False, 1, f"{type(exc).__name__}: {exc}"))
        report["error"] = f"{type(exc).__name__}: {exc}"

    report["passed"] = bool(checks) and all(check.passed for check in checks)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not checks:
        checks.append(CheckResult("visual and browser acceptance", False, 1, "No visual cases or interaction flows were executed."))
    return VerificationResult(all(check.passed for check in checks), tuple(checks))

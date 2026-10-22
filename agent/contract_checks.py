"""Independent local regression on public interactions; never formal acceptance."""
from __future__ import annotations

import json
import errno
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import time
from urllib.request import urlopen

from .ticketbooking_contract import DOM_ADAPTER, PUBLIC_CASES, PUBLIC_SUPPORT, public_acceptance_context
from .ticketbooking_probes import JOURNEY_PROBES, JOURNEY_PROBE_COUNT
from .verify import CheckResult
from .regression_entry import entry_adapter
from .browser_contract import browser_guard_result, verify_browser_contract
from .sandbox_process import SandboxInfrastructureError


def wait_for_owned_port_release(pid: int, port: int = 3000) -> None:
    """The npm leader can exit before its owned listener finishes SIGTERM.

    Called only after stopping a server we started, never for an occupied port
    discovered before startup. Escalation targets that exact process group.
    """
    deadline = time.monotonic() + 5
    escalated = False
    while True:
        try:
            with socket.socket() as probe:
                if os.name != "nt":
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))
            return
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            if time.monotonic() >= deadline:
                if escalated or os.name == "nt":
                    raise SandboxInfrastructureError(
                        f"Owned server port {port} was not released; stop model repairs",
                        reason="owned_server_cleanup_failed") from exc
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                escalated = True
                deadline = time.monotonic() + 1
            time.sleep(0.02)


class _FailureDOM(HTMLParser):
    """Keep observed feedback from a runner dump without changing its assertion."""
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.open_nodes = []
        self.nodes = {"alerts": [], "headings": [], "auth_links": []}

    def handle_starttag(self, tag, attrs):
        if tag in {"input", "img", "br", "hr", "meta", "link", "area", "base", "embed", "source", "wbr"}:
            return
        self.depth += 1
        attributes = dict(attrs)
        category = None
        if attributes.get("role") == "alert" or "data-field-error" in attributes or "field-error" in (attributes.get("class") or ""):
            category = "alerts"
        elif tag in {"h1", "h2"}:
            category = "headings"
        elif tag == "a":
            category = "auth_links"
        if category:
            self.open_nodes.append((self.depth, category, []))

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_data(self, data):
        for _, _, parts in self.open_nodes:
            parts.append(data)

    def handle_endtag(self, tag):
        if tag in {"input", "img", "br", "hr", "meta", "link", "area", "base", "embed", "source", "wbr"}:
            return
        remaining = []
        for depth, category, parts in self.open_nodes:
            if depth != self.depth:
                remaining.append((depth, category, parts))
                continue
            text = " ".join("".join(parts).split())[:160]
            if category == "auth_links" and not re.search(r"sign[ -]?(?:out|in)|log[ -]?(?:out|in)|register", text, re.I):
                continue
            if text and text not in self.nodes[category] and len(self.nodes[category]) < 3:
                self.nodes[category].append(text)
        self.open_nodes = remaining
        self.depth = max(0, self.depth - 1)


def observed_failure_context(messages) -> str:
    """Advisory snapshot facts, never API responses or executed later steps."""
    parser = _FailureDOM()
    for message in messages:
        if "Ignored nodes:" not in message:
            continue
        dump = message.split("Ignored nodes:", 1)[1]
        dump = re.sub(r"\x1b\[[0-9;]*m", "", dump)
        start = dump.find("<")
        if start >= 0:
            try:
                parser.feed(dump[start:start + 100000])
            except Exception:
                # This is optional diagnostic context. A damaged/truncated DOM
                # must never erase the runner's complete case verdicts.
                return ""
    facts = []
    for key in ("alerts", "headings", "auth_links"):
        if parser.nodes[key]:
            facts.append(key + "=" + json.dumps(parser.nodes[key], ensure_ascii=False))
    return ("Observed DOM snapshot: " + "; ".join(facts))[:280] if facts else ""


def _dependency_file(output_dir: Path, relative: str) -> Path:
    """Support npm workspaces hoisting frontend dependencies to the project root."""
    candidates = [output_dir / "frontend/node_modules" / relative, output_dir / "node_modules" / relative]
    return next((path for path in candidates if path.is_file()), candidates[0])


def _prerequisite_diagnostic(output_dir: Path, *, node: str | None, npm: str | None,
                             files: list[tuple[str, Path]], startup_only: bool) -> str:
    """Describe an existing gate failure without changing its prerequisites."""
    missing = ["npm/npm.cmd (PATH)"] if not npm else []
    if not startup_only:
        if not node:
            missing.append("node (PATH)")
        missing.extend(relative for relative, path in files if not path.is_file())
    details = [
        "Runtime startup did not start." if startup_only else "Public case execution did not start.",
        "Failed prerequisites: " + ", ".join(missing),
        f"Command resolution: node={node or 'NOT FOUND'}; npm/npm.cmd={npm or 'NOT FOUND'}",
    ]
    if not startup_only:
        directories = (output_dir / "frontend/node_modules", output_dir / "node_modules")
        details.append("Browser entry adapter configured; this failure concerns command/dependency prerequisites.")
        details.append("Dependency search directories (in order): " + " | ".join(map(str, directories)))
        for relative, path in files:
            if path.is_file():
                details.append(f"FOUND {relative}: {path}")
            else:
                details.append(f"MISSING {relative}; searched: " +
                               " | ".join(str(directory / relative) for directory in directories))
    details.append("Restore the missing npm/npm.cmd command on PATH." if startup_only else
                   "Restore listed missing commands on PATH or install listed missing test dependencies. "
                   "Do not replace/relax published assertions.")
    return "\n".join(details)


def _wait_for_homepage(server, *, timeout: float = 20.0) -> None:
    """Require HTTP 200 within the startup deadline on every response path."""
    deadline = time.monotonic() + timeout
    failure = "Backend did not serve the public homepage on port 3000"
    while True:
        if server.poll() is not None:
            raise RuntimeError(
                f"Backend exited with code {server.returncode} before the public homepage was reachable"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(failure)
        try:
            with urlopen("http://127.0.0.1:3000", timeout=min(1.0, remaining)) as response:
                # A response received after the deadline does not establish
                # timely readiness, even if its status is otherwise acceptable.
                if response.status == 200:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(failure)
                    return
        except OSError:
            # Connection failures and HTTP errors share the same bounded retry
            # path as successful HTTP responses that are not ready (e.g. 204).
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(failure)
        time.sleep(min(0.1, remaining))


def verify_public_acceptance(output_dir: Path, tree: dict, *, startup_only: bool = False) -> tuple[CheckResult, ...]:
    if not startup_only and not public_acceptance_context(tree):
        return ()
    from .sandbox_context import in_worker
    if not in_worker():
        from .container_checks import run_checks
        return run_checks("public", output_dir, options={"tree": tree, "startup_only": startup_only})
    name = "web runtime startup" if startup_only else "public interaction regression (reconstructed jsdom, not Stage 3)"
    frontend = output_dir / "frontend"
    try:
        adapter = entry_adapter(DOM_ADAPTER, frontend) if not startup_only else ""
    except (OSError, ValueError) as exc:
        return (CheckResult(name, False, 1, f"Regression entry configuration: {exc}"),)
    node, npm = shutil.which("node"), shutil.which("npm") or shutil.which("npm.cmd")
    vitest = _dependency_file(output_dir, "vitest/vitest.mjs")
    packages = ("react", "react-router-dom", "jsdom", "@testing-library/react", "@testing-library/dom", "@testing-library/jest-dom")
    required = [_dependency_file(output_dir, f"{package}/package.json")
                for package in packages]
    if not npm or (not startup_only and (not node or not vitest.is_file() or not all(p.is_file() for p in required))):
        files = [("vitest/vitest.mjs", vitest),
                 *zip((f"{package}/package.json" for package in packages), required)]
        return (CheckResult(name, False, 1, _prerequisite_diagnostic(output_dir, node=node, npm=npm,
            files=files, startup_only=startup_only)),)
    backend = output_dir / "backend"
    try:
        package = json.loads((backend / "package.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError) as exc:
        return (CheckResult(name, False, 1, f"Cannot read backend startup contract: {exc}"),)
    if not package.get("scripts", {}).get("start"):
        return (CheckResult(name, False, 1, "Backend must expose the platform npm start command."),)
    # Never accidentally probe a different process already occupying the contract port.
    try:
        with socket.socket() as probe:
            # Node's Unix listener can reuse a recently closed address. A plain
            # bind here falsely reports TIME_WAIT as an active foreign server.
            # SO_REUSEADDR still refuses a live listener (no SO_REUSEPORT).
            if os.name != "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", 3000))
    except OSError as exc:
        return (CheckResult(name, False, None, f"Regression environment: port 3000 is already occupied: {exc}"),)
    protected = frontend / ".arc"
    protected.mkdir(parents=True, exist_ok=True)
    config = protected / "public-regression.config.mjs"
    report = protected / "public-regression.json"
    if not startup_only:
        fixture = adapter
        fixture += "\n" + re.sub(r"^import[^\n]+\n", "", PUBLIC_SUPPORT, count=1)
        fixture += "\n\n" + "\n\n".join(source for _, source in PUBLIC_CASES)
        fixture += "\n" + JOURNEY_PROBES
        (protected / "public-case-reconstruction.test.tsx").write_text(fixture, encoding="utf-8")
        config.write_text("export default " + json.dumps({"root": str(frontend),
            "esbuild": {"jsx": "automatic"}, "test": {
            "environment": "jsdom", "environmentOptions": {"jsdom": {"url": "http://127.0.0.1:3000"}},
            "include": [".arc/public-case-reconstruction.test.tsx"], "fileParallelism": False,
        }}) + ";\n", encoding="utf-8")
        report.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment.update(PORT="3000", E2E_BASE_URL="http://127.0.0.1:3000")
    environment.pop("OPENAI_API_KEY", None)
    server = None
    try:
        prefix = "delivery-startup" if startup_only else "public-regression-server"
        server_log = protected / (prefix + ".log")
        with server_log.open("w", encoding="utf-8") as log:
            command = [npm, "run", "start"]
            if os.name == "nt" and npm.lower().endswith((".cmd", ".bat")):
                command = f'"{npm}" run start'
            server = subprocess.Popen(command, cwd=backend, env=environment, stdout=log, stderr=subprocess.STDOUT,
                shell=isinstance(command, str), start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
            (protected / (prefix + ".pid")).write_text(str(server.pid), encoding="utf-8")
            _wait_for_homepage(server)
            if startup_only:
                return (CheckResult(name, True, 0,
                    "Owned backend npm start served HTTP 200 on port 3000; behavior acceptance remains pending."),)
            result = subprocess.run([node, str(vitest), "run", "--config", str(config), "--reporter=json",
                "--outputFile=" + str(report)], cwd=frontend, env=environment, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=180, check=False)
            if not report.exists():
                return (CheckResult(name, False, result.returncode, (result.stdout + result.stderr)[-6000:]),)
            data = json.loads(report.read_text(encoding="utf-8"))
            expected_count = len(PUBLIC_CASES) + JOURNEY_PROBE_COUNT
            passed = result.returncode == 0 and data.get("numTotalTests") == expected_count and data.get("numPassedTests") == expected_count
            details = [f"Local reconstructed public bodies plus journey probes: {data.get('numPassedTests')}/{expected_count} passed; "
                       f"{len(PUBLIC_CASES)} unchanged public bodies + {JOURNEY_PROBE_COUNT} additional local probes; "
                       "file-local helpers reconstructed; jsdom, not the official Playwright evaluator."]
            failed_requirements = set()
            for suite in data.get("testResults", []):
                for assertion in suite.get("assertionResults", []):
                    if assertion.get("status") != "passed":
                        requirement = re.match(r"^(REQ-\d+(?:\.\d+)*):", assertion.get("title", ""))
                        if requirement and assertion.get("status") == "failed":
                            failed_requirements.add(requirement.group(1))
                        # Preserve every failing case and its actual assertion;
                        # DOM dumps otherwise displace later cases in feedback.
                        failures = [message.split("Ignored nodes:", 1)[0].strip()
                                    for message in assertion.get("failureMessages", [])]
                        context = observed_failure_context(assertion.get("failureMessages", []))
                        details.append(assertion.get("title", "Unknown case")[:140] + ": " +
                                       " ".join("\n".join(failures).split())[:220] +
                                       (" | " + context if context else ""))
            assertions = [assertion for suite in data.get("testResults", [])
                          for assertion in suite.get("assertionResults", [])]
            statuses = tuple((assertion.get("fullName") or assertion.get("title", ""),
                              assertion.get("status", "")) for assertion in assertions)
            complete = (data.get("numTotalTests") == expected_count and len(statuses) == expected_count
                        and len({title for title, _ in statuses}) == expected_count
                        and all(title and status in {"passed", "failed"} for title, status in statuses)
                        and sum(status == "passed" for _, status in statuses) == data.get("numPassedTests"))
            passed = passed and complete
            if not complete:
                details.append("Incomplete or inconsistent case report; cannot establish a passing candidate.")
            public = CheckResult(name, passed, result.returncode, "\n\n".join(details)[:22000],
                                 tuple(sorted(failed_requirements)), statuses if complete else ())
            browser = verify_browser_contract(protected) if passed else browser_guard_result(False,
                "Not executed: fix the preceding complete jsdom regression before the Chromium guard. "
                "This is not a passing browser result.")
            return (public, browser)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        startup = server_log.read_text(encoding="utf-8", errors="replace")[-4000:] if server_log.exists() else ""
        return (CheckResult(name, False, None, str(exc) + "\nRegression server log:\n" + startup),)
    finally:
        if server is not None:
            try:
                if os.name == "nt":
                    stopped = subprocess.run(["taskkill", "/PID", str(server.pid), "/T", "/F"],
                                             capture_output=True, check=False)
                    if stopped.returncode and server.poll() is None:
                        raise OSError("Windows denied stopping the regression process tree")
                else:
                    try:
                        os.killpg(server.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        os.killpg(server.pid, signal.SIGKILL)
                        server.wait(timeout=5)
                    else:
                        raise
                wait_for_owned_port_release(server.pid)
            except SandboxInfrastructureError:
                raise
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                return (CheckResult(name, False, None,
                    f"Regression environment cleanup failed for owned PID {server.pid}: {exc}. "
                    "Inspect .arc/public-regression-server.log and public-regression.json; "
                    "stop only this regression process and retry in an environment allowing child process cleanup."),)

"""Independent local regression on public interactions; never formal acceptance."""
from __future__ import annotations

import json
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


def _dependency_file(output_dir: Path, relative: str) -> Path:
    """Support npm workspaces hoisting frontend dependencies to the project root."""
    candidates = [output_dir / "frontend/node_modules" / relative, output_dir / "node_modules" / relative]
    return next((path for path in candidates if path.is_file()), candidates[0])


def verify_public_acceptance(output_dir: Path, tree: dict, *, startup_only: bool = False) -> tuple[CheckResult, ...]:
    if not startup_only and not public_acceptance_context(tree):
        return ()
    name = "web runtime startup" if startup_only else "public interaction regression (reconstructed jsdom, not Stage 3)"
    frontend = output_dir / "frontend"
    app = next((p for p in (frontend / "src").glob("App.*") if p.suffix in {".jsx", ".tsx", ".js", ".ts"}), None)
    node, npm = shutil.which("node"), shutil.which("npm") or shutil.which("npm.cmd")
    vitest = _dependency_file(output_dir, "vitest/vitest.mjs")
    required = [_dependency_file(output_dir, f"{package}/package.json")
                for package in ("react", "react-router-dom", "jsdom", "@testing-library/react", "@testing-library/dom")]
    if not npm or (not startup_only and (app is None or not node or not vitest.is_file() or not all(p.is_file() for p in required))):
        return (CheckResult(name, False, 1,
            "Public case regression requires the real React App entry point, node/npm, vitest, jsdom, "
            "@testing-library/react and @testing-library/dom in frontend dependencies. Add missing test "
            "dependencies or restore the actual App entry point; do not replace/relax published assertions."),)
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
        fixture = DOM_ADAPTER.replace("../src/App.jsx", "../src/" + app.name)
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
            deadline = time.monotonic() + 20
            while True:
                if server.poll() is not None:
                    raise RuntimeError(f"Backend exited with code {server.returncode} before the public homepage was reachable")
                try:
                    with urlopen("http://127.0.0.1:3000", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Backend did not serve the public homepage on port 3000")
                    time.sleep(0.1)
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
                        details.append(assertion.get("title", "Unknown case")[:140] + ": " +
                                       "\n".join(failures)[:220])
            return (CheckResult(name, passed, result.returncode, "\n\n".join(details)[:12000],
                               tuple(sorted(failed_requirements))),)
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
            except (OSError, subprocess.TimeoutExpired) as exc:
                return (CheckResult(name, False, None,
                    f"Regression environment cleanup failed for owned PID {server.pid}: {exc}. "
                    "Inspect .arc/public-regression-server.log and public-regression.json; "
                    "stop only this regression process and retry in an environment allowing child process cleanup."),)

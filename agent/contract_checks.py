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
from .verify import CheckResult


def verify_public_acceptance(output_dir: Path, tree: dict) -> tuple[CheckResult, ...]:
    if not public_acceptance_context(tree):
        return ()
    name = "public interaction regression (reconstructed jsdom, not Stage 3)"
    frontend = output_dir / "frontend"
    app = next((p for p in (frontend / "src").glob("App.*") if p.suffix in {".jsx", ".tsx", ".js", ".ts"}), None)
    node, npm = shutil.which("node"), shutil.which("npm") or shutil.which("npm.cmd")
    vitest = frontend / "node_modules/vitest/vitest.mjs"
    required = [frontend / f"node_modules/{package}/package.json"
                for package in ("react", "react-router-dom", "jsdom", "@testing-library/react", "@testing-library/dom")]
    if app is None or not node or not npm or not vitest.is_file() or not all(p.is_file() for p in required):
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
            probe.bind(("127.0.0.1", 3000))
    except OSError as exc:
        return (CheckResult(name, False, None, f"Regression environment: port 3000 is already occupied: {exc}"),)
    protected = frontend / ".arc"
    protected.mkdir(parents=True, exist_ok=True)
    fixture = DOM_ADAPTER.replace("../src/App.jsx", "../src/" + app.name)
    fixture += "\n" + re.sub(r"^import[^\n]+\n", "", PUBLIC_SUPPORT, count=1)
    fixture += "\n\n" + "\n\n".join(source for _, source in PUBLIC_CASES)
    (protected / "public-case-reconstruction.test.tsx").write_text(fixture, encoding="utf-8")
    config = protected / "public-regression.config.mjs"
    config.write_text("export default " + json.dumps({"root": str(frontend), "test": {
        "environment": "jsdom", "environmentOptions": {"jsdom": {"url": "http://127.0.0.1:3000"}},
        "include": [".arc/public-case-reconstruction.test.tsx"], "fileParallelism": False,
    }}) + ";\n", encoding="utf-8")
    report = protected / "public-regression.json"
    report.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment.update(PORT="3000", E2E_BASE_URL="http://127.0.0.1:3000")
    environment.pop("OPENAI_API_KEY", None)
    server = None
    try:
        with (protected / "public-regression-server.log").open("w", encoding="utf-8") as log:
            command = [npm, "run", "start"]
            if os.name == "nt" and npm.lower().endswith((".cmd", ".bat")):
                command = f'"{npm}" run start'
            server = subprocess.Popen(command, cwd=backend, env=environment, stdout=log, stderr=subprocess.STDOUT,
                shell=isinstance(command, str), start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
            (protected / "public-regression-server.pid").write_text(str(server.pid), encoding="utf-8")
            deadline = time.monotonic() + 20
            while True:
                if server.poll() is not None:
                    raise RuntimeError("Backend exited before the public homepage was reachable")
                try:
                    with urlopen("http://127.0.0.1:3000", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Backend did not serve the public homepage on port 3000")
                    time.sleep(0.1)
            result = subprocess.run([node, str(vitest), "run", "--config", str(config), "--reporter=json",
                "--outputFile=" + str(report)], cwd=frontend, env=environment, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=180, check=False)
            if not report.exists():
                return (CheckResult(name, False, result.returncode, (result.stdout + result.stderr)[-6000:]),)
            data = json.loads(report.read_text(encoding="utf-8"))
            passed = result.returncode == 0 and data.get("numTotalTests") == 30 and data.get("numPassedTests") == 30
            details = [f"Local reconstructed public bodies: {data.get('numPassedTests')}/30 passed; "
                       "file-local helpers reconstructed; jsdom, not the official Playwright evaluator."]
            for suite in data.get("testResults", []):
                for assertion in suite.get("assertionResults", []):
                    if assertion.get("status") != "passed":
                        details.append(assertion.get("title", "Unknown case") + ": " +
                            "\n".join(assertion.get("failureMessages", []))[:2500])
            return (CheckResult(name, passed, result.returncode, "\n\n".join(details)[:12000]),)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return (CheckResult(name, False, None, str(exc)),)
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

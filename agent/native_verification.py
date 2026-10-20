"""Execute the original Chromium probe even while complete jsdom cases fail."""
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess

from .browser_contract import BROWSER_CHECK, browser_guard_result, verify_browser_contract
from .contract_checks import verify_public_acceptance as original_public_acceptance
from .visual_acceptance import _wait_for_server
from .station_units import verify_station_units, unavailable
from .verify import CheckResult


def _stop_owned_server(server):
    if os.name == 'nt':
        stopped = subprocess.run(['taskkill', '/PID', str(server.pid), '/T', '/F'],
                                 capture_output=True, timeout=10, check=False)
        if stopped.returncode and server.poll() is None:
            raise RuntimeError('Cannot stop owned native regression process tree')
    else:
        try:
            os.killpg(server.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            raise
        os.killpg(server.pid, signal.SIGKILL)
        server.wait(timeout=5)


def _combine(original, locations):
    passed = original.passed and locations.passed
    return CheckResult(BROWSER_CHECK, passed, 0 if passed else 1,
        original.output+'\n\n'+locations.name+': '+locations.output,
        tuple(sorted(set(original.requirement_ids + locations.requirement_ids))),
        original.case_statuses + locations.case_statuses)


def _run_owned_browser(project: Path, original=None):
    from .sandbox_context import in_worker
    if not in_worker():
        return _combine(browser_guard_result(False, "Native server requires an isolated session"),
                        unavailable("Native server requires an isolated session"))
    protected = project/'frontend/.arc'
    protected.mkdir(parents=True, exist_ok=True)
    log_path = protected/'independent-native-server.log'
    server = None
    try:
        with socket.socket() as probe:
            if os.name != 'nt':
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', 3000))
        npm = shutil.which('npm') or shutil.which('npm.cmd')
        if not npm:
            raise RuntimeError('npm not found; native guard did not execute')
        env = os.environ.copy()
        env.update(PORT='3000', E2E_BASE_URL='http://127.0.0.1:3000')
        env.pop('OPENAI_API_KEY', None)
        command = [npm, 'run', 'start']
        shell = os.name == 'nt' and npm.lower().endswith(('.cmd', '.bat'))
        if shell:
            command = f'"{npm}" run start'
        with log_path.open('wb') as log:
            server = subprocess.Popen(command, shell=shell, cwd=project/'backend', env=env,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=os.name != 'nt',
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
            _wait_for_server(server, 3000, log_path)
            native = original if original is not None else verify_browser_contract(protected)
            result = _combine(native, verify_station_units(protected))
    except Exception as exc:
        message = f'Independent native guard: {exc}'
        result = _combine(original or browser_guard_result(False, message), unavailable(message))
    finally:
        if server is not None:
            try:
                _stop_owned_server(server)
            except Exception as exc:
                message = f'Owned native server cleanup failed: {exc}'
                result = _combine(browser_guard_result(False, message), unavailable(message))
    return result


def verify_public_acceptance(output_dir: Path, tree: dict, *, startup_only=False):
    from .sandbox_context import in_worker
    if not in_worker():
        from .ticketbooking_contract import public_acceptance_context
        if not startup_only and not public_acceptance_context(tree):
            return ()
        from .container_checks import run_checks
        return run_checks("native", output_dir, options={"tree": tree, "startup_only": startup_only})
    checks = original_public_acceptance(output_dir, tree, startup_only=startup_only)
    if startup_only or len(checks) != 2 or not checks[0].case_statuses:
        return checks
    browser = checks[1]
    if browser.name != BROWSER_CHECK:
        return checks
    # The original oracle and probe are untouched. Replace only its unexecuted
    # placeholder with actual native evidence, retaining all jsdom case verdicts.
    original = None if browser.output.startswith('Not executed:') else browser
    return (checks[0], _run_owned_browser(output_dir, original))

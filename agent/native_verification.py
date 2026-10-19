"""Execute the original Chromium probe even while complete jsdom cases fail."""
import os
from dataclasses import replace
from pathlib import Path
import shutil
import signal
import socket
import subprocess

from .browser_contract import BROWSER_CHECK, browser_guard_result, verify_browser_contract
from .contract_checks import verify_public_acceptance as original_public_acceptance
from .visual_acceptance import _wait_for_server
from .station_units import verify_station_units, unavailable
from .booking_completion import verify_booking_completion, completion_result
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


def _combine(original, *additional):
    checks = (original, *additional)
    passed = all(check.passed for check in checks)
    return CheckResult(BROWSER_CHECK, passed, 0 if passed else 1,
        original.output + ''.join('\n\n' + check.name + ': ' + check.output for check in additional),
        tuple(sorted({node for check in checks for node in check.requirement_ids})),
        tuple(status for check in checks for status in check.case_statuses))


def _run_owned_browser(project: Path, original=None):
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
            completion = (verify_booking_completion(protected) if native.passed else
                          completion_result(False, 'Not executed: original native booking entry failed'))
            result = _combine(native, verify_station_units(protected), completion)
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
    try:
        checks = original_public_acceptance(output_dir, tree, startup_only=startup_only)
    finally:
        # The original runner regenerates this exact fixture on every invocation.
        # Retain its bytes as evidence after execution, outside ordinary Vitest
        # discovery: generated self-tests do not own the regression's backend.
        protected = output_dir.resolve() / 'frontend/.arc'
        fixture = protected / 'public-case-reconstruction.test.tsx'
        evidence = protected / 'public-case-reconstruction.fixture.txt'
        if fixture.exists() or fixture.is_symlink():
            if fixture.resolve() != fixture or evidence.resolve() != evidence:
                raise RuntimeError('Cannot archive a linked protected regression fixture')
            fixture.replace(evidence)
    if not startup_only and checks:
        public = checks[0]
        if (not public.passed and public.exit_code not in (None, 0)
                and public.case_statuses
                and all(status == 'passed' for _, status in public.case_statuses)):
            public = replace(public, output=public.output + '\n\n'
                'Runner-level failure: all recorded case assertions passed, but the test '
                f'process exited with code {public.exit_code}. The original runner stdout/stderr '
                'are not retained in this public summary, so the cause is unknown. '
                'This is neither a failed business assertion nor a passing verification. '
                'Investigate runner or asynchronous runtime errors before changing passing '
                'business behavior; preserve the original tests and exit-code requirement.')
            checks = (public, *checks[1:])
    if startup_only or len(checks) != 2 or not checks[0].case_statuses:
        return checks
    browser = checks[1]
    if browser.name != BROWSER_CHECK:
        return checks
    # The original oracle and probe are untouched. Replace only its unexecuted
    # placeholder with actual native evidence, retaining all jsdom case verdicts.
    original = None if browser.output.startswith('Not executed:') else browser
    return (checks[0], _run_owned_browser(output_dir, original))

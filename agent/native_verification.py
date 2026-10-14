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


def _run_owned_browser(project: Path):
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
            result = verify_browser_contract(protected)
    except Exception as exc:
        result = browser_guard_result(False, f'Independent native guard: {exc}')
    finally:
        if server is not None:
            try:
                _stop_owned_server(server)
            except Exception as exc:
                result = browser_guard_result(False, f'Owned native server cleanup failed: {exc}')
    return result


def verify_public_acceptance(output_dir: Path, tree: dict, *, startup_only=False):
    checks = original_public_acceptance(output_dir, tree, startup_only=startup_only)
    if startup_only or len(checks) != 2 or not checks[0].case_statuses:
        return checks
    browser = checks[1]
    if browser.name != BROWSER_CHECK or not browser.output.startswith('Not executed:'):
        return checks
    # The original oracle and probe are untouched. Replace only its unexecuted
    # placeholder with actual native evidence, retaining all jsdom case verdicts.
    return (checks[0], _run_owned_browser(output_dir))

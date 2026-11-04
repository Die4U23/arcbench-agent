"""Run a generated web project and its public Playwright tests in isolation.

The check installs, builds and starts the project, runs the supplied tests
against it from a scratch directory, and condenses the outcome into a compact
structure suitable for a repair prompt. Application failures never raise; they
become a status on the returned result. Child processes see a sanitized
environment: no credentials, an empty HOME, a scratch npm cache and userconfig.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import socket
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .platform_contract import read_contract

ERROR_LIMIT = 1500
LOG_TAIL_LIMIT = 6000
DEFAULT_TIMEOUTS = dict(install=600, build=300, start=60, tests=900, test=10, playwright_install=300)
TEST_SOURCE_SUFFIXES = ('.ts', '.js', '.mts', '.cts', '.mjs', '.cjs', '.tsx', '.jsx')
SPEC_SUFFIXES = ('.spec.ts', '.spec.js', '.spec.mjs', '.spec.cjs', '.test.ts')
ENV_REFERENCE = re.compile(r"""process\.env(?:\.([A-Za-z_][A-Za-z0-9_]*)|\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\])""")
RELATIVE_IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)['"](\.\.?/[^'"\n]+)['"]""")
ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
SECRET_NAME = re.compile(r'KEY|TOKEN|SECRET|PASS|AUTH|CREDENTIAL|COOKIE|OPENAI|API', re.I)
URL_CREDENTIALS = re.compile(r'://([^/@\s:]+):([^/@\s]*)@')
FORWARDED_ENV = ('PATH', 'PATHEXT', 'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'TMPDIR',
                 'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
                 'NUMBER_OF_PROCESSORS', 'OS', 'LANG', 'LC_ALL', 'PLAYWRIGHT_BROWSERS_PATH',
                 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy')
BROWSER_MISSING = re.compile(r"Executable doesn't exist|browserType\.launch: .*(not found|missing)|Please run the following command to download new browsers", re.I)
PLAYWRIGHT_CONFIG = re.compile(r'^playwright\.config\.[cm]?[jt]s$')
STAGED_IGNORE = {'node_modules', '.git', '.arc', 'dist', 'build', 'coverage', 'test-results', 'playwright-report'}
MAX_SUITE_PARENT_DEPTH = 3
WINDOWS = os.name == 'nt'


@dataclass
class RuntimeFailure:
    name: str
    file: str | None
    error: str
    kind: str = 'app'


@dataclass
class RuntimeCheckResult:
    status: str
    stage: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0
    precondition: int = 0
    failures: list[RuntimeFailure] = field(default_factory=list)
    preconditions: list[RuntimeFailure] = field(default_factory=list)
    log_tail: str = ''
    duration_seconds: float = 0.0
    env_supplied: dict[str, str] = field(default_factory=dict)
    env_unset: list[str] = field(default_factory=list)
    launcher: str = ''
    api_probes: list[dict] = field(default_factory=list)
    notes: dict = field(default_factory=dict)
    tests_log_tail: str = ''
    signals: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)

    def to_feedback(self, limit: int = 20) -> dict:
        groups: dict[str, dict] = {}
        for failure in self.failures:
            lines = [line for line in failure.error.splitlines() if line.strip()]
            key = lines[0].strip()[:300] if lines else '(no error message)'
            group = groups.setdefault(key, dict(error=key, count=0, tests=[]))
            group['count'] += 1
            if len(group['tests']) < 5:
                group['tests'].append(dict(name=failure.name, file=failure.file,
                                           detail='\n'.join(lines[1:])[:400]))
        ordered = sorted(groups.values(), key=lambda item: -item['count'])
        feedback = dict(status=self.status, stage=self.stage,
                        counts=dict(passed=self.passed, failed=self.failed, skipped=self.skipped,
                                    precondition=self.precondition, total=self.total),
                        failure_groups=ordered[:limit], omitted_groups=max(0, len(ordered) - limit),
                        env_supplied=sorted(self.env_supplied), env_unset=list(self.env_unset))
        if self.api_probes:
            feedback['api_probes'] = self.api_probes[:limit]
            feedback['api_probe_note'] = ('HTTP probes ran before the page tests. ok false is an interface failure. '
                                          'failure_groups are page results.')
        if self.preconditions:
            # Evaluator preconditions the harness deliberately left unset are reported, not repaired.
            feedback['harness_preconditions'] = [dict(name=item.name, file=item.file, error=item.error[:300])
                                                 for item in self.preconditions[:limit]]
            feedback['harness_precondition_note'] = ('These tests failed on an evaluator-supplied precondition that this '
                                                     'local check does not provide; they are not application defects.')
        if self.status != 'passed' and not self.failures:
            feedback['log_tail'] = self.log_tail[-2000:]
        feedback['signals'] = self.signals
        feedback['diagnostics'] = self.diagnostics
        return feedback


def test_env_names(tests_dir: Path) -> set[str]:
    names: set[str] = set()
    for path in sorted(Path(tests_dir).rglob('*')):
        if path.is_file() and path.suffix in TEST_SOURCE_SUFFIXES and 'node_modules' not in path.parts:
            text = path.read_text(encoding='utf-8', errors='replace')
            names.update(a or b for a, b in ENV_REFERENCE.findall(text))
    return names


def discover_test_env(tests_dir: Path, base_url: str) -> dict[str, str]:
    names = test_env_names(tests_dir)
    run_id = random_run_id()
    urls = [name for name in names if is_base_url_name(name)]
    stems = [name[:-len('BASE_URL')] for name in urls]
    env: dict[str, str] = {}
    for name in sorted(names):
        if name.endswith('RUN_ID'):
            env[name] = run_id
        elif name in urls:
            stem = name[:-len('BASE_URL')]
            # A longer name sharing another URL's stem (X_OTHER_BASE_URL next to X_BASE_URL)
            # denotes a second, separately prepared instance that one app cannot provide.
            if not any(other != stem and other and stem.startswith(other) for other in stems):
                env[name] = base_url
    return env


def classify_failures(failures: list[RuntimeFailure], unset_names) -> tuple[list[RuntimeFailure], list[RuntimeFailure]]:
    """Split failures into application defects and evaluator-precondition failures.

    A failure whose message names an environment variable the tests reference
    but this harness deliberately left unset, or that complains about
    process.env itself, is a harness precondition rather than an app bug.
    """
    patterns = [re.compile(r'(?<![A-Za-z0-9_])' + re.escape(name) + r'(?![A-Za-z0-9_])') for name in unset_names]
    app: list[RuntimeFailure] = []
    preconditions: list[RuntimeFailure] = []
    for failure in failures:
        text = failure.error or ''
        if 'process.env' in text or any(pattern.search(text) for pattern in patterns):
            preconditions.append(RuntimeFailure(failure.name, failure.file, failure.error, 'harness_precondition'))
        else:
            app.append(failure)
    return app, preconditions


def is_base_url_name(name: str) -> bool:
    return name == 'PLAYWRIGHT_BASE_URL' or (name.endswith('BASE_URL') and not name.endswith('_TEMPLATE'))


def random_run_id(length: int = 12) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return secrets.choice(string.ascii_lowercase) + ''.join(secrets.choice(alphabet) for _ in range(length - 1))


def browser_directory_exists(path: Path) -> bool:
    """Inaccessible optional browser caches are not usable candidates."""
    try:
        return path.is_dir()
    except OSError:
        return False


def default_browsers_path() -> Path | None:
    """Playwright's default browser cache for this user, so an empty HOME does not hide installed browsers."""
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA')
        candidate = Path(base) / 'ms-playwright' if base else None
    elif sys.platform == 'darwin':
        candidate = Path.home() / 'Library' / 'Caches' / 'ms-playwright'
    else:
        cache = os.environ.get('XDG_CACHE_HOME')
        candidate = (Path(cache) if cache else Path.home() / '.cache') / 'ms-playwright'
    return candidate if candidate is not None and browser_directory_exists(candidate) else None


def browser_cache_dirs() -> list[Path]:
    """Places a runner image or a developer machine may already keep Chromium. Computed before HOME is replaced."""
    found: list[Path] = []
    raw = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    if raw and raw != '0':
        found.append(Path(raw))
    found.extend((
        Path('/ms-playwright'),
        Path.home() / '.cache' / 'ms-playwright',
        Path('/root/.cache/ms-playwright'),
    ))
    default = default_browsers_path()
    if default is not None:
        found.append(default)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in found:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def revisions_in(directories: list[Path]) -> set[str]:
    found: set[str] = set()
    for directory in directories:
        if not browser_directory_exists(directory):
            continue
        try:
            children = list(directory.iterdir())
        except OSError:
            continue
        for child in children:
            if not browser_directory_exists(child):
                continue
            for prefix in ('chromium-', 'chromium_headless_shell-'):
                if child.name.startswith(prefix):
                    found.add(child.name[len(prefix):])
    return found


def preferred_browser_dir() -> Path | None:
    """A browser directory that exists, preferring one that already contains Chromium."""
    existing = [path for path in browser_cache_dirs() if browser_directory_exists(path)]
    populated = [path for path in existing if revisions_in([path])]
    return (populated or existing or [None])[0]


def clean_env(extra: dict[str, str] | None = None, *, home: Path | None = None, npm_cache: Path | None = None,
              npm_userconfig: Path | None = None) -> dict[str, str]:
    """Forward only harmless variables; credentials never reach child processes."""
    # Record the real cache before HOME points at the empty scratch directory.
    pinned_browsers = preferred_browser_dir() if home is not None else None
    wanted = {name.upper() for name in FORWARDED_ENV}
    env = {}
    for name, value in os.environ.items():
        upper = name.upper()
        forwarded = upper in wanted or upper.startswith('NPM_CONFIG_')
        if upper == 'PLAYWRIGHT_BROWSERS_PATH' and value != '0' and not browser_directory_exists(Path(value)):
            continue
        if URL_CREDENTIALS.search(value):
            continue
        if forwarded and not SECRET_NAME.search(upper):
            env[name] = value
    if home is not None:
        # An empty HOME hides Playwright's per-user browser cache; pass the directory recorded above.
        if not any(name.upper() == 'PLAYWRIGHT_BROWSERS_PATH' for name in env):
            if pinned_browsers is not None:
                env['PLAYWRIGHT_BROWSERS_PATH'] = str(pinned_browsers)
        env['HOME'] = env['USERPROFILE'] = str(home)
    if npm_cache is not None:
        env['npm_config_cache'] = str(npm_cache)
    if npm_userconfig is not None:
        env['npm_config_userconfig'] = str(npm_userconfig)
    env.update(extra or {})
    return env


def parse_playwright_report(report: dict) -> tuple[int, int, int, list[RuntimeFailure]]:
    passed = failed = skipped = 0
    failures: list[RuntimeFailure] = []

    def walk(suite: dict, titles: list[str], file: str | None) -> None:
        nonlocal passed, failed, skipped
        title = suite.get('title') or ''
        path = titles + [title] if title and title != suite.get('file') else titles
        file = suite.get('file') or file
        for spec in suite.get('specs') or []:
            name = ' > '.join(path + [spec.get('title') or '(untitled)'])
            for test in spec.get('tests') or []:
                status = test.get('status')
                if status in ('expected', 'flaky'):
                    passed += 1
                elif status == 'skipped':
                    skipped += 1
                else:
                    failed += 1
                    failures.append(RuntimeFailure(name, spec.get('file') or file, first_error(test)))
        for child in suite.get('suites') or []:
            walk(child, path, file)

    for suite in report.get('suites') or []:
        walk(suite, [], None)
    for error in report.get('errors') or []:
        failures.append(RuntimeFailure('(test harness error)', (error.get('location') or {}).get('file'),
                                       clean_text(error.get('message') or error.get('stack') or json.dumps(error))))
    return passed, failed, skipped, failures


def first_error(test: dict) -> str:
    for result in reversed(test.get('results') or []):
        errors = ([result['error']] if result.get('error') else []) + list(result.get('errors') or [])
        for error in errors:
            message = error.get('message') or error.get('stack') or error.get('value')
            if message:
                return clean_text(message)
        if result.get('status') not in (None, 'passed', 'skipped'):
            return f"test {result['status']}"
    return f"test status {test.get('status')}"


def clean_text(text: str, limit: int = ERROR_LIMIT) -> str:
    text = redact(ANSI.sub('', str(text)).strip())
    return text if len(text) <= limit else text[:limit] + '...[truncated]'


def secret_values() -> list[str]:
    values = [value for name, value in os.environ.items() if SECRET_NAME.search(name) and len(value) >= 8]
    for value in os.environ.values():
        for user, password in URL_CREDENTIALS.findall(value):
            values.append(f'{user}:{password}@')
            if len(password) >= 4:
                values.append(password)
    return values


def redact(text: str) -> str:
    for value in sorted(set(secret_values()), key=len, reverse=True):
        text = text.replace(value, '***')
    return text


def read_tail(path: Path, limit: int) -> str:
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read()
    except OSError:
        return ''
    return ANSI.sub('', data.decode('utf-8', errors='replace'))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def pid_alive(pid: int) -> bool:
    if WINDOWS:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        try:
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        return waited == 0
    except ChildProcessError:
        return True


def process_parents() -> dict[int, int]:
    """Map pid -> parent pid for live processes (Toolhelp on Windows, /proc on Linux, ps elsewhere)."""
    parents: dict[int, int] = {}
    if WINDOWS:
        import ctypes
        from ctypes import wintypes

        class ProcessEntry(ctypes.Structure):
            _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD),
                        ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)), ('th32ModuleID', wintypes.DWORD),
                        ('cntThreads', wintypes.DWORD), ('th32ParentProcessID', wintypes.DWORD),
                        ('pcPriClassBase', ctypes.c_long), ('dwFlags', wintypes.DWORD), ('szExeFile', ctypes.c_char * 260)]
        kernel = ctypes.windll.kernel32
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        for function in (kernel.Process32First, kernel.Process32Next):
            function.restype = wintypes.BOOL
            function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        snapshot = kernel.CreateToolhelp32Snapshot(0x2, 0)
        if snapshot is None or snapshot == wintypes.HANDLE(-1).value:
            return parents
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(ProcessEntry)
            if kernel.Process32First(snapshot, ctypes.byref(entry)):
                while True:
                    parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                    if not kernel.Process32Next(snapshot, ctypes.byref(entry)):
                        break
        finally:
            kernel.CloseHandle(snapshot)
        return parents
    proc = Path('/proc')
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / 'stat').read_text(encoding='utf-8', errors='replace')
                parents[int(entry.name)] = int(stat.rsplit(')', 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
        return parents
    try:
        output = subprocess.run(['ps', '-eo', 'pid=,ppid='], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return parents
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            parents[int(parts[0])] = int(parts[1])
    return parents


def descendant_pids(root_pid: int) -> set[int]:
    """Processes started (transitively) by root_pid, computed from a live parent snapshot."""
    parents = process_parents()
    children: dict[int, set[int]] = {}
    for pid, parent in parents.items():
        children.setdefault(parent, set()).add(pid)
    found: set[int] = set()
    pending = [root_pid]
    while pending:
        current = pending.pop()
        for child in children.get(current, ()):
            if child not in found and child != root_pid:
                found.add(child)
                pending.append(child)
    return found


def spawn(command: list[str], cwd: Path, env: dict[str, str], log: Path) -> subprocess.Popen:
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log, 'ab')
    try:
        options = dict(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP) if WINDOWS else dict(start_new_session=True)
        return subprocess.Popen(command, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                stdout=handle, stderr=subprocess.STDOUT, **options)
    finally:
        handle.close()


def kill_process_tree(process: subprocess.Popen) -> set[int]:
    """Stop the process and everything it started; returns the descendant pids observed beforehand."""
    owned = descendant_pids(process.pid) | {process.pid}
    if process.poll() is None:
        if WINDOWS:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(process.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        else:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    process.wait(5)
                    break
                except subprocess.TimeoutExpired:
                    continue
    elif not WINDOWS:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.wait(10)
    except subprocess.TimeoutExpired:
        process.kill()
    return owned


def _proc_listening_pids(port: int) -> set[int] | None:
    """Linux: resolve LISTEN sockets on the port through /proc/net/tcp* inodes and /proc/*/fd."""
    inodes: set[str] = set()
    found_table = False
    for table in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            lines = Path(table).read_text(encoding='utf-8', errors='replace').splitlines()[1:]
        except OSError:
            continue
        found_table = True
        for line in lines:
            parts = line.split()
            if len(parts) > 9 and parts[3] == '0A' and int(parts[1].rsplit(':', 1)[1], 16) == port:
                inodes.add(parts[9])
    if not found_table:
        return None
    pids: set[int] = set()
    if not inodes:
        return pids
    targets = {f'socket:[{inode}]' for inode in inodes}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            for fd in (entry / 'fd').iterdir():
                try:
                    if os.readlink(fd) in targets:
                        pids.add(int(entry.name))
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return pids


def listening_pids(port: int) -> set[int]:
    if WINDOWS:
        try:
            output = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            return set()
        pids = set()
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3].upper() == 'LISTENING' and parts[1].rsplit(':', 1)[-1] == str(port):
                pids.add(int(parts[4]))
        return pids
    via_proc = _proc_listening_pids(port)
    if via_proc is not None:
        return via_proc
    ss = shutil.which('ss')
    if not ss:
        return set()
    try:
        output = subprocess.run([ss, '-ltnp'], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    pids = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[3].rsplit(':', 1)[-1] == str(port):
            pids.update(int(match) for match in re.findall(r'pid=(\d+)', line))
    return pids


def release_port(port: int, timeout: float = 10, allowed_pids=frozenset()) -> None:
    """Wait for the port to close; only processes we started (allowed_pids) are ever killed."""
    allowed = set(allowed_pids) - {0, os.getpid()}
    deadline = time.monotonic() + timeout
    while port_open(port) and time.monotonic() < deadline:
        for pid in listening_pids(port) & allowed:
            if WINDOWS:
                subprocess.run(['taskkill', '/T', '/F', '/PID', str(pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        time.sleep(0.3)


def run_stage(command: list[str], cwd: Path, env: dict[str, str], log: Path, timeout: float) -> int | None:
    """Return the exit code, or None when the stage exceeded its timeout."""
    process = spawn(command, cwd, env, log)
    try:
        return process.wait(timeout)
    except subprocess.TimeoutExpired:
        return None
    finally:
        kill_process_tree(process)


def tool(name: str) -> str | None:
    return shutil.which(name + '.cmd') if WINDOWS and shutil.which(name + '.cmd') else shutil.which(name)


def usable_bash() -> str | None:
    """A POSIX shell for deploy.sh; the Windows WSL shim is not one for this file tree."""
    bash = shutil.which('bash')
    if not bash:
        return None
    if WINDOWS:
        lowered = bash.lower()
        if '\\system32\\' in lowered or '\\windowsapps\\' in lowered:
            return None
    return bash


def start_command(project_dir: Path, npm: str) -> tuple[list[str], str]:
    """Prefer the project's own deploy.sh under bash, as the official runner does; else npm start."""
    bash = usable_bash()
    if bash and (Path(project_dir) / 'deploy.sh').is_file():
        return [bash, 'deploy.sh'], 'deploy.sh'
    return [npm, 'run', 'start'], 'npm start'


def _is_link(path: Path) -> bool:
    return os.path.islink(path) or (hasattr(os.path, 'isjunction') and os.path.isjunction(path))


def safe_rmtree(path: Path) -> None:
    """Remove a scratch tree without following symlinks or junctions into other locations."""
    path = Path(path)
    if _is_link(path):
        _remove_link(path)
        return
    if not path.exists():
        return
    for current, dirs, files in os.walk(path, topdown=True):
        for name in list(dirs):
            child = Path(current) / name
            if _is_link(child):
                dirs.remove(name)
                _remove_link(child)
        for name in files:
            child = Path(current) / name
            if _is_link(child):
                _remove_link(child)
    shutil.rmtree(path, ignore_errors=True)


def _remove_link(path: Path) -> None:
    for remover in (os.rmdir, os.unlink):
        try:
            remover(path)
            return
        except OSError:
            continue


def link_directory(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        if WINDOWS:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        pass
    link.mkdir(parents=True, exist_ok=True)
    (link / 'package.json').write_text(json.dumps(dict(name='@playwright/test', main='index.js')), encoding='utf-8')
    (link / 'index.js').write_text(f'module.exports = require({json.dumps(str(target))});\n', encoding='utf-8')
    (link / 'cli.js').write_text(f'require({json.dumps(str(target / "cli.js"))});\n', encoding='utf-8')


def read_chromium_revision(package: Path) -> str | None:
    """Chromium revision declared by a package, from playwright-core/browsers.json.

    package is node_modules/@playwright/test, so node_modules/playwright-core is
    package.parent.parent (the sibling of node_modules/@playwright), not package.parent.
    """
    for path in (package.parent.parent / 'playwright-core' / 'browsers.json',
                 package / 'node_modules' / 'playwright-core' / 'browsers.json'):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        for browser in data.get('browsers') or []:
            if isinstance(browser, dict) and browser.get('name') == 'chromium' and browser.get('revision'):
                return str(browser['revision'])
    return None


def read_package_version(package: Path) -> str | None:
    for path in (package / 'package.json', package.parent.parent / 'playwright-core' / 'package.json'):
        if not path.is_file():
            continue
        try:
            version = json.loads(path.read_text(encoding='utf-8')).get('version')
        except (OSError, ValueError):
            continue
        if isinstance(version, str) and version:
            return version
    return None


def packages_beside_browsers(env: dict[str, str] | None = None) -> list[Path]:
    """@playwright/test installed beside a browser cache directory."""
    anchors: list[Path] = []
    seen_anchors: set[str] = set()

    def add_anchor(raw: str | None) -> None:
        if not raw or raw == '0':
            return
        path = Path(raw)
        key = str(path)
        if key not in seen_anchors:
            seen_anchors.add(key)
            anchors.append(path)

    if env is not None:
        add_anchor(env.get('PLAYWRIGHT_BROWSERS_PATH'))
    add_anchor(os.environ.get('PLAYWRIGHT_BROWSERS_PATH'))
    add_anchor('/ms-playwright')
    packages: list[Path] = []
    seen: set[str] = set()
    for anchor in anchors:
        for candidate in (anchor.parent / 'node_modules' / '@playwright' / 'test',
                          anchor / 'node_modules' / '@playwright' / 'test'):
            key = str(candidate)
            if key not in seen:
                seen.add(key)
                packages.append(candidate)
    return packages


def discover_playwright_packages(project_dir: Path, env: dict[str, str]) -> tuple[list[Path], list[Path]]:
    """Preinstalled packages first. The project's own @playwright/test is last and must still match browsers.

    Search order: npm root -g, /usr/lib/node_modules, /usr/local/lib/node_modules, /opt,
    then a package beside PLAYWRIGHT_BROWSERS_PATH and beside /ms-playwright.
    """
    preinstalled: list[Path] = []
    npm = tool('npm')
    if npm:
        try:
            root = subprocess.run([npm, 'root', '-g'], env=env, capture_output=True, text=True, timeout=60).stdout.strip()
            if root:
                preinstalled.append(Path(root.splitlines()[-1]) / '@playwright' / 'test')
        except (OSError, subprocess.SubprocessError):
            pass
    preinstalled.extend((
        Path('/usr/lib/node_modules/@playwright/test'),
        Path('/usr/local/lib/node_modules/@playwright/test'),
        Path('/opt/node_modules/@playwright/test'),
    ))
    opt = Path('/opt')
    if opt.is_dir():
        try:
            children = list(opt.iterdir())
        except OSError:
            children = []
        for child in children:
            preinstalled.append(child / 'node_modules' / '@playwright' / 'test')
    preinstalled.extend(packages_beside_browsers(env))
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in preinstalled:
        key = str(path)
        if key not in seen:
            seen.add(key)
            deduped.append(path)
    project = [Path(project_dir) / 'node_modules' / '@playwright' / 'test']
    return deduped, project


def choose_playwright_package(preinstalled: list[Path], project: list[Path],
                              browser_dirs: list[Path]) -> tuple[Path | None, dict]:
    """Use a package only when its browsers.json chromium revision is already installed.

    A project copy whose revision does not match is ignored, even if it has cli.js.
    When a matching revision is declared but that package has no cli.js, the notes
    carry the version so the caller can install that exact package into the cache.
    """
    installed = revisions_in(browser_dirs)
    notes = dict(method=None, package=None, version=None, chromium_revision=None,
                 browser_dirs=[str(path) for path in browser_dirs])

    def take(candidates: list[Path], method: str) -> Path | None:
        for candidate in candidates:
            if not (candidate / 'cli.js').is_file():
                continue
            revision = read_chromium_revision(candidate)
            if revision and revision in installed:
                notes.update(method=method, package=str(candidate), version=read_package_version(candidate),
                             chromium_revision=revision)
                return candidate
        return None

    chosen = take(preinstalled, 'preinstalled') or take(project, 'project')
    if chosen is not None:
        return chosen, notes
    for candidate in [*preinstalled, *project]:
        revision = read_chromium_revision(candidate)
        version = read_package_version(candidate)
        if revision and revision in installed and version:
            notes.update(method='inferred', package=str(candidate), version=version, chromium_revision=revision)
            return None, notes
    notes['method'] = 'install-chromium'
    return None, notes


def runtime_log_fields(result: RuntimeCheckResult) -> dict:
    """Fields that must appear on stdout. The platform does not return the report file."""
    fields = dict(notes=result.notes)
    if result.status not in {'passed', 'failed'} and result.tests_log_tail:
        fields['tests_log_tail'] = result.tests_log_tail[-1500:]
    return fields


def _install_playwright_package(spec: str, cache_dir: Path, env: dict[str, str], log: Path, timeout: float) -> int | None:
    npm = tool('npm')
    if not npm:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    if not (cache_dir / 'package.json').exists():
        (cache_dir / 'package.json').write_text('{"name":"playwright-cache","private":true}\n', encoding='utf-8')
    return run_stage([npm, 'install', '--no-audit', '--no-fund', '--no-save', spec], cache_dir, env, log, timeout)


def resolve_playwright(project_dir: Path, cache_dir: Path, env: dict[str, str], log: Path,
                       timeout: float, *, preinstalled: list[Path] | None = None,
                       project: list[Path] | None = None, browser_dirs: list[Path] | None = None
                       ) -> tuple[Path | None, str, dict]:
    """Locate an @playwright/test whose Chromium revision is already on disk. Returns (path, how, notes)."""
    if preinstalled is None or project is None:
        discovered_preinstalled, discovered_project = discover_playwright_packages(project_dir, env)
        preinstalled = discovered_preinstalled if preinstalled is None else preinstalled
        project = discovered_project if project is None else project
    dirs = browser_dirs if browser_dirs is not None else [path for path in browser_cache_dirs() if path.is_dir()]
    cached = cache_dir / 'node_modules' / '@playwright' / 'test'
    chosen, notes = choose_playwright_package([*preinstalled, cached], project, dirs)
    if chosen is not None:
        return chosen.resolve(), f"{notes['method']} {chosen}", notes
    if notes.get('method') == 'inferred' and notes.get('version'):
        spec = f"@playwright/test@{notes['version']}"
        code = _install_playwright_package(spec, cache_dir, env, log, timeout)
        installed_revision = read_chromium_revision(cached)
        if code == 0 and (cached / 'cli.js').is_file() and installed_revision and installed_revision in revisions_in(dirs):
            notes.update(method='inferred', package=str(cached), chromium_revision=installed_revision)
            return cached.resolve(), f'installed {spec} into cache', notes
        notes['install_exit'] = code
    launcher = next((candidate for candidate in [*preinstalled, *project, cached] if (candidate / 'cli.js').is_file()), None)
    node = shutil.which('node')
    if launcher is not None and node:
        revision = read_chromium_revision(launcher)
        notes.update(method='install-chromium', package=str(launcher), version=read_package_version(launcher),
                     chromium_revision=revision)
        code = run_stage([node, str(launcher / 'cli.js'), 'install', 'chromium'], cache_dir, env, log, timeout)
        notes['install_exit'] = code
        if code == 0 and revision and revision in revisions_in(browser_cache_dirs()):
            return launcher.resolve(), 'installed chromium for existing package', notes
        return None, f'chromium install failed ({code})', notes
    return None, 'no playwright package matched installed browsers', notes


def playwright_config(tests: Path, base_url: str, report: Path, output: Path, test_timeout: float) -> str:
    config = dict(testDir=str(tests), outputDir=str(output), timeout=int(test_timeout * 1000), workers=1, retries=0,
                  fullyParallel=False, forbidOnly=True, reporter=[['json', dict(outputFile=str(report))]],
                  use=dict(baseURL=base_url, headless=True, trace='off', screenshot='off', video='off', timezoneId='UTC'),
                  projects=[dict(name='chromium', use=dict(browserName='chromium'))])
    return 'module.exports = ' + json.dumps(config, indent=2) + ';\n'


def external_import_targets(tests_dir: Path) -> list[Path]:
    """Existing files that test sources import from outside tests_dir via relative paths."""
    tests_dir = Path(tests_dir).resolve()
    targets: list[Path] = []
    for path in sorted(tests_dir.rglob('*')):
        if not path.is_file() or path.suffix not in TEST_SOURCE_SUFFIXES or 'node_modules' in path.parts:
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        for spec in RELATIVE_IMPORT.findall(text):
            base = (path.parent / spec.split('?', 1)[0]).resolve()
            if base.is_relative_to(tests_dir):
                continue
            stripped = base.with_suffix('') if base.suffix in ('.js', '.mjs', '.cjs', '.jsx') else base
            candidates = [base, *(stripped.with_name(stripped.name + suffix) for suffix in TEST_SOURCE_SUFFIXES + ('.json',)),
                          *(base / ('index' + suffix) for suffix in TEST_SOURCE_SUFFIXES)]
            for candidate in candidates:
                if candidate.is_file():
                    if candidate not in targets:
                        targets.append(candidate)
                    break
    return targets


def stage_tests(tests_dir: Path, harness: Path) -> tuple[Path | None, str | None]:
    """Copy the suite (plus the smallest parent holding helpers it imports) into the harness.

    Returns (staged tests dir, None) or (None, reason) when helpers live too far
    outside the suite to stage safely.
    """
    tests_dir = Path(tests_dir).resolve()
    ignore = lambda _, names: [name for name in names if name in STAGED_IGNORE or PLAYWRIGHT_CONFIG.match(name)]
    targets = external_import_targets(tests_dir)
    if not targets:
        shutil.copytree(tests_dir, harness / 'tests', ignore=ignore)
        return harness / 'tests', None
    common = Path(os.path.commonpath([str(tests_dir), *(str(target) for target in targets)]))
    depth = len(tests_dir.relative_to(common).parts)
    if common == Path(common.anchor) or depth > MAX_SUITE_PARENT_DEPTH:
        names = ', '.join(sorted(str(target) for target in targets)[:5])
        return None, f'tests import helpers outside the suite that cannot be staged safely: {names}'
    staged_root = harness / 'suite'
    shutil.copytree(common, staged_root, ignore=ignore)
    return staged_root / tests_dir.relative_to(common), None


def wait_for_http(port: int, process: subprocess.Popen, timeout: float) -> str:
    """Return 'ready', 'exited' or 'timeout'."""
    deadline = time.monotonic() + timeout
    url = f'http://127.0.0.1:{port}/'
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return 'exited'
        try:
            with urllib.request.urlopen(url, timeout=2):
                return 'ready'
        except urllib.error.HTTPError:
            return 'ready'
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.5)
    return 'timeout'


def smoke_probe_routes() -> list[dict]:
    return [
        dict(method='GET', path='/api/health', probe='read', expect=200),
        dict(method='GET', path='/', probe='read', expect=200, require_body=True),
    ]


def probe_plan(project_dir: Path) -> list[dict]:
    """Health, the root page, then parameter-free read routes from the contract. Duplicates collapse."""
    routes = smoke_probe_routes()
    document = read_contract(project_dir)
    extra = document.get('routes') if isinstance(document, dict) else None
    if isinstance(extra, list):
        routes.extend(item for item in extra if isinstance(item, dict))
    planned = []
    seen = set()
    for route in routes:
        method = str(route.get('method') or '').upper()
        path = route.get('path')
        if (method, path) in seen:
            continue
        seen.add((method, path))
        planned.append(route)
        if len(planned) >= 20:
            break
    return planned


def probe_routes(base_url: str, routes: list[dict], timeout: float = 5) -> list[dict]:
    """Call read probes. Mutating and templated routes are listed and not requested."""
    results = []
    for route in routes:
        method = str(route.get('method') or '').upper()
        path = str(route.get('path') or '')
        templated = '{' in path or '/:' in path
        if route.get('probe') != 'read' or method != 'GET' or not path.startswith('/') or path.startswith('//') or templated:
            results.append(dict(method=method, path=path, skipped='not a parameter-free GET read probe'))
            continue
        url = base_url.rstrip('/') + path
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method='GET'), timeout=timeout) as response:
                status = response.status
                body = response.read(2048)
        except urllib.error.HTTPError as error:
            status = error.code
            body = error.read(2048)
        except (urllib.error.URLError, OSError, ValueError) as error:
            results.append(dict(method=method, path=path, ok=False, error=f'{type(error).__name__}: {error}'[:300]))
            continue
        text = redact(body.decode('utf-8', errors='replace'))[:400]
        expect = route.get('expect')
        if isinstance(expect, int):
            ok = status == expect
            note = None
        elif status in (401, 403):
            ok = True
            note = 'auth_required'
        else:
            ok = 200 <= status < 400
            note = None
        if ok and route.get('require_body') and not text.strip():
            ok = False
        item = dict(method=method, path=path, status=status, ok=ok, body=text)
        if note:
            item['note'] = note
        results.append(item)
    return results


def api_failures(probes: list[dict]) -> list[RuntimeFailure]:
    failures = []
    for probe in probes:
        if probe.get('skipped') or probe.get('ok'):
            continue
        detail = probe.get('error') or f"HTTP {probe.get('status')}"
        message = f"{probe.get('method')} {probe.get('path')} {detail}"
        if probe.get('body'):
            message += '\n' + str(probe['body'])[:300]
        failures.append(RuntimeFailure(f"{probe.get('method')} {probe.get('path')}", 'docs/contract.json', message, 'api'))
    return failures


DIAGNOSTIC_LOG_LIMIT = 1500


def failure_payload(failure: RuntimeFailure) -> dict:
    return dict(name=failure.name, file=failure.file, error=failure.error, kind=failure.kind)


def signal_counts(passed: int, failed: int, skipped: int, failures: list[RuntimeFailure]) -> dict:
    return dict(passed=passed, failed=failed, skipped=skipped,
                failures=[failure_payload(item) for item in failures])


def probe_signal(probes: list[dict]) -> dict:
    failures = api_failures(probes)
    executed = [item for item in probes if 'skipped' not in item]
    return signal_counts(len(executed) - len(failures), len(failures), len(probes) - len(executed), failures)


def diagnostic_log_tail(text: str, status: str, limit: int = DIAGNOSTIC_LOG_LIMIT) -> str:
    """Redacted tail for statuses other than passed and failed. Those two carry an empty tail."""
    if status in {'passed', 'failed'}:
        return ''
    return redact(text or '')[-limit:]


def playwright_diagnostic(how: str = '', notes: dict | None = None) -> dict:
    notes = notes or {}
    revision = notes.get('chromium_revision') or notes.get('revision') or ''
    return dict(how=str(how or ''), version=str(notes.get('version') or ''),
                revision=str(revision or ''),
                browser_dirs=[str(item) for item in notes.get('browser_dirs') or []])


def aggregate_outcomes(outcomes: list[dict]) -> dict:
    return dict(
        passed=sum(item['passed'] for item in outcomes),
        failed=sum(item['failed'] for item in outcomes),
        skipped=sum(item['skipped'] for item in outcomes),
        total=sum(item['total'] for item in outcomes),
        precondition=sum(item['precondition'] for item in outcomes),
        failures=[failure for item in outcomes for failure in item['failures']],
        preconditions=[failure for item in outcomes for failure in item['preconditions']],
    )


def combined_status(outcomes: list[dict]) -> str:
    if len(outcomes) == 1:
        return outcomes[0]['status']
    statuses = [item['status'] for item in outcomes]
    if 'unavailable' in statuses:
        return 'unavailable'
    if 'timeout' in statuses:
        return 'timeout'
    if any(status == 'failed' for status in statuses):
        return 'failed'
    if outcomes and all(status == 'passed' for status in statuses):
        return 'passed'
    if outcomes and all(status == 'test_harness_failed' for status in statuses):
        return 'test_harness_failed'
    return 'failed'


def run_playwright_suite(*, node: str, package: Path, tests: Path, harness: Path, base_url: str, report: Path,
                         output_dir: Path, env: dict[str, str], env_unset: list[str], log: Path, timeout: float,
                         test_timeout: float, grep: str | None, signal_name: str = '') -> dict:
    """Run one staged suite. Returns counts plus a signal record. Does not start or stop the server."""
    config = harness / 'playwright.config.cjs'
    config.write_text(playwright_config(tests, base_url, report, output_dir, test_timeout), encoding='utf-8')
    command = [node, str(package / 'cli.js'), 'test', '--config', str(config)]
    if grep:
        command += ['--grep', grep]
    code = run_stage(command, harness, env, log, timeout)
    empty = dict(status='timeout', passed=0, failed=0, skipped=0, total=0, precondition=0,
                 failures=[], preconditions=[], signal=signal_counts(0, 0, 0, []), signal_name=signal_name)
    if code is None:
        return empty
    output = read_tail(log, 200000)
    try:
        data = json.loads(report.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = None
    if data is None:
        status = 'unavailable' if BROWSER_MISSING.search(output) else 'test_harness_failed'
        return dict(status=status, passed=0, failed=0, skipped=0, total=0, precondition=0,
                    failures=[], preconditions=[], signal=signal_counts(0, 0, 0, []), signal_name=signal_name)
    passed, failed, skipped, failures = parse_playwright_report(data)
    total = passed + failed + skipped
    failures, preconditions = classify_failures(failures, env_unset)
    failed = sum(1 for item in failures if item.name != '(test harness error)')
    if BROWSER_MISSING.search(output) or any(BROWSER_MISSING.search(item.error) for item in failures):
        status = 'unavailable'
    elif total == 0 or (passed == 0 and failed == 0 and not preconditions) or (failures and not failed):
        status = 'test_harness_failed'
    else:
        status = 'passed' if not failures and not preconditions and code == 0 else 'failed'
    return dict(status=status, passed=passed, failed=failed, skipped=skipped, total=total,
                precondition=len(preconditions), failures=failures, preconditions=preconditions,
                signal=signal_counts(passed, failed, skipped, failures), signal_name=signal_name)


def normalize_suites(tests_dir: Path | None, suites: list[tuple[str, Path]] | None) -> list[tuple[str, Path]]:
    """None + a tests directory is the input suite. None + no directory, or an empty list, is probes only."""
    if suites is None:
        if tests_dir is None:
            return []
        return [('input', Path(tests_dir))]
    return [(str(name), Path(directory)) for name, directory in suites]


def run_runtime_check(project_dir: Path, tests_dir: Path | None = None, *,
                      suites: list[tuple[str, Path]] | None = None,
                      work_dir: Path, install: bool = True,
                      timeouts: dict | None = None, port: int | None = None, grep: str | None = None,
                      playwright_cache: Path | None = None, deadline: float | None = None) -> RuntimeCheckResult:
    """Install, build and start once, then run each suite against that server.

    tests_dir is the single-suite form and runs as the "input" signal. suites is a
    list of (signal_name, directory) pairs. None with tests_dir set uses
    [("input", tests_dir)]. None with tests_dir None, or an empty list, runs HTTP
    probes only (GET /api/health, GET /, and parameter-free read routes in
    docs/contract.json). Probes run after the server is up and before browser
    resolution. A missing browser returns status unavailable and still includes
    probe results. Deadline is an absolute time.monotonic() bound shared by all stages.
    """
    project_dir, work_dir = Path(project_dir).resolve(), Path(work_dir).resolve()
    suite_list = [(name, directory.resolve()) for name, directory in normalize_suites(tests_dir, suites)]
    if not project_dir.is_dir():
        raise FileNotFoundError(f'project directory not found: {project_dir}')
    for _name, directory in suite_list:
        if not directory.is_dir():
            raise FileNotFoundError(f'tests directory not found: {directory}')
    limits = {**DEFAULT_TIMEOUTS, **(timeouts or {})}
    started = time.monotonic()
    run_dir = work_dir / f'run-{time.strftime("%Y%m%d-%H%M%S")}-{secrets.token_hex(3)}'
    logs = run_dir / 'logs'
    logs.mkdir(parents=True)
    port = port or free_port()
    base_url = f'http://127.0.0.1:{port}'
    env_supplied: dict[str, str] = {}
    env_unset: list[str] = []
    launcher = ''
    app: subprocess.Popen | None = None
    owned: set[int] = set()
    stage = 'prepare'
    resolution_notes: dict = {}
    signals: dict = {}
    playwright_info = playwright_diagnostic()

    def remaining() -> float | None:
        return None if deadline is None else deadline - time.monotonic()

    def budget(limit: float) -> float:
        left = remaining()
        return limit if left is None else max(0.0, min(limit, left))

    def finish(status: str, **counts) -> RuntimeCheckResult:
        tail = ''.join(f'--- {log.stem} ---\n{read_tail(log, LOG_TAIL_LIMIT)}\n' for log in sorted(logs.glob('*.log'),
                       key=lambda item: item.stat().st_mtime))
        tests_parts = [redact(read_tail(log, DIAGNOSTIC_LOG_LIMIT)) for log in sorted(logs.glob('tests*.log'))]
        tests_tail = '\n'.join(part for part in tests_parts if part)[-DIAGNOSTIC_LOG_LIMIT:]
        diagnostics = dict(playwright=dict(playwright_info), log_tail=diagnostic_log_tail(tail, status))
        return RuntimeCheckResult(status=status, stage=stage, log_tail=redact(tail)[-LOG_TAIL_LIMIT:],
                                  duration_seconds=round(time.monotonic() - started, 2),
                                  env_supplied=dict(env_supplied), env_unset=list(env_unset), launcher=launcher,
                                  notes=dict(resolution_notes), tests_log_tail=tests_tail,
                                  signals={key: dict(value) for key, value in signals.items()},
                                  diagnostics=diagnostics, **counts)

    def note(text: str) -> None:
        with open(logs / f'{stage}.log', 'a', encoding='utf-8') as handle:
            handle.write(redact(text) + '\n')

    def expired() -> bool:
        left = remaining()
        return left is not None and left <= 0

    try:
        home = run_dir / 'home'
        home.mkdir()
        userconfig = home / '.npmrc'
        userconfig.write_text('', encoding='utf-8')
        npm_cache = work_dir / '.npm-cache'
        env_options = dict(home=home, npm_cache=npm_cache, npm_userconfig=userconfig)
        prepared: list[dict] = []
        for index, (name, suite_dir) in enumerate(suite_list):
            harness = run_dir / 'harness' if len(suite_list) == 1 else run_dir / f'harness-{index}'
            harness.mkdir(parents=True)
            tests, staging_error = stage_tests(suite_dir, harness)
            if tests is None:
                note(staging_error or f'{name} tests could not be staged')
                return finish('test_harness_failed')
            (harness / 'package.json').write_text('{"name":"runtime-check-harness","private":true}\n', encoding='utf-8')
            supplied = discover_test_env(tests, base_url)
            unset = sorted(test_env_names(tests) - set(supplied))
            prepared.append(dict(name=name, tests=tests, harness=harness, env=supplied, unset=unset))
        if len(prepared) == 1:
            env_supplied = dict(prepared[0]['env'])
            env_unset = list(prepared[0]['unset'])
        elif prepared:
            merged: dict[str, str] = {}
            unset_names: set[str] = set()
            for item in prepared:
                for key, value in item['env'].items():
                    merged.setdefault(key, value)
                unset_names.update(item['unset'])
            env_supplied = merged
            env_unset = sorted(unset_names - set(merged))
        base_env = clean_env(**env_options)
        npm, node = tool('npm'), shutil.which('node')
        if not npm or not node:
            note('node/npm not found on PATH')
            return finish('unavailable')

        stage = 'install'
        if expired():
            return finish('timeout')
        if not (project_dir / 'package.json').is_file():
            note('project has no package.json')
            return finish('install_failed')
        if install and not (project_dir / 'node_modules').is_dir():
            locked = any((project_dir / name).is_file() for name in ('package-lock.json', 'npm-shrinkwrap.json'))
            command = ([npm, 'ci', '--include=dev', '--no-audit', '--no-fund'] if locked else
                       [npm, 'install', '--include=dev', '--no-audit', '--no-fund', '--no-package-lock'])
            code = run_stage(command, project_dir, base_env, logs / 'install.log', budget(limits['install']))
            if code != 0:
                # A partial tree must not be mistaken for a successful install next round.
                safe_rmtree(project_dir / 'node_modules')
            if code is None:
                return finish('timeout')
            if code != 0:
                return finish('install_failed')

        stage = 'build'
        if expired():
            return finish('timeout')
        try:
            scripts = json.loads((project_dir / 'package.json').read_text(encoding='utf-8-sig')).get('scripts') or {}
        except ValueError as error:
            note(f'package.json is not valid JSON: {error}')
            return finish('build_failed')
        if 'build' in scripts:
            code = run_stage([npm, 'run', 'build'], project_dir, base_env, logs / 'build.log', budget(limits['build']))
            if code is None:
                return finish('timeout')
            if code != 0:
                return finish('build_failed')

        stage = 'start'
        if expired():
            return finish('timeout')
        command, launcher = start_command(project_dir, npm)
        if launcher != 'deploy.sh' and 'start' not in scripts:
            note('package.json has no start script')
            return finish('start_failed')
        note(f'launcher: {launcher}')
        start_limit = limits['start'] + (limits['install'] + limits['build'] if launcher == 'deploy.sh' else 0)
        app = spawn(command, project_dir, clean_env(dict(PORT=str(port), HOST='127.0.0.1'), **env_options), logs / 'start.log')
        state = wait_for_http(port, app, budget(start_limit))
        if state != 'ready':
            note(f'app {state} before answering on {base_url}/')
            return finish('timeout' if state == 'timeout' else 'start_failed')

        stage = 'probe'
        probes = probe_routes(base_url, probe_plan(project_dir), timeout=max(1.0, min(5.0, budget(5))))
        signals['probes'] = probe_signal(probes)
        if not prepared:
            failures = api_failures(probes)
            executed = [item for item in probes if 'skipped' not in item]
            return finish('passed' if executed and not failures else 'failed',
                          passed=len(executed) - len(failures), failed=len(failures), total=len(executed),
                          failures=failures, api_probes=probes)

        stage = 'playwright'
        if expired():
            return finish('timeout', api_probes=probes)
        package, how, resolution = resolve_playwright(project_dir, Path(playwright_cache or work_dir / '.playwright-cache'),
                                                      base_env, logs / 'playwright.log', budget(limits['playwright_install']))
        resolution_notes.update(resolution)
        playwright_info.clear()
        playwright_info.update(playwright_diagnostic(how, resolution))
        note(f'@playwright/test: {how} {json.dumps(resolution, ensure_ascii=False)}')
        if package is None:
            return finish('unavailable', api_probes=probes)

        stage = 'tests'
        outcomes: list[dict] = []
        for index, item in enumerate(prepared):
            if expired():
                return finish('timeout', api_probes=probes)
            link_directory(package, item['harness'] / 'node_modules' / '@playwright' / 'test')
            single = len(prepared) == 1
            outcome = run_playwright_suite(
                node=node, package=package, tests=item['tests'], harness=item['harness'], base_url=base_url,
                report=run_dir / ('report.json' if single else f'report-{index}.json'),
                output_dir=run_dir / 'test-results' if single else run_dir / 'test-results' / str(index),
                env=clean_env(item['env'], **env_options), env_unset=item['unset'],
                log=logs / ('tests.log' if single else f'tests-{index}.log'),
                timeout=budget(limits['tests']), test_timeout=limits['test'], grep=grep, signal_name=item['name'])
            signals[item['name']] = outcome['signal']
            outcomes.append(outcome)
            if outcome['status'] in {'timeout', 'unavailable'}:
                return finish(outcome['status'], api_probes=probes, **aggregate_outcomes(outcomes))
        return finish(combined_status(outcomes), api_probes=probes, **aggregate_outcomes(outcomes))
    except Exception as error:  # a broken harness must not abort the caller's generation loop
        note(f'{type(error).__name__}: {error}')
        return finish('test_harness_failed')
    finally:
        if app is not None:
            owned = kill_process_tree(app)
            release_port(port, allowed_pids=owned)

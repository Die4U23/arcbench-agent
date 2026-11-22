"""Controller-owned local self-test acceptance and exact frozen-suite exports.

These artifacts are local diagnostics from public requirements, never formal
platform results. The controller is the sole writer, as for sandbox snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .sandbox_files import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES, validate_relative_path

_README = '''# Local self-tests

report.json records local self-test acceptance, actual execution counts, source
binding and frozen-suite integrity. These are generated development tests from
public requirements, not official platform scoring tests or a formal score.
requirements_sha256, when present, hashes the isolated public input bundle.

Each suite-N/ contains the exact known frozen files, including plan.json and
*.spec.cjs. The report gives expected frozen hashes and exported-byte hashes;
integrity=false means the observed bytes changed and acceptance is refused.
All exported attempts are retained, including failed or unexecuted suites.

When delivery is present, delivery.ready and READY_FOR_EVALUATION describe only
the separate build/start/Chromium smoke check on the bound source. They do not
change accepted, requirement status, or formal results. delivery.runtime keeps
the actual smoke and authored-suite counts, failures and skipped checks.
delivery.requirements lists unresolved obligations; source coverage claims do
not count as verified behavior. A nonzero authored failure count may coexist
with a runnable candidate. A build/start/smoke or integrity failure cannot.

To rerun one suite, use the same packaged agent Python environment, Node/npm,
compatible @playwright/test and Chromium installation/dependency cache. From
the platform-agent directory, run the following Python code, replacing APP and
suite-1 as appropriate. It copies application source into a temporary scratch
directory, installs/builds/starts that copy, and runs the exact exported suite.
The harness supplies Playwright's baseURL fixture for its local server; these
compiled suites do not require a manually guessed BASE_URL environment value.
Runtime ports, timings and dependency versions can vary between reruns.

```python
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
import json
from agent.platform_generation import sync_project_copy
from agent.platform_runtime_check import run_runtime_check

APP = Path('/absolute/path/to/generated/application')
SUITE = APP / 'self-tests' / 'suite-1'
with TemporaryDirectory(prefix='local-self-test-') as temporary:
    root = Path(temporary)
    scratch = root / 'project'
    sync_project_copy(APP, scratch)
    result = run_runtime_check(scratch, suites=[('authored', SUITE)],
                               work_dir=root / 'check')
    print(json.dumps(asdict(result), indent=2))
```
'''


@dataclass
class SelfTestArtifacts:
    directory: Path
    identity: tuple[int, int]
    report_bytes: bytes


def _linked(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def _regular_bytes(path):
    info = path.lstat()
    if (_linked(path) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_size > MAX_FILE_BYTES):
        raise ValueError('self-test artifact is not a bounded unlinked regular file')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('self-test artifact changed while opening')
        content = stream.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ValueError('self-test artifact exceeds byte limit')
    return content


def _encoded(report):
    return (json.dumps(report, indent=2, ensure_ascii=False) + '\n').encode('utf-8')


def record_delivery_failure(state, source_sha256, candidate, reason='candidate_not_delivered'):
    """Correct an exported report after a guarded promotion is rolled back."""
    _check_owned(state)
    report = json.loads(state.report_bytes)
    report.update(status='incomplete', accepted=False, source_sha256=source_sha256)
    if candidate:
        report['candidate'] = candidate
    report['reasons'] = list(dict.fromkeys([*report.get('reasons', []), reason]))
    if isinstance(report.get('runtime'), dict):
        report['runtime']['source_matches'] = bool(
            _sha256(source_sha256) and report['runtime'].get('source_matches')
            and report['runtime'].get('checked_source_sha256') == source_sha256)
    content = _encoded(report)
    descriptor, temporary = tempfile.mkstemp(prefix='.report-', dir=state.directory)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
        os.replace(temporary, state.directory / 'report.json')
        state.report_bytes = content
    finally:
        Path(temporary).unlink(missing_ok=True)


def initialize_self_test(app):
    """Reserve the visible directory exclusively; never reuse user artifacts."""
    root = Path(app).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('self-test project must be a directory')
    if any(path.name.casefold() == 'self-tests' for path in root.iterdir()):
        raise FileExistsError('self-tests is reserved and already exists')
    directory = root / 'self-tests'
    directory.mkdir(exist_ok=False)
    content = _encoded(dict(version=1, scope='public_requirements_local_self_tests',
        status='not_run', accepted=False, reasons=['self_tests_not_run'], suites=[]))
    with (directory / 'report.json').open('xb') as stream:
        stream.write(content)
    with (directory / 'README.md').open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(_README)
    info = directory.stat()
    return SelfTestArtifacts(directory, (info.st_dev, info.st_ino), content)


def attach_candidate_delivery(state, delivery, *, diagnostic_only=False):
    """Add the independent delivery receipt without changing any self-test result."""
    _check_owned(state)
    report = json.loads(state.report_bytes)
    report['candidate_readiness' if diagnostic_only else 'delivery'] = delivery
    content = _encoded(report)
    descriptor, temporary = tempfile.mkstemp(prefix='.report-', dir=state.directory)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
        os.replace(temporary, state.directory / 'report.json')
        state.report_bytes = content
    finally:
        Path(temporary).unlink(missing_ok=True)


def verify_exported_suites(state, report):
    """Recheck the visible exported bytes at the independent delivery boundary."""
    _check_owned(state)
    if json.loads(state.report_bytes) != report:
        raise ValueError('self-test report changed before candidate export')
    for suite in report.get('suites', []):
        name = validate_relative_path(suite['directory'])
        if len(name.parts) != 1 or not suite.get('exported') or not suite.get('integrity'):
            raise ValueError('self-test suite was not safely exported')
        _, integrity, observed = _frozen_files(dict(directory=state.directory / name,
                                                   hashes=suite['frozen_hashes']))
        if not integrity or observed != suite['exported_hashes']:
            raise ValueError('exported self-test suite changed')


def _check_owned(state):
    directory = state.directory
    info = directory.lstat()
    if (_linked(directory) or not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != state.identity):
        raise ValueError('controller-owned self-tests directory changed')
    if _regular_bytes(directory / 'report.json') != state.report_bytes:
        raise ValueError('controller-owned self-test report changed')


def _frozen_files(suite):
    root = Path(suite['directory'])
    if _linked(root) or not root.is_dir():
        raise ValueError('frozen suite directory is unsafe')
    # Parent links could redirect a nominal frozen-suite location too.
    if any(_linked(parent) for parent in root.parents):
        raise ValueError('frozen suite parent is linked')
    hashes = suite.get('hashes')
    if not isinstance(hashes, dict) or not 1 <= len(hashes) <= MAX_FILES:
        raise ValueError('frozen suite has no bounded manifest')
    files, total = {}, 0
    seen = set()
    for name, digest in hashes.items():
        relative = validate_relative_path(name)
        if len(relative.parts) != 1 or name.casefold() in seen or not _sha256(digest):
            raise ValueError('frozen suite manifest is unsafe')
        seen.add(name.casefold())
        content = _regular_bytes(root / name)
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ValueError('frozen suite exceeds total byte limit')
        files[name] = content
    observed = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    integrity = set(path.name for path in root.iterdir()) == set(hashes) and observed == hashes
    return files, integrity, observed


def _sha256(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _count(value):
    return type(value) is int and value >= 0


def _runtime_reasons(report, authored, expected, source):
    reasons = []
    if (not isinstance(report, dict) or report.get('enabled') is not True
            or report.get('skipped') or report.get('final_status') != 'passed'):
        return ['runtime_not_passed']
    if (not _sha256(source) or report.get('checked_source_sha256') != source
            or report.get('source_sha256') != source):
        reasons.append('runtime_source_mismatch')
    frozen = report.get('authored_checks')
    frozen = frozen if isinstance(frozen, dict) else {}
    if frozen.get('hashes') != authored.get('hashes') or frozen.get('cases') != expected:
        reasons.append('runtime_suite_mismatch')
    rounds = report.get('rounds')
    rounds = rounds if isinstance(rounds, list) else []
    selected = [row for row in rounds if isinstance(row, dict) and row.get('round') == report.get('best_round')]
    if len(selected) != 1:
        return reasons + ['runtime_round_missing']
    row = selected[0]
    if row.get('source_sha256') != source:
        reasons.append('runtime_round_source_mismatch')
    if row.get('status') != 'passed' or row.get('stage') != 'tests':
        reasons.append('runtime_tests_not_passed')
    if (not all(_count(row.get(key)) for key in ('passed', 'failed', 'skipped', 'precondition', 'total'))
            or row.get('passed') != expected or row.get('total') != expected
            or any(row.get(key) != 0 for key in ('failed', 'skipped', 'precondition'))
            or row.get('harness_preconditions')):
        reasons.append('runtime_cases_incomplete')
    signals = row.get('signals')
    signals = signals if isinstance(signals, dict) else {}
    if not set(signals) <= {'authored', 'probes'}:
        reasons.append('unexpected_runtime_suite')
    signal = signals.get('authored')
    signal = signal if isinstance(signal, dict) else {}
    if (signal.get('passed') != expected or type(signal.get('passed')) is not int
            or signal.get('failed') != 0 or signal.get('skipped') != 0
            or signal.get('failures') != []):
        reasons.append('authored_cases_incomplete')
    # Other suites and probes may also expose failures; they cannot be hidden by
    # a passing authored signal or an inconsistent aggregate.
    for name, block in signals.items():
        if (not isinstance(block, dict)
                or not all(_count(block.get(key)) for key in ('passed', 'failed', 'skipped'))
                or block.get('failed') != 0 or (name != 'probes' and block.get('skipped') != 0)
                or block.get('failures') != []):
            reasons.append('runtime_signal_incomplete')
            break
    else:
        if sum(block['passed'] for name, block in signals.items() if name != 'probes') != row.get('passed'):
            reasons.append('runtime_counts_mismatch')
    return reasons


def _label(value):
    return value if isinstance(value, str) and re.fullmatch('[a-z0-9_]{1,80}', value) else None


def _runtime_summary(report, source):
    """Whitelist diagnostics; never export raw harness output or environment."""
    report = report if isinstance(report, dict) else {}
    rounds = report.get('rounds')
    rounds = rounds if isinstance(rounds, list) else []
    selected = next((row for row in rounds if isinstance(row, dict)
                     and row.get('round') == report.get('best_round')), {})
    last = next((row for row in reversed(rounds) if isinstance(row, dict)), {})
    counts = {key: selected.get(key) if _count(selected.get(key)) else None
              for key in ('passed', 'failed', 'skipped', 'precondition', 'total')}
    checked = report.get('checked_source_sha256')
    signals = selected.get('signals')
    signals = signals if isinstance(signals, dict) else {}
    signal_counts = {name: {key: block.get(key) if _count(block.get(key)) else None
                          for key in ('passed', 'failed', 'skipped')}
                     for name, block in signals.items() if name in {'authored', 'probes'} and isinstance(block, dict)}
    return dict(enabled=report.get('enabled') is True, final_status=_label(report.get('final_status')),
        skipped=_label(report.get('skipped')), stopped=_label(report.get('stopped')),
        rounds=len(rounds), selected_round=selected.get('round') if _count(selected.get('round')) else None,
        status=_label(selected.get('status')), stage=_label(selected.get('stage')), counts=counts,
        signals=signal_counts,
        api_type_status='not_checked' if report.get('enabled') is True else 'not_run',
        last_status=_label(last.get('status')), last_stage=_label(last.get('stage')),
        checked_source_sha256=checked if _sha256(checked) else None,
        source_matches=_sha256(source) and checked == source and report.get('source_sha256') == source)


def finalize_self_test(state, *, required_ids, static, runtime_report, authored,
                       source_sha256, missing_coverage, behavior_enabled=True,
                       runtime_enabled=True, authored_suites=(), error=None,
                       requirements_sha256=None, candidate=None, verification_stop=None):
    """Export exact known suites, then accept only complete source-bound tests.

    ``authored`` identifies the single final checked suite. ``authored_suites``
    preserves additional frozen attempts, including failed step diagnostics.
    Initialization errors and changed ownership raise without touching user data.
    Export errors are recorded as a failing acceptance result whenever safe.
    """
    _check_owned(state)
    reasons, exported, frozen_files, frozen_ok = [], [], None, False
    if verification_stop is not None:
        reasons.append(verification_stop['reason'])
    if error:
        reasons.append('generation_error')
    if candidate and candidate.get('status') != 'promoted':
        reasons.append('candidate_not_delivered')
    if not behavior_enabled:
        reasons.append('behavior_checks_disabled')
    if not runtime_enabled:
        reasons.append('runtime_checks_disabled')
    if not isinstance(static, dict) or static.get('passed') is not True or static.get('errors'):
        reasons.append('static_invalid')
    if missing_coverage:
        reasons.append('source_coverage_incomplete')
    required = {ref for ref in required_ids if isinstance(ref, str) and ref}
    if not required or len(required) != len(required_ids):
        reasons.append('required_ids_missing')
    suites = []
    seen = set()
    for suite in [authored, *authored_suites]:
        if suite is None:
            continue
        try:
            key = str(Path(suite['directory']).absolute())
            if key in seen:
                continue
            seen.add(key)
            suites.append(suite)
        except (KeyError, TypeError, ValueError):
            reasons.append('suite_manifest_invalid')
    for index, suite in enumerate(suites, 1):
        item = dict(directory=f'suite-{index}', integrity=False, exported=False)
        exported.append(item)
        try:
            files, integrity, observed = _frozen_files(suite)
            if not integrity:
                reasons.append('frozen_suite_changed')
            item.update(integrity=integrity, frozen_hashes=suite['hashes'],
                exported_hashes=observed)
            destination = state.directory / item['directory']
            _check_owned(state)
            destination.mkdir(exist_ok=False)
            for name, data in files.items():
                with (destination / name).open('xb') as stream:
                    stream.write(data)
            item['exported'] = True
            if suite is authored:
                frozen_files, frozen_ok = files, integrity
        except (OSError, ValueError, TypeError, KeyError):
            reasons.append('suite_export_failed')
    plan = authored.get('plan') if isinstance(authored, dict) else None
    cases = plan.get('cases') if isinstance(plan, dict) else None
    expected = len(cases) if isinstance(cases, list) else 0
    if not expected or not frozen_files or not frozen_ok:
        reasons.append('authored_suite_missing_or_changed')
    else:
        try:
            plan = json.loads(frozen_files['plan.json'])
            if plan != authored['plan'] or authored.get('cases') != expected:
                reasons.append('authored_plan_mismatch')
            if not any(name.endswith(('.spec.cjs', '.spec.js', '.spec.ts')) and data.strip()
                       for name, data in frozen_files.items()):
                reasons.append('authored_tests_empty')
            bound = {case['requirement_id'] for case in cases}
            if bound != required:
                reasons.append('authored_requirements_incomplete')
        except (ValueError, KeyError, TypeError):
            reasons.append('authored_plan_invalid')
        reasons.extend(_runtime_reasons(runtime_report, authored, expected, source_sha256))
    report = dict(version=1, scope='public_requirements_local_self_tests',
        status='incomplete' if reasons else 'passed', accepted=not reasons,
        reasons=list(dict.fromkeys(reasons)), required_ids=sorted(required),
        source_sha256=source_sha256 if _sha256(source_sha256) else None,
        requirements_sha256=requirements_sha256 if _sha256(requirements_sha256) else None,
        expected_cases=expected, runtime=_runtime_summary(runtime_report, source_sha256), suites=exported)
    if candidate:
        report['candidate'] = candidate
    if verification_stop is not None:
        report['generation_stop'] = dict(verification_stop, exported_source_sha256=report['source_sha256'])
    _check_owned(state)
    content = _encoded(report)
    descriptor, temporary = tempfile.mkstemp(prefix='.report-', dir=state.directory)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
        os.replace(temporary, state.directory / 'report.json')
        state.report_bytes = content
    finally:
        Path(temporary).unlink(missing_ok=True)
    return report

"""Controller-owned coverage claims, committed under the project snapshot limits."""
import json
from pathlib import Path

from . import sandbox_files
from .platform_execution import ExecutionStopped

LEDGER_PATH = 'docs/coverage.json'


class CoverageStateError(ExecutionStopped):
    """A controller failure; never a request to repair application code."""
    def __init__(self, reason, detail):
        self.detail = detail
        super().__init__(reason)


def _validate(document):
    if not isinstance(document, dict) or not isinstance(document.get('items'), list):
        raise CoverageStateError('coverage_state_invalid', 'Coverage ledger must contain an items list')
    seen = set()
    for row in document['items']:
        if (not isinstance(row, dict) or not isinstance(row.get('kind'), str)
                or row['kind'] not in {'work_item', 'ui_element', 'scenario'}
                or not isinstance(row.get('ref'), str)
                or not isinstance(row.get('status'), str) or row['status'] not in {'covered', 'partial', 'missing'}
                or not isinstance(row.get('files'), list)
                or any(not isinstance(name, str) for name in row['files'])):
            raise CoverageStateError('coverage_state_invalid', 'Invalid coverage ledger row')
        key = (row['kind'], row['ref'])
        if key in seen:
            raise CoverageStateError('coverage_state_invalid', 'Duplicate coverage ledger reference')
        seen.add(key)


def _decode(raw):
    try:
        document = json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise CoverageStateError('coverage_state_invalid', 'Coverage ledger is not valid UTF-8 JSON') from error
    _validate(document)
    return document


def read_coverage(project):
    try:
        raw = (Path(project) / LEDGER_PATH).read_bytes()
    except OSError as error:
        raise CoverageStateError('coverage_storage_failed', 'Unable to read coverage ledger') from error
    return _decode(raw)


def write_coverage(project, document, *, before=None):
    """Initialize or replace claims atomically; model file-output limits do not apply."""
    _validate(document)
    raw = (json.dumps(document, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    try:
        if before is None:
            before = sandbox_files.snapshot_project(Path(project))
        if LEDGER_PATH in before:
            _decode(before[LEDGER_PATH])  # Never reset a damaged ledger as empty state.
        after = dict(before, **{LEDGER_PATH: raw})
        # Count the final serialized bytes, replacing rather than adding the old ledger.
        if (len(raw) > sandbox_files.MAX_FILE_BYTES
                or len(after) > sandbox_files.MAX_FILES
                or sum(map(len, after.values())) > sandbox_files.MAX_TOTAL_BYTES):
            raise CoverageStateError('coverage_capacity_exceeded', 'Coverage commit exceeds project snapshot capacity')
        sandbox_files.apply_snapshot(Path(project), after, before=before)
    except sandbox_files.UnsafeSandboxFile as error:
        raise CoverageStateError('coverage_state_invalid', str(error)) from error
    except OSError as error:
        raise CoverageStateError('coverage_storage_failed', 'Unable to commit coverage ledger') from error
    return Path(project) / LEDGER_PATH

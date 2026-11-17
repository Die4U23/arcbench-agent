"""Explicit live-process pause at a quiescent request boundary, never stack replay."""
from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from .platform_execution import ExecutionStopped, source_digest
from .sandbox_files import snapshot_project


def encoded(value):
    def convert(item):
        if is_dataclass(item):
            return asdict(item)
        if isinstance(item, (set, frozenset)):
            return sorted(item)
        raise TypeError(type(item).__name__)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=convert).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def _contains_secret(value, secret):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str) and secret in item:
            return True
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _plain(path):
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('checkpoint links are forbidden')


def _exclusive(path, raw):
    _plain(path)
    with path.open('xb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def inspect_checkpoint(path, expected_sha256):
    path = Path(path)
    _plain(path.parent)
    _plain(path)
    raw = path.read_bytes()
    if digest(raw) != expected_sha256:
        raise ValueError('checkpoint digest differs')
    value = json.loads(raw)
    if value['version'] != 1 or value['execution_resume'] != 'same_live_process_only':
        raise ValueError('unsupported checkpoint')
    if time.time() >= value['deadline_utc']:
        raise ValueError('checkpoint expired')
    source = {name: base64.b64decode(data, validate=True) for name, data in value['source'].items()}
    if source_digest(source) != value['source_sha256']:
        raise ValueError('checkpoint source differs')
    return value


def request_continue(path, expected_sha256):
    path = Path(path)
    value = inspect_checkpoint(path, expected_sha256)
    if (path.parent/'resumed.json').exists():
        raise ValueError('checkpoint already consumed')
    _exclusive(path.parent/'continue.json', encoded(dict(
        checkpoint_sha256=expected_sha256, owner=value['owner'])))


class LivePause:
    """The live owner retains its stack, ledger and lease throughout the pause."""
    def __init__(self, root, project, lease, identity, events, before_request):
        if type(before_request) is not int or before_request < 1:
            raise ValueError('pause request must be a positive logical request index')
        self.root, self.project = Path(root), Path(project)
        self.lease, self.identity, self.events = lease, identity, events
        self.before_request, self.consumed = before_request, False

    @staticmethod
    def _control(control):
        return {k: v for k, v in vars(control).items() if k not in {'clock', 'emit'}}

    def boundary(self, model, *, messages, tools, stage, max_tokens,
                 deadline=None, deadline_reason='self_test_time_reserve'):
        budget, control = model.campaign_budget, model.execution_control
        if self.consumed or budget.state['logical_requests'] + 1 != self.before_request:
            return
        budget.check_time(stage)
        control.permit('request')
        if budget.state['pending']:
            raise ValueError('pause requires no in-flight request')
        if self.lease is None:
            raise ValueError('live pause requires the original exclusive project lease')
        _plain(self.lease.path)
        if self.lease.path.read_text() != self.lease.identity:
            raise ValueError('project lease differs')
        _plain(self.root)
        self.root.mkdir(parents=True, exist_ok=False)
        source = control.source(self.project)
        identity = self.identity()
        deadline = min(control.deadline, model.request_deadline,
                       deadline if deadline is not None else control.deadline)
        if control.clock() >= deadline:
            control.stop(deadline_reason)
        owner = dict(pid=os.getpid(), lease=self.lease.identity, pause=uuid4().hex)
        value = dict(version=1, execution_resume='same_live_process_only', owner=owner,
            identity=identity, source_sha256=source_digest(source),
            source={name: base64.b64encode(data).decode('ascii') for name, data in source.items()},
            control=self._control(control), ledger=budget.state,
            ledger_sha256=digest(budget.path.read_bytes()),
            request=dict(messages=messages, tools=tools, stage=stage, max_tokens=max_tokens),
            deadline_monotonic=deadline, deadline_utc=time.time()+deadline-control.clock())
        raw = encoded(value)
        structured = json.loads(raw)
        for name in ('OPENAI_API_KEY', 'API_KEY', 'ARCBENCH_API_KEY'):
            secret = os.environ.get(name, '')
            if secret and (_contains_secret(structured, secret)
                           or any(secret.encode() in data for data in source.values())):
                raise ValueError('credential detected in checkpoint')
        path = self.root/'checkpoint.json'
        _exclusive(self.root/'checkpoint.tmp', raw)
        os.replace(self.root/'checkpoint.tmp', path)
        expected = digest(raw)
        self.events.mark_run_paused('Live owner waiting; checkpoint SHA256 '+expected)
        model._append_log(dict(event='execution_paused', checkpoint=str(path), sha256=expected,
                               execution_resume='same_live_process_only'))
        request = self.root/'continue.json'
        while not request.exists():
            control.check()
            remaining = deadline-control.clock()
            if remaining <= 0:
                control.stop(deadline_reason)
            time.sleep(min(0.1, remaining))
        control.permit('request')
        if control.clock() >= deadline:
            control.stop(deadline_reason)
        inspect_checkpoint(path, expected)
        _plain(request)
        _plain(self.lease.path)
        if json.loads(request.read_text()) != dict(checkpoint_sha256=expected, owner=owner):
            raise ValueError('continue request differs')
        saved = json.loads(raw)
        if (encoded(self.identity()) != encoded(saved['identity'])
                or encoded(dict(messages=messages, tools=tools, stage=stage, max_tokens=max_tokens)) != encoded(saved['request'])
                or encoded(self._control(control)) != encoded(saved['control'])
                or encoded(budget.state) != encoded(saved['ledger'])
                or digest(budget.path.read_bytes()) != saved['ledger_sha256']
                or self.lease.path.read_text() != self.lease.identity
                or source_digest(control.source(self.project)) != saved['source_sha256']):
            raise ValueError('paused execution identity or state differs')
        _exclusive(self.root/'resumed.json', encoded(dict(checkpoint_sha256=expected, owner=owner)))
        self.consumed = True
        self.events.mark_run_resumed('Verified live owner continued at the original request boundary')
        model._append_log(dict(event='execution_resumed', checkpoint_sha256=expected))


class ApiErrorPause:
    """Supervised wait after recovery admission; never creates retry capacity."""
    def __init__(self, root, project, lease, identity, events):
        self.root, self.project, self.lease = Path(root), project, lease
        self.identity, self.events = identity, events

    def boundary(self, model, **request):
        budget, control = model.campaign_budget, model.execution_control
        remaining = budget.settings.api_wait_seconds - budget.state['api_wait_seconds']
        if remaining <= 0:
            control.stop('request_interrupted_usage_unknown')
        started = control.clock()
        pause = LivePause(self.root/str(budget.state['api_retries']), self.project, self.lease,
                          self.identity, self.events, budget.state['logical_requests'] + 1)
        try:
            pause.boundary(model, **request, deadline=started + remaining,
                           deadline_reason='request_interrupted_usage_unknown')
        finally:
            allowed = budget.finish_api_wait(0, control.clock() - started)
        if not allowed:
            control.stop('request_interrupted_usage_unknown')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inspect', 'continue', 'resume-process'))
    parser.add_argument('checkpoint', type=Path)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args()
    inspect_checkpoint(args.checkpoint, args.sha256)
    if args.action == 'resume-process':
        raise SystemExit('Cross-process execution resume is unsupported; ledger and stale lease remain closed')
    if args.action == 'continue':
        request_continue(args.checkpoint, args.sha256)
    print(json.dumps(dict(status='continue_requested' if args.action == 'continue' else 'verified',
                          execution_resume='same_live_process_only')))


if __name__ == '__main__':
    main()

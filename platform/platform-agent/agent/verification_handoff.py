"""One clean same-host transfer of frozen final verification, never generation replay.

The first owner publishes readiness only AFTER releasing its ordinary lease.
Crashes before readiness leave no resumable handoff. A consumed handoff is never
reusable, including when its new owner crashes or fails verification.
"""
from __future__ import annotations

import base64
from collections import Counter
import ctypes
from dataclasses import asdict
import json
import os
from pathlib import Path
import platform
import time
from types import SimpleNamespace

from .execution_checkpoint import LivePause, _contains_secret, _exclusive, _plain, digest, encoded
from .platform_execution import ControlPolicy, ExecutionLimits, RunControl, source_digest
from .sandbox_files import snapshot_project


class VerificationHandoff(Exception):
    def __init__(self, path, sha256):
        self.path, self.sha256 = Path(path), sha256
        super().__init__('Frozen final verification awaits a new process')


def boot_identity():
    """Use the OS boot marker, not a rounded wall-clock-minus-uptime guess."""
    if os.name == 'nt':
        class TimeOfDay(ctypes.Structure):
            _fields_ = [('boot',ctypes.c_longlong),('current',ctypes.c_longlong),
                        ('bias',ctypes.c_longlong),('zone',ctypes.c_ulong),
                        ('reserved',ctypes.c_ulong),('boot_bias',ctypes.c_ulonglong),
                        ('sleep_bias',ctypes.c_ulonglong)]
        info = TimeOfDay()
        status = ctypes.windll.ntdll.NtQuerySystemInformation(3,ctypes.byref(info),ctypes.sizeof(info),None)
        if status != 0:
            raise ValueError('OS boot identity unavailable')
        marker = str(info.boot)
    else:
        marker = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    return digest(encoded([platform.node(), marker]))


def remaining_deadline(saved):
    now, utc = time.monotonic(), time.time()
    if (saved['boot'] != boot_identity() or now < saved['monotonic'] or utc < saved['utc']):
        raise ValueError('handoff host, boot or clock changed')
    deadline = min(saved['deadline'], now + saved['deadline_utc'] - utc)
    if now >= deadline:
        raise ValueError('handoff original deadline expired')
    return deadline


def configuration(settings):
    endpoint = os.environ.get('OPENAI_BASE_URL','').strip()
    if endpoint and '://' not in endpoint:
        endpoint = 'https://' + endpoint.lstrip('/')
    return digest(encoded(dict(settings=asdict(settings), model=os.environ.get('MODEL','').strip(),
                               endpoint_sha256=digest(endpoint.encode()),
                               transport=os.environ.get('ARCBENCH_MODEL_TRANSPORT',''))))


def code_identity():
    root = Path(__file__).resolve().parents[1]
    return file_identity(root, installed_package=True)


def runtime_paths(paths):
    for path in vars(paths).values():
        _plain(path)
    return {key:str(path.resolve()) for key,path in vars(paths).items()}


def file_identity(directory, *, installed_package=False):
    root = Path(directory)
    _plain(root)
    result = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if installed_package and ('__pycache__' in relative.parts or path.suffix in {'.pyc','.pyo'}
                or relative.as_posix().startswith(('arcbench-agent-runtime/build/',
                    'arcbench-agent-runtime/src/arcbench_agent_runtime.egg-info/'))):
            continue
        _plain(path)
        if path.is_file():
            if path.stat().st_nlink != 1:
                raise ValueError('handoff hard links are forbidden')
            result[relative.as_posix()] = digest(path.read_bytes())
    return result


def prepare(args, evidence, *, settings, model, task, ir, deadline, reports, result):
    from . import behavior_checks
    from .platform_self_test import _check_owned
    app = Path(args.output_dir).resolve()
    control, budget = model.execution_control, model.campaign_budget
    lease = getattr(args,'project_lease',None)
    if (lease is None or lease.path.read_text() != lease.identity or budget.state['pending']
            or budget.state['stopped'] or control.stopped
            or control.control_state not in {'RUNNING','WARNING','PROBATION'}
            or evidence.candidate is not None or result.get('status') != 'complete'
            or not evidence.static.get('passed') or evidence.missing
            or not evidence.runtime_enabled or not evidence.behavior_enabled
            or not evidence.runtime_report.get('suite_complete') or not evidence.authored):
        raise ValueError('clean complete frozen verification boundary required')
    control.check()
    source = control.source(app)
    suites = list(evidence.suites)
    if evidence.step_gate is not None:
        suites.extend(evidence.step_gate.collections.values())
    def frozen(suite):
        if not behavior_checks.verify_frozen(suite['directory'],suite['hashes']):
            raise ValueError('frozen suite changed before handoff')
        return dict(suite,directory=str(Path(suite['directory']).resolve()))
    _check_owned(evidence.artifacts)
    now, utc = time.monotonic(), time.time()
    deadline = min(deadline,control.deadline)
    clock = dict(boot=boot_identity(),monotonic=now,utc=utc,deadline=deadline,
                 deadline_utc=utc+deadline-now)
    remaining_deadline(clock)
    value = dict(version=1, execution_resume='frozen_final_verification_only',
        project=str(app), requirements=str(Path(args.requirements).resolve()), task=task,
        agent_code=code_identity(), configuration=configuration(settings), clock=clock,
        runtime_paths=runtime_paths(evidence.runtime.paths),
        previous_owner=dict(pid=os.getpid(),lease=lease.identity), source_sha256=source_digest(source),
        control=LivePause._control(control), ledger=budget.state, ledger_path=str(budget.path.resolve()),
        closeout_start=evidence.closeout_start,
        ledger_sha256=digest(budget.path.read_bytes()), session_tools=model.session.tool_calls_used,
        continuation=dict(stage='final_verification',pending_model_request=None,generation_result=result),
        ir=ir, required_ids=list(evidence.required_ids), requirements_sha256=evidence.requirements_sha256,
        static=evidence.static, missing=evidence.missing, runtime_report=evidence.runtime_report,
        authored=frozen(evidence.authored), suites=[frozen(s) for s in suites],
        artifacts=dict(directory=str(evidence.artifacts.directory),identity=evidence.artifacts.identity,
            report=base64.b64encode(evidence.artifacts.report_bytes).decode('ascii'),
            files=file_identity(evidence.artifacts.directory)))
    raw = encoded(value)
    for name in ('OPENAI_API_KEY','API_KEY','ARCBENCH_API_KEY'):
        secret = os.environ.get(name,'')
        if secret and (_contains_secret(json.loads(raw),secret) or any(secret.encode() in b for b in source.values())):
            raise ValueError('credential detected in handoff')
    root = reports/'final-verification-handoff'
    _plain(root)
    root.mkdir(exist_ok=False)
    path = root/'checkpoint.json'
    _exclusive(path,raw)
    evidence.runtime.events.mark_run_paused('Frozen final verification prepared; clean lease release required')
    raise VerificationHandoff(path,digest(raw))


def publish_ready(handoff):
    """Called only by run_owned after its lease context exited successfully."""
    raw = handoff.path.read_bytes()
    if digest(raw) != handoff.sha256:
        raise ValueError('handoff changed before clean release')
    _exclusive(handoff.path.parent/'ready.json',encoded(dict(checkpoint_sha256=handoff.sha256,
        previous_owner=json.loads(raw)['previous_owner'])))


def inspect(args, settings):
    from arcbench_agent_runtime.context import RuntimePaths
    from . import behavior_checks
    from .platform_inputs import bundle_kwargs, resolve_task_inputs
    from .task_bundle import load_task_bundle
    path = Path(args.resume_final_verification).resolve()
    _plain(Path(args.resume_final_verification).absolute())
    raw = path.read_bytes()
    if not args.checkpoint_sha256 or digest(raw) != args.checkpoint_sha256:
        raise ValueError('handoff digest differs')
    value = json.loads(raw)
    if value['version'] != 1 or value['execution_resume'] != 'frozen_final_verification_only':
        raise ValueError('unsupported handoff boundary')
    app = Path(args.output_dir).resolve()
    _plain(app)
    if (str(app) != value['project'] or str(Path(args.requirements).resolve()) != value['requirements']
            or value['configuration'] != configuration(settings) or value['agent_code'] != code_identity()
            or runtime_paths(RuntimePaths.from_env(project_dir=str(app))) != value['runtime_paths']):
        raise ValueError('handoff project, requirements, configuration or code differs')
    deadline = remaining_deadline(value['clock'])
    _plain(path.parent/'ready.json')
    if json.loads((path.parent/'ready.json').read_text()) != dict(checkpoint_sha256=args.checkpoint_sha256,
                                                               previous_owner=value['previous_owner']):
        raise ValueError('clean handoff readiness differs')
    if (path.parent/'claimed.json').exists():
        raise ValueError('handoff already consumed')
    inputs = resolve_task_inputs(Path(args.requirements).resolve(),include_tests=False)
    bundle = load_task_bundle(**bundle_kwargs(inputs),include_tests=False)
    if (bundle.source_hash != value['requirements_sha256']
            or list(bundle.index.atomic_ids) != value['required_ids']
            or source_digest(snapshot_project(app)) != value['source_sha256']):
        raise ValueError('handoff source or public requirements differs')
    ledger_path = Path(value['ledger_path'])
    _plain(ledger_path)
    if (digest(ledger_path.read_bytes()) != value['ledger_sha256']
            or json.loads(ledger_path.read_text()) != value['ledger']
            or value['ledger']['pending'] or value['ledger']['stopped']):
        raise ValueError('handoff ledger differs or has pending usage')
    for suite in [value['authored'],*value['suites']]:
        suite['directory'] = Path(suite['directory'])
        _plain(suite['directory'])
        if not behavior_checks.verify_frozen(suite['directory'],suite['hashes']):
            raise ValueError('handoff frozen suite differs')
    if file_identity(value['artifacts']['directory']) != value['artifacts']['files']:
        raise ValueError('handoff self-test artifacts differ')
    from .platform_self_test import SelfTestArtifacts, _check_owned
    artifact = value['artifacts']
    _check_owned(SelfTestArtifacts(Path(artifact['directory']),tuple(artifact['identity']),
                                  base64.b64decode(artifact['report'],validate=True)))
    return path,value,deadline


def resume(args):
    """Caller owns a NEW ordinary lease. No ModelClient or generate invocation."""
    from arcbench_agent_runtime import AgentRuntime
    from . import platform_generation as generation
    from .platform_self_test import SelfTestArtifacts, _check_owned
    from .platform_settings import PlatformSettings
    from .platform_static import missing_coverage, validate_source
    settings = PlatformSettings.from_env()
    path,value,deadline = inspect(args,settings)
    lease = getattr(args,'project_lease',None)
    if lease is None:
        raise ValueError('resume requires an exclusive project lease')
    _plain(lease.path)
    if lease.path.read_text() != lease.identity:
        raise ValueError('resume project lease changed')
    # Exclusive and permanent: failures/crashes do not authorize a second run.
    _exclusive(path.parent/'claimed.json',encoded(dict(checkpoint_sha256=args.checkpoint_sha256,
        pid=os.getpid(),lease=args.project_lease.identity)))
    control = RunControl()
    saved = value['control']
    for key,item in saved.items():
        if key == 'limits': item = ExecutionLimits(**item)
        elif key == 'policy': item = ControlPolicy(**item)
        elif key in {'actions','states'}: item = Counter(item)
        elif key in {'_passed_requirements','_failed_requirements'}: item = set(item)
        setattr(control,key,item)
    control.deadline = min(control.deadline,deadline)
    # Reopen only diagnostic accounting. This object is never exposed to a model.
    budget = object.__new__(generation.PlatformTokenBudget)
    budget.path,budget.state,budget.settings = Path(value['ledger_path']),value['ledger'],settings
    budget.live_allowed = False
    report_dir = budget.path.parent
    control.emit = lambda row:generation.append_jsonl(report_dir/'context.jsonl',row)
    model = SimpleNamespace(execution_control=control,campaign_budget=budget,
        session=SimpleNamespace(tool_calls_used=value['session_tools']),max_tool_calls=settings.max_tool_calls,
        _append_log=control.emit)
    model.reserve_validation_steps = lambda count,**kwargs: generation.PlatformGenerationModel.reserve_validation_steps(model,count,**kwargs)
    artifacts = value['artifacts']
    state = SelfTestArtifacts(Path(artifacts['directory']),tuple(artifacts['identity']),base64.b64decode(artifacts['report'],validate=True))
    _check_owned(state)
    app = Path(args.output_dir).resolve()
    evidence = generation.GenerationEvidence(artifacts=state,required_ids=tuple(value['required_ids']),
        requirements_sha256=value['requirements_sha256'],static=value['static'],missing=value['missing'],
        authored=value['authored'],suites=value['suites'],runtime_report=value['runtime_report'],
        runtime=AgentRuntime.from_env(**value['runtime_paths']),control=control,source_ready=True,
        behavior_enabled=True,runtime_enabled=True,closeout_start=value.get('closeout_start'))
    error = None
    try:
        control.check()
        evidence.runtime.events.mark_run_resumed('New process continues frozen final verification only')
        def forbidden(*args,**kwargs):
            raise RuntimeError('final-verification handoff prohibits generation, repair and source restoration')
        tools = SimpleNamespace(source_revision=0,unseal=forbidden)
        generation.run_runtime_loop(app,None,settings,model=model,tools=tools,make_packet=forbidden,
            record=forbidden,validate=lambda:validate_source(app,include_test_evidence=False),task_id=value['task'],
            report_path=report_dir/'runtime-check.json',ir=value['ir'],model_calls_allowed=lambda:False,
            authored_checks=value['authored'],report=evidence.runtime_report,runtime_deadline=control.deadline)
        if source_digest(control.source(app)) != value['source_sha256']:
            raise ValueError('frozen source changed during final verification')
        evidence.static = validate_source(app,include_test_evidence=False)
        evidence.missing = missing_coverage(app,value['ir'])
        control.check()
    except Exception as caught:
        error = f'{type(caught).__name__}: {str(caught)[:500]}'
    code = None
    try:
        result = generation.finish_generation(evidence,app,error)
        _exclusive(path.parent/'finished.json',encoded(dict(exit_code=result,
            control=LivePause._control(control),ledger=budget.state,execution_resume=value['execution_resume'])))
        code = result
        return code
    finally:
        evidence.record_closeout(code)

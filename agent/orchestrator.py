from __future__ import annotations

import html
import hashlib
import json
import logging
from dataclasses import asdict
import os
import re
import time
import traceback
from pathlib import Path
from typing import Any

from arcbench_agent_runtime import AgentRuntime

from .sandbox_process import SandboxInfrastructureError

from .config import AgentConfig
from .candidate_checkpoint import CandidateCheckpoint, PUBLIC_CHECK, public_checks_ready
from .browser_contract import BROWSER_CHECK
from .command_output import failure_details
from .native_verification import verify_public_acceptance
from .llm import BudgetExhaustion, ModelBudgetExceeded, ModelClient, RequirementReviewError
from .model_transport import ModelRequestInterrupted
from .request_metrics import observe_operation, emit as emit_metric
from .requirements import RequirementModule, load_requirement_tree, root_modules, walk_requirement_ids
from .requirement_progress import mark_subtree_progress, record_local_verification
from .repair_policy import failed_behavior_ids, focused_subtree, repair_guidance
from .tools import ProjectTools
from .verify import (
    CheckResult,
    VerificationResult,
    prepare_web_template_structure,
    verify_demo_page,
    verify_project,
    verify_web_template_structure,
)
from .visual_acceptance import collect_visual_references, verify_visual_acceptance


LOGGER = logging.getLogger("arcbench_agent")
REPAIR_REQUEST_ALLOWANCE = 12
CONTINUABLE_REPAIR_STOPS = {"allocation", "model_finished", "checkpoint_failed", "truncated"}
DELIVERY_BUDGETS = {"total_token_budget", "model_request_budget",
                    "delivery_token_reserve", "delivery_request_reserve"}


def _integration_allowances(model) -> tuple[int | None, int | None]:
    """Leave half the remaining finite budget for evidence-led repairs/audits."""
    token_cap = getattr(model, 'max_total_tokens', None)
    request_cap = getattr(model, 'max_model_requests', None)
    prompt = getattr(model, 'prompt_tokens_used', None)
    completion = getattr(model, 'completion_tokens_used', None)
    requests = getattr(model, 'model_requests_used', None)
    tokens = (max(0, token_cap - prompt - completion) // 2
              if all(isinstance(value, int) for value in (token_cap, prompt, completion)) else None)
    calls = (max(0, request_cap - requests) // 2
             if all(isinstance(value, int) for value in (request_cap, requests)) else None)
    return tokens, calls


def _finalize_delivery(output_dir: Path, result: VerificationResult,
                       budget: BudgetExhaustion | None, *,
                       interruption: ModelRequestInterrupted | None = None,
                       generation_stop_reason: str | None = None,
                       earlier_revision_reviews: tuple[CheckResult, ...] = ()) -> VerificationResult:
    """A runnable candidate can reach the evaluator without claiming test success."""
    checks = result.checks
    ready = False
    budget_delivery = budget is not None and budget.budget in DELIVERY_BUDGETS
    bounded_stop = generation_stop_reason == "bounded_repair_exhausted"
    stopped_generation = budget_delivery or interruption is not None or bounded_stop
    audit_unavailable = any(check.name == "requirement audit validity" and not check.passed for check in checks)
    if stopped_generation or audit_unavailable:
        by_name = {check.name: check for check in checks}
        required = ("frontend: npm run build", "web template structure")
        # Only behavior/test failures are eligible for evaluator handoff.
        blocking = [check for check in checks if not check.passed
                    and not (audit_unavailable and check.name == "requirement audit validity")
                    and not (stopped_generation and (check.name.endswith(": npm run test")
                             or check.name in {"requirement coverage", "agent generation budget",
                                 "agent model request", "agent generation stop", PUBLIC_CHECK, BROWSER_CHECK}))]
        if not blocking and all(name in by_name and by_name[name].passed for name in required):
            startup = verify_public_acceptance(output_dir, {}, startup_only=True)
            checks = (*checks, *startup)
            ready = bool(startup) and all(check.passed for check in startup)
    final = VerificationResult(result.passed, checks, ready_for_evaluation=ready)
    report_dir = output_dir / ".arc"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "delivery-report.json").write_text(json.dumps({
        "local_verification_passed": final.passed,
        "ready_for_evaluation": final.passed or ready,
        "formal_acceptance": "pending",
        "handoff_reason": ("model request interrupted" if interruption is not None else
            "bounded repair slices exhausted" if bounded_stop else
            "requirement audit unavailable" if audit_unavailable else "budget finalization") if ready else None,
        "generation_stop_reason": generation_stop_reason,
        "budget": asdict(budget) if budget is not None else None,
        "model_request_interruption": interruption.report() if interruption is not None else None,
        "earlier_revision_reviews": [asdict(check) for check in earlier_revision_reviews],
        "checks": [asdict(check) for check in final.checks],
        "note": "Candidate delivery is separate from acceptance. Unrun audits remain unverified.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return final


def _demo_page(module: RequirementModule) -> tuple[str, str]:
    heading = module.name or module.node_id
    description = str(module.subtree.get("description") or "This page was generated by the offline ARC-Bench project demo.")
    safe_heading = html.escape(heading)
    safe_description = html.escape(description)
    page = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{safe_heading}</title>
    <style>body{{font:16px/1.6 system-ui,sans-serif;max-width:48rem;margin:10vh auto;padding:0 1.25rem;color:#172033}}main{{border:1px solid #dbe2ea;border-radius:1rem;padding:2rem}}h1{{margin-top:0}}.tag{{color:#53647a}}</style>
  </head>
  <body>
    <main>
      <p class="tag">ARC-Bench offline project demo</p>
      <h1>{safe_heading}</h1>
      <p>{safe_description}</p>
    </main>
  </body>
</html>
"""
    return page, heading


def _register_traceability(runtime: AgentRuntime, tree: dict[str, Any]) -> None:
    runtime.traceability.init_db()
    runtime.traceability.store_requirement_tree(tree)


def _record_test(runtime: AgentRuntime, node_id: str, passed: bool | None) -> None:
    test_id = f"TEST-{node_id}"
    runtime.traceability.upsert_test(
        test_id=test_id,
        req_id=node_id,
        type="INTEGRATION",
        passed=passed,
    )
    runtime.traceability.set_test_pass_status(test_id, passed)


def _requirement_test_status(result: VerificationResult, node_id: str) -> bool | None:
    """Global infrastructure/budget failures do not identify failed requirements."""
    if any(not check.passed and node_id in check.requirement_ids for check in result.checks):
        return False
    return True if result.passed else None


def _record_verification(runtime: AgentRuntime, tree: dict[str, Any], result: VerificationResult) -> None:
    record_local_verification(runtime, tree, result)
    _register_traceability(runtime, tree)


def _raise_if_model_interrupted(model: ModelClient) -> None:
    stopped = getattr(model, "last_request_interruption", None)
    if isinstance(stopped, ModelRequestInterrupted):
        raise stopped


def _verify_local_candidate(config: AgentConfig, tree: dict[str, Any], candidate: CandidateCheckpoint,
                            project_tools: ProjectTools, *, include_root: bool = True,
                            final: bool = False, allow_restore: bool = True) -> VerificationResult:
    def checks() -> tuple[CheckResult, ...]:
        project = verify_project(config.output_dir, include_root=include_root)
        structure = verify_web_template_structure(config.output_dir)
        public = verify_public_acceptance(config.output_dir, tree) if public_checks_ready(
            project.checks, structure.passed) else ()
        return (*project.checks, *structure.checks, *public)

    current = checks()
    note = candidate.consider(current, final=final, allow_restore=allow_restore,
                              require_comparable=allow_restore)
    if note:
        LOGGER.warning("%s", note)
        project_tools.source_revision = getattr(project_tools, "source_revision", 0) + 1
        try:
            project_tools.ensure_dependencies()
        except SandboxInfrastructureError:
            raise
        except Exception as exc:
            return VerificationResult(False, (
                CheckResult("project dependencies", False, 1, str(exc)),
                CheckResult("candidate regression prevention", True, 0, note)))
        current = (*checks(), CheckResult("candidate regression prevention", True, 0, note))
    return VerificationResult(all(check.passed for check in current), current)


def _prepare_local_candidate(config: AgentConfig, tree: dict[str, Any], candidate: CandidateCheckpoint,
                             project_tools: ProjectTools, *, include_root: bool = True,
                             final: bool = False, allow_restore: bool = True) -> VerificationResult:
    restore_note = ""
    for attempt in range(2):
        try:
            project_tools.ensure_dependencies()
        except SandboxInfrastructureError:
            raise
        except Exception as exc:
            local = VerificationResult(False, (CheckResult("project dependencies", False, 1, str(exc)),))
            if attempt == 0 and allow_restore:
                restore_note = candidate.consider(local.checks, final=final)
                if restore_note:
                    LOGGER.warning("%s", restore_note)
                    project_tools.source_revision = getattr(project_tools, "source_revision", 0) + 1
                    continue
            break
        else:
            local = _verify_local_candidate(config, tree, candidate, project_tools,
                include_root=include_root, final=final, allow_restore=allow_restore)
            break
    if restore_note:
        local = VerificationResult(local.passed, (*local.checks,
            CheckResult("candidate regression prevention", True, 0, restore_note)))
    return local


def _source_fingerprint(project: Path) -> str:
    """Compare production file bytes, excluding self-tests and mutable run data."""
    excluded = {"node_modules", "dist", "build", "coverage", "requirements", "data",
                "storage", "uploads", "logs", "tests", "test", "__tests__", "__mocks__",
                "venv"}
    suffixes = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".css", ".html",
                ".svg", ".png", ".jpg", ".jpeg", ".webp"}
    root = project.resolve()
    digest = hashlib.sha256()
    for current, directories, names in os.walk(root):
        base = Path(current)
        directories[:] = sorted(name for name in directories
            if not name.startswith(".") and name.casefold() not in excluded
            and (base / name).resolve() == base / name)
        for name in sorted(names):
            path = base / name
            if name.startswith(".") or path.resolve() != path:
                continue
            if re.search(r"\.(?:test|spec)\.[^.]+$", name, re.IGNORECASE):
                continue
            manifest = name in {"package.json", "tsconfig.json", "jsconfig.json"}
            if path.suffix.casefold() not in suffixes and not manifest:
                continue
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _boundary_input_fingerprint(project: Path) -> str | None:
    """Observe local check inputs without reading secrets or hashing dependencies."""
    digest = hashlib.sha256()
    content_suffixes = {'.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.py',
                        '.css', '.html', '.sh', '.ps1', '.yaml', '.yml'}
    manifests = {'package.json', 'package-lock.json', 'npm-shrinkwrap.json',
                 'tsconfig.json', 'jsconfig.json'}
    root = project.resolve()
    content_bytes = 0
    started = time.monotonic()
    observed_nodes = 0
    def raise_walk_error(error):
        raise error
    try:
        for current, directories, names in os.walk(root, followlinks=False, onerror=raise_walk_error):
            observed_nodes += 1 + len(directories) + len(names)
            if observed_nodes > 50_000 or time.monotonic() - started > 2:
                return None
            base = Path(current)
            directories[:] = sorted(name for name in directories if name != '.git')
            # External directory links can hide changed dependency/test inputs.
            if any((base / name).resolve() != base / name for name in directories):
                return None
            for name in sorted(names):
                if time.monotonic() - started > 2:
                    return None
                path = base / name
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    return None
                relative = path.relative_to(root)
                stat = path.stat()
                digest.update(repr((relative.as_posix(), stat.st_size, stat.st_mtime_ns,
                                    stat.st_ctime_ns, stat.st_ino)).encode())
                # Dependency installs and runtime/secret files are observed by
                # metadata. Hash source and check scripts too, including .arc.
                if ('node_modules' not in relative.parts and not name.startswith('.')
                        and (path.suffix.lower() in content_suffixes or name in manifests)):
                    content_bytes += stat.st_size
                    if content_bytes > 16 * 1024 * 1024:
                        return None
                    digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


class _BoundaryVerification:
    """One-use handoff from a full slice boundary to its immediate verification."""
    def __init__(self):
        self.saved = None

    def clear(self):
        self.saved = None

    def remember(self, project, revision, result):
        self.clear()
        public = next((check for check in result.checks if check.name == PUBLIC_CHECK), None)
        # Incomplete/infrastructure reports must run the normal verification path.
        if public is None or not public.case_statuses:
            return
        stamp = _boundary_input_fingerprint(project)
        if stamp is not None:
            self.saved = (stamp, revision, result)

    def take(self, project, revision, *, final=False):
        saved, self.saved = self.saved, None
        if final or saved is None or saved[1] != revision:
            return None
        if _boundary_input_fingerprint(project) != saved[0]:
            return None
        return saved[2]


def _repair_feedback(result: VerificationResult) -> str:
    failures = [check for check in result.checks if not check.passed]
    passed = [check.name for check in result.checks if check.passed]
    parts = []
    parts.extend(check.output for check in result.checks if check.name == "candidate regression prevention")
    for check in failures:
        output = failure_details(check.output)
        if check.name != PUBLIC_CHECK and len(output) > 4_000:
            output = output[:2_000] + "\n...[middle truncated]\n" + output[-2_000:]
        parts.append(f"{check.name}: FAILED (exit={check.exit_code})\n{output}")
    if passed:
        parts.append("Already passing checks: " + ", ".join(passed))
    return "\n\n".join(parts)


def _repair_modules(
    result: VerificationResult,
    modules: list[RequirementModule],
    module_paths: dict[str, set[str]],
) -> list[RequirementModule]:
    if any(check.name.startswith("project:") and not check.passed for check in result.checks):
        # Root scripts belong to the whole project, even when their output
        # mentions a source path written by one earlier module.
        return modules
    failure_text = "\n".join(
        f"{check.name}\n{failure_details(check.output)}" for check in result.checks if not check.passed
    )
    by_requirement = [
        module for module in modules
        if any(
            re.search(rf"(?<![\w.-]){re.escape(node_id)}(?![\w.-])", failure_text)
            for node_id in walk_requirement_ids(module.subtree)
        )
    ]
    if by_requirement:
        return by_requirement
    normalized = failure_text.replace("\\", "/").casefold()
    by_path = [
        module for module in modules
        if any(path.casefold() in normalized for path in module_paths.get(module.node_id, set()))
    ]
    return by_path or modules


def run_offline_demo(runtime: AgentRuntime, config: AgentConfig, tree: dict[str, Any], modules: list[RequirementModule]) -> VerificationResult:
    module = modules[0]
    node_ids = walk_requirement_ids(module.subtree)
    runtime.events.mark_design_started(module.node_id, "Offline demo planning started")
    runtime.events.mark_design_done(module.node_id, "Deterministic demo plan ready")
    runtime.events.mark_implementation_started(module.node_id, "Generating a static Web page")
    page, heading = _demo_page(module)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    (config.output_dir / "index.html").write_text(page, encoding="utf-8", newline="\n")
    runtime.events.mark_implementation_done(module.node_id, "Generated index.html")
    result = verify_demo_page(config.output_dir, heading)
    for node_id in node_ids:
        if result.passed:
            runtime.events.mark_test_passed(node_id, "Offline page verification passed")
        else:
            runtime.events.mark_test_failed(node_id, result.summary())
        _record_test(runtime, node_id, result.passed)
    _register_traceability(runtime, tree)
    return result


@observe_operation("model_agent")
def run_model_agent(runtime: AgentRuntime, config: AgentConfig, tree: dict[str, Any], modules: list[RequirementModule]) -> VerificationResult:
    if config.task_type != "web":
        raise ValueError(
            f"The initial model-driven implementation currently supports task type 'web', not {config.task_type!r}"
        )
    prepare_web_template_structure(config.output_dir)
    model = ModelClient(max_turns=config.max_model_turns, max_tool_calls=config.max_tool_calls)
    candidate = CandidateCheckpoint(config.output_dir)
    try:
        result = _run_model_agent(runtime, config, tree, modules, model, candidate=candidate)
        _raise_if_model_interrupted(model)
        return result
    except SandboxInfrastructureError as exc:
        # This failure is global, not a requirement-specific repair allocation.
        # Do not run another checkpoint, restore, review or model call here.
        origin = traceback.extract_tb(exc.__traceback__)[-1]
        record = {"failure_kind": "infrastructure", "reason": exc.reason, "scope": "run",
                  "first_location": {"file": Path(origin.filename).name,
                                     "function": origin.name, "line": origin.lineno},
                  "occurrences": 1, "retry_count": 0, "action": "stop_run",
                  "model_requests": getattr(model, "model_requests_used", None),
                  "prompt_tokens": getattr(model, "prompt_tokens_used", None),
                  "completion_tokens": getattr(model, "completion_tokens_used", None)}
        emit_metric("repair_circuit_opened", **record)
        report = config.output_dir / ".arc" / "infrastructure-failure.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        LOGGER.error("Infrastructure failure stops all repair slices: %s", exc.reason)
        result = VerificationResult(False, (CheckResult("execution infrastructure", False, 1, str(exc)),))
        _record_verification(runtime, tree, result)
        return result
    except ModelRequestInterrupted as exc:
        LOGGER.warning("%s; running final local verification without further model calls", exc)
        project_tools = ProjectTools(config.output_dir, timeout_seconds=120)
        local = _prepare_local_candidate(config, tree, candidate, project_tools, final=True)
        result = VerificationResult(False, (*local.checks,
            CheckResult("agent model request", False, 1, str(exc))))
        result = _finalize_delivery(config.output_dir, result, None, interruption=exc)
        _record_verification(runtime, tree, result)
        return result
    finally:
        # Even an unexpected failure must leave a reconcilable usage total.
        log_usage = getattr(model, "log_usage", None)
        if callable(log_usage):
            log_usage()


def _run_model_agent(runtime: AgentRuntime, config: AgentConfig, tree: dict[str, Any],
                     modules: list[RequirementModule], model: ModelClient, *,
                     candidate: CandidateCheckpoint | None = None) -> VerificationResult:
    project_tools = ProjectTools(config.output_dir, timeout_seconds=120)
    changed_paths = getattr(project_tools, "changed_paths", project_tools.written_paths)
    plans: dict[str, str] = {}
    planning_subtrees: dict[str, dict[str, Any]] = {}
    module_paths: dict[str, set[str]] = {}
    budget_failure: BudgetExhaustion | None = None
    budget_exhausted = False
    deferred_modules: set[str] = set()
    visual_references = collect_visual_references(tree)
    candidate = candidate if candidate is not None else CandidateCheckpoint(config.output_dir)
    boundary_verification = _BoundaryVerification()

    def local_verification(*, include_root: bool = True, final: bool = False) -> VerificationResult:
        return _verify_local_candidate(config, tree, candidate, project_tools,
            include_root=include_root, final=final)
    # A resumed project may already contain failing code. Feed its concrete
    # failures into the first implementation turn instead of making the model
    # rediscover them through another cycle of broad source reads.
    existing_project = any(
        path.is_file()
        for folder in ("frontend", "backend")
        for path in (config.output_dir / folder / "src").rglob("*")
    )
    initial_feedback = ""
    if existing_project:
        try:
            project_tools.ensure_dependencies()
        except SandboxInfrastructureError:
            raise
        except Exception as exc:
            initial_feedback = f"Dependency preparation failed; inspect package manifests before source repair:\n{exc}"
        else:
            initial_result = local_verification()
            if not initial_result.passed:
                initial_feedback = _repair_feedback(initial_result)

    def checkpoint_feedback(*, include_root: bool = False, allow_restore: bool = False,
                            repair_focus: str | None = None) -> str:
        local = _prepare_local_candidate(config, tree, candidate, project_tools,
            include_root=include_root, allow_restore=allow_restore)
        if include_root and allow_restore:
            boundary_verification.remember(config.output_dir,
                getattr(project_tools, 'source_revision', 0), local)
        checks = local.checks
        failures = [check for check in checks if not check.passed]
        if not failures:
            LOGGER.info("Implementation checkpoint: current build and test scripts passed")
            return "Current build and test scripts pass. Finish any uncovered requirements and run final checks."
        feedback = _repair_feedback(VerificationResult(False, checks))
        if repair_focus:
            feedback += "\n\nCurrent repair task:\n" + repair_guidance(local, repair_focus)
        LOGGER.warning("Implementation checkpoint found %d failing checks:\n%s", len(failures), feedback[:6000])
        return feedback

    def repair_checkpoint_feedback() -> str:
        return checkpoint_feedback(include_root=True, allow_restore=True)

    def repair_diagnostic_feedback() -> str:
        return checkpoint_feedback(include_root=True)

    root_visual_references = tree.get("visual_reference", [])
    if isinstance(root_visual_references, str):
        root_visual_references = [root_visual_references]
    for module_index, module in enumerate(modules):
        mark_subtree_progress(runtime, module.subtree, "mark_design_started", "Analyzing requirement subtree")
        planning_subtree = dict(module.subtree)
        planning_subtree["project_requirements"] = tree
        module_visual_references = planning_subtree.get("visual_reference", [])
        if isinstance(module_visual_references, str):
            module_visual_references = [module_visual_references]
        if isinstance(root_visual_references, list):
            planning_subtree["visual_reference"] = list(
                dict.fromkeys([*root_visual_references, *module_visual_references])
            )
        planning_subtrees[module.node_id] = planning_subtree
        try:
            plan = model.plan(
                config.task_type,
                planning_subtree,
                reference_dir=config.requirement_dir,
            )
        except ModelBudgetExceeded as exc:
            budget_exhausted = True
            budget_failure = exc.report
            LOGGER.warning("%s; preserving current files for verification", exc)
            break
        _raise_if_model_interrupted(model)
        plans[module.node_id] = plan
        mark_subtree_progress(runtime, module.subtree, "mark_design_done", "Implementation plan prepared")
        mark_subtree_progress(runtime, module.subtree, "mark_implementation_started", "Applying requirement subtree")
        writes_before = len(changed_paths)
        token_limit = getattr(model, "max_total_tokens", None)
        token_allowance = None
        request_allowance = None
        if isinstance(token_limit, int):
            remaining_tokens = token_limit - model.prompt_tokens_used - model.completion_tokens_used
            token_allowance = max(0, remaining_tokens // (len(modules) - module_index + 2))
        request_limit = getattr(model, "max_model_requests", None)
        if isinstance(request_limit, int):
            remaining_requests = request_limit - model.model_requests_used - (len(modules) - module_index - 1)
            request_allowance = max(0, remaining_requests // (len(modules) - module_index + 2))
        boundary_verification.clear()
        budget_exhausted = model.implement(
            task_type=config.task_type,
            subtree=planning_subtree,
            plan=plan,
            project_tools=project_tools,
            repair_feedback=initial_feedback or None,
            reference_dir=config.requirement_dir,
            checkpoint_feedback=checkpoint_feedback,
            boundary_feedback=repair_checkpoint_feedback,
            token_allowance=token_allowance,
            request_allowance=request_allowance,
        )
        _raise_if_model_interrupted(model)
        initial_feedback = ""
        if budget_exhausted:
            budget_exhausted = True
            candidate_report = getattr(model, "last_budget_report", None)
            budget_failure = candidate_report if isinstance(candidate_report, BudgetExhaustion) else None
            message = (
                budget_failure.summary()
                if budget_failure is not None
                else "Model implementation budget exhausted before completion"
            )
            LOGGER.warning(
                "%s; stopping further requirement generation and preserving current files for verification",
                message,
            )
            break
        deferred = getattr(model, "last_implementation_deferred", False) is True
        if deferred:
            deferred_modules.add(module.node_id)
        if len(changed_paths) == writes_before and not getattr(model, "last_implementation_handoff", False) and not deferred:
            raise RuntimeError(f"No project files were changed for requirement {module.node_id}")
        module_paths.setdefault(module.node_id, set()).update(changed_paths[writes_before:])
        message = "Module work deferred to whole-project integration; acceptance remains unverified" if deferred else (
            "No file changes in recent tool turns; local scripts passed, continuing to the next requirement"
            if getattr(model, "last_implementation_handoff", False)
            else "Implementation turn completed"
        )
        if not deferred:
            mark_subtree_progress(runtime, module.subtree, "mark_implementation_done", message)

    if not budget_exhausted and (len(modules) > 1 or deferred_modules):
        LOGGER.info("Integrating all requirement modules through the application entry point")
        integration_tokens, integration_requests = _integration_allowances(model)
        LOGGER.info("Integration allocation: tokens=%s requests=%s; remaining half retained for focused repairs",
                    integration_tokens, integration_requests)
        boundary_verification.clear()
        budget_exhausted = model.implement(
            task_type=config.task_type,
            subtree=tree,
            plan=(
                "Integrate every module into one usable application. Inspect the actual application entry point, "
                "routing, providers, API handlers and durable storage. All earlier behaviors must remain reachable. "
                "Standalone component or validation tests do not establish completion: add integration tests "
                "mounting the real App and exercising the required multi-module user journey with the real API. "
                "Verify authentication, selection, final mutation, reload/restart persistence and isolation where "
                "required. Repair unreachable pages, placeholder actions and unconnected backend code. "
                "Run the full build and test suites, and finish only after all leaf requirements are integrated."
            ),
            project_tools=project_tools,
            reference_dir=config.requirement_dir,
            checkpoint_feedback=lambda: checkpoint_feedback(include_root=True),
            boundary_feedback=repair_checkpoint_feedback,
            token_allowance=integration_tokens,
            request_allowance=integration_requests,
        )
        _raise_if_model_interrupted(model)
        if budget_exhausted:
            candidate_report = getattr(model, "last_budget_report", None)
            budget_failure = candidate_report if isinstance(candidate_report, BudgetExhaustion) else None

    def audit_requirement_coverage() -> str:
        missing = model.review_requirements(tree, project_tools)
        _raise_if_model_interrupted(model)
        if not isinstance(missing, str):
            raise RuntimeError("Requirement audit returned no valid result")
        if missing:
            LOGGER.warning("Requirement coverage audit requires repair:\n%s", missing[:6000])
        return missing

    def coverage_repair_checkpoint_feedback() -> str:
        feedback = repair_diagnostic_feedback()
        if not feedback.startswith("Current build and test scripts pass."):
            return feedback
        # A diagnostic cannot discharge the audit obligation or make another
        # model call. The controller performs a fresh audit after the slice.
        missing = "\n".join(check.output for check in result.checks
            if check.name == "requirement coverage" and not check.passed)
        return "requirement coverage: FAILED (exit=1)\n" + (missing or
            "Source audit remains unverified; preserve its repair obligations until a fresh controller audit.")

    def verify_current_project(*, include_visual: bool = True) -> VerificationResult:
        try:
            project_tools.ensure_dependencies()
        except SandboxInfrastructureError:
            raise
        except Exception as exc:
            boundary_verification.clear()
            return VerificationResult(False, (CheckResult("project dependencies", False, 1, str(exc)),))
        local_result = boundary_verification.take(config.output_dir,
            getattr(project_tools, 'source_revision', 0), final=budget_exhausted)
        if local_result is None:
            local_result = local_verification(final=budget_exhausted)
        else:
            LOGGER.info('Reusing complete local verification from the immediate unchanged slice boundary')
        coverage_checks = ()
        if local_result.passed and not budget_exhausted:
            try:
                missing = audit_requirement_coverage()
                coverage_checks = (CheckResult("requirement coverage", not missing, 1 if missing else 0,
                                              missing or "Source audit found no missing required behavior; platform acceptance remains separate."),)
            except RequirementReviewError as exc:
                coverage_checks = (CheckResult("requirement audit validity", False, 1, str(exc) +
                    "\nNo trustworthy audit verdict after two attempts; coverage remains unverified."),)
            except ModelBudgetExceeded as exc:
                coverage_checks = (CheckResult("requirement coverage", False, 1, exc.report.summary()),)
        visual_result = (
            verify_visual_acceptance(
                config.output_dir,
                config.requirement_dir,
                visual_references,
                model,
            )
            if visual_references and include_visual and local_result.passed
            and not isinstance(getattr(model, "last_budget_report", None), BudgetExhaustion)
            else None
        )
        _raise_if_model_interrupted(model)
        checks = (*local_result.checks, *coverage_checks)
        if visual_result is not None:
            checks = (*checks, *visual_result.checks)
        return VerificationResult(
            local_result.passed
            and all(check.passed for check in coverage_checks)
            and (visual_result is None or visual_result.passed),
            checks,
        )

    result = verify_current_project(include_visual=not budget_exhausted)
    if isinstance(getattr(model, "last_budget_report", None), BudgetExhaustion):
        budget_exhausted = True
        budget_failure = model.last_budget_report
    # A changing assertion message or another leaf's edits must not reset an
    # already attempted family's allowance. A partial repair gets one
    # continuation, after every currently failing family has had its first turn.
    repair_attempts: dict[str, int] = {}
    pending_continuations: set[str] = set()
    generation_stop_reason = None
    earlier_revision_reviews = ()
    while not result.passed and not budget_exhausted:
        if any(check.name == "requirement audit validity" and not check.passed for check in result.checks):
            LOGGER.warning("Reviewer protocol failed after bounded retry; preserving checks and candidate for evaluation")
            break
        families = tuple(node_id for node_id in failed_behavior_ids(result)
                         if focused_subtree(tree, node_id))
        focus = next((node_id for node_id in families
                      if node_id not in repair_attempts), None)
        if focus is None:
            focus = next((node_id for node_id in families
                          if node_id in pending_continuations), None)
        if families and focus is None:
            LOGGER.warning("Failing behavioral families exhausted their bounded repair slices; stopping repeated attempts")
            generation_stop_reason = "bounded_repair_exhausted"
            break
        # Without a trustworthy behavioral family, diagnose the whole project
        # once using ROOT, with the same bounded progress-based continuation.
        repair_key = focus or "ROOT"
        if repair_key in repair_attempts and repair_key not in pending_continuations:
            LOGGER.warning("Verification failure family has no eligible continuation; stopping repeated attempts")
            generation_stop_reason = "bounded_repair_exhausted"
            break
        pending_continuations.discard(repair_key)
        repair_attempts[repair_key] = repair_attempts.get(repair_key, 0) + 1
        source_before = _source_fingerprint(config.output_dir)
        feedback = _repair_feedback(result)
        guidance = repair_guidance(result, focus)
        if guidance:
            feedback = ('Complete verification report (preserve passing behavior; '
                        'the active repair task follows):\n' + feedback
                        + '\n\nCurrent repair task:\n' + guidance)
        repair_checkpoint = (
            coverage_repair_checkpoint_feedback
            if any(check.name == "requirement coverage" and not check.passed for check in result.checks)
            else repair_diagnostic_feedback
        )
        repair_modules = _repair_modules(result, modules, module_paths)
        LOGGER.warning("Verification failed; repairing %d of %d requirement modules", len(repair_modules), len(modules))
        # A shared boundary failure needs one view of the complete requirements,
        # not sequential module contexts that can undo each other's behavior.
        repair_targets = repair_modules if len(repair_modules) == 1 else [RequirementModule(
            node_id="ROOT", name=str(tree.get("name", "Project integration")), subtree=tree,
        )]
        if focus:
            focused = focused_subtree(tree, focus)
            repair_targets = [RequirementModule(node_id=focus, name=str(focused.get('name', focus)), subtree=focused)]
            LOGGER.info("Focused behavioral repair: %s; full project regression remains required", focus)
        repair_stop_reason = None
        for module in repair_targets:
            writes_before = len(changed_paths)
            boundary_verification.clear()
            repair_budget_exhausted = model.implement(
                task_type=config.task_type,
                subtree=module.subtree if focus else planning_subtrees.get(module.node_id, tree),
                plan=plans.get(module.node_id,
                    "Repair the concrete failures across module boundaries while preserving every original "
                    "scenario. Read-only public information and authenticated mutations may have different "
                    "access rules: satisfy both without hiding required journey context or weakening guards. "
                    "Inspect the failing assertions and real application wiring; do not expand unrelated modules."),
                project_tools=project_tools,
                repair_feedback=feedback,
                reference_dir=config.requirement_dir,
                checkpoint_feedback=repair_checkpoint,
                # Enforce source locally at every slice stop. Coverage audits
                # run in verify_current_project, outside the tool allocation.
                boundary_feedback=repair_checkpoint_feedback,
                verification_feedback=lambda: checkpoint_feedback(include_root=True, repair_focus=focus),
                request_allowance=REPAIR_REQUEST_ALLOWANCE,
            )
            _raise_if_model_interrupted(model)
            repair_stop_reason = getattr(model, "last_implementation_stop_reason", None)
            module_paths.setdefault(module.node_id, set()).update(changed_paths[writes_before:])
            if repair_budget_exhausted:
                budget_exhausted = True
                candidate_report = getattr(model, "last_budget_report", None)
                budget_failure = candidate_report if isinstance(candidate_report, BudgetExhaustion) else None
                message = (
                    budget_failure.summary()
                    if budget_failure is not None
                    else "Model repair budget exhausted before completion"
                )
                LOGGER.warning(
                    "%s; stopping further repair calls",
                    message,
                )
                break
        result = verify_current_project(include_visual=not budget_exhausted)
        if isinstance(getattr(model, "last_budget_report", None), BudgetExhaustion):
            budget_exhausted = True
            budget_failure = model.last_budget_report
        if (not budget_exhausted and repair_attempts[repair_key] == 1
                and repair_stop_reason in CONTINUABLE_REPAIR_STOPS
                and _source_fingerprint(config.output_dir) != source_before):
            # Measure after the full regression: reverted edits cannot earn a
            # continuation, and repeated writes/log timestamps are not progress.
            # A truncated response executes no tools, but earlier complete
            # writes in that slice can still earn this one bounded continuation.
            pending_continuations.add(repair_key)
            LOGGER.info("Retained production source changes after repair stop=%s; queued one continuation for %s", repair_stop_reason, focus or "ROOT")

    if generation_stop_reason == "bounded_repair_exhausted":
        # The controller has finished all eligible slices. Finalize the actual
        # retained source locally without restarting model audits or generation.
        # Keep earlier audit/visual evidence separate from fresh build/behavior.
        previous_reviews = tuple(check for check in result.checks
            if check.name in {"requirement coverage", "requirement audit validity", "visual and browser acceptance"}
            or check.name.startswith(("visual reference:", "browser interaction:")))
        revision_before = getattr(project_tools, "source_revision", 0)
        local = _prepare_local_candidate(config, tree, candidate, project_tools, final=True)
        if getattr(project_tools, "source_revision", 0) != revision_before:
            earlier_revision_reviews = previous_reviews
            previous_reviews = tuple(CheckResult(check.name, False, check.exit_code,
                "Earlier source revision evidence; final restored source was not re-audited.\n" + check.output,
                check.requirement_ids, check.case_statuses)
                for check in previous_reviews if not check.passed)
        result = VerificationResult(False, (*local.checks, *previous_reviews,
            CheckResult("agent generation stop", False, 1,
                "All eligible bounded repair slices ended. Further generation stopped; "
                "remaining checks and unrun reviews do not establish formal acceptance.")))

    if budget_exhausted:
        budget_check = CheckResult(
            "agent generation budget",
            False,
            1,
            (budget_failure.summary() if budget_failure is not None else "Model implementation budget exhausted.")
            + "\nPartial project files were preserved and the available project checks were run.",
        )
        result = VerificationResult(False, (*result.checks, budget_check))

    result = _finalize_delivery(config.output_dir, result, budget_failure,
        generation_stop_reason=generation_stop_reason,
        earlier_revision_reviews=earlier_revision_reviews)

    _record_verification(runtime, tree, result)
    return result


def run_agent(runtime: AgentRuntime, config: AgentConfig) -> int:
    runtime.events.mark_run_started("ARC-Bench software engineering agent started")
    tree = load_requirement_tree(config.requirement_dir)
    modules = root_modules(tree)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    _register_traceability(runtime, tree)
    if config.demo_mode:
        result = run_offline_demo(runtime, config, tree, modules)
    else:
        result = run_model_agent(runtime, config, tree, modules)
    if result.passed:
        runtime.events.mark_run_completed("Agent finished and verification passed")
        LOGGER.info("Agent completed successfully")
        return 0
    if result.ready_for_evaluation:
        runtime.events.mark_run_completed("Runnable candidate delivered for formal evaluation; local verification did not fully pass")
        LOGGER.warning("Candidate delivered before further generation; formal acceptance pending. Unresolved checks:\n%s", result.summary())
        return 0
    runtime.events.mark_run_failed("Agent finished but verification did not pass")
    LOGGER.error("Verification failed:\n%s", result.summary())
    return 1

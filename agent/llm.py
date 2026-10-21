from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
from dataclasses import dataclass
from itertools import count
from pathlib import Path, PurePosixPath
import re
import time
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from openai import APIStatusError

from .tools import MAX_BATCH_OUTPUT_CHARS, ProjectTools, TOOL_SCHEMAS
from .model_transport import CompletionClient, DEFAULT_MODEL_DEADLINE_SECONDS, ModelRequestInterrupted
from .request_metrics import RequestObservation, emit as emit_metric, identifier as metric_identifier
from .ticketbooking_contract import public_acceptance_context
from .initial_bootstrap import initial_bootstrap_gaps
from .generation_contract import generation_contract_context
from .prompt_context import requirement_context
from .read_evidence import ReadEvidence, source_path as _normalized_source_path
from .tool_context import initial_file_inventory, failure_source_context
from .visual_acceptance import REPORT_RELATIVE_PATH, SCREENSHOT_RELATIVE_DIR, collect_visual_references

LOGGER = logging.getLogger(__name__)
MAX_COMPACTED_EXCHANGES = 6
MAX_EXCHANGE_SUMMARY_CHARS = 900
MAX_CONVERSATION_CHARS = 240_000
DEFAULT_MAX_MODEL_REQUESTS = 250
DEFAULT_MAX_TOTAL_TOKENS = 8_000_000
MAX_COMPLETION_TOKENS_PER_REQUEST = 12_000
DEEPSEEK_THINKING_IMPLEMENTATION_MAX_TOKENS = 24_000
DEEPSEEK_PRO_EDIT_MAX_TOKENS = 24_000
MAX_IDENTICAL_FAILED_TOOL_TURNS = 3
DEFAULT_MAX_IDLE_TOOL_TURNS = 12
MAX_REPAIR_REQUESTS = 12
EARLY_CHECKPOINT_TURN = 4
CHECKPOINT_INTERVAL_TURNS = 6
PASSING_CHECKPOINT_IDLE_TURNS = 4


def _source_tool_failure(name: str, arguments: dict[str, Any], error: Exception) -> tuple[str, str] | None:
    """Recognize stale-source errors without grouping unrelated tool failures."""
    if name == "replace_text" and isinstance(error, ValueError):
        mismatch = re.fullmatch(r"replace_text requires one exact match in (.+); found (\d+)", str(error))
        if mismatch and int(mismatch.group(2)) != 1:
            return _normalized_source_path(mismatch.group(1)), "replacement_match"
    if isinstance(error, FileNotFoundError):
        explicit = re.fullmatch(r"File not found: (.+)", str(error))
        if name in {"read_file", "read_files"} and explicit:
            return _normalized_source_path(explicit.group(1)), "missing_file"
        if name == "replace_text" and getattr(error, "filename", None) and isinstance(arguments.get("path"), str):
            return _normalized_source_path(arguments["path"]), "missing_file"
    return None


def _fixed_regression_signature(feedback: str) -> tuple[str, ...]:
    """Keep case/assertion identity while ignoring generated account diagnostics."""
    failures = re.findall(r"^REQ-\d+(?:\.\d+)*: [^\n]+", feedback, re.MULTILINE)
    signatures = []
    for failure in failures:
        # The public check formats each line as requirement: case title: failure.
        # Never normalize a case title, locator, count, date, or validation limit.
        parts = failure.split(": ", 2)
        if len(parts) == 3:
            # uniqueTicketBookingAccount emits Date.now() plus up to six base36
            # characters. The duplicate-email case uppercases the identifier.
            # Mask this reserved username shape only in diagnostics.
            parts[2] = re.sub(r"(?<![\w-])tb-user-[0-9]{13}-[a-z0-9]{1,6}(?![\w-])",
                              "<generated-ticket-account>", parts[2], flags=re.IGNORECASE)
        signatures.append(": ".join(parts))
    return tuple(signatures)


@dataclass(frozen=True)
class BudgetExhaustion:
    budget: str
    limit: int
    used: int
    requested: int
    tool_names: tuple[str, ...]
    turns_used: int
    tool_calls_used: int
    model_requests_used: int
    prompt_tokens_used: int = 0
    completion_tokens_used: int = 0
    detail: str = ""

    def summary(self) -> str:
        tools = ", ".join(self.tool_names) or "unknown/not requested"
        status = "generation stopped for delivery" if self.budget.startswith("delivery_") else "exhausted before completion"
        summary = (
            f"{self.budget} {status}: configured_limit={self.limit}, already_used={self.used}, "
            f"requested_tool_calls={self.requested}, requested_tools=[{tools}], model_turns_used={self.turns_used}, "
            f"total_tool_calls_used={self.tool_calls_used}, total_model_requests={self.model_requests_used}, "
            f"prompt_tokens={self.prompt_tokens_used}, completion_tokens={self.completion_tokens_used}"
        )
        return summary + (f", detail={self.detail}" if self.detail else "")


class ModelBudgetExceeded(Exception):
    def __init__(self, report: BudgetExhaustion) -> None:
        self.report = report
        super().__init__(report.summary())


class RequirementReviewError(RuntimeError):
    """A model audit has no trustworthy verdict; it is not a production defect."""


class ModelClient:
    def __init__(self, *, max_turns: int | None = None, max_tool_calls: int | None = None) -> None:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        self.model = os.environ.get("MODEL", "").strip()
        self.visual_model = os.environ.get("VISUAL_MODEL", "").strip() or self.model
        self.visual_review_model = os.environ.get("VISUAL_REVIEW_MODEL", "").strip() or self.visual_model
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
        self.metrics_provider = urlsplit(base_url).hostname or "api.openai.com"
        self.is_official_deepseek = urlsplit(base_url).hostname == 'api.deepseek.com'
        separate_visual_plan = (urlsplit(base_url).hostname == 'api.deepseek.com'
            and self.model.casefold() == 'deepseek-v4-pro'
            and self.visual_model.casefold() != self.model.casefold())
        visual_default = 'disabled' if separate_visual_plan else 'enabled'
        self.visual_implementation = os.environ.get("ARCBENCH_VISUAL_IMPLEMENTATION", visual_default).strip().lower()
        if self.visual_implementation not in {"enabled", "disabled"}:
            raise ValueError("ARCBENCH_VISUAL_IMPLEMENTATION must be enabled or disabled")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set. Pass --demo with a supported local task file for deterministic offline mode.")
        if not self.model:
            raise RuntimeError("MODEL is not set by the ARC-Bench Runner.")
        self.is_glm = urlsplit(base_url).hostname == "open.bigmodel.cn"
        self.is_deepseek = (
            urlsplit(base_url).hostname == "api.deepseek.com"
            or self.model.lower().startswith("deepseek-")
        )
        self.implementation_thinking = os.environ.get("ARCBENCH_IMPLEMENTATION_THINKING", "").strip().lower()
        if self.implementation_thinking not in {"", "enabled", "disabled"}:
            raise ValueError("ARCBENCH_IMPLEMENTATION_THINKING must be enabled or disabled")
        options: dict[str, Any] = {"api_key": api_key}
        if base_url:
            options["base_url"] = base_url
        from .model_transport import default_model_deadline
        self.client = CompletionClient(deadline_seconds=float(os.environ.get(
            "ARCBENCH_MODEL_DEADLINE_SECONDS", default_model_deadline(self.model, base_url))),
            assemble_stream=urlsplit(base_url).hostname == 'api.deepseek.com', **options)
        self.last_request_interruption: ModelRequestInterrupted | None = None
        self.max_turns = max_turns
        self.max_tool_calls = max_tool_calls
        self.max_model_requests = int(os.environ.get("ARCBENCH_MAX_MODEL_REQUESTS", DEFAULT_MAX_MODEL_REQUESTS))
        self.max_total_tokens = int(os.environ.get("ARCBENCH_MAX_TOTAL_TOKENS", DEFAULT_MAX_TOTAL_TOKENS))
        self.max_idle_tool_turns = int(os.environ.get("ARCBENCH_MAX_IDLE_TOOL_TURNS", DEFAULT_MAX_IDLE_TOOL_TURNS))
        if self.max_model_requests < 1 or self.max_total_tokens < 1 or self.max_idle_tool_turns < 2:
            raise ValueError("Model, token, and idle tool-turn limits must be positive; idle limit must be at least 2")
        self.tool_calls_used = 0
        self.model_requests_used = 0
        self.prompt_tokens_used = 0
        self.completion_tokens_used = 0
        self.cache_hit_tokens_used = 0
        self.cache_miss_tokens_used = 0
        self.cache_observed_requests = 0
        self.model_seconds = 0.0
        self.tool_seconds = 0.0
        self.last_budget_report: BudgetExhaustion | None = None
        self._requirement_review_cache: tuple[str, str] | None = None
        self._nonthinking_scopes: set[tuple[str, str, str, str, str]] = set()
        LOGGER.info("Model request policy: deadline_seconds=%g sdk_max_retries=0; read_timeout_seconds=%g; transport=%s; incomplete usage remains unknown",
                    self.client.deadline_seconds, self.client._options['timeout'].read,
                    'assembled_stream' if self.client.assemble_stream else 'complete_json')
        LOGGER.info('Model routing: text_generation=%s visual_planning=%s visual_implementation=%s; actual request models logged per response',
                    self.model, self.visual_model, self.visual_implementation)

    def _create_completion(self, stage: str, **kwargs: Any) -> Any:
        self.raise_if_request_interrupted()
        LOGGER.info('Model output policy: stage=%s request=%d max_tokens=%s',
                    stage, self.model_requests_used, kwargs.get('max_tokens'))
        started = time.perf_counter()
        observation = RequestObservation(stage=stage, requested_model=kwargs.get("model"),
            provider=getattr(self, "metrics_provider", None), request=self.model_requests_used)
        response = None
        error = None
        try:
            response = self.client.chat.completions.create(**kwargs)
            return response
        except ModelRequestInterrupted as exc:
            stopped = ModelRequestInterrupted(reason=exc.reason, deadline_seconds=exc.deadline_seconds,
                stage=stage, request=self.model_requests_used, cause_types=exc.cause_types)
            self.last_request_interruption = stopped
            LOGGER.warning("%s", stopped)
            LOGGER.info("Model request: stage=%s request=%d context_chars=%d token_counts=unavailable",
                stage, self.model_requests_used, self._message_context_chars(kwargs.get("messages", [])))
            error = stopped
            raise stopped from exc
        except BaseException as exc:
            error = exc
            raise
        finally:
            elapsed = time.perf_counter() - started
            observation.finish(response=response, elapsed=elapsed, error=error)
            self.model_seconds = getattr(self, "model_seconds", 0.0) + elapsed
            LOGGER.info("Model latency: stage=%s model=%s seconds=%.3f", stage, kwargs.get("model"), elapsed)

    def raise_if_request_interrupted(self) -> None:
        stopped = getattr(self, "last_request_interruption", None)
        if isinstance(stopped, ModelRequestInterrupted):
            raise stopped

    def _edit_output_cap(self, model: str) -> int:
        # Four-plus observed Pro tool responses exhausted 12K and were discarded.
        # Give complete edits the same bounded room already used for Pro thinking.
        # This does not change the run budget, deadline, retry or completeness rules.
        if getattr(self, 'is_official_deepseek', False) and model.casefold() == 'deepseek-v4-pro':
            return DEEPSEEK_PRO_EDIT_MAX_TOKENS
        return MAX_COMPLETION_TOKENS_PER_REQUEST

    @staticmethod
    def _implementation_scope_key(
        task_type: str, subtree: dict[str, Any], project_dir: Path | None, model: str,
    ) -> tuple[str, str, str, str, str] | None:
        # Missing identity must not merge unrelated anonymous scopes. State is
        # local to this client and never persists into a new run/client.
        scope_id = subtree.get("id")
        if not isinstance(scope_id, str) or not scope_id.strip() or not isinstance(project_dir, Path):
            return None
        project = subtree.get("project_requirements")
        if not isinstance(project, dict):
            project = subtree
        fingerprint = hashlib.sha256(json.dumps(project, sort_keys=True,
            ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        return task_type, scope_id, fingerprint, os.path.normcase(str(project_dir.resolve())), model

    @staticmethod
    def _pure_reasoning_length(response: Any) -> bool:
        choice = response.choices[0]
        usage = getattr(response, "usage", None)
        completion = getattr(usage, "completion_tokens", None)
        reasoning = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
        return (getattr(choice, "finish_reason", None) == "length"
            and type(completion) is int and completion > 0
            and type(reasoning) is int and reasoning == completion
            and not getattr(choice.message, "content", None)
            and not getattr(choice.message, "tool_calls", None))

    def _check_model_budget(self, *, reserve_for_delivery: bool = False) -> None:
        requests_used = getattr(self, "model_requests_used", 0)
        tokens_used = getattr(self, "prompt_tokens_used", 0) + getattr(self, "completion_tokens_used", 0)
        for budget, limit, used in (
            ("model_request_budget", getattr(self, "max_model_requests", None), requests_used),
            ("total_token_budget", getattr(self, "max_total_tokens", None), tokens_used),
        ):
            if isinstance(limit, int) and used >= limit:
                report = BudgetExhaustion(
                    budget=budget,
                    limit=limit,
                    used=used,
                    requested=0,
                    tool_names=(),
                    turns_used=requests_used,
                    tool_calls_used=self.tool_calls_used,
                    model_requests_used=requests_used,
                    prompt_tokens_used=self.prompt_tokens_used,
                    completion_tokens_used=self.completion_tokens_used,
                )
                self.last_budget_report = report
                raise ModelBudgetExceeded(report)
        if reserve_for_delivery:
            # Stop generation before the hard cap. Local final verification does
            # not need another model call; retain headroom for the last response.
            for budget, limit, used, reserve in (
                ("delivery_token_reserve", self.max_total_tokens, tokens_used,
                 min(400_000, self.max_total_tokens // 20)),
                ("delivery_request_reserve", self.max_model_requests, requests_used,
                 min(3, self.max_model_requests // 20)),
            ):
                if reserve and used >= limit - reserve:
                    report = BudgetExhaustion(
                        budget=budget, limit=limit, used=used, requested=0, tool_names=(),
                        turns_used=requests_used, tool_calls_used=self.tool_calls_used,
                        model_requests_used=requests_used, prompt_tokens_used=self.prompt_tokens_used,
                        completion_tokens_used=self.completion_tokens_used,
                        detail=f"Generation stopped early for delivery; reserve={reserve}; formal acceptance pending",
                    )
                    self.last_budget_report = report
                    raise ModelBudgetExceeded(report)

    def _record_model_request(self) -> None:
        self.raise_if_request_interrupted()
        self._check_model_budget(reserve_for_delivery=True)
        self.model_requests_used = getattr(self, "model_requests_used", 0) + 1

    def _record_usage(self, response: Any, *, stage: str = "model", context_chars: int | None = None) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            LOGGER.info(
                "Model request: stage=%s request=%d context_chars=%s token_counts=unavailable",
                stage,
                self.model_requests_used,
                context_chars if context_chars is not None else "unknown",
            )
            return
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None)
        cache_hit = getattr(usage, "prompt_cache_hit_tokens", None)
        if cache_hit is None:
            cache_hit = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
        cache_miss = getattr(usage, "prompt_cache_miss_tokens", None)
        if cache_hit is not None and cache_miss is None:
            cache_miss = max(0, prompt_tokens - int(cache_hit))
        self.prompt_tokens_used += prompt_tokens
        self.completion_tokens_used += completion_tokens
        if cache_hit is not None and cache_miss is not None:
            self.cache_hit_tokens_used += int(cache_hit)
            self.cache_miss_tokens_used += int(cache_miss)
            self.cache_observed_requests += 1
        LOGGER.info(
            "Model request: stage=%s request=%d context_chars=%s prompt_tokens=%d completion_tokens=%d "
            "reasoning_tokens=%s cache_hit_tokens=%s cache_miss_tokens=%s",
            stage,
            self.model_requests_used,
            context_chars if context_chars is not None else "unknown",
            prompt_tokens,
            completion_tokens,
            reasoning_tokens if reasoning_tokens is not None else "unavailable",
            cache_hit if cache_hit is not None else "unavailable",
            cache_miss if cache_miss is not None else "unavailable",
        )
        token_limit = getattr(self, "max_total_tokens", None)
        if isinstance(token_limit, int) and self.prompt_tokens_used + self.completion_tokens_used > token_limit:
            try:
                self._check_model_budget()
            except ModelBudgetExceeded:
                # Preserve the final response and mark overshoot even if the
                # model finishes without requesting another turn.
                pass

    @staticmethod
    def _compact_tool_exchange(
        assistant_message: dict[str, Any], tool_messages: list[dict[str, Any]],
    ) -> str:
        """Keep a small, useful ledger for an exchange whose raw payload is dropped."""
        calls = assistant_message.get("tool_calls", [])
        summaries: list[str] = []
        for index, call in enumerate(calls):
            function = call.get("function", {}) if isinstance(call, dict) else {}
            name = function.get("name", "tool") if isinstance(function, dict) else "tool"
            response = tool_messages[index].get("content", "") if index < len(tool_messages) else ""
            if not isinstance(response, str):
                response = str(response)
            # Preserve useful errors/build outcomes, while dropping large file contents.
            if name in {"run_project_script", "run_project_scripts"}:
                detail = response[:500]
            elif name in {"write_files", "write_file", "replace_text"}:
                detail = response[:350]
            elif name in {"read_files", "read_file", "list_files", "search_text"}:
                detail = response[:240]
            else:
                detail = response[:180]
            summaries.append(f"{name}: {detail}")
        content = assistant_message.get("content")
        if isinstance(content, str) and content.strip():
            summaries.insert(0, f"Agent note: {content.strip()[:180]}")
        return " | ".join(summaries)[:MAX_EXCHANGE_SUMMARY_CHARS]

    @staticmethod
    def _message_context_chars(messages: list[dict[str, Any]]) -> int:
        # Measures serialized prompt size without logging project contents.
        return len(json.dumps(messages, ensure_ascii=False, default=str))

    @staticmethod
    def _reject_truncated_response(response: Any, stage: str) -> None:
        if getattr(response.choices[0], "finish_reason", None) == "length":
            raise RuntimeError(
                f"Model {stage} response reached its output limit before completion; "
                "the partial result was not accepted."
            )

    def log_usage(self) -> None:
        if self.prompt_tokens_used or self.completion_tokens_used:
            LOGGER.info(
                "Model usage: requests=%d prompt_tokens=%d completion_tokens=%d total_tokens=%d",
                self.model_requests_used,
                self.prompt_tokens_used,
                self.completion_tokens_used,
                self.prompt_tokens_used + self.completion_tokens_used,
            )
        else:
            LOGGER.info("Model usage: requests=%d token counts unavailable from provider", self.model_requests_used)
        if self.last_request_interruption is not None:
            LOGGER.warning("Model usage incomplete: request=%d stage=%s token_counts=unavailable; unknown usage retained",
                self.last_request_interruption.request, self.last_request_interruption.stage)
        if self.cache_observed_requests:
            cache_input = self.cache_hit_tokens_used + self.cache_miss_tokens_used
            LOGGER.info(
                "Prompt cache: observed_requests=%d hit_tokens=%d miss_tokens=%d hit_rate=%.1f%%",
                self.cache_observed_requests,
                self.cache_hit_tokens_used,
                self.cache_miss_tokens_used,
                100 * self.cache_hit_tokens_used / cache_input if cache_input else 0.0,
            )
        else:
            LOGGER.info("Prompt cache: hit/miss counts unavailable from provider")
        LOGGER.info(
            "Execution time: model_seconds=%.3f tool_seconds=%.3f",
            getattr(self, "model_seconds", 0.0), getattr(self, "tool_seconds", 0.0),
        )

    @staticmethod
    def _visual_references(subtree: dict[str, Any]) -> list[tuple[str, str]]:
        references: list[tuple[str, str]] = []
        seen: set[str] = set()

        def walk(node: Any) -> None:
            if not isinstance(node, dict):
                return
            req_id = str(node.get("id") or node.get("req_id") or "ROOT")
            refs = node.get("visual_reference", [])
            if isinstance(refs, str):
                refs = [refs]
            optional_refs = node.get("optional_visual_reference", [])
            if isinstance(optional_refs, str):
                optional_refs = [optional_refs]
            if isinstance(refs, list) and isinstance(optional_refs, list):
                refs = [*refs, *optional_refs]
            if isinstance(refs, list):
                for ref in refs:
                    reference = str(ref).strip()
                    if reference and reference not in seen:
                        references.append((req_id, reference))
                        seen.add(reference)
            children = node.get("children", [])
            if isinstance(children, list):
                for child in children:
                    walk(child)

        walk(subtree)
        return references

    def _visual_inputs(
        self, subtree: dict[str, Any], reference_dir: Path | None
    ) -> list[dict[str, Any]]:
        references = self._visual_references(subtree)
        if not references:
            return []

        content: list[dict[str, Any]] = []
        for req_id, reference in references:
            parsed = urlsplit(reference)
            if parsed.scheme.lower() in {"http", "https"}:
                host = (parsed.hostname or "").lower()
                if not host or parsed.username or parsed.password:
                    raise ValueError(f"Invalid visual reference for {req_id}: {reference}")
                if host == "localhost" or host.endswith((".localhost", ".local")):
                    raise ValueError(f"Local-network visual URLs are not allowed: {reference}")
                try:
                    address = ipaddress.ip_address(host)
                except ValueError:
                    address = None
                if address is not None and not address.is_global:
                    raise ValueError(f"Local-network visual URLs are not allowed: {reference}")
                mime_type = mimetypes.guess_type(parsed.path)[0]
                if mime_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                    raise ValueError(f"Unsupported remote visual reference image type: {reference}")
                image_url = reference
            else:
                if parsed.scheme:
                    raise ValueError(f"Unsupported visual reference URL scheme: {reference}")
                if reference_dir is None:
                    raise ValueError(f"A task directory is required to resolve visual reference: {reference}")
                task_root = reference_dir.resolve()
                image_path = (task_root / unquote(parsed.path)).resolve()
                if image_path != task_root and task_root not in image_path.parents:
                    raise ValueError(f"Visual reference escapes the task directory: {reference}")
                if not image_path.is_file():
                    raise FileNotFoundError(f"Visual reference for {req_id} was not found: {image_path}")
                mime_type = mimetypes.guess_type(image_path.name)[0]
                if mime_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                    raise ValueError(f"Unsupported visual reference image type: {image_path.name}")
                image_bytes = image_path.read_bytes()
                if len(image_bytes) > 8 * 1024 * 1024:
                    raise ValueError(f"Visual reference exceeds the 8 MiB limit: {image_path.name}")
                encoded = base64.b64encode(image_bytes).decode("ascii")
                image_url = f"data:{mime_type};base64,{encoded}"

            content.append({"type": "text", "text": f"Visual reference for {req_id}: {reference}"})
            content.append({"type": "image_url", "image_url": {"url": image_url}})
        return content

    def _repair_visual_inputs(
        self, subtree: dict[str, Any], project_tools: ProjectTools
    ) -> list[dict[str, Any]]:
        relevant_references = {reference for _, reference in self._visual_references(subtree)}
        if not relevant_references:
            return []
        project_root = project_tools.project_dir.resolve()
        report_path = (project_root / REPORT_RELATIVE_PATH).resolve()
        screenshot_root = (project_root / SCREENSHOT_RELATIVE_DIR).resolve()
        if project_root not in report_path.parents or project_root not in screenshot_root.parents:
            return []
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        cases = report.get("cases") if isinstance(report, dict) else None
        if not isinstance(cases, list):
            return []
        flows = report.get("flows", [])
        failed_interaction = isinstance(flows, list) and any(
            isinstance(flow, dict) and flow.get("passed") is False for flow in flows
        )

        content: list[dict[str, Any]] = []
        total_image_bytes = 0
        for case in cases:
            if not isinstance(case, dict) or case.get("reference") not in relevant_references:
                continue
            if case.get("verdict") == "pass" and not failed_interaction:
                continue
            screenshot_name = case.get("screenshot")
            if not isinstance(screenshot_name, str):
                continue
            screenshot_path = (project_root / screenshot_name).resolve()
            if screenshot_path.parent != screenshot_root or screenshot_path.suffix.lower() != ".png":
                continue
            try:
                screenshot_bytes = screenshot_path.read_bytes()
            except OSError:
                continue
            if not screenshot_bytes or len(screenshot_bytes) > 8 * 1024 * 1024:
                continue
            total_image_bytes += len(screenshot_bytes)
            if total_image_bytes > 16 * 1024 * 1024:
                break
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"Actual browser screenshot for failed visual case {case.get('name')!r}, "
                        f"reference {case['reference']!r}, route {case.get('route')!r}. "
                        "Compare this image directly with the attached requirement reference "
                        "and repair the material differences described in the verification feedback."
                    ),
                }
            )
            encoded = base64.b64encode(screenshot_bytes).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}})
        return content

    def review_visual_case(
        self,
        *,
        reference: str,
        reference_dir: Path,
        case: dict[str, Any],
        screenshot: bytes,
    ) -> dict[str, Any]:
        reference_inputs = self._visual_inputs(
            {"id": case["name"], "visual_reference": [reference]}, reference_dir
        )
        screenshot_data = base64.b64encode(screenshot).decode("ascii")
        user_content = [
            {
                "type": "text",
                "text": (
                    f"Compare the generated page screenshot with its visual reference.\n"
                    f"Case: {case['name']}\nRoute: {case['route']}\n"
                    f"Viewport: {case['viewport']['width']}x{case['viewport']['height']}\n"
                    f"Capture target: {case.get('target_selector') or 'viewport'}\n"
                    f"Visual expectations: {json.dumps(case['visual_expectations'], ensure_ascii=False)}\n"
                    "Reference image follows, then generated screenshot. The reference can be a crop. "
                    "Judge large differences in geometry, alignment, hierarchy, typography, colors, "
                    "and imagery as repair-worthy; ignore minor browser rasterization differences. "
                    "Return only a JSON object: {\"verdict\":\"pass\"|\"repair\",\"issues\":[...],"
                    "\"matched\":[...]}. Only use pass when no material visual mismatch remains."
                ),
            },
            *reference_inputs,
            {"type": "text", "text": "Generated page screenshot:"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{screenshot_data}"},
            },
        ]
        self._record_model_request()
        response = self._create_completion("visual_review",
            model=self.visual_review_model,
            max_tokens=MAX_COMPLETION_TOKENS_PER_REQUEST,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a visual acceptance reviewer. Compare only what is visible in the "
                        "two images and the supplied case expectations. Do not infer unseen parts. "
                        "Return valid JSON only; use verdict=repair for a material discrepancy or "
                        "when the screenshot cannot be assessed."
                    ),
                },
                {"role": "user", "content": user_content},
            ],
        )
        self._record_usage(response, stage="visual_review", context_chars=self._message_context_chars(messages=[
            {"role": "system", "content": "visual review"}, {"role": "user", "content": user_content}
        ]))
        self._reject_truncated_response(response, "visual review")
        raw = response.choices[0].message.content or ""
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            raise RuntimeError("Visual reviewer returned no JSON verdict")
        try:
            review = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise RuntimeError("Visual reviewer returned invalid JSON") from exc
        verdict = review.get("verdict")
        issues = review.get("issues", [])
        matched = review.get("matched", [])
        if verdict not in {"pass", "repair"} or not isinstance(issues, list) or not isinstance(matched, list):
            raise RuntimeError("Visual reviewer returned an invalid verdict schema")
        return {"verdict": verdict, "issues": issues, "matched": matched}

    def review_requirements(self, subtree: dict[str, Any], project_tools: ProjectTools) -> str:
        """Fresh-context source audit; script success is not requirement coverage."""
        sources = []
        source_contents = {}
        remaining = 120_000
        listing = project_tools.list_files(".").splitlines()
        audit_complete = not any(name.startswith("...[truncated at ") for name in listing)
        for name in listing:
            parts = Path(name).parts
            if any(part in {"test", "tests", "__tests__"} for part in parts):
                continue
            if ".test." in name or ".spec." in name or Path(name).suffix not in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json"}:
                continue
            if Path(name).suffix == ".json" and Path(name).name != "package.json":
                continue
            try:
                content = project_tools.read_file(name)
            except (ValueError, FileNotFoundError):
                audit_complete = False
                continue
            if len(content) > remaining:
                audit_complete = False
                sources.append(f"{name}: [omitted: audit size limit]")
                continue
            if content.endswith("\n...[truncated]"):
                audit_complete = False
            sources.append(f"--- {name} ---\n{content}")
            source_contents[name] = content
            remaining -= len(content)
        if not audit_complete:
            # Neither a prior verdict nor a model seeing only a prefix can
            # certify the unseen source. Fail explicitly without a model call.
            self._requirement_review_cache = None
            raise RequirementReviewError(
                "Requirement source audit incomplete: file listing or source content "
                "was truncated, omitted, or unreadable; partition the audit input "
                "before requesting a complete verdict"
            )
        messages = [{"role": "system", "content": (
            "Audit production source against every supplied requirement. Green self-tests are not evidence of "
            "coverage. Trace actual entry points, route wiring, mutation APIs and durable state. Check that "
            "reload reconstructs the required visible state from the same stored record, not merely that a "
            "database file exists; verify ownership and duplicate confirmation. "
            "For user or selected-journey changes, check that prior owned booking/confirmation state is cleared "
            "before rendering and that delayed API responses cannot restore a previous account's state. "
            "Treat the supplied requirements as the complete contract. Do not invent additional obligations "
            "such as persisting an unconfirmed draft unless the supplied text explicitly requires it. "
            "Report only concrete missing REQUIRED behavior. Exclude satisfied behavior, observations marked "
            "OK or not a defect, optional refactoring, styling, extra tests, and hypothetical future bypasses. "
            "Trace frontend and backend together; server-side enforcement can satisfy a requirement. "
            "For each issue quote the exact requirement text and an exact excerpt from the supplied production "
            "source that establishes the gap. State the failing user action and missing observable behavior. "
            "Return JSON only: {\"verdict\":\"pass\"|\"repair\",\"issues\":[{\"requirement_id\":string,"
            "\"requirement_quote\":string,\"path\":string,\"evidence\":string,\"missing_behavior\":string}]}. "
            "Pass requires an empty issues array; repair requires at least one evidenced issue. "
            "Use repair if required behavior cannot be established from the source supplied."
        )}, {"role": "user", "content": json.dumps(subtree, ensure_ascii=False) + "\n" + "\n".join(sources)
             + public_acceptance_context(subtree)}]
        fingerprint = hashlib.sha256(json.dumps(messages, ensure_ascii=False,
            sort_keys=True).encode("utf-8")).hexdigest()
        cached = getattr(self, "_requirement_review_cache", None)
        if audit_complete and cached is not None and cached[0] == fingerprint:
            LOGGER.info("Requirement source unchanged; reusing the preceding audit verdict")
            return cached[1]
        # A malformed or invented verdict gets one corrective request against
        # the same immutable source view. Never turn rejected evidence into a
        # production repair instruction or cache it as approval.
        options = {"extra_body": {"thinking": {"type": "disabled"}}} if getattr(self, "is_deepseek", False) else {}
        for attempt in range(2):
            self._record_model_request()
            response = self._create_completion("requirement_review", model=self.model, messages=messages,
                                               max_tokens=6000, **options)
            self._record_usage(response, stage="requirement_review", context_chars=self._message_context_chars(messages))
            raw = response.choices[0].message.content or ""
            try:
                try:
                    self._reject_truncated_response(response, "requirement review")
                except RuntimeError as exc:
                    raise RequirementReviewError(str(exc)) from exc
                missing = self._parse_requirement_review(raw, subtree, source_contents)
            except RequirementReviewError as exc:
                if attempt == 1:
                    raise
                LOGGER.warning("Requirement audit verdict rejected; retrying once: %s", exc)
                messages.extend([
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": (
                        "Your verdict was rejected: " + str(exc) + ". Re-audit only the same supplied requirements "
                        "and production source. Use an exact supplied path and exact source excerpt, with no "
                        "invented obligation or shortened/paraphrased evidence. Do not repeat an invalid issue. "
                        "Return the required JSON schema; pass only if every required behavior is established."
                    )},
                ])
                continue
            # An unseen tail or omitted file can change while the supplied
            # prefix stays identical. Never reuse an incomplete source view.
            self._requirement_review_cache = (fingerprint, missing) if audit_complete else None
            return missing
        raise AssertionError("Requirement audit attempts exhausted")

    @staticmethod
    def _parse_requirement_review(raw: str, subtree: dict[str, Any], source_contents: dict[str, str]) -> str:
        match = re.search(r"\{[\s\S]*\}", raw)
        try:
            review = json.loads(match.group(0)) if match else {}
        except json.JSONDecodeError as exc:
            raise RequirementReviewError("Requirement reviewer returned invalid JSON") from exc
        if not isinstance(review, dict) or review.get("verdict") not in {"pass", "repair"} or not isinstance(review.get("issues"), list):
            raise RequirementReviewError("Requirement reviewer returned an invalid verdict")
        if (review["verdict"] == "pass") != (not review["issues"]):
            raise RequirementReviewError("Requirement reviewer returned a contradictory verdict")
        requirement_nodes = {}
        def collect_nodes(value):
            if isinstance(value, dict):
                if isinstance(value.get("id"), str):
                    requirement_nodes[value["id"]] = value
                for child in value.values():
                    collect_nodes(child)
            elif isinstance(value, list):
                for child in value:
                    collect_nodes(child)
        def text_values(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for key, child in value.items():
                    if key != "children":
                        yield from text_values(child)
            elif isinstance(value, list):
                for child in value:
                    yield from text_values(child)
        collect_nodes(subtree)
        missing_items = []
        for issue in review["issues"]:
            fields = ("requirement_id", "requirement_quote", "path", "evidence", "missing_behavior")
            if not isinstance(issue, dict) or any(
                not isinstance(issue.get(key), str) or not issue[key].strip() for key in fields
            ):
                raise RequirementReviewError("Requirement reviewer returned an unevidenced issue")
            node = requirement_nodes.get(issue["requirement_id"])
            quote = " ".join(issue["requirement_quote"].split())
            if node is None or not any(quote in " ".join(text.split()) for text in text_values(node)):
                raise RequirementReviewError("Requirement reviewer cited text outside the supplied requirement")
            source_path = PurePosixPath(issue["path"].replace("\\", "/"))
            source = source_contents.get(source_path.as_posix()) if not source_path.is_absolute() and ".." not in source_path.parts else None
            evidence = " ".join(issue["evidence"].split())
            if source is None or evidence not in " ".join(source.split()):
                raise RequirementReviewError("Requirement reviewer cited evidence outside the supplied production source")
            missing_items.append(
                f"{issue['requirement_id']} {source_path.as_posix()}: {issue['missing_behavior']}\n"
                f"Required: {issue['requirement_quote']}\nSource evidence: {issue['evidence']}"
            )
        return "\n".join(missing_items)

    def plan(
        self,
        task_type: str,
        subtree: dict[str, Any],
        *,
        reference_dir: Path | None = None,
    ) -> str:
        user_content: str | list[dict[str, Any]] = (
            requirement_context(task_type, subtree)
            + public_acceptance_context(subtree)
        )
        visual_inputs = self._visual_inputs(subtree, reference_dir)
        model = self.visual_model if visual_inputs else self.model
        if visual_inputs:
            user_content = [{"type": "text", "text": user_content}, *visual_inputs]

        messages = [
            {
                "role": "system",
                "content": (
                    "You are planning one software requirement subtree. First turn each leaf "
                    "requirement into observable acceptance criteria, then give a concise, "
                    "ordered implementation plan that covers every criterion. Include relevant "
                    "valid-input, invalid-input, boundary, and state-transition cases. Do not "
                    "invent requirements or rename specified labels, routes, API paths, methods, "
                    "response fields, or status codes. Do not claim code has been changed or tested. "
                    "Required system data and stated valid examples must pass. Resolve ambiguous "
                    "validation wording consistently with those examples: a count of non-whitespace "
                    "characters does not ban internal spaces when required names contain spaces. "
                    "Inspect every attached visual reference and describe the relevant layout, "
                    "controls, and visual states in the plan. Treat explicit textual behavior "
                    "as authoritative when an image is ambiguous. For each image, distinguish a "
                    "full-page reference from a component crop; do not infer unseen page content "
                    "from a crop. Include a concrete visual checklist covering hierarchy, major "
                    "regions, spacing/alignment, colors, typography, imagery, and controls. Call "
                    "out visible controls that need real behavior. Keep this checklist separate "
                    "from functional acceptance criteria. Group related scenarios and keep the "
                    "whole plan below 1,500 words; avoid restating the full requirement text."
                ),
            },
            {
                "role": "user",
                "content": user_content,
            },
        ]
        self._record_model_request()
        try:
            request_options = {"reasoning_effort": "low"} if getattr(self, "is_deepseek", False) else {}
            response = self._create_completion("planning",
                model=model, messages=messages, max_tokens=MAX_COMPLETION_TOKENS_PER_REQUEST,
                **request_options,
            )
        except ModelRequestInterrupted:
            raise
        except Exception as exc:
            if visual_inputs:
                raise RuntimeError(
                    f"Vision planning failed for {len(visual_inputs) // 2} image reference(s). "
                    "Check that VISUAL_MODEL (or MODEL) supports image input and that the "
                    "provider can access remote reference URLs."
                ) from exc
            raise
        self._record_usage(response, stage="planning", context_chars=self._message_context_chars(messages))
        if getattr(response.choices[0], "finish_reason", None) == "length":
            LOGGER.warning("Planning output was truncated; retrying once with a shorter plan request")
            self._record_model_request()
            retry_messages = [
                *messages,
                {"role": "user", "content": (
                    "The previous plan exceeded the output limit and was discarded. "
                    "Return an ordered plan below 900 words. Group similar scenarios, "
                    "preserve every explicit requirement, and omit repeated task wording."
                )},
            ]
            response = self._create_completion(
                "planning-retry", model=model, messages=retry_messages,
                max_tokens=MAX_COMPLETION_TOKENS_PER_REQUEST, **request_options,
            )
            self._record_usage(
                response, stage="planning-retry",
                context_chars=self._message_context_chars(retry_messages),
            )
        self._reject_truncated_response(response, "planning")
        content = response.choices[0].message.content or ""
        if not content.strip():
            raise RuntimeError("Model returned an empty implementation plan")
        return content.strip()

    def implement(
        self,
        *,
        task_type: str,
        subtree: dict[str, Any],
        plan: str,
        project_tools: ProjectTools,
        repair_feedback: str | None = None,
        reference_dir: Path | None = None,
        checkpoint_feedback: Callable[[], str] | None = None,
        boundary_feedback: Callable[[], str] | None = None,
        verification_feedback: Callable[[], str] | None = None,
        token_allowance: int | None = None,
        request_allowance: int | None = None,
    ) -> bool:
        """Run implementation, keeping repair fragments within an atomic request slice."""
        tool_schemas = list(TOOL_SCHEMAS)
        if verification_feedback is not None:
            tool_schemas.append({"type": "function", "function": {
                "name": "verify_candidate",
                "description": "Run the complete local build, self-tests, protected public regression and Chromium checks after an atomic edit. Does not roll back source. At most two executions per repair slice; unchanged source reuses the result. This is not formal Stage 3.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            }})
        verification_runs = 0
        verified_revision = None
        verified_feedback = ""
        visual_inputs = self._visual_inputs(subtree, reference_dir)
        if getattr(self, "visual_implementation", "enabled") == "disabled":
            visual_inputs = []
        model = self.visual_model if visual_inputs else self.model
        system_message = (
            "You are an implementation agent working in the current project directory. "
            "Implement only the supplied requirement subtree and preserve existing work. "
            "Match explicit labels, routes, roles, API contracts, and fixed values exactly. "
            "Required system data and valid examples must pass unchanged. If name length counts "
            "non-whitespace characters and required names contain spaces, count characters excluding "
            "whitespace; do not forbid internal spaces. Test the exact required example values. "
            "Write focused tests for observable behavior, and fix the implementation rather "
            "than deleting, skipping, or weakening a failing test. "
            "If a generated test contradicts the original requirement, correct its expected behavior "
            "to the requirement while retaining meaningful assertions. Fix stale mocks, relative imports "
            "and setup helpers instead of changing correct product behavior to satisfy a faulty test. "
            "Validate generated positive fixtures after adding uniqueness or retry suffixes: the "
            "entire resulting value must stay within each documented length bound. When a supposed "
            "valid fixture is rejected, inspect its returned validation error before blaming persistence. "
            "Do not invent navigation or storage-key requirements: when successful login must return "
            "home, preserve that destination even if the sign-in link carries a journey. Test account "
            "isolation through visible behavior and authenticated API ownership, not an unspecified "
            "localStorage key. Inspect the actual entry point before assuming provider order: every "
            "context consumer, including another provider, must be below its provider. "
            "Reconcile access rules across all supplied scenarios: public read-only information must remain "
            "visible when required, while protected forms and mutations still require authentication. "
            "Do not apply a page-wide authentication guard that hides information another requirement "
            "explicitly makes public. Preserve selected journey context across these boundaries. "
            "Treat every requirement as an acceptance condition, not merely a visual suggestion. "
            "When project_requirements is supplied, it describes the whole application. Preserve previously "
            "implemented modules, routes, providers, API handlers and tests when integrating this subtree. "
            "Tests must mount the actual application entry point as well as individual components; green "
            "isolated tests cannot prove a page is reachable or connected to the backend. "
            "Before editing, inspect the project and map each leaf requirement to observable "
            "behavior. Implement and test valid, invalid, boundary, and state-transition cases "
            "that the requirement implies. Add meaningful automated tests whose assertions check "
            "behavior, not just element presence or a successful render; inspect the assertions and "
            "For persistence requirements, a React remount is not a browser reload. An in-memory Map "
            "does not persist. Connect UI confirmation to the authenticated backend and durable storage; "
            "verify records after a fresh browser load and a server restart. "
            "run the relevant tests after changes. If a package defines a test script, create the "
            "referenced test files before finishing; an unmatched test glob is a failure. "
            "For a CommonJS backend using Vitest, do not require('vitest') inside tests: "
            "use globals enabled in vitest.config.js, or write ESM tests with imports. "
            "When writing DOM tests, follow the actual API "
            "contracts: getByLabelText matches the rendered label, not the input id or name. "
            "For real form controls, preserve meaningful visible labels and the exact required "
            "accessible names. getByLabelText may include required-marker asterisks inside labels "
            "even when the marker has aria-hidden=true. Give the control an explicit aria-label "
            "matching its required name, or render the marker with CSS outside the DOM label text; "
            "preserve required validation and the connected form behavior. "
            "Use selectOptions for selects, not type; use change events for date inputs when user typing "
            "does not work in jsdom. Reread both the failing test helper and the component before editing "
            "a shared helper, and update every call site when changing its argument shape. "
            "In jsdom, import.meta.url can be an HTTP URL: do not pass it to Node readFileSync. "
            "Use an explicit filesystem path based on the frontend test working directory for source checks, "
            "or mount the entry point directly. Keep test assertions and application behavior intact. "
            "EventTarget.dispatchEvent returns a boolean, not "
            "the Event object. Retain the Event instance to assert defaultPrevented, or assert the "
            "dispatchEvent boolean; never read defaultPrevented from its boolean return value. "
            "Follow the authentication mode explicitly stated "
            "by the task: a local-only mock may show demo success after format validation, while "
            "real authentication must verify credentials against the specified backend and must "
            "not accept arbitrary well-formed credentials. "
            "When visual references are provided, inspect and use them during implementation "
            "(not only during planning). Match the depicted page or component closely while "
            "respecting written behavior. Do not add prominent sections or imagery absent from "
            "the reference unless the written requirements require them. A visual reference may "
            "be a crop: reproduce that component without treating the crop as a whole-page design. "
            "Every element styled or labeled as a link, menu, button, selector, or form control "
            "must have working behavior; use semantic interactive elements, and do not leave "
            "action-looking text or images inert. Optional visual references guide appearance and do not "
            "create additional mandatory acceptance conditions. For mandatory visual-reference tasks, write a root-level "
            "`arcbench-visual-acceptance.json` manifest following the schema supplied in the user "
            "message. It must map each reference to its route and viewport, record visual "
            "expectations, and define browser interaction flows with observable assertions. "
            "Also ensure the project has a `start` script that binds to the PORT environment "
            "variable so the runner can open the generated app in a headless browser. "
            "For every Web task, the project root MUST contain both `frontend/` and `backend/` "
            "directories because the ARC-Bench Web runner requires this layout. Put the browser "
            "application, pages, styles, and client-side assets in `frontend/`; put the server, "
            "API routes, business logic, and persistence code in `backend/`. The frontend must "
            "have its own `frontend/package.json` with a non-empty `scripts.build` command and "
            "all dependencies needed to build independently after the runner installs frontend "
            "dependencies. If the starter lacks this manifest, create it in the first write "
            "batch before running build or test scripts. If the root test script names "
            "`backend/tests`, create real executable tests there before running it. "
            "Complete the first scaffold as one coherent write batch: include every relative import, "
            "stylesheet and asset referenced by the entry and its components. Use .jsx/.tsx for files "
            "containing JSX and keep import paths consistent, rather than assuming the default Vite "
            "pipeline parses JSX in .js. Check these inputs before the first frontend build. "
            "Check the actual test compiler too: a separate vitest.config can override Vite's React "
            "plugin, so JSX transformation must be configured for both build and test execution. "
            "Run and pass the frontend build from `frontend/`. Keep a usable root-level "
            "package manifest and `start` script that binds to PORT and launches the generated "
            "application; if the root manifest defines `build`, it must also succeed. Do not "
            "substitute `public/`, `server/`, or empty placeholder directories for the required "
            "frontend and backend implementation. "
            "Use tools to inspect before editing. Paths are relative to the project root. "
            "Use `read_files` to inspect code, `write_files` to create related files, and "
            "`replace_text` to edit an existing source file when old_text has one exact match. "
            "If replacement reports zero or multiple matches, reread the relevant file before retrying. "
            "Batch related file inspections into one `read_files` call and related file updates into one `write_files` call. "
            "If a batch read is truncated, use read_file with start_line/end_line around the reported failure; "
            "do not infer unseen code or repeatedly read only the beginning of a large file. "
            "Each batch accepts at most 20 files; read_files returns at most 40,000 characters (or the configured lower limit), and write_files accepts "
            "at most 30,000 characters per file and 120,000 characters total. Keep batches within these limits and "
            "do not split a related change into many small tool calls. Group up to three relevant build/test scripts "
            "into one `run_project_scripts` call so you can inspect all results together. "
            "During edits, run frontend/build and backend/test directly; leave the root scripts "
            "for final verification so delegated scripts are not run twice each checkpoint. "
            "Root build/test scripts must run on both Windows and Linux. Node spawnSync/execFileSync "
            "cannot directly launch npm.cmd on Windows: use a compatible launch with safe fixed arguments, "
            "and propagate spawn errors as well as nonzero status. A green child script does not prove "
            "its root wrapper succeeds. "
            "Do not access .arc, .git, dependencies, or files outside the project. "
            "Do not claim a build or test passed unless its run_project_script(s) result has exit_code 0. "
            "Before changing production behavior for a failed test, inspect its assertion and setup against "
            "the supplied requirement. Fix an incorrect assertion while preserving the intended behavioral "
            "coverage; never add hidden UI just to satisfy a selector, skip tests, or weaken required behavior. "
            "Tests that assert an element is absent must use a non-throwing query. For browser-state tests, "
            "isolate localStorage, sessionStorage, mocks and pending async work between independent tests; "
            "preserve storage only within the same explicit reload scenario. Run the failing test alone and "
            "then the complete suite to distinguish a business defect from order-dependent test pollution. "
            "Clear account-owned booking/confirmation state when signing out or switching users, and clear "
            "previous journey state when changing a selected train/date. Cancel or ignore stale API responses "
            "so old account or journey details cannot reappear. "
            "Render a fact in one authoritative text node: a station containing its city name plus "
            "a separate city heading causes ambiguous visible substring matches. Keep full city and "
            "full station together and time in its own semantic node. Use a neutral confirmation "
            "heading and one success notice containing the concrete Booking number: identifier. "
            "Do not repeat train/date facts in both journey and confirmation summaries. Validation "
            "messages must explain the actual invalid condition and boundary (at least/at most), "
            "not just a numeric range; retain all original validation rules. "
            "A proposed repair must change the failing behavior, not merely add a comment. After "
            "checkpoint rollback reread current source before reapplying any patch. "
            "Signout must send the old credential to the actual logout API while clearing private "
            "UI immediately; do not erase the credential before constructing the request. Pending "
            "logout must not redirect or clear a newly signed-in session. On booking reload validate "
            "the saved record owner, train and date before displaying it, and invalidate confirmation "
            "work on unmount or identity changes. In self-tests parse the real Authorization header "
            "format (including Bearer), destroy the prior component tree for an actual reload, and "
            "navigate to search before iterating the next route. "
            "Async effects must also work under StrictMode setup/cleanup and document reloads. "
            "Use an effect-local disposed flag and an operation generation to ignore stale success "
            "and failure callbacks, including sign-in, sign-out and navigation; reset lifecycle state "
            "on each setup. For fetch work shared with jsdom/Node, prefer this response invalidation "
            "over cross-realm AbortController signals, which can reject before an HTTP request. "
            "If cancellation is used, handle abort rejections on every derived promise without "
            "suppressing genuine request errors. A JSON report with passing assertions and nonzero "
            "exit is still failed: inspect asynchronous errors; never ignore unhandled errors. "
            "The runner prepared the target project; do not copy or replace a starter template. "
            "When implementation is complete, provide a short summary."
        )
        if verification_feedback is not None:
            system_message += (
                " During this repair use verify_candidate after completing related source edits, "
                "before declaring completion. It returns the current protected behavioral failures "
                "as well as self-tests, without reverting your edits. A generated self-test alone "
                "cannot establish that the active requirement is repaired. Reserve requests to "
                "read this result and correct remaining failures in the active scope."
            )
        text_message = (
            requirement_context(task_type, subtree)
            + f"\n\nImplementation plan:\n{plan}"
            + generation_contract_context(subtree)
            + public_acceptance_context(subtree)
        )
        if collect_visual_references(subtree):
            text_message += (
                "\n\nVisual acceptance manifest schema (required at project root):\n"
                '{"version":1,"cases":[{"name":"stable-case-id","reference":"exact task image reference",'
                '"route":"/path","viewport":{"width":1440,"height":900},'
                '"target_selector":"optional CSS selector for a reference crop",'
                '"full_page":false,"setup":{"route":"/login","steps":[...]},'
                '"visual_expectations":["observable appearance expectation"]}],'
                '"flows":[{"name":"user flow","route":"/path","viewport":{"width":1440,"height":900},'
                '"covers":["#selector-for-every-visible-actionable-control"],'
                '"steps":[{"action":"fill|click|check|uncheck|select_option|navigate|expect_navigation|expect_visible|expect_hidden|expect_text|expect_value|expect_checked|expect_unchecked|wait_for_url",'
                '"selector":"CSS selector when needed","value":"value or expected text when needed"}]}]}\n'
                "In setup.steps use the same action-object schema as flows.steps; never use plain strings. "
                "When an existing manifest is present, retain its cases and flows for earlier modules. "
                "Use only references from the supplied task, list every reference exactly once, and include "
                "all visible actionable controls in tested flows, with one unique selector per control. "
                "Pair every action immediately with an assertion that proves a state change (for example, "
                "a newly visible result, changed field value, checked state, hidden menu, changed URL, or "
                "a document navigation/reload when clicking a same-page link), "
                "not merely that the control exists. Use navigate with an immediate wait_for_url assertion "
                "to reset to a local route between testing navigation links. Use a case setup flow "
                "when a captured page needs prior sign-in state. The manifest is runtime input for "
                "the Agent's browser verification, not a claim that verification has passed."
            )
        repair_text = (
            "Verification failed. Fix the root cause without weakening tests, then rerun the "
            f"relevant build and full existing test suite:\n{repair_feedback}"
            if repair_feedback else ""
        )
        first_dynamic_content: str | list[dict[str, Any]] | None = None
        if visual_inputs:
            first_dynamic_content = [*visual_inputs]
            if repair_text:
                first_dynamic_content.insert(0, {"type": "text", "text": repair_text})
                first_dynamic_content.extend(self._repair_visual_inputs(subtree, project_tools))
        elif repair_text:
            first_dynamic_content = repair_text
        # Keep the image payload only for the first implementation request. The plan
        # already contains a textual visual checklist, so replaying base64 images on
        # every tool turn adds substantial prompt cost without new information.
        compact_user_content = repair_text
        if visual_inputs:
            compact_user_content += (
                "\n\nThe visual references and any repair screenshots were supplied in the first "
                "request. Use their analyzed constraints from the plan and this task; do not ask "
                "for them to be resent."
            )
        system_msg = {"role": "system", "content": system_message}
        # Keep the full task in an identical message at the front of every request.
        # Dynamic images, verification feedback, and progress follow that prefix.
        task_msg = {"role": "user", "content": text_message}
        conversation: list[dict[str, Any]] = [system_msg, task_msg]
        if first_dynamic_content:
            conversation.append({"role": "user", "content": first_dynamic_content})
        inventory = initial_file_inventory(project_tools)
        if inventory:
            conversation.append({"role": "user", "content": inventory})
        epoch_exchanges: list[list[dict[str, Any]]] = []
        epoch_reads: set[tuple[str, str]] = set()
        read_evidence = ReadEvidence()
        compacted_summaries: list[str] = []
        checkpoint_note = ""
        budget_warned = False
        failed_tool_signature: tuple[tuple[str, str, str], ...] | None = None
        identical_failed_turns = 0
        progress_paths = getattr(project_tools, "changed_paths", getattr(project_tools, "written_paths", []))
        initial_writes = len(progress_paths)
        project_dir = getattr(project_tools, "project_dir", None)
        resumed_project = isinstance(project_dir, Path) and any(
            path.is_file()
            for folder in ("frontend", "backend")
            for path in (project_dir / folder / "src").rglob("*")
        )
        initial_bootstrap = (task_type == "web" and isinstance(project_dir, Path)
            and not resumed_project and all((project_dir / name).is_dir() for name in ("frontend", "backend")))
        bootstrap_incomplete = False
        checkpoint_revision: tuple[int, int] | None = None
        observed_writes = initial_writes
        idle_tool_turns = 0
        passing_checkpoint = False
        passing_checkpoint_turn: int | None = None
        write_only_prompted = False
        thinking_disabled_epoch = False
        scope_key = self._implementation_scope_key(task_type, subtree, project_dir, model)
        self.last_implementation_handoff = False
        self.last_implementation_deferred = False
        self.last_implementation_stop_reason = "error"
        self.last_implementation_stop_detail = ""
        if repair_feedback:
            caller_request_allowance = request_allowance
            request_allowance = min(request_allowance, MAX_REPAIR_REQUESTS) if request_allowance is not None else MAX_REPAIR_REQUESTS
            LOGGER.info("Atomic repair slice: request_limit=%d caller_request_allowance=%s token_allowance=%s",
                        request_allowance, caller_request_allowance, token_allowance)
            conversation.append({"role": "user", "content": (
                f"This repair has at most {request_allowance} model requests. Complete related file edits "
                "atomically before finishing; the controller will run the complete protected regression "
                "at the end of this repair slice. Do not leave a partially migrated API or component contract."
            )})
        starting_tokens = getattr(self, "prompt_tokens_used", 0) + getattr(self, "completion_tokens_used", 0)
        starting_requests = getattr(self, "model_requests_used", 0)
        allowance_warned = False
        fixed_failure_signature: tuple[str, ...] = ()
        unchanged_fixed_checkpoints = 0
        observed_source_revision = getattr(project_tools, "source_revision", 0)
        source_failure_stamp = (observed_source_revision, len(progress_paths))
        source_failures: dict[tuple[str, str], int] = {}
        source_failure_details: dict[tuple[str, str], str] = {}
        required_source_reads: dict[str, str] = {}
        written_context_start = 0
        restore_notice = ""

        def reset_changed_source_failures() -> None:
            nonlocal source_failure_stamp
            stamp = (getattr(project_tools, "source_revision", 0), len(progress_paths))
            if stamp[0] != source_failure_stamp[0]:
                source_failures.clear()
                source_failure_details.clear()
                required_source_reads.clear()
            elif stamp[1] != source_failure_stamp[1]:
                changed = {_normalized_source_path(path) for path in progress_paths[source_failure_stamp[1]:]
                           if isinstance(path, str)}
                for path in changed:
                    required_source_reads.pop(path, None)
                    for key in tuple(source_failures):
                        if key[0] == path:
                            source_failures.pop(key)
                            source_failure_details.pop(key, None)
            source_failure_stamp = stamp

        def note_source_failure(key: tuple[str, str], detail: str) -> None:
            source_failures[key] = source_failures.get(key, 0) + 1
            source_failure_details[key] = detail
            if source_failures[key] >= MAX_IDENTICAL_FAILED_TOOL_TURNS - 1:
                required_source_reads[key[0]] = key[1]
                # Cached reads cannot establish fresh source after failed edits.
                epoch_reads.clear()
                read_evidence.clear()

        def clear_fresh_source_reads(name: str, arguments: dict[str, Any], result: str) -> None:
            refreshed = set()
            if name == "read_file" and isinstance(arguments.get("path"), str):
                # A real bounded range can contain the required unique anchor.
                # An empty out-of-range read contains no fresh source evidence.
                if result or (arguments.get("start_line", 1) == 1 and arguments.get("end_line") is None):
                    refreshed.add(_normalized_source_path(arguments["path"]))
            elif name == "read_files":
                try:
                    payload = json.loads(result)
                    for file in payload.get("files", []):
                        if not isinstance(file, dict) or not isinstance(file.get("path"), str) or not isinstance(file.get("content"), str):
                            continue
                        content = file["content"]
                        for marker in ("\n...[file truncated]", "\n...[batch output limit reached]"):
                            content = content.removesuffix(marker)
                        if content or not payload.get("truncated"):
                            refreshed.add(_normalized_source_path(file["path"]))
                except (TypeError, ValueError, AttributeError):
                    pass
            for path in refreshed:
                if path not in required_source_reads and not any(key[0] == path for key in source_failures):
                    continue
                required_source_reads.pop(path, None)
                for key in tuple(source_failures):
                    if key[0] == path:
                        source_failures.pop(key)
                        source_failure_details.pop(key, None)
                read_evidence.invalidate([path])
            if name == "list_files" and isinstance(arguments.get("path"), str):
                parent = _normalized_source_path(arguments["path"])
                for path, family in tuple(required_source_reads.items()):
                    if family == "missing_file" and (parent == "." or path.startswith(parent.rstrip("/") + "/")):
                        # Discovery does not make a retry of the missing file new.
                        required_source_reads.pop(path)

        def source_refresh_notice() -> str:
            paths = ", ".join(required_source_reads)
            return ("The same source file failed twice without a source change: " + paths + ". "
                "Change the tool arguments only after a fresh read_file/read_files of the affected existing file, "
                "using a bounded line range for a large file, "
                "or list_files of the missing path's parent to discover the real path. "
                "Unrelated reads and no-op writes do not resolve this failure. Another unrefreshed "
                "failure will end this implementation pass for full boundary verification.")

        def compact_context() -> list[dict[str, Any]]:
            nonlocal thinking_disabled_epoch
            # Reset only at a context boundary. Between resets, append complete
            # turns so the provider can reuse the entire previous request prefix.
            # Retain a bounded suffix of complete exchanges so a just-read source
            # anchor survives compaction. Dropping every payload made the next
            # turn reread files instead of using the evidence it had just fetched.
            retained: list[list[dict[str, Any]]] = []
            retained_chars = 0
            for exchange in reversed(epoch_exchanges[-2:]):
                size = self._message_context_chars(exchange)
                if retained_chars + size > min(80_000, MAX_CONVERSATION_CHARS // 3):
                    break
                retained.insert(0, exchange)
                retained_chars += size
            older_exchanges = epoch_exchanges[:-len(retained)] if retained else epoch_exchanges
            for older in older_exchanges:
                summary = self._compact_tool_exchange(older[0], older[1:])
                if summary:
                    compacted_summaries.append(summary)
            del compacted_summaries[:-MAX_COMPACTED_EXCHANGES]
            epoch_exchanges[:] = retained
            epoch_reads.clear()
            read_evidence.retain(retained)
            if not retained:
                thinking_disabled_epoch = False
            parts = [compact_user_content] if compact_user_content else []
            if checkpoint_note:
                parts.append("Latest checkpoint verification feedback:\n" + checkpoint_note)
            if restore_notice:
                parts.append(restore_notice)
            if compacted_summaries:
                ledger = "\n".join(
                    f"{index + 1}. {summary}"
                    for index, summary in enumerate(compacted_summaries)
                )
                written_paths = list(dict.fromkeys(getattr(project_tools, "written_paths", [])[written_context_start:]))
                if written_paths:
                    shown_paths = written_paths[-60:]
                    ledger += "\nFiles already written: " + ", ".join(shown_paths)
                    if len(written_paths) > len(shown_paths):
                        ledger += f" (and {len(written_paths) - len(shown_paths)} earlier files)"
                parts.append("Compact progress ledger from earlier tool exchanges:\n" + ledger)
            compacted: list[dict[str, Any]] = [system_msg, task_msg]
            if parts:
                compacted.append({"role": "user", "content": "\n\n".join(parts)})
            for exchange in retained:
                compacted.extend(exchange)
            return compacted

        def rebase_restored_source() -> None:
            nonlocal conversation, observed_source_revision, written_context_start, restore_notice
            nonlocal passing_checkpoint, passing_checkpoint_turn
            revision = getattr(project_tools, "source_revision", 0)
            if revision == observed_source_revision:
                return
            observed_source_revision = revision
            reset_changed_source_failures()
            # A controller restore invalidates successful edits as well as reads.
            # Drop the entire old protocol segment and progress ledger together;
            # retaining either could make the model patch source that was undone.
            epoch_exchanges.clear()
            compacted_summaries.clear()
            written_context_start = len(getattr(project_tools, "written_paths", []))
            restore_notice = (
                "The controller restored an earlier candidate. Earlier source reads, successful writes "
                "and progress summaries were discarded; they do not describe the current files. "
                "Reread the current source before making another edit."
            )
            passing_checkpoint = False
            passing_checkpoint_turn = None
            conversation = compact_context()

        def finish_implementation(reason: str, *, exhausted: bool = False,
                                  deferred: bool = False, handoff: bool = False,
                                  detail: str = "", checked_feedback: str | None = None) -> bool:
            nonlocal checkpoint_note
            # Every module/repair stop has an enforcement boundary. Periodic
            # diagnostics must not undo a migration before related edits finish.
            checker = boundary_feedback if boundary_feedback is not None else checkpoint_feedback
            if checker is not None:
                if checked_feedback is not None:
                    checkpoint_note = checked_feedback
                else:
                    try:
                        checkpoint_note = checker()
                    except ModelRequestInterrupted:
                        raise
                    except Exception as exc:
                        checkpoint_note = f"Checkpoint verification failed: {type(exc).__name__}: {exc}"
                rebase_restored_source()
                if reason == "model_finished" or handoff:
                    handoff = checkpoint_note.startswith("Current build and test scripts pass.")
                    deferred = not handoff
                    if deferred:
                        reason = "checkpoint_failed"
                detail = detail or checkpoint_note[:300]
            if self.last_budget_report is not None:
                exhausted = True
                reason = self.last_budget_report.budget
            self.last_implementation_handoff = handoff and not exhausted
            self.last_implementation_deferred = deferred and not exhausted
            self.last_implementation_stop_reason = reason
            self.last_implementation_stop_detail = detail
            return exhausted

        self.last_budget_report = None
        for turn_index in count():
            reset_changed_source_failures()
            try:
                self._check_model_budget(reserve_for_delivery=True)
            except ModelBudgetExceeded:
                return finish_implementation("model_budget", exhausted=True)
            phase_tokens = getattr(self, "prompt_tokens_used", 0) + getattr(self, "completion_tokens_used", 0) - starting_tokens
            phase_requests = getattr(self, "model_requests_used", 0) - starting_requests
            if (token_allowance is not None and phase_tokens >= token_allowance) or (request_allowance is not None and phase_requests >= request_allowance):
                LOGGER.warning("Module allocation reached (tokens=%d requests=%d); deferring unfinished work to whole-project integration", phase_tokens, phase_requests)
                return finish_implementation("allocation", deferred=True, detail=(
                    f"phase_tokens={phase_tokens} token_allowance={token_allowance}; "
                    f"phase_requests={phase_requests} request_allowance={request_allowance}; "
                    f"atomic_repair={bool(repair_feedback)}"))
            if token_allowance is not None and not allowance_warned and phase_tokens >= token_allowance * .75:
                allowance_warned = True
                conversation.append({"role": "user", "content": "This module has little allocated budget left. Finish its required application wiring now; leave unresolved failures for whole-project integration. Avoid expanding tests or optional styling."})
            # Give a green module two final turns, then verify its latest files.
            # Optional edits must not indefinitely postpone the next module.
            if passing_checkpoint_turn is not None and turn_index >= passing_checkpoint_turn + 2:
                try:
                    checker = boundary_feedback if boundary_feedback is not None else checkpoint_feedback
                    feedback = checker() if checker is not None else "No checkpoint available"
                except ModelRequestInterrupted:
                    raise
                except Exception as exc:
                    feedback = f"Checkpoint verification failed: {type(exc).__name__}: {exc}"
                if feedback.startswith("Current build and test scripts pass."):
                    LOGGER.info("Implementation final checkpoint passed; moving to the next requirement")
                    return finish_implementation("checkpoint_passed", handoff=True, checked_feedback=feedback)
                passing_checkpoint_turn = None
                passing_checkpoint = False
                checkpoint_note = feedback
                conversation.append({"role": "user", "content": "Final module checkpoint failed; repair these failures:\n" + feedback})
                rebase_restored_source()
            if self.max_tool_calls is not None and self.tool_calls_used >= self.max_tool_calls:
                self.last_budget_report = BudgetExhaustion(
                    budget="tool_call_budget",
                    limit=self.max_tool_calls,
                    used=self.tool_calls_used,
                    requested=0,
                    tool_names=(),
                    turns_used=turn_index,
                    tool_calls_used=self.tool_calls_used,
                    model_requests_used=self.model_requests_used,
                    prompt_tokens_used=self.prompt_tokens_used,
                    completion_tokens_used=self.completion_tokens_used,
                )
                return finish_implementation("tool_call_budget", exhausted=True)
            checkpoint_due = not repair_feedback and ((
                self.max_turns is None
                and len(progress_paths) > initial_writes
                and turn_index >= EARLY_CHECKPOINT_TURN
                and (turn_index - EARLY_CHECKPOINT_TURN) % CHECKPOINT_INTERVAL_TURNS == 0
            ) or (
                self.max_turns is not None
                and turn_index == max(1, self.max_turns - 8)
            ))
            current_checkpoint_revision = (len(progress_paths), getattr(project_tools, "source_revision", 0))
            if (checkpoint_feedback is not None and checkpoint_due
                    and (not initial_bootstrap or current_checkpoint_revision != checkpoint_revision)):
                try:
                    gaps = initial_bootstrap_gaps(project_dir) if initial_bootstrap else ()
                except Exception as exc:
                    # A scheduling optimization must never suppress the real verifier
                    # when the cheap source inspection is unavailable.
                    LOGGER.info("Initial bootstrap diagnostic unavailable (%s); using full checks", type(exc).__name__)
                    gaps = ()
                bootstrap_incomplete = bool(gaps)
                try:
                    if gaps:
                        feedback = ("Initial scaffold is incomplete; full periodic checks have not run. "
                            "Complete these declared inputs before the next build:\n- " + "\n- ".join(gaps))
                        LOGGER.info("Initial bootstrap diagnostic: %d missing/invalid declared inputs; periodic checks deferred", len(gaps))
                    else:
                        feedback = checkpoint_feedback()
                    checkpoint_revision = current_checkpoint_revision
                except ModelRequestInterrupted:
                    raise
                except Exception as exc:
                    feedback = f"Checkpoint verification could not run: {type(exc).__name__}: {exc}"
                passing_checkpoint = feedback.startswith("Current build and test scripts pass.")
                # Stop a broad implementation pass when the independent oracle
                # repeats the same assertions. Return to the orchestrator's
                # bounded repair context; this is neither success nor exhaustion.
                fixed_failures = _fixed_regression_signature(feedback)
                if "public interaction regression (reconstructed jsdom, not Stage 3): FAILED" in feedback and fixed_failures:
                    unchanged_fixed_checkpoints = unchanged_fixed_checkpoints + 1 if fixed_failures == fixed_failure_signature else 1
                    fixed_failure_signature = fixed_failures
                    if unchanged_fixed_checkpoints >= 3:
                        LOGGER.warning("Fixed regression assertions unchanged across three checkpoints; deferring to focused verification/repair")
                        return finish_implementation("unchanged_checkpoint", deferred=True,
                            checked_feedback=feedback if boundary_feedback is None else None)
                else:
                    fixed_failure_signature = ()
                    unchanged_fixed_checkpoints = 0
                if passing_checkpoint and passing_checkpoint_turn is None:
                    passing_checkpoint_turn = turn_index
                    conversation.append({"role": "user", "content": "Local module checks pass. You have two final turns to finish required behavior and report any uncovered acceptance criteria. Defer optional test expansion or styling polish; the agent will recheck the files and proceed to the next module."})
                if feedback and feedback != checkpoint_note:
                    checkpoint_note = feedback
                    # Keep the existing request prefix cacheable across checkpoints.
                    # A new feedback message supersedes older feedback without
                    # forcing every file read back into an uncached summary.
                    conversation.append({"role": "user", "content": "Latest checkpoint verification feedback (supersedes earlier feedback):\n" + feedback})
            rebase_restored_source()
            remaining_requests = self.max_model_requests - getattr(self, "model_requests_used", 0)
            remaining_tokens = self.max_total_tokens - getattr(self, "prompt_tokens_used", 0) - getattr(self, "completion_tokens_used", 0)
            remaining_turns = None if self.max_turns is None else self.max_turns - turn_index
            remaining_tools = (
                None if self.max_tool_calls is None
                else self.max_tool_calls - getattr(self, "tool_calls_used", 0)
            )
            if not budget_warned and (
                remaining_requests <= 3
                or remaining_tokens <= self.max_total_tokens // 10
                or (remaining_turns is not None and remaining_turns <= max(1, min(3, self.max_turns // 5)))
                or (remaining_tools is not None and remaining_tools <= max(1, min(5, self.max_tool_calls // 5)))
            ):
                budget_warned = True
                conversation.append({
                    "role": "user",
                    "content": (
                        f"Budget notice: {remaining_tokens} total tokens remain; generation will stop "
                        "with a delivery reserve. Finish application wiring and repair the current failures. "
                        "Do not expand tests or optional styling, weaken requirements, or remove failing assertions. "
                        "Report unresolved failures honestly; local checks and formal acceptance are separate."
                    ),
                })
            refresh_target = next(iter(required_source_reads.items()), None)
            refresh_tool = ("read_file" if refresh_target[1] == "replacement_match" else "list_files") if refresh_target else None
            # An optional tool can be ignored until the slice runs out. Reserve
            # the third-last request for fresh feedback, leaving two for repair.
            verification_due = (
                verification_feedback is not None and request_allowance is not None
                and request_allowance - phase_requests == 3 and verification_runs < 2
                and len(progress_paths) > initial_writes and refresh_tool is None
                and verified_revision != (len(progress_paths), getattr(project_tools, "source_revision", 0))
            )
            write_only = (idle_tool_turns >= getattr(self, "max_idle_tool_turns", DEFAULT_MAX_IDLE_TOOL_TURNS) // 2
                          and not passing_checkpoint and refresh_tool is None and not verification_due)
            edit_tool = ("write_files" if bootstrap_incomplete else
                "replace_text" if (resumed_project or repair_text or checkpoint_note or len(progress_paths) > initial_writes) else "write_files")
            if write_only and not write_only_prompted:
                write_only_prompted = True
                conversation.append({
                    "role": "user",
                    "content": (
                        "You have inspected the project repeatedly without changing a file. "
                        "The next step must be a concrete source or behavior-test edit for this "
                        f"requirement. Prefer {edit_tool} for the focused change; "
                        "write_file or write_files may create missing source or behavior tests. "
                        "Make a concrete edit now; do not read or list files again. "
                        "Current check failures:\n" + checkpoint_note[:2_000]
                    ),
                })
            if verification_due:
                conversation.append({"role": "user", "content": (
                    "Three requests remain in this repair slice. Run verify_candidate now on the current "
                    "edits; use the remaining two requests to correct its feedback. This diagnostic does "
                    "not roll back incomplete edits. Complete related file changes before finishing."
                )})
            forced_tool = refresh_tool or ("verify_candidate" if verification_due else edit_tool if write_only else None)
            messages = list(conversation)
            try:
                self._record_model_request()
            except ModelBudgetExceeded:
                return finish_implementation("model_budget", exhausted=True)
            context_chars = self._message_context_chars(messages)
            request_options: dict[str, Any] = {}
            max_tokens = self._edit_output_cap(model)
            api_forced_tool = forced_tool
            if getattr(self, "is_deepseek", False):
                # Keep DeepSeek thinking enabled for implementation by default.
                # Retained tool-call turns must include their reasoning_content.
                thinking = ("disabled" if thinking_disabled_epoch or scope_key in self._nonthinking_scopes
                    else (getattr(self, "implementation_thinking", "") or "enabled"))
                request_options["extra_body"] = {
                    "thinking": {"type": thinking}
                }
                if thinking == "enabled":
                    # DeepSeek counts reasoning and visible output against the same limit.
                    # Low effort preserves thinking while leaving room for tool calls.
                    request_options["reasoning_effort"] = "low"
                    max_tokens = DEEPSEEK_THINKING_IMPLEMENTATION_MAX_TOKENS
                    # Named tool_choice is unsupported in thinking mode. Enforce
                    # the required action locally instead of disabling reasoning
                    # for every later turn that retains this tool conversation.
                    api_forced_tool = None
            if getattr(self, "is_glm", False) and self.implementation_thinking:
                request_options["extra_body"] = {"thinking": {"type": self.implementation_thinking}}
            try:
                response = self._create_completion("implementation",
                    model=model,
                    messages=messages,
                    tools=tool_schemas,
                    tool_choice={"type": "function", "function": {"name": api_forced_tool}}
                        if api_forced_tool else "auto",
                    max_tokens=max_tokens,
                    **request_options,
                )
            except (ModelRequestInterrupted, APIStatusError):
                # A provider status (balance/auth/rate limit/invalid request)
                # is actionable evidence, not a diagnosis of vision support.
                # Preserve it and stop; do not retry or switch models here.
                raise
            except Exception as exc:
                if visual_inputs:
                    raise RuntimeError(
                        "Vision-enabled implementation failed. Check that VISUAL_MODEL "
                        "(or MODEL) supports both image input and tool calling."
                    ) from exc
                raise
            self._record_usage(response, stage="implementation", context_chars=context_chars)
            if (
                getattr(response.choices[0], "finish_reason", None) == "length"
                and getattr(self, "is_deepseek", False)
                and thinking == "enabled"
            ):
                # Discard the incomplete output and allow one smaller, focused
                # continuation. Never execute tool calls from a truncated reply.
                if scope_key is not None and self._pure_reasoning_length(response):
                    self._nonthinking_scopes.add(scope_key)
                    LOGGER.info("Pure reasoning output limit: keeping this requirement scope nonthinking across compaction and continuation")
                LOGGER.warning("DeepSeek implementation output was truncated; retrying one focused tool turn")
                phase_requests = getattr(self, "model_requests_used", 0) - starting_requests
                phase_tokens = getattr(self, "prompt_tokens_used", 0) + getattr(self, "completion_tokens_used", 0) - starting_tokens
                if (request_allowance is not None and phase_requests >= request_allowance) or (token_allowance is not None and phase_tokens >= token_allowance):
                    LOGGER.warning("No repair allocation remains for a truncation retry; returning to verification")
                    return finish_implementation("allocation", deferred=True, detail=(
                        f"truncated response discarded; phase_requests={phase_requests} "
                        f"request_allowance={request_allowance}; phase_tokens={phase_tokens} "
                        f"token_allowance={token_allowance}"))
                try:
                    self._record_model_request()
                except ModelBudgetExceeded:
                    return finish_implementation("model_budget", exhausted=True)
                retry_note = {
                    "role": "user",
                    "content": (
                        "Your previous response exceeded the output limit and was discarded. "
                        + (f"First complete the required {refresh_tool} of the affected source. " if refresh_tool else
                           "Run verify_candidate now before making further edits. " if verification_due else
                           "Use the files already inspected. Make one concrete, small tool call now (prefer write_files or replace_text). ")
                        + "Use at most 4,000 characters of new content. Do not repeat the analysis or reread the whole project."
                    ),
                }
                conversation.append(retry_note)
                retry_messages = [*messages, retry_note]
                # A second thinking request can spend its entire output budget
                # reasoning again. Request a bounded edit without thinking, and
                # keep the subsequent tool conversation in the same mode.
                thinking = "disabled"
                request_options["extra_body"] = {"thinking": {"type": thinking}}
                request_options.pop("reasoning_effort", None)
                response = self._create_completion(
                    "implementation-retry",
                    model=model,
                    messages=retry_messages,
                    tools=tool_schemas,
                    tool_choice={"type": "function", "function": {"name": forced_tool or edit_tool}},
                    max_tokens=self._edit_output_cap(model),
                    **request_options,
                )
                self._record_usage(
                    response,
                    stage="implementation-retry",
                    context_chars=self._message_context_chars(retry_messages),
                )
            if (
                getattr(response.choices[0], "finish_reason", None) == "length"
                and getattr(self, "is_deepseek", False)
            ):
                # Preserve the last complete candidate and let the orchestrator
                # verify it and route the remaining failures. Neither partial
                # tools nor an output cap are evidence of successful delivery.
                LOGGER.warning(
                    "DeepSeek implementation output remains truncated; discarding partial "
                    "tools and handing the current candidate to verification"
                )
                partial = response.choices[0].message
                calls = partial.tool_calls or []
                LOGGER.warning('Discarded output shape: content_chars=%d reasoning_chars=%d '
                    'tool_count=%d tool_argument_chars=%s', len(partial.content or ''),
                    len(getattr(partial, 'reasoning_content', None) or ''), len(calls),
                    [len(call.function.arguments or '') for call in calls])
                return finish_implementation("truncated", deferred=True)
            if getattr(response.choices[0], "finish_reason", None) == "length":
                finish_implementation("truncated", deferred=True)
            self._reject_truncated_response(response, "implementation")
            assistant_message = response.choices[0].message
            if not assistant_message.tool_calls:
                if refresh_tool:
                    return finish_implementation("repeated_tool_failure", deferred=True, detail=source_refresh_notice())
                if write_only:
                    LOGGER.warning("Focused edit request returned no file edit tool call; handing off for verification")
                    return finish_implementation("no_progress", deferred=True)
                return finish_implementation("model_finished")
            if getattr(self, "is_deepseek", False) and thinking == "disabled":
                # A non-thinking tool call has no reasoning_content. Keep this
                # conversation segment non-thinking until compaction discards it.
                thinking_disabled_epoch = True
            tool_calls = list(assistant_message.tool_calls)
            remaining = None if self.max_tool_calls is None else self.max_tool_calls - self.tool_calls_used
            if remaining is not None and len(tool_calls) > remaining:
                self.last_budget_report = BudgetExhaustion(
                    budget="tool_call_budget",
                    limit=self.max_tool_calls,
                    used=self.tool_calls_used,
                    requested=len(tool_calls),
                    tool_names=tuple(call.function.name for call in tool_calls),
                    turns_used=turn_index + 1,
                    tool_calls_used=self.tool_calls_used,
                    model_requests_used=self.model_requests_used,
                    prompt_tokens_used=self.prompt_tokens_used,
                    completion_tokens_used=self.completion_tokens_used,
                )
                return finish_implementation("tool_call_budget", exhausted=True)
            assistant_payload = assistant_message.model_dump(exclude_none=True)
            tool_payloads: list[dict[str, Any]] = []
            for tool_call in tool_calls:
                self.tool_calls_used += 1
                tool_started = time.perf_counter()
                arguments: Any = {}
                source_read_returned = False
                try:
                    arguments = json.loads(tool_call.function.arguments or "{}")
                    reset_changed_source_failures()
                    read_key = (
                        tool_call.function.name,
                        json.dumps(arguments, sort_keys=True, ensure_ascii=False),
                    )
                    paths = ([arguments.get("path")] if tool_call.function.name in {"write_file", "replace_text"} else
                        [item.get("path") for item in arguments.get("files", []) if isinstance(item, dict)]
                        if tool_call.function.name == "write_files" else [])
                    stale_path = next((_normalized_source_path(path) for path in paths
                        if isinstance(path, str) and _normalized_source_path(path) in required_source_reads), None)
                    if stale_path is not None:
                        result = "Tool error: SourceRefreshRequired: " + source_refresh_notice()
                        note_source_failure((stale_path, required_source_reads[stale_path]), result)
                    elif refresh_tool and api_forced_tool is None and tool_call.function.name not in (
                        {"read_file", "read_files"} if refresh_tool == "read_file" else {"list_files"}
                    ):
                        result = f"Only {refresh_tool} is available until the required source refresh completes. " + source_refresh_notice()
                        note_source_failure(refresh_target, result)
                    elif verification_due and tool_call.function.name != "verify_candidate":
                        result = "Run verify_candidate now; two requests remain after verification for source corrections."
                    elif write_only and tool_call.function.name not in {"replace_text", "write_file", "write_files"}:
                        result = "Only source editing tools are available for this focused edit: replace_text, write_file, write_files. Make a concrete change now."
                    elif tool_call.function.name == "verify_candidate" and verification_feedback is not None:
                        if arguments != {}:
                            raise ValueError("verify_candidate takes no arguments")
                        revision = (len(progress_paths), getattr(project_tools, "source_revision", 0))
                        if revision == verified_revision:
                            result = "Unchanged source; previous complete local verification:\n" + verified_feedback
                        elif verification_runs >= 2:
                            result = "On-demand verification limit reached for this slice. Finish the active repair; full boundary verification remains mandatory."
                        else:
                            verification_runs += 1
                            verified_feedback = verification_feedback()
                            verified_revision = (len(progress_paths), getattr(project_tools, "source_revision", 0))
                            result = verified_feedback
                    elif tool_call.function.name in {"list_files", "search_text"} and read_key in epoch_reads:
                        result = (
                            "The same read already returned in this conversation and no project file "
                            "has changed. Use its earlier result to make the next concrete edit."
                        )
                    else:
                        result = project_tools.call(tool_call.function.name, arguments)
                        result = failure_source_context(tool_call.function.name, result, project_tools)
                        reset_changed_source_failures()
                        clear_fresh_source_reads(tool_call.function.name, arguments, result)
                        if tool_call.function.name in {"read_file", "read_files"}:
                            source_read_returned = True
                            original_chars = len(result)
                            filtered = read_evidence.filter(tool_call.function.name, arguments, result)
                            if len(filtered) <= MAX_BATCH_OUTPUT_CHARS:
                                result = filtered
                            if len(result) < original_chars:
                                emit_metric("source_read_reused", request=self.model_requests_used,
                                    tool=tool_call.function.name, original_chars=original_chars,
                                    sent_chars=len(result), removed_chars=original_chars - len(result))
                        if tool_call.function.name in {"list_files", "read_files", "search_text"}:
                            epoch_reads.add(read_key)
                        elif len(progress_paths) > observed_writes:
                            epoch_reads.clear()
                            # A source edit invalidates that file's digest, not every
                            # unrelated file still present in the conversation.
                            # read_files always rereads disk before comparing hashes,
                            # including identical batches and external changes.
                            changed = {_normalized_source_path(path)
                                for path in progress_paths[observed_writes:] if isinstance(path, str)}
                            read_evidence.invalidate(changed)
                        if tool_call.function.name in {"write_files", "write_file"}:
                            read_evidence.remember_write(tool_call.function.name, arguments, assistant_payload)
                except ModelRequestInterrupted:
                    raise
                except Exception as exc:
                    source_read_returned = False
                    result = f"Tool error: {type(exc).__name__}: {exc}"
                    if isinstance(arguments, dict):
                        source_failure = _source_tool_failure(tool_call.function.name, arguments, exc)
                        if source_failure is not None:
                            note_source_failure(source_failure, result)
                    LOGGER.warning("Project tool failed: name=%s error=%s", tool_call.function.name, result[:300])
                elapsed = time.perf_counter() - tool_started
                self.tool_seconds = getattr(self, "tool_seconds", 0.0) + elapsed
                LOGGER.info(
                    "Project tool: name=%s seconds=%.3f result_chars=%d",
                    tool_call.function.name, elapsed, len(result),
                )
                emit_metric("tool_returned", request=self.model_requests_used,
                    tool=metric_identifier(tool_call.function.name), seconds=elapsed,
                    result_chars=len(result), tool_error=result.startswith("Tool error:"))
                tool_payloads.append(
                    {"role": "tool", "tool_call_id": tool_call.id, "content": result[:MAX_BATCH_OUTPUT_CHARS]}
                )
                if source_read_returned and len(result) <= MAX_BATCH_OUTPUT_CHARS:
                    read_evidence.remember(tool_call.function.name, arguments, tool_payloads[-1])
            exchange = [assistant_payload, *tool_payloads]
            conversation.extend(exchange)
            epoch_exchanges.append(exchange)
            if required_source_reads:
                conversation.append({"role": "user", "content": source_refresh_notice()})
            stalled_source = next((key for key, failures in source_failures.items()
                                   if failures >= MAX_IDENTICAL_FAILED_TOOL_TURNS), None)
            if stalled_source is not None:
                LOGGER.warning("Repeated source tool failures: path=%s family=%s; deferring this implementation pass", *stalled_source)
                return finish_implementation("repeated_tool_failure", deferred=True,
                    detail=source_failure_details[stalled_source][:300])
            failed_turn = tuple(
                (call.function.name, call.function.arguments or "", payload["content"])
                for call, payload in zip(tool_calls, tool_payloads)
            ) if all(payload["content"].startswith("Tool error:") for payload in tool_payloads) else None
            if failed_turn is not None:
                identical_failed_turns = identical_failed_turns + 1 if failed_turn == failed_tool_signature else 1
                failed_tool_signature = failed_turn
            else:
                identical_failed_turns = 0
                failed_tool_signature = None
            if identical_failed_turns >= MAX_IDENTICAL_FAILED_TOOL_TURNS:
                LOGGER.warning("Repeated identical tool failures; deferring this implementation pass")
                return finish_implementation("repeated_tool_failure", deferred=True, detail=failed_turn[0][2][:300])
            current_writes = len(progress_paths)
            if current_writes > observed_writes:
                observed_writes = current_writes
                idle_tool_turns = 0
                passing_checkpoint = False
                write_only_prompted = False
            else:
                idle_tool_turns += 1
            if (
                passing_checkpoint
                and current_writes > initial_writes
                and idle_tool_turns >= PASSING_CHECKPOINT_IDLE_TURNS
            ):
                LOGGER.info(
                    "Implementation checkpoint passed and no files changed for %d tool turns; "
                    "moving to the next requirement",
                    idle_tool_turns,
                )
                return finish_implementation("checkpoint_passed", handoff=True)
            if idle_tool_turns >= self.max_idle_tool_turns:
                if repair_feedback:
                    return finish_implementation("no_progress", deferred=True)
                if checkpoint_feedback is None and resumed_project:
                    # Integration must return to the orchestrator's real checks
                    # instead of turning an unverified idle pass into a fatal
                    # global budget failure. The checks decide what to repair.
                    LOGGER.warning("Idle implementation pass deferred to whole-project verification")
                    return finish_implementation("no_progress", deferred=True)
                try:
                    checker = boundary_feedback if boundary_feedback is not None else checkpoint_feedback
                    feedback = checker() if checker is not None else "No checkpoint available"
                except ModelRequestInterrupted:
                    raise
                except Exception as exc:
                    feedback = f"Checkpoint verification failed: {type(exc).__name__}: {exc}"
                if (current_writes > initial_writes or resumed_project) and feedback.startswith("Current build and test scripts pass."):
                    LOGGER.warning(
                        "No project file changes for %d tool turns; local scripts pass, moving to the next requirement",
                        idle_tool_turns,
                    )
                    return finish_implementation("checkpoint_passed", handoff=True, checked_feedback=feedback)
                LOGGER.warning("No progress in this implementation pass; deferring to verification: %s", feedback[:300])
                return finish_implementation("no_progress", deferred=True, detail=feedback[:300],
                    checked_feedback=feedback)
            if (visual_inputs and turn_index == 0) or self._message_context_chars(conversation) > MAX_CONVERSATION_CHARS:
                conversation = compact_context()
            if identical_failed_turns == MAX_IDENTICAL_FAILED_TOOL_TURNS - 1:
                conversation.append({
                    "role": "user",
                    "content": "The previous tool call failed identically twice. Change the tool arguments or "
                               "approach; repeating the same failed call will stop this implementation pass.",
                })
            if self.max_tool_calls is not None and self.tool_calls_used >= self.max_tool_calls:
                self.last_budget_report = BudgetExhaustion(
                    budget="tool_call_budget",
                    limit=self.max_tool_calls,
                    used=self.tool_calls_used,
                    requested=0,
                    tool_names=(),
                    turns_used=turn_index + 1,
                    tool_calls_used=self.tool_calls_used,
                    model_requests_used=self.model_requests_used,
                    prompt_tokens_used=self.prompt_tokens_used,
                    completion_tokens_used=self.completion_tokens_used,
                )
                return finish_implementation("tool_call_budget", exhausted=True)
            if self.max_turns is not None and turn_index == self.max_turns - 1:
                self.last_budget_report = BudgetExhaustion(
                    budget="model_turn_budget",
                    limit=self.max_turns,
                    used=self.max_turns,
                    requested=0,
                    tool_names=(),
                    turns_used=self.max_turns,
                    tool_calls_used=self.tool_calls_used,
                    model_requests_used=self.model_requests_used,
                    prompt_tokens_used=self.prompt_tokens_used,
                    completion_tokens_used=self.completion_tokens_used,
                )
                return finish_implementation("model_turn_budget", exhausted=True)

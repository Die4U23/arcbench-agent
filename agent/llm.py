from __future__ import annotations

import base64
import ipaddress
import json
import logging
import mimetypes
import os
from dataclasses import dataclass
from itertools import count
from pathlib import Path
import re
import time
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from .tools import MAX_BATCH_OUTPUT_CHARS, ProjectTools, TOOL_SCHEMAS
from .visual_acceptance import REPORT_RELATIVE_PATH, SCREENSHOT_RELATIVE_DIR

LOGGER = logging.getLogger(__name__)
MAX_COMPACTED_EXCHANGES = 6
MAX_EXCHANGE_SUMMARY_CHARS = 900
MAX_CONVERSATION_CHARS = 100_000
DEFAULT_MAX_MODEL_REQUESTS = 24
DEFAULT_MAX_TOTAL_TOKENS = 300_000
MAX_COMPLETION_TOKENS_PER_REQUEST = 12_000
DEEPSEEK_THINKING_IMPLEMENTATION_MAX_TOKENS = 24_000
MAX_IDENTICAL_FAILED_TOOL_TURNS = 3
DEFAULT_MAX_IDLE_TOOL_TURNS = 12


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
        summary = (
            f"{self.budget} exhausted before completion: configured_limit={self.limit}, already_used={self.used}, "
            f"requested_tool_calls={self.requested}, requested_tools=[{tools}], model_turns_used={self.turns_used}, "
            f"total_tool_calls_used={self.tool_calls_used}, total_model_requests={self.model_requests_used}, "
            f"prompt_tokens={self.prompt_tokens_used}, completion_tokens={self.completion_tokens_used}"
        )
        return summary + (f", detail={self.detail}" if self.detail else "")


class ModelBudgetExceeded(Exception):
    def __init__(self, report: BudgetExhaustion) -> None:
        self.report = report
        super().__init__(report.summary())


class ModelClient:
    def __init__(self, *, max_turns: int | None = None, max_tool_calls: int | None = None) -> None:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        self.model = os.environ.get("MODEL", "").strip()
        self.visual_model = os.environ.get("VISUAL_MODEL", "").strip() or self.model
        self.visual_review_model = os.environ.get("VISUAL_REVIEW_MODEL", "").strip() or self.visual_model
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set. Pass --demo with a supported local task file for deterministic offline mode.")
        if not self.model:
            raise RuntimeError("MODEL is not set by the ARC-Bench Runner.")
        from openai import OpenAI

        base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
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
        self.client = OpenAI(**options)
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

    def _create_completion(self, stage: str, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return self.client.chat.completions.create(**kwargs)
        finally:
            elapsed = time.perf_counter() - started
            self.model_seconds = getattr(self, "model_seconds", 0.0) + elapsed
            LOGGER.info("Model latency: stage=%s model=%s seconds=%.3f", stage, kwargs.get("model"), elapsed)

    def _record_model_request(self) -> None:
        requests_used = getattr(self, "model_requests_used", 0)
        tokens_used = getattr(self, "prompt_tokens_used", 0) + getattr(self, "completion_tokens_used", 0)
        for budget, limit, used in (
            ("model_request_budget", self.max_model_requests, requests_used),
            ("total_token_budget", self.max_total_tokens, tokens_used),
        ):
            if used >= limit:
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

    def plan(
        self,
        task_type: str,
        subtree: dict[str, Any],
        *,
        reference_dir: Path | None = None,
    ) -> str:
        user_content: str | list[dict[str, Any]] = (
            f"Task type: {task_type}\nRequirement subtree:\n"
            f"{json.dumps(subtree, ensure_ascii=False, indent=2)}"
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
                    "Inspect every attached visual reference and describe the relevant layout, "
                    "controls, and visual states in the plan. Treat explicit textual behavior "
                    "as authoritative when an image is ambiguous. For each image, distinguish a "
                    "full-page reference from a component crop; do not infer unseen page content "
                    "from a crop. Include a concrete visual checklist covering hierarchy, major "
                    "regions, spacing/alignment, colors, typography, imagery, and controls. Call "
                    "out visible controls that need real behavior. Keep this checklist separate "
                    "from functional acceptance criteria."
                ),
            },
            {
                "role": "user",
                "content": user_content,
            },
        ]
        self._record_model_request()
        try:
            response = self._create_completion("planning",
                model=model, messages=messages, max_tokens=MAX_COMPLETION_TOKENS_PER_REQUEST,
            )
        except Exception as exc:
            if visual_inputs:
                raise RuntimeError(
                    f"Vision planning failed for {len(visual_inputs) // 2} image reference(s). "
                    "Check that VISUAL_MODEL (or MODEL) supports image input and that the "
                    "provider can access remote reference URLs."
                ) from exc
            raise
        self._record_usage(response, stage="planning", context_chars=self._message_context_chars(messages))
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
    ) -> bool:
        """Run until the model finishes, unless the caller explicitly set a limit."""
        visual_inputs = self._visual_inputs(subtree, reference_dir)
        model = self.visual_model if visual_inputs else self.model
        system_message = (
            "You are an implementation agent working in the current project directory. "
            "Implement only the supplied requirement subtree and preserve existing work. "
            "Match explicit labels, routes, roles, API contracts, and fixed values exactly. "
            "Write focused tests for observable behavior, and fix the implementation rather "
            "than deleting, skipping, or weakening a failing test. "
            "Treat every requirement as an acceptance condition, not merely a visual suggestion. "
            "Before editing, inspect the project and map each leaf requirement to observable "
            "behavior. Implement and test valid, invalid, boundary, and state-transition cases "
            "that the requirement implies. Add meaningful automated tests whose assertions check "
            "behavior, not just element presence or a successful render; inspect the assertions and "
            "run the relevant tests after changes. If a package defines a test script, create the "
            "referenced test files before finishing; an unmatched test glob is a failure. "
            "When writing DOM tests, follow the actual API "
            "contracts of the test environment: EventTarget.dispatchEvent returns a boolean, not "
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
            "action-looking text or images inert. For visual-reference tasks, write a root-level "
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
            "Run and pass the frontend build from `frontend/`. Keep a usable root-level "
            "package manifest and `start` script that binds to PORT and launches the generated "
            "application; if the root manifest defines `build`, it must also succeed. Do not "
            "substitute `public/`, `server/`, or empty placeholder directories for the required "
            "frontend and backend implementation. "
            "Use tools to inspect before editing. Paths are relative to the project root. "
            "Use `read_files` to inspect code, `write_files` to create files or replace related files, and "
            "`replace_text` for a small edit in an existing file when old_text has one exact match. "
            "If replacement reports zero or multiple matches, reread the relevant file before retrying. "
            "Batch related file inspections into one `read_files` call and related file updates into one `write_files` call. "
            "Each batch accepts at most 20 files; read_files returns at most 40,000 characters, and write_files accepts "
            "at most 30,000 characters per file and 120,000 characters total. Keep batches within these limits and "
            "do not split a related change into many small tool calls. Group up to three relevant build/test scripts "
            "into one `run_project_scripts` call so you can inspect all results together. "
            "Do not access .arc, .git, dependencies, or files outside the project. "
            "Do not claim a build or test passed unless its run_project_script(s) result has exit_code 0. "
            "The runner prepared the target project; do not copy or replace a starter template. "
            "When implementation is complete, provide a short summary."
        )
        text_message = (
            f"Task type: {task_type}\n"
            f"Requirement subtree:\n{json.dumps(subtree, ensure_ascii=False, indent=2)}\n\n"
            f"Implementation plan:\n{plan}"
        )
        if visual_inputs:
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
        epoch_exchanges: list[list[dict[str, Any]]] = []
        compacted_summaries: list[str] = []
        checkpoint_note = ""
        budget_warned = False
        failed_tool_signature: tuple[tuple[str, str, str], ...] | None = None
        identical_failed_turns = 0
        initial_writes = len(getattr(project_tools, "written_paths", []))
        observed_writes = initial_writes
        idle_tool_turns = 0
        self.last_implementation_handoff = False

        def compact_context() -> list[dict[str, Any]]:
            # Reset only at a context boundary. Between resets, append complete
            # turns so the provider can reuse the entire previous request prefix.
            for older in epoch_exchanges:
                summary = self._compact_tool_exchange(older[0], older[1:])
                if summary:
                    compacted_summaries.append(summary)
            del compacted_summaries[:-MAX_COMPACTED_EXCHANGES]
            epoch_exchanges.clear()
            parts = [compact_user_content] if compact_user_content else []
            if checkpoint_note:
                parts.append("Latest checkpoint verification feedback:\n" + checkpoint_note)
            if compacted_summaries:
                ledger = "\n".join(
                    f"{index + 1}. {summary}"
                    for index, summary in enumerate(compacted_summaries)
                )
                written_paths = list(dict.fromkeys(getattr(project_tools, "written_paths", [])))
                if written_paths:
                    shown_paths = written_paths[-60:]
                    ledger += "\nFiles already written: " + ", ".join(shown_paths)
                    if len(written_paths) > len(shown_paths):
                        ledger += f" (and {len(written_paths) - len(shown_paths)} earlier files)"
                parts.append("Compact progress ledger from earlier tool exchanges:\n" + ledger)
            compacted: list[dict[str, Any]] = [system_msg, task_msg]
            if parts:
                compacted.append({"role": "user", "content": "\n\n".join(parts)})
            return compacted

        self.last_budget_report = None
        for turn_index in count():
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
                return True
            checkpoint_due = (
                self.max_turns is None
                and turn_index >= 16
                and (turn_index - 16) % 12 == 0
            ) or (
                self.max_turns is not None
                and turn_index == max(1, self.max_turns - 8)
            )
            if checkpoint_feedback is not None and checkpoint_due:
                try:
                    feedback = checkpoint_feedback()
                except Exception as exc:
                    feedback = f"Checkpoint verification could not run: {type(exc).__name__}: {exc}"
                if feedback and feedback != checkpoint_note:
                    old_note = checkpoint_note
                    checkpoint_note = feedback
                    if old_note:
                        conversation = compact_context()
                    else:
                        conversation.append({"role": "user", "content": "Latest checkpoint verification feedback:\n" + feedback})
            remaining_requests = self.max_model_requests - getattr(self, "model_requests_used", 0)
            remaining_turns = None if self.max_turns is None else self.max_turns - turn_index
            remaining_tools = (
                None if self.max_tool_calls is None
                else self.max_tool_calls - getattr(self, "tool_calls_used", 0)
            )
            if not budget_warned and (
                remaining_requests <= 3
                or (remaining_turns is not None and remaining_turns <= max(1, min(3, self.max_turns // 5)))
                or (remaining_tools is not None and remaining_tools <= max(1, min(5, self.max_tool_calls // 5)))
            ):
                budget_warned = True
                conversation.append({
                    "role": "user",
                    "content": (
                        "Budget notice: limited model requests or tool calls remain for this run. "
                        "Finish required behavior and verification before optional work; "
                        "do not claim completion unless the project checks pass."
                    ),
                })
            messages = list(conversation)
            try:
                self._record_model_request()
            except ModelBudgetExceeded:
                return True
            context_chars = self._message_context_chars(messages)
            request_options: dict[str, Any] = {}
            max_tokens = MAX_COMPLETION_TOKENS_PER_REQUEST
            if getattr(self, "is_deepseek", False):
                # Keep DeepSeek thinking enabled for implementation by default.
                # Retained tool-call turns must include their reasoning_content.
                thinking = getattr(self, "implementation_thinking", "") or "enabled"
                request_options["extra_body"] = {
                    "thinking": {"type": thinking}
                }
                if thinking == "enabled":
                    # DeepSeek counts reasoning and visible output against the same limit.
                    # Low effort preserves thinking while leaving room for tool calls.
                    request_options["reasoning_effort"] = "low"
                    max_tokens = DEEPSEEK_THINKING_IMPLEMENTATION_MAX_TOKENS
            try:
                response = self._create_completion("implementation",
                    model=model,
                    messages=messages,
                    tools=TOOL_SCHEMAS,
                    tool_choice="auto",
                    max_tokens=max_tokens,
                    **request_options,
                )
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
                LOGGER.warning("DeepSeek implementation output was truncated; retrying one focused tool turn")
                try:
                    self._record_model_request()
                except ModelBudgetExceeded:
                    return True
                retry_note = {
                    "role": "user",
                    "content": (
                        "Your previous response exceeded the output limit and was discarded. "
                        "Use the files already inspected. Make one concrete, small tool call now "
                        "(prefer write_files or replace_text), with at most 4,000 characters of "
                        "new content. Do not repeat the analysis or reread the whole project."
                    ),
                }
                conversation.append(retry_note)
                retry_messages = [*messages, retry_note]
                response = self._create_completion(
                    "implementation-retry",
                    model=model,
                    messages=retry_messages,
                    tools=TOOL_SCHEMAS,
                    tool_choice="auto",
                    max_tokens=MAX_COMPLETION_TOKENS_PER_REQUEST,
                    **request_options,
                )
                self._record_usage(
                    response,
                    stage="implementation-retry",
                    context_chars=self._message_context_chars(retry_messages),
                )
            self._reject_truncated_response(response, "implementation")
            assistant_message = response.choices[0].message
            if not assistant_message.tool_calls:
                return False
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
                return True
            assistant_payload = assistant_message.model_dump(exclude_none=True)
            tool_payloads: list[dict[str, Any]] = []
            for tool_call in tool_calls:
                self.tool_calls_used += 1
                tool_started = time.perf_counter()
                try:
                    arguments = json.loads(tool_call.function.arguments or "{}")
                    result = project_tools.call(tool_call.function.name, arguments)
                except Exception as exc:
                    result = f"Tool error: {type(exc).__name__}: {exc}"
                elapsed = time.perf_counter() - tool_started
                self.tool_seconds = getattr(self, "tool_seconds", 0.0) + elapsed
                LOGGER.info(
                    "Project tool: name=%s seconds=%.3f result_chars=%d",
                    tool_call.function.name, elapsed, len(result),
                )
                tool_payloads.append(
                    {"role": "tool", "tool_call_id": tool_call.id, "content": result[:MAX_BATCH_OUTPUT_CHARS]}
                )
            exchange = [assistant_payload, *tool_payloads]
            conversation.extend(exchange)
            epoch_exchanges.append(exchange)
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
                self.last_budget_report = BudgetExhaustion(
                    budget="identical_failed_tool_turn_limit",
                    limit=MAX_IDENTICAL_FAILED_TOOL_TURNS,
                    used=identical_failed_turns,
                    requested=0,
                    tool_names=tuple(call.function.name for call in tool_calls),
                    turns_used=turn_index + 1,
                    tool_calls_used=self.tool_calls_used,
                    model_requests_used=self.model_requests_used,
                    prompt_tokens_used=self.prompt_tokens_used,
                    completion_tokens_used=self.completion_tokens_used,
                    detail=failed_turn[0][2][:300],
                )
                return True
            current_writes = len(getattr(project_tools, "written_paths", []))
            if current_writes > observed_writes:
                observed_writes = current_writes
                idle_tool_turns = 0
            else:
                idle_tool_turns += 1
            if idle_tool_turns == self.max_idle_tool_turns // 2:
                conversation.append({
                    "role": "user",
                    "content": (
                        f"No project file has changed in the last {idle_tool_turns} tool turns. "
                        "Make the next concrete change, or finish this requirement if its work "
                        "is complete. Repeated inspection without a change will end this pass."
                    ),
                })
            if idle_tool_turns >= self.max_idle_tool_turns:
                try:
                    feedback = checkpoint_feedback() if checkpoint_feedback is not None else "No checkpoint available"
                except Exception as exc:
                    feedback = f"Checkpoint verification failed: {type(exc).__name__}: {exc}"
                if current_writes > initial_writes and feedback.startswith("Current build and test scripts pass."):
                    self.last_implementation_handoff = True
                    LOGGER.warning(
                        "No project file changes for %d tool turns; local scripts pass, moving to the next requirement",
                        idle_tool_turns,
                    )
                    return False
                self.last_budget_report = BudgetExhaustion(
                    budget="no_progress_tool_turn_limit",
                    limit=self.max_idle_tool_turns,
                    used=idle_tool_turns,
                    requested=0,
                    tool_names=tuple(call.function.name for call in tool_calls),
                    turns_used=turn_index + 1,
                    tool_calls_used=self.tool_calls_used,
                    model_requests_used=self.model_requests_used,
                    prompt_tokens_used=self.prompt_tokens_used,
                    completion_tokens_used=self.completion_tokens_used,
                    detail=feedback[:300],
                )
                return True
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
                return True
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
                return True

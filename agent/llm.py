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
from pathlib import Path
import re
import time
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from .tools import MAX_BATCH_OUTPUT_CHARS, ProjectTools, TOOL_SCHEMAS
from .ticketbooking_contract import public_acceptance_context
from .visual_acceptance import REPORT_RELATIVE_PATH, SCREENSHOT_RELATIVE_DIR, collect_visual_references

LOGGER = logging.getLogger(__name__)
MAX_COMPACTED_EXCHANGES = 6
MAX_EXCHANGE_SUMMARY_CHARS = 900
MAX_CONVERSATION_CHARS = 240_000
DEFAULT_MAX_MODEL_REQUESTS = 250
DEFAULT_MAX_TOTAL_TOKENS = 8_000_000
MAX_COMPLETION_TOKENS_PER_REQUEST = 12_000
DEEPSEEK_THINKING_IMPLEMENTATION_MAX_TOKENS = 24_000
MAX_IDENTICAL_FAILED_TOOL_TURNS = 3
DEFAULT_MAX_IDLE_TOOL_TURNS = 12
EARLY_CHECKPOINT_TURN = 4
CHECKPOINT_INTERVAL_TURNS = 6
PASSING_CHECKPOINT_IDLE_TURNS = 4


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
        self.visual_implementation = os.environ.get("ARCBENCH_VISUAL_IMPLEMENTATION", "enabled").strip().lower()
        if self.visual_implementation not in {"enabled", "disabled"}:
            raise ValueError("ARCBENCH_VISUAL_IMPLEMENTATION must be enabled or disabled")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set. Pass --demo with a supported local task file for deterministic offline mode.")
        if not self.model:
            raise RuntimeError("MODEL is not set by the ARC-Bench Runner.")
        from openai import OpenAI

        base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
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
        self._requirement_review_cache: tuple[str, str] | None = None

    def _create_completion(self, stage: str, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return self.client.chat.completions.create(**kwargs)
        finally:
            elapsed = time.perf_counter() - started
            self.model_seconds = getattr(self, "model_seconds", 0.0) + elapsed
            LOGGER.info("Model latency: stage=%s model=%s seconds=%.3f", stage, kwargs.get("model"), elapsed)

    def _check_model_budget(self) -> None:
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

    def _record_model_request(self) -> None:
        self._check_model_budget()
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
        audit_complete = True
        for name in project_tools.list_files(".").splitlines():
            parts = Path(name).parts
            if any(part in {"test", "tests", "__tests__"} for part in parts):
                continue
            if ".test." in name or ".spec." in name or Path(name).suffix not in {".js", ".jsx", ".ts", ".tsx", ".json"}:
                continue
            if Path(name).suffix == ".json" and Path(name).name != "package.json":
                continue
            try:
                content = project_tools.read_file(name)
            except (ValueError, FileNotFoundError):
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
        self._record_model_request()
        # This is a bounded verdict, not an implementation turn. Prevent an
        # entire output allowance being consumed by hidden reasoning with no
        # usable verdict, as observed in the local acceptance audit.
        options = {"extra_body": {"thinking": {"type": "disabled"}}} if getattr(self, "is_deepseek", False) else {}
        response = self._create_completion("requirement_review", model=self.model, messages=messages,
                                           max_tokens=6000, **options)
        self._record_usage(response, stage="requirement_review", context_chars=self._message_context_chars(messages))
        self._reject_truncated_response(response, "requirement review")
        raw = response.choices[0].message.content or ""
        match = re.search(r"\{[\s\S]*\}", raw)
        review = json.loads(match.group(0)) if match else {}
        if review.get("verdict") not in {"pass", "repair"} or not isinstance(review.get("issues"), list):
            raise RuntimeError("Requirement reviewer returned an invalid verdict")
        if (review["verdict"] == "pass") != (not review["issues"]):
            raise RuntimeError("Requirement reviewer returned a contradictory verdict")
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
                raise RuntimeError("Requirement reviewer returned an unevidenced issue")
            node = requirement_nodes.get(issue["requirement_id"])
            quote = " ".join(issue["requirement_quote"].split())
            if node is None or not any(quote in " ".join(text.split()) for text in text_values(node)):
                raise RuntimeError("Requirement reviewer cited text outside the supplied requirement")
            source = source_contents.get(issue["path"])
            evidence = " ".join(issue["evidence"].split())
            if source is None or evidence not in " ".join(source.split()):
                raise RuntimeError("Requirement reviewer cited evidence outside the supplied production source")
            missing_items.append(
                f"{issue['requirement_id']} {issue['path']}: {issue['missing_behavior']}\n"
                f"Required: {issue['requirement_quote']}\nSource evidence: {issue['evidence']}"
            )
        missing = "\n".join(missing_items)
        # An unseen tail or omitted file can change while the supplied prefix
        # stays identical. Never reuse an audit of an incomplete source view.
        self._requirement_review_cache = (fingerprint, missing) if audit_complete else None
        return missing

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
        token_allowance: int | None = None,
        request_allowance: int | None = None,
    ) -> bool:
        """Run until the model finishes, unless the caller explicitly set a limit."""
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
            "Use selectOptions for selects, not type; use change events for date inputs when user typing "
            "does not work in jsdom. Reread both the failing test helper and the component before editing "
            "a shared helper, and update every call site when changing its argument shape. "
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
            "The runner prepared the target project; do not copy or replace a starter template. "
            "When implementation is complete, provide a short summary."
        )
        text_message = (
            f"Task type: {task_type}\n"
            f"Requirement subtree:\n{json.dumps(subtree, ensure_ascii=False, indent=2)}\n\n"
            f"Implementation plan:\n{plan}"
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
        epoch_exchanges: list[list[dict[str, Any]]] = []
        epoch_reads: set[tuple[str, str]] = set()
        epoch_file_reads: dict[str, str] = {}
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
        observed_writes = initial_writes
        idle_tool_turns = 0
        passing_checkpoint = False
        passing_checkpoint_turn: int | None = None
        write_only_prompted = False
        thinking_disabled_epoch = False
        self.last_implementation_handoff = False
        self.last_implementation_deferred = False
        starting_tokens = getattr(self, "prompt_tokens_used", 0) + getattr(self, "completion_tokens_used", 0)
        starting_requests = getattr(self, "model_requests_used", 0)
        allowance_warned = False

        def compact_context() -> list[dict[str, Any]]:
            nonlocal thinking_disabled_epoch
            # Reset only at a context boundary. Between resets, append complete
            # turns so the provider can reuse the entire previous request prefix.
            for older in epoch_exchanges:
                summary = self._compact_tool_exchange(older[0], older[1:])
                if summary:
                    compacted_summaries.append(summary)
            del compacted_summaries[:-MAX_COMPACTED_EXCHANGES]
            epoch_exchanges.clear()
            epoch_reads.clear()
            epoch_file_reads.clear()
            thinking_disabled_epoch = False
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
            try:
                self._check_model_budget()
            except ModelBudgetExceeded:
                return True
            phase_tokens = getattr(self, "prompt_tokens_used", 0) + getattr(self, "completion_tokens_used", 0) - starting_tokens
            phase_requests = getattr(self, "model_requests_used", 0) - starting_requests
            if (token_allowance is not None and phase_tokens >= token_allowance) or (request_allowance is not None and phase_requests >= request_allowance):
                self.last_implementation_deferred = True
                LOGGER.warning("Module allocation reached (tokens=%d requests=%d); deferring unfinished work to whole-project integration", phase_tokens, phase_requests)
                return False
            if token_allowance is not None and not allowance_warned and phase_tokens >= token_allowance * .75:
                allowance_warned = True
                conversation.append({"role": "user", "content": "This module has little allocated budget left. Finish its required application wiring now; leave unresolved failures for whole-project integration. Avoid expanding tests or optional styling."})
            # Give a green module two final turns, then verify its latest files.
            # Optional edits must not indefinitely postpone the next module.
            if passing_checkpoint_turn is not None and turn_index >= passing_checkpoint_turn + 2:
                try:
                    feedback = checkpoint_feedback() if checkpoint_feedback is not None else "No checkpoint available"
                except Exception as exc:
                    feedback = f"Checkpoint verification failed: {type(exc).__name__}: {exc}"
                if feedback.startswith("Current build and test scripts pass."):
                    self.last_implementation_handoff = True
                    LOGGER.info("Implementation final checkpoint passed; moving to the next requirement")
                    return False
                passing_checkpoint_turn = None
                passing_checkpoint = False
                checkpoint_note = feedback
                conversation.append({"role": "user", "content": "Final module checkpoint failed; repair these failures:\n" + feedback})
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
                and len(progress_paths) > initial_writes
                and turn_index >= EARLY_CHECKPOINT_TURN
                and (turn_index - EARLY_CHECKPOINT_TURN) % CHECKPOINT_INTERVAL_TURNS == 0
            ) or (
                self.max_turns is not None
                and turn_index == max(1, self.max_turns - 8)
            )
            if checkpoint_feedback is not None and checkpoint_due:
                try:
                    feedback = checkpoint_feedback()
                except Exception as exc:
                    feedback = f"Checkpoint verification could not run: {type(exc).__name__}: {exc}"
                passing_checkpoint = feedback.startswith("Current build and test scripts pass.")
                if passing_checkpoint and repair_feedback:
                    self.last_implementation_handoff = True
                    LOGGER.info("Repair checkpoint passed; returning to whole-project verification and coverage audit")
                    return False
                if passing_checkpoint and passing_checkpoint_turn is None:
                    passing_checkpoint_turn = turn_index
                    conversation.append({"role": "user", "content": "Local module checks pass. You have two final turns to finish required behavior and report any uncovered acceptance criteria. Defer optional test expansion or styling polish; the agent will recheck the files and proceed to the next module."})
                if feedback and feedback != checkpoint_note:
                    checkpoint_note = feedback
                    # Keep the existing request prefix cacheable across checkpoints.
                    # A new feedback message supersedes older feedback without
                    # forcing every file read back into an uncached summary.
                    conversation.append({"role": "user", "content": "Latest checkpoint verification feedback (supersedes earlier feedback):\n" + feedback})
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
            write_only = idle_tool_turns >= getattr(self, "max_idle_tool_turns", DEFAULT_MAX_IDLE_TOOL_TURNS) // 2 and not passing_checkpoint
            edit_tool = "replace_text" if (resumed_project or repair_text or checkpoint_note or len(progress_paths) > initial_writes) else "write_files"
            if write_only and not write_only_prompted:
                write_only_prompted = True
                conversation.append({
                    "role": "user",
                    "content": (
                        "You have inspected the project repeatedly without changing a file. "
                        "The next step must be a concrete source or behavior-test edit for this "
                        f"requirement. Use {edit_tool} now; do not read or list files "
                        "again. Current check failures:\n" + checkpoint_note[:2_000]
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
                thinking = "disabled" if thinking_disabled_epoch else (getattr(self, "implementation_thinking", "") or "enabled")
                request_options["extra_body"] = {
                    "thinking": {"type": thinking}
                }
                if thinking == "enabled":
                    # DeepSeek counts reasoning and visible output against the same limit.
                    # Low effort preserves thinking while leaving room for tool calls.
                    request_options["reasoning_effort"] = "low"
                    max_tokens = DEEPSEEK_THINKING_IMPLEMENTATION_MAX_TOKENS
                if write_only:
                    # DeepSeek only supports required tool choice outside thinking mode.
                    # Keep the focused edit bounded while retaining the inspected source.
                    thinking = "disabled"
                    request_options["extra_body"] = {"thinking": {"type": thinking}}
                    request_options.pop("reasoning_effort", None)
                    max_tokens = MAX_COMPLETION_TOKENS_PER_REQUEST
            if getattr(self, "is_glm", False) and self.implementation_thinking:
                request_options["extra_body"] = {"thinking": {"type": self.implementation_thinking}}
            try:
                response = self._create_completion("implementation",
                    model=model,
                    messages=messages,
                    tools=TOOL_SCHEMAS,
                    tool_choice={"type": "function", "function": {"name": edit_tool}} if write_only else "auto",
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
                if write_only:
                    LOGGER.warning("Focused edit request returned no file edit tool call; handing off for verification")
                return False
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
                return True
            assistant_payload = assistant_message.model_dump(exclude_none=True)
            tool_payloads: list[dict[str, Any]] = []
            for tool_call in tool_calls:
                self.tool_calls_used += 1
                tool_started = time.perf_counter()
                try:
                    arguments = json.loads(tool_call.function.arguments or "{}")
                    read_key = (
                        tool_call.function.name,
                        json.dumps(arguments, sort_keys=True, ensure_ascii=False),
                    )
                    if write_only and tool_call.function.name != edit_tool:
                        result = f"Only {edit_tool} is available for this focused edit. Make a concrete change now."
                    elif tool_call.function.name in {"list_files", "read_files", "search_text"} and read_key in epoch_reads:
                        result = (
                            "The same read already returned in this conversation and no project file "
                            "has changed. Use its earlier result to make the next concrete edit."
                        )
                    else:
                        result = project_tools.call(tool_call.function.name, arguments)
                        if tool_call.function.name == "read_files":
                            try:
                                read_payload = json.loads(result)
                                files = read_payload.get("files", [])
                                if isinstance(files, list):
                                    for file in files:
                                        if not isinstance(file, dict):
                                            continue
                                        path, content = file.get("path"), file.get("content")
                                        if not isinstance(path, str) or not isinstance(content, str):
                                            continue
                                        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                                        if epoch_file_reads.get(path) == digest:
                                            file["content"] = "[unchanged since earlier read in this conversation]"
                                        else:
                                            epoch_file_reads[path] = digest
                                    result = json.dumps(read_payload, ensure_ascii=False)
                            except (TypeError, ValueError):
                                pass
                        if tool_call.function.name in {"list_files", "read_files", "search_text"}:
                            epoch_reads.add(read_key)
                        elif len(progress_paths) > observed_writes:
                            epoch_reads.clear()
                            epoch_file_reads.clear()
                except Exception as exc:
                    result = f"Tool error: {type(exc).__name__}: {exc}"
                    LOGGER.warning("Project tool failed: name=%s error=%s", tool_call.function.name, result[:300])
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
                self.last_implementation_handoff = True
                LOGGER.info(
                    "Implementation checkpoint passed and no files changed for %d tool turns; "
                    "moving to the next requirement",
                    idle_tool_turns,
                )
                return False
            if idle_tool_turns >= self.max_idle_tool_turns:
                if checkpoint_feedback is None and resumed_project:
                    # Integration must return to the orchestrator's real checks
                    # instead of turning an unverified idle pass into a fatal
                    # global budget failure. The checks decide what to repair.
                    self.last_implementation_deferred = True
                    LOGGER.warning("Idle implementation pass deferred to whole-project verification")
                    return False
                try:
                    feedback = checkpoint_feedback() if checkpoint_feedback is not None else "No checkpoint available"
                except Exception as exc:
                    feedback = f"Checkpoint verification failed: {type(exc).__name__}: {exc}"
                if (current_writes > initial_writes or resumed_project) and feedback.startswith("Current build and test scripts pass."):
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

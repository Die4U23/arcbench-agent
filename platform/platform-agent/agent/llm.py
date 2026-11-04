"""Platform model transport and accounting; execution policy lives in the controller."""
from __future__ import annotations
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from .model_transport import CompletionClient, ModelRequestInterrupted, default_model_deadline
from .request_metrics import RequestObservation

LOGGER = logging.getLogger(__name__)
DEFAULT_MAX_MODEL_REQUESTS = 250
DEFAULT_MAX_TOTAL_TOKENS = 8_000_000


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

class ModelClient:
    def __init__(self, *, max_turns=None, max_tool_calls=None):
        api_key = os.environ.get('OPENAI_API_KEY', '').strip()
        self.model = os.environ.get('MODEL', '').strip()
        self.base_url = os.environ.get('OPENAI_BASE_URL', '').strip()
        if not api_key or not self.model or not self.base_url:
            raise ValueError('Runner must provide OPENAI_API_KEY, MODEL and OPENAI_BASE_URL')
        self.metrics_provider = urlsplit(self.base_url).hostname
        self.is_deepseek = self.metrics_provider == 'api.deepseek.com' or self.model.lower().startswith('deepseek-')
        transport = os.environ.get('ARCBENCH_MODEL_TRANSPORT', 'auto').strip()
        if transport not in {'auto', 'json', 'assembled_stream'}:
            raise ValueError('ARCBENCH_MODEL_TRANSPORT must be auto, json or assembled_stream')
        assemble_stream = (transport == 'assembled_stream' or
                           (transport == 'auto' and self.metrics_provider == 'api.deepseek.com'))
        self.client = CompletionClient(api_key=api_key, base_url=self.base_url,
            deadline_seconds=float(os.environ.get('ARCBENCH_MODEL_DEADLINE_SECONDS',
                                                  default_model_deadline(self.model, self.base_url))),
            assemble_stream=assemble_stream)
        LOGGER.info('Model response transport: %s', 'assembled_stream' if assemble_stream else 'json')
        self.last_request_interruption = None
        self.max_turns, self.max_tool_calls = max_turns, max_tool_calls
        self.max_model_requests = int(os.environ.get('ARCBENCH_MAX_MODEL_REQUESTS', DEFAULT_MAX_MODEL_REQUESTS))
        self.max_total_tokens = int(os.environ.get('ARCBENCH_MAX_TOTAL_TOKENS', DEFAULT_MAX_TOTAL_TOKENS))
        if self.max_model_requests < 1 or self.max_total_tokens < 1:
            raise ValueError('Request and token limits must be positive')
        self.tool_calls_used = self.model_requests_used = 0
        self.prompt_tokens_used = self.completion_tokens_used = 0
        self.cache_hit_tokens_used = self.cache_miss_tokens_used = self.cache_observed_requests = 0
        self.model_seconds = self.tool_seconds = 0.0
        self.last_budget_report = None


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
        self._check_model_budget(reserve_for_delivery=False)
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
    def _message_context_chars(messages: list[dict[str, Any]]) -> int:
        # Measures serialized prompt size without logging project contents.
        return len(json.dumps(messages, ensure_ascii=False, default=str))

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

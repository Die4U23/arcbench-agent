"""Generic model adapter for competition tasks.

This module deliberately composes the legacy model session only for its shared
transport and cumulative accounting. Competition prompts and tool policy live
here so legacy task-specific assumptions cannot enter the task context.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from .campaign_budget import CampaignBudget, CampaignBudgetStopped
from .model_transport import ModelRequestInterrupted
from .llm import ModelBudgetExceeded, ModelClient
from .platform_settings import PlatformSettings, models_equivalent
from .platform_execution import ExecutionStopped, source_digest, validate_arguments
from .sandbox_files import snapshot_project
from .tools import TOOL_SCHEMAS


DEFAULT_CONTEXT_WINDOW_TOKENS = 32_768
DEFAULT_OUTPUT_RESERVE_TOKENS = 16_384
DEFAULT_REQUEST_ALLOWANCE = 8
DEFAULT_TOOL_CALL_LIMIT = 120
MAX_TOOL_RESULT_CHARS = 12_000
MAX_CONTEXT_READ_CHARS = 8_000
MAX_IDENTICAL_TOOL_FAILURES = 3


def same_campaign_model(requested, returned) -> bool:
    """The platform proxy reports deepseek-flash for a deepseek-v4-flash request."""
    return models_equivalent(PlatformSettings(), requested, returned)


_DESIGN_CONTRACT = {
    "entities": [],
    "ownership": [],
    "routes": [],
    "api": [],
    "state_transitions": [],
    "seed": [],
    "persistence": [],
    "clock": [],
    "shared_components": [],
}

_READ_ONLY_TOOLS = {"read_file", "read_files", "list_files", "search_text"}
_WRITE_TOOLS = {"write_files", "replace_text"}


class CompetitionModelError(RuntimeError):
    """A model response cannot safely continue through this adapter."""


class CompetitionModel:
    """Generic design, implementation, and evidence-audit model adapter.

    ``session`` is an already configured ModelClient-compatible object. It is
    intentionally injected so offline callers can provide a scripted session
    and the live controller can share one cumulative budget across stages.
    """

    def __init__(
        self,
        session: Any,
        *,
        context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS,
        output_reserve_tokens: int = DEFAULT_OUTPUT_RESERVE_TOKENS,
        request_allowance: int = DEFAULT_REQUEST_ALLOWANCE,
        max_tool_calls: int | None = None,
        audit_log: Any = None,
        campaign_budget: CampaignBudget | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        if context_window_tokens < 1 or output_reserve_tokens < 1:
            raise ValueError("context and output limits must be positive")
        if output_reserve_tokens >= context_window_tokens:
            raise ValueError("output reserve must be smaller than the context window")
        if request_allowance < 0:
            raise ValueError("request allowance cannot be negative")
        self.session = session
        if reasoning_effort not in {None, 'none', 'low', 'medium', 'high', 'max'}:
            raise ValueError('invalid reasoning effort')
        self.reasoning_effort = reasoning_effort
        self.context_window_tokens = context_window_tokens
        self.output_reserve_tokens = output_reserve_tokens
        self.request_allowance = request_allowance
        self.max_tool_calls = (max_tool_calls if max_tool_calls is not None else
                               getattr(session, "max_tool_calls", None))
        if self.max_tool_calls is None:
            self.max_tool_calls = DEFAULT_TOOL_CALL_LIMIT
        if self.max_tool_calls < 0:
            raise ValueError("tool call limit cannot be negative")
        self.audit_log = audit_log
        self.campaign_budget = campaign_budget
        if isinstance(session, ModelClient) and campaign_budget is not None:
            if not campaign_budget.live_allowed:
                raise CompetitionModelError("live competition requires verified campaign rates")
            if session.model != campaign_budget.config.model:
                raise CompetitionModelError("model differs from immutable campaign configuration")
            if session.base_url != campaign_budget.config.endpoint:
                raise CompetitionModelError("endpoint differs from immutable campaign configuration")
        self._implementation_budget_stage = "competition_implementation"
        self._context_sources: dict[str, dict[str, Any]] = {}
        self._stopped_reason: str | None = None
        self._manifest_logged: set[str] = set()

    def set_budget_stage(self, stage: str) -> None:
        """Select the preallocated implementation or repair envelope."""
        if stage not in {"initial", "repair"}:
            raise ValueError("implementation budget stage must be initial or repair")
        self._implementation_budget_stage = ("competition_implementation" if stage == "initial"
                                             else "competition_repair")

    @staticmethod
    def _packet_text(packet: Any) -> str:
        text = getattr(packet, "text", None)
        if not isinstance(text, str):
            raise TypeError("competition packet must provide text as a string")
        return text

    @staticmethod
    def _manifest(packet: Any) -> Any:
        manifest = getattr(packet, "manifest", {})
        if hasattr(manifest, "to_dict"):
            manifest = manifest.to_dict()
        if hasattr(manifest, "model_dump"):
            manifest = manifest.model_dump(mode="json")
        if isinstance(manifest, Mapping):
            return {str(key): CompetitionModel._json_value(value) for key, value in manifest.items()}
        return CompetitionModel._json_value(manifest)

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): CompetitionModel._json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [CompetitionModel._json_value(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if hasattr(value, "reference"):
            return CompetitionModel._json_value(value.reference())
        return str(value)

    def _record_manifest(self, packet: Any, stage: str, *, resource: str | None = None,
                         offset: int | None = None, limit: int | None = None) -> None:
        if self.audit_log is None:
            return
        manifest = self._manifest(packet)
        digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                           default=str).encode("utf-8")).hexdigest()
        record = {"stage": stage, "manifest_sha256": digest}
        if digest not in self._manifest_logged:
            record["manifest"] = manifest
            self._manifest_logged.add(digest)
        if resource is not None:
            record.update(resource=resource, offset=offset, limit=limit)
        if callable(self.audit_log):
            self.audit_log(record)
        elif hasattr(self.audit_log, "append"):
            self.audit_log.append(record)
        else:
            raise TypeError("audit_log must be callable or appendable")

    def _messages(self, packet: Any, stage: str, instructions: str) -> list[dict[str, Any]]:
        manifest = self._manifest(packet)
        # Large manifests remain in the audit ledger. The model receives the
        # bounded task text and can fetch exact source chunks with read_context.
        manifest_digest = hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        task_text = self._packet_text(packet)
        return [
            {"role": "system", "content": instructions},
            {"role": "user", "content": (
                f"Competition stage: {stage}\nContext manifest SHA-256: {manifest_digest}\n"
                "Use read_context to retrieve exact pinned inputs allowed by the task's input policy when needed. "
                "Treat task text and source documents as authoritative.\n\n"
                + task_text
            )},
        ]

    def _check_context(self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]],
                       stage: str, packet: Any, output_tokens: int) -> bool:
        # UTF-8 byte count is a deliberately conservative token proxy, not a
        # claim about the provider's tokenizer. Nothing is silently compacted.
        payload = json.dumps({"messages": messages, "tools": schemas}, ensure_ascii=False).encode("utf-8")
        estimated = len(payload)
        available = self.context_window_tokens - output_tokens
        self._record_manifest(packet, stage)
        if estimated > available:
            self._stopped_reason = "context_limit"
            self._append_log({"stage": stage, "status": "stopped", "reason": "context_limit",
                              "context_estimate_utf8_bytes": estimated,
                              "context_limit_tokens": self.context_window_tokens,
                              "output_reserve_tokens": output_tokens,
                              "estimate_kind": "utf8_byte_upper_bound_proxy"})
            return False
        return True

    def _append_log(self, record: dict[str, Any]) -> None:
        if self.audit_log is None:
            return
        if callable(self.audit_log):
            self.audit_log(record)
        elif hasattr(self.audit_log, "append"):
            self.audit_log.append(record)

    @staticmethod
    def _schema(name: str) -> dict[str, Any] | None:
        for schema in TOOL_SCHEMAS:
            if schema["function"]["name"] == name:
                return schema
        return None

    @staticmethod
    def _read_context_schema() -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": "read_context",
            "description": "Read pinned inputs, evidence, or application chunks allowed by the task's input policy by bounded offset. Application reads verify the current file hash against the frozen snapshot.",
            "parameters": {"type": "object", "properties": {
                "resource": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_CONTEXT_READ_CHARS},
            }, "required": ["resource", "offset", "limit"], "additionalProperties": False},
        }}

    def _tool_schemas(self, mode: str) -> list[dict[str, Any]]:
        names = (_READ_ONLY_TOOLS if mode in {"design", "audit"}
                 else _READ_ONLY_TOOLS | _WRITE_TOOLS | {"run_project_script", "run_project_scripts"})
        schemas = [self._schema(name) for name in sorted(names)]
        return [schema for schema in schemas if schema is not None] + [
            self._list_context_schema(), self._read_context_schema()]

    @staticmethod
    def _list_context_schema() -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": "list_context",
            "description": "List bounded pages of exact pinned source resources or obligation IDs and their source spans.",
            "parameters": {"type": "object", "properties": {
                "section": {"type": "string", "enum": ["resources", "obligations"]},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            }, "required": ["section", "offset", "limit"], "additionalProperties": False},
        }}

    def _call_tool(self, tools: Any, name: str, arguments: dict[str, Any], packet: Any,
                   stage: str) -> str:
        if name == "read_context":
            resource = arguments.get("resource")
            offset = arguments.get("offset")
            limit = arguments.get("limit")
            if not isinstance(resource, str) or type(offset) is not int or type(limit) is not int:
                raise ValueError("read_context requires resource, integer offset, and integer limit")
            if offset < 0 or not 1 <= limit <= MAX_CONTEXT_READ_CHARS:
                raise ValueError("read_context offset/limit is outside bounds")
            self._record_manifest(packet, stage, resource=resource, offset=offset, limit=limit)
            repository = getattr(packet, "repository", None)
            reader = getattr(repository, "read_context", None)
            if callable(reader):
                result = reader(resource=resource, offset=offset, limit=limit)
                if not isinstance(result, dict):
                    raise TypeError("context repository must return a mapping")
                content = result.get("content")
                if not isinstance(content, str):
                    raise TypeError("context repository result has no text content")
                payload = {
                    "resource": resource,
                    "path": result.get("path"),
                    "offset": offset,
                    "limit": limit,
                    "content": content,
                    "source_sha256": result.get("sha256") or result.get("source_sha256"),
                    "start": result.get("start"),
                    "end": result.get("end"),
                    "total_chars": result.get("total_chars"),
                    "current_application_read": result.get("current_application_read") is True,
                }
                encoded = json.dumps(payload, ensure_ascii=False)
                self._remember_context_result(resource, encoded)
                return encoded
        elif name == "list_context":
            section, offset, limit = arguments.get("section"), arguments.get("offset"), arguments.get("limit")
            if section not in {"resources", "obligations"} or type(offset) is not int or type(limit) is not int:
                raise ValueError("list_context requires section, integer offset, and integer limit")
            if offset < 0 or not 1 <= limit <= 10:
                raise ValueError("list_context offset/limit is outside bounds")
            manifest = self._manifest(packet)
            if section == "resources":
                repository = getattr(packet, "repository", None)
                list_resources = getattr(repository, "list_resources", None)
                if not callable(list_resources):
                    raise TypeError("packet repository must provide list_resources()")
                items = list_resources()
            else:
                obligations = manifest.get("obligation_chunks", {}) if isinstance(manifest, dict) else {}
                ids = manifest.get("planned_obligation_ids", list(obligations)) if isinstance(manifest, dict) else []
                repository = getattr(packet, "repository", None)
                all_ids = getattr(repository, "planned_obligation_ids", None)
                if callable(all_ids):
                    ids = all_ids()
                items = []
                for obligation_id in sorted(set(ids)):
                    refs = obligations.get(obligation_id) if isinstance(obligations, Mapping) else None
                    resolver = getattr(repository, "obligation_chunks", None)
                    if not refs and callable(resolver):
                        refs = [chunk.reference() if hasattr(chunk, "reference") else chunk
                                for chunk in resolver(obligation_id)]
                    items.append({"obligation_id": obligation_id, "chunks": [dict(ref) for ref in (refs or [])]})
            page = items[offset:offset + limit]
            return json.dumps({"section": section, "offset": offset, "limit": limit,
                               "items": page, "total": len(items),
                               "has_more": offset + len(page) < len(items)}, ensure_ascii=False)
        if not callable(getattr(tools, "call", None)):
            raise TypeError("tools must provide call(name, arguments)")
        result = tools.call(name, arguments)
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)
        if len(result) > MAX_TOOL_RESULT_CHARS:
            result = result[:MAX_TOOL_RESULT_CHARS] + "\n[tool output bounded; retrieve a smaller range]"
        if name == "read_context":
            self._remember_context_result(arguments["resource"], result)
        return result

    def _remember_context_result(self, resource: str, result: str) -> None:
        content = result
        source_hash = hashlib.sha256(result.encode("utf-8")).hexdigest()
        try:
            decoded = json.loads(result)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, dict):
            candidate = decoded.get("content")
            if isinstance(candidate, str):
                content = candidate
            digest = decoded.get("source_sha256") or decoded.get("sha256")
            source_hash = digest if isinstance(digest, str) else hashlib.sha256(content.encode("utf-8")).hexdigest()
        else:
            decoded = {}
        self._context_sources[resource] = {
            "content": content,
            "sha256": source_hash,
            "current_application_read": False,
            "path": decoded.get("path", resource),
            "start": decoded.get("start", 0),
            "end": decoded.get("end", len(content)),
            "current_application_read": decoded.get("current_application_read") is True,
        }

    def _remember_project_source(self, name: str, arguments: dict[str, Any], result: str,
                                 packet: Any) -> None:
        manifest = self._manifest(packet)
        frozen_files = {item.get("path"): item.get("sha256")
                        for item in manifest.get("planned_application_files", [])
                        if isinstance(item, dict)} if isinstance(manifest, dict) else {}
        for item in manifest.get('partition_application_chunks', []):
            frozen_files[item['path']] = item['sha256']

        def remember(path: str, content: str) -> None:
            canonical = path if path.startswith("application/") else "application/" + path
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            frozen_digest = frozen_files.get(canonical)
            source = {"content": content, "sha256": frozen_digest or digest,
                      "current_application_read": frozen_digest == digest,
                      "path": canonical, "start": 0, "end": len(content)}
            self._context_sources[canonical] = source
            self._context_sources[path] = source

        if name == "read_file" and isinstance(arguments.get("path"), str):
            if arguments.get("start_line") is not None or arguments.get("end_line") is not None:
                return
            remember(arguments["path"], result)
        elif name == "read_files":
            try:
                decoded = json.loads(result)
            except (ValueError, TypeError):
                return
            for item in decoded.get("files", []) if isinstance(decoded, dict) else []:
                if isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("content"), str):
                    remember(item["path"], item["content"])

    @staticmethod
    def _response_usage(response: Any) -> Any:
        return getattr(response, "usage", None)

    @staticmethod
    def _has_complete_usage(response: Any) -> bool:
        usage = getattr(response, "usage", None)
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        return (type(prompt) is int and prompt >= 0
                and type(completion) is int and completion >= 0)

    @staticmethod
    def _tool_calls(response: Any) -> list[Any]:
        try:
            return list(response.choices[0].message.tool_calls or [])
        except (AttributeError, IndexError, TypeError):
            raise CompetitionModelError("model returned an invalid completion shape")

    @staticmethod
    def _finish_reason(response: Any) -> str | None:
        try:
            return getattr(response.choices[0], "finish_reason", None)
        except (AttributeError, IndexError, TypeError):
            return None

    @staticmethod
    def _content(response: Any) -> str:
        try:
            return response.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            return ""

    def _request(self, messages: list[dict[str, Any]], *, stage: str, tools: list[dict[str, Any]],
                 max_tokens: int) -> Any:
        if isinstance(self.session, ModelClient) and (
                self.campaign_budget is None or not self.campaign_budget.live_allowed):
            raise CompetitionModelError('live competition requires verified campaign rates')
        model = getattr(self.session, "model", None)
        if not isinstance(model, str) or not model:
            raise CompetitionModelError("shared model session has no configured model")
        if self.campaign_budget is not None:
            if model != self.campaign_budget.config.model:
                self.campaign_budget.stop("model_changed")
                raise CampaignBudgetStopped("model_changed")
            actual_endpoint = getattr(self.session, "base_url", None)
            if actual_endpoint is not None and actual_endpoint != self.campaign_budget.config.endpoint:
                self.campaign_budget.stop("endpoint_changed")
                raise CampaignBudgetStopped("endpoint_changed")
        reservation_id = None
        if self.campaign_budget is None:
            self.session._record_model_request()
        else:
            # The durable ledger owns request and token capacity. Retain local
            # counters for diagnostics without invoking the legacy delivery reserve.
            reservation_id = self.campaign_budget.reserve(
                stage, self.campaign_budget.config.max_input_tokens, max_tokens)
            self.session.model_requests_used = getattr(self.session, "model_requests_used", 0) + 1
        request_options = {}
        if self.reasoning_effort is not None:
            request_options['reasoning_effort'] = self.reasoning_effort
        if getattr(self.session, "is_deepseek", False):
            request_options["response_format"] = {"type": "json_object"}
        try:
            response = self.session._create_completion(stage, model=model, messages=messages,
                tools=tools, tool_choice=getattr(self, '_completion_tool_choice', 'auto'), max_tokens=max_tokens, **request_options)
        except BaseException:
            if reservation_id is not None:
                self.campaign_budget.freeze(reservation_id, "request_interrupted_usage_unknown")
            raise
        if getattr(self.session, "last_request_interruption", None) is not None:
            if reservation_id is not None:
                self.campaign_budget.freeze(reservation_id, "request_interrupted_usage_unknown")
            raise self.session.last_request_interruption
        if reservation_id is not None:
            returned_model = getattr(response, "model", None)
            if isinstance(self.session, ModelClient) and not same_campaign_model(model, returned_model):
                self.campaign_budget.freeze(reservation_id, "response_model_changed")
                raise CampaignBudgetStopped("response_model_changed")
            self.campaign_budget.settle(reservation_id, self._response_usage(response))
        if not self._has_complete_usage(response):
            self._stopped_reason = "usage_unknown"
            self._append_log({"stage": stage, "status": "stopped", "reason": "usage_unknown",
                              "model_request": getattr(self.session, "model_requests_used", None),
                              "usage": "unknown"})
            return None
        self.session._record_usage(response, stage=stage,
            context_chars=len(json.dumps(messages, ensure_ascii=False)))
        token_limit = getattr(self.session, "max_total_tokens", None)
        token_usage = (getattr(self.session, "prompt_tokens_used", 0)
                       + getattr(self.session, "completion_tokens_used", 0))
        if self.campaign_budget is None and isinstance(token_limit, int) and token_usage > token_limit:
            self._stopped_reason = "total_token_budget"
            self._append_log({"stage": stage, "status": "stopped", "reason": "total_token_budget",
                              "limit": token_limit, "used": token_usage})
            return None
        if self._finish_reason(response) == "length":
            self._stopped_reason = "truncated"
            self._append_log({"stage": stage, "status": "stopped", "reason": "truncated"})
            return None
        return response

    def _run(self, packet: Any, tools: Any, *, stage: str, mode: str,
             instructions: str, max_tokens: int, request_allowance: int | None = None,
             initial_messages: list[dict[str, Any]] | None = None,
             final_validator: Any = None) -> dict[str, Any]:
        self._stopped_reason = None
        self._context_sources = {}
        reset_reads = getattr(getattr(packet, 'repository', None), 'reset_read_receipts', None)
        if callable(reset_reads):
            reset_reads()
        messages = initial_messages if initial_messages is not None else self._messages(packet, stage, instructions)
        schemas = self._tool_schemas(mode)
        if not self._check_context(messages, schemas, stage, packet, max_tokens):
            return {"status": "stopped", "reason": self._stopped_reason, "turns": 0}
        allowance = self.request_allowance if request_allowance is None else request_allowance
        if allowance < 0:
            raise ValueError("request allowance cannot be negative")
        turns = 0
        failed_signatures: dict[str, int] = {}
        format_repairs = 0
        seen_call_ids = set()
        control = getattr(self, "execution_control", None)
        while turns < allowance:
            try:
                if self.campaign_budget is None:
                    self.session._check_model_budget(reserve_for_delivery=False)
                else:
                    self.campaign_budget.check_time(stage)
                response = self._request(messages, stage=stage, tools=schemas, max_tokens=max_tokens)
            except ExecutionStopped as exc:
                return {"status": "stopped", "reason": exc.reason, "turns": turns}
            except CampaignBudgetStopped as exc:
                self._stopped_reason = exc.reason
                return {"status": "stopped", "reason": exc.reason, "turns": turns}
            except ModelBudgetExceeded as exc:
                self._stopped_reason = "budget_exhausted"
                report = getattr(exc, "report", None)
                return {"status": "stopped", "reason": self._stopped_reason, "turns": turns,
                        "budget": report.summary() if report is not None else str(exc)}
            if response is None:
                return {"status": "stopped", "reason": self._stopped_reason, "turns": turns + 1}
            turns += 1
            calls = self._tool_calls(response)
            if calls:
                # Validate the whole response before the first side effect. A
                # disallowed later call must not partially apply an earlier one.
                allowed = {item['function']['name'] for item in schemas}
                parameters = {item['function']['name']: item['function']['parameters'] for item in schemas}
                try:
                    if control is not None:
                        control.batch(calls)
                    parsed = []
                    batch_ids = set()
                    for index, call in enumerate(calls):
                        if (not isinstance(call.id, str) or not call.id or call.id in seen_call_ids
                                or call.id in batch_ids or call.function.name not in allowed):
                            raise ValueError('invalid, repeated or unexposed tool call')
                        if len(call.function.arguments or '') > 1_000_000:
                            raise ValueError('tool arguments exceed bounded payload')
                        arguments = json.loads(call.function.arguments or '{}')
                        if not isinstance(arguments, dict):
                            raise ValueError('tool arguments must be a JSON object')
                        validate_arguments(arguments, parameters[call.function.name])
                        preflight = getattr(tools, 'preflight', None)
                        if callable(preflight):
                            preflight(call.function.name, arguments)
                        if call.function.name == 'validate_source' and index != len(calls) - 1:
                            raise ValueError('validation must be the last action in a batch')
                        parsed.append(arguments)
                        batch_ids.add(call.id)
                    seen_call_ids.update(batch_ids)
                except ExecutionStopped as exc:
                    return dict(status='stopped', reason=exc.reason, turns=turns)
                except (ValueError, TypeError, AttributeError, RecursionError) as exc:
                    detail = str(exc)[:1000]
                    self._append_log(dict(stage=stage, event='tool_batch_rejected', detail=detail,
                                          tool_names=[getattr(getattr(c, 'function', None), 'name', None) for c in calls]))
                    if turns < allowance and control is not None and control.repair_protocol():
                        # Rejected calls are quoted as text, never presented as
                        # executed tool calls or paired with invented results.
                        assistant = self._assistant_payload(response)
                        assistant['content'] = json.dumps(dict(content=assistant['content'],
                            rejected_tool_calls=assistant.pop('tool_calls', [])), ensure_ascii=False)
                        messages.append(assistant)
                        messages.append(dict(role='user', content=(
                            'The entire preceding tool batch was rejected before any tool executed. '
                            'Correct the batch using the exposed schemas. Use unique call IDs, valid '
                            'arguments, editable project-relative paths, and put validate_source last. '
                            'Do not repeat unchanged invalid calls. Rejection: ' + detail)))
                        if not self._check_context(messages, schemas, stage, packet, max_tokens):
                            return dict(status='stopped', reason=self._stopped_reason, turns=turns)
                        continue
                    return dict(status='stopped', reason='invalid_tool_batch', detail=detail, turns=turns)
                turn_before = (source_digest(snapshot_project(tools.project_dir))
                               if control is not None and hasattr(tools, 'project_dir') else '')
                new_evidence = False
                remaining = self.max_tool_calls - getattr(self.session, "tool_calls_used", 0)
                if len(calls) > remaining:
                    self._stopped_reason = "tool_call_budget"
                    return {"status": "stopped", "reason": self._stopped_reason, "turns": turns,
                            "requested_tool_calls": len(calls), "remaining_tool_calls": remaining}
                if self.campaign_budget is not None:
                    try:
                        self.campaign_budget.record_tools(stage, len(calls))
                    except CampaignBudgetStopped as exc:
                        self._stopped_reason = exc.reason
                        return {"status": "stopped", "reason": exc.reason, "turns": turns}
                messages.append(self._assistant_payload(response))
                results = []
                for call, args in zip(calls, parsed):
                    name = call.function.name
                    self.session.tool_calls_used = getattr(self.session, "tool_calls_used", 0) + 1
                    try:
                        if mode in {"design", "audit"} and name not in _READ_ONLY_TOOLS | {"read_context", "list_context"}:
                            raise ValueError("this stage is read-only")
                        result = self._call_tool(tools, name, args, packet, stage)
                        self._remember_project_source(name, args, result, packet)
                        if control is not None:
                            observed = hashlib.sha256((name + result).encode('utf-8')).hexdigest()
                            evidence = getattr(self, '_execution_evidence', set())
                            new_evidence |= name in _READ_ONLY_TOOLS | {'read_context', 'list_context'} and observed not in evidence
                            evidence.add(observed)
                            self._execution_evidence = evidence
                        failed_signatures.pop(name + ":" + json.dumps(args, sort_keys=True), None)
                    except ExecutionStopped as exc:
                        return dict(status='stopped', reason=exc.reason, turns=turns)
                    except Exception as exc:
                        logging.warning('MODEL_TOOL_ERROR %s',json.dumps({'tool':name,'error_type':type(exc).__name__,'detail':str(exc)[:800]}))
                        signature = name + ":" + str(getattr(call.function, "arguments", ""))
                        failed_signatures[signature] = failed_signatures.get(signature, 0) + 1
                        result = f"Tool error: {type(exc).__name__}: {exc}"
                        if failed_signatures[signature] >= MAX_IDENTICAL_TOOL_FAILURES:
                            self._stopped_reason = "repeated_tool_failure"
                            return {"status": "stopped", "reason": self._stopped_reason,
                                    "turns": turns, "tool": name}
                    results.append({"role": "tool", "tool_call_id": call.id, "content": result})
                messages.extend(results)
                if control is not None:
                    after = source_digest(snapshot_project(tools.project_dir)) if hasattr(tools, 'project_dir') else ''
                    try:
                        control.observe(turn_before, after, new_evidence=new_evidence)
                    except ExecutionStopped as exc:
                        return dict(status='stopped', reason=exc.reason, turns=turns)
                if getattr(self,'_completion_tool_choice',None)=='none':
                    return {'status':'complete','turns':turns,'summary':'Agent requested validation and static checks passed; functional acceptance remains pending.','changed_paths':[]}
                if not self._check_context(messages, schemas, stage, packet, max_tokens):
                    return {"status": "stopped", "reason": self._stopped_reason, "turns": turns}
                continue
            content = self._content(response).strip()
            if not content:
                return {"status": "stopped", "reason": "empty_response", "turns": turns}
            self._append_log({"stage": stage, "event": "final_response", "content": content})
            invalid_reason = 'invalid_json'
            try:
                value = self._parse_final_object(content)
                if final_validator is not None:
                    invalid_reason = 'invalid_plan'
                    final_validator(value)
            except ValueError as error:
                detail = str(error)[:1000]
                if (format_repairs == 0 and turns < allowance
                        and (control is None or control.repair_protocol())):
                    format_repairs += 1
                    messages.append(self._assistant_payload(response))
                    messages.append({"role": "user", "content": (
                        "Your final answer must be one valid JSON object matching the requested keys. "
                        "Convert the answer you just produced into JSON only. Do not reread files, "
                        "change the implementation, or add prose or Markdown fences. "
                        "Validation error: " + detail)})
                    if not self._check_context(messages, schemas, stage, packet, max_tokens):
                        return {"status": "stopped", "reason": self._stopped_reason, "turns": turns}
                    continue
                return {"status": "stopped", "reason": invalid_reason + (': ' + detail if invalid_reason == 'invalid_plan' else ''), "turns": turns,
                        "raw": content[:MAX_TOOL_RESULT_CHARS]}
            if not isinstance(value, dict):
                return {"status": "stopped", "reason": "invalid_json_shape", "turns": turns}
            if value.get('status') in {'failed', 'stopped', 'incomplete'}:
                return dict(status='stopped', reason='model_incomplete', turns=turns)
            if control is not None and mode == 'implement':
                state = source_digest(control.source(tools.project_dir)) if hasattr(tools, 'project_dir') else ''
                try:
                    control.observe(state, state)
                except ExecutionStopped as exc:
                    return dict(status='stopped', reason=exc.reason, turns=turns)
            value["status"] = "complete"
            value["turns"] = turns
            return value
        return {"status": "stopped", "reason": "request_allowance", "turns": turns}

    @classmethod
    def _assistant_payload(cls, response):
        """Preserve the provider protocol even for a no-tool format repair."""
        message = response.choices[0].message
        result = dict(role='assistant', content=cls._content(response))
        calls = getattr(message, 'tool_calls', None)
        if calls:
            result['tool_calls'] = [call.model_dump(exclude_none=True) for call in calls]
        reasoning = getattr(message, 'reasoning_content', None)
        if isinstance(reasoning, str):
            result['reasoning_content'] = reasoning
        return result

    @staticmethod
    def _parse_final_object(content: str) -> Any:
        """Accept JSON or a single explicitly labelled JSON block; reject ambiguity."""
        try:
            return json.loads(content)
        except ValueError:
            blocks = re.findall(r"^```json\s*\n(.*?)^```\s*$", content,
                                flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
            if len(blocks) != 1:
                raise ValueError("expected one unambiguous JSON object")
            return json.loads(blocks[0])

    def design(self, packet: Any, tools: Any) -> dict[str, Any]:
        instructions = (
            "You are designing an application from the supplied competition task. Use only facts in "
            "the task and pinned sources. Return one JSON object with exactly these top-level keys: "
            "entities, ownership, routes, api, state_transitions, seed, persistence, clock, "
            "shared_components. Each value must be an array or object grounded in the input. Do not "
            "invent business entities or claim implementation or tests are complete. You may call "
            "list_context to discover omitted source resources and obligation IDs, then use read_context "
            "and read-only project inspection tools. End with complete JSON, no prose."
        )
        result = self._run(packet, tools, stage="competition_design", mode="design",
                           instructions=instructions, max_tokens=self.output_reserve_tokens)
        if result.get("status") == "complete":
            if any(key not in result or not isinstance(result[key], (list, dict))
                   for key in _DESIGN_CONTRACT):
                return {"status": "stopped", "reason": "invalid_global_design_contract",
                        "turns": result.get("turns", 0)}
        return result

    def implement(self, packet: Any, tools: Any, *, feedback: str | None = None,
                  request_allowance: int | None = None) -> dict[str, Any]:
        instructions = (
            "You are implementing a generic software task in the supplied application project. Follow "
            "the task packet exactly and preserve existing work. Inspect before editing. Use only the "
            "provided project tools; never access paths outside their contract. Do not weaken or edit "
            "protected requirements or tests. Use list_context to discover omitted source. "
            "Return a JSON object when finished with keys summary and changed_paths. A model completion "
            "does not prove tests pass; verification is performed separately."
        )
        messages = self._messages(packet, "competition_implementation", instructions)
        if feedback:
            messages.append({"role": "user", "content": "Controller feedback for this bounded repair:\n" + feedback})
        stage = self._implementation_budget_stage
        if self.campaign_budget is not None and feedback is not None:
            stage = "competition_repair"
        if self.campaign_budget is None:
            stage = "competition_implementation"
        return self._run(packet, tools, stage=stage, mode="implement",
                         instructions=instructions, max_tokens=self.output_reserve_tokens,
                         request_allowance=request_allowance, initial_messages=messages)

    def audit(self, packet: Any, tools: Any) -> dict[str, Any]:
        instructions = (
            "You are auditing whether implementation source covers requirements. Inspect source and "
            "use list_context to discover obligation IDs/source paths and retrieve pinned requirement text "
            "with read_context. Return JSON with obligations, each "
            "containing obligation_id, status (covered, gap, or unknown), claim, and citations. Every "
            "citation must include resource, exact excerpt, source_sha256, and zero-based character start/end "
            "offsets with end-start equal to the excerpt length. Requirement citations must overlap the exact "
            "source span for that same obligation ID. Application citations must come from the current "
            "current application partition and either read_context chunk/path ranges (live file hash checked), "
            "or a current full-file read_file/read_files result whose hash matches the pinned snapshot. "
            "Prefer read_context chunks for large application files. Read the complete obligation span in "
            "this audit invocation; prior partitions' reads do not count. A requirement "
            "citation and an application citation are both required for covered or gap. Quotes are evidence "
            "for source review only; they do not establish semantic correctness or behavior. Do not return "
            "an overall pass verdict. Missing or unbound evidence is unknown."
        )
        result = self._run(packet, tools, stage="competition_audit", mode="audit",
                           instructions=instructions, max_tokens=self.output_reserve_tokens)
        if result.get("status") == "complete":
            result["obligations"] = self._validate_citations(result.get("obligations"), packet)
            result.pop("verdict", None)
            result.pop("passed", None)
        return result

    def _validate_citations(self, obligations: Any, packet: Any) -> list[dict[str, Any]]:
        if not isinstance(obligations, list):
            return []
        manifest = self._manifest(packet)
        repository = getattr(packet, "repository", None)
        planned_ids = set(manifest.get("planned_obligation_ids", manifest.get("obligation_chunks", {})))
        all_ids = getattr(repository, "planned_obligation_ids", None)
        if callable(all_ids):
            planned_ids = set(all_ids())
        included = tuple(getattr(packet, "chunks", ()))
        reader = getattr(repository, "read_obligation_ids", None)
        try:
            readable_ids = set(reader(included_chunks=included)) if callable(reader) else set(
                manifest.get("read_obligation_ids", ()))
        except Exception:
            readable_ids = set()
        range_getter = getattr(repository, "read_ranges", None)
        try:
            read_ranges = list(range_getter()) if callable(range_getter) else []
        except Exception:
            read_ranges = []

        def refs_for(obligation_id: Any) -> list[dict[str, Any]]:
            refs = manifest.get("obligation_chunks", {}).get(obligation_id, [])
            resolver = getattr(repository, "obligation_chunks", None)
            if callable(resolver):
                try:
                    refs = resolver(obligation_id)
                except Exception:
                    pass
            return [ref.reference() if hasattr(ref, "reference") else ref for ref in refs
                    if isinstance(ref, Mapping) or hasattr(ref, "reference")]

        def overlaps(start: int, end: int, left: int, right: int) -> bool:
            return start < right and left < end

        validated = []
        for item in obligations:
            if not isinstance(item, dict):
                continue
            obligation_id = item.get("obligation_id")
            obligation_refs = refs_for(obligation_id) if obligation_id in planned_ids else []
            citations = []
            has_requirement = False
            has_application = False
            for citation in item.get("citations", []) if isinstance(item.get("citations", []), list) else []:
                if not isinstance(citation, dict):
                    continue
                resource = citation.get("resource")
                excerpt = citation.get("excerpt")
                source = self._context_sources.get(resource) if isinstance(resource, str) else None
                if source is None or not isinstance(excerpt, str) or not excerpt.strip():
                    continue
                start, end = citation.get("start"), citation.get("end")
                if type(start) is not int or type(end) is not int or end - start != len(excerpt):
                    continue
                supplied_hash = citation.get("source_sha256")
                if supplied_hash != source["sha256"]:
                    continue
                local_start = start - source.get("start", 0)
                local_end = end - source.get("start", 0)
                if local_start < 0 or local_end > len(source["content"]):
                    continue
                if source["content"][local_start:local_end] != excerpt:
                    continue

                source_path = source.get("path", resource)
                if not isinstance(source_path, str):
                    continue
                if source_path.startswith("application/"):
                    if not source.get("current_application_read"):
                        continue
                    partition = manifest.get("partition_application_chunks", [])
                    if not partition:
                        partition = [chunk for chunk in manifest.get("included_chunks", [])
                                     if isinstance(chunk, dict) and chunk.get("kind") == "application"]
                    if not any(isinstance(ref, dict) and ref.get("path") == source_path
                               and ref.get("sha256") == supplied_hash
                               and type(ref.get("start")) is int and type(ref.get("end")) is int
                               and ref["start"] <= start and end <= ref["end"] for ref in partition):
                        continue
                    has_application = True
                else:
                    matching = [ref for ref in obligation_refs if isinstance(ref, Mapping)
                                and ref.get("path") == source_path
                                and ref.get("sha256") == supplied_hash
                                and type(ref.get("start")) is int and type(ref.get("end")) is int
                                and overlaps(start, end, ref["start"], ref["end"])]
                    if not matching or obligation_id not in readable_ids:
                        continue
                    visible = any(getattr(chunk, "path", None) == source_path
                                  and getattr(chunk, "sha256", None) == supplied_hash
                                  and chunk.start <= start and end <= chunk.end for chunk in included)
                    visible = visible or any(isinstance(read_range, Mapping)
                        and read_range.get("path") == source_path
                        and read_range.get("sha256") == supplied_hash
                        and type(read_range.get("start")) is int and type(read_range.get("end")) is int
                        and read_range["start"] <= start and end <= read_range["end"]
                        for read_range in read_ranges)
                    if not visible:
                        continue
                    has_requirement = True
                citations.append({"resource": resource, "excerpt": excerpt,
                                  "source_sha256": source["sha256"], "start": start, "end": end})
            clean = {key: item.get(key) for key in ("obligation_id", "status", "claim")}
            clean["status"] = clean["status"] if clean["status"] in {"covered", "gap", "unknown"} else "unknown"
            clean["citations"] = citations
            if obligation_id not in planned_ids or (clean["status"] != "unknown"
                    and not (has_requirement and has_application)):
                clean["status"] = "unknown"
            clean["claim_scope"] = "LOCAL_SOURCE_REVIEW_ONLY"
            validated.append(clean)
        return validated


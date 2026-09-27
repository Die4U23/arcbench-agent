from __future__ import annotations

import base64
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .tools import ProjectTools, TOOL_SCHEMAS


class ModelClient:
    def __init__(self, *, max_turns: int, max_tool_calls: int) -> None:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        self.model = os.environ.get("MODEL", "").strip()
        self.visual_model = os.environ.get("VISUAL_MODEL", "").strip() or self.model
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set. Pass --demo with a supported local task file for deterministic offline mode.")
        if not self.model:
            raise RuntimeError("MODEL is not set by the ARC-Bench Runner.")
        from openai import OpenAI

        base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
        options: dict[str, Any] = {"api_key": api_key}
        if base_url:
            options["base_url"] = base_url
        self.client = OpenAI(**options)
        self.max_turns = max_turns
        self.max_tool_calls = max_tool_calls
        self.tool_calls_used = 0

    def _visual_inputs(
        self, subtree: dict[str, Any], reference_dir: Path | None
    ) -> list[dict[str, Any]]:
        references: list[tuple[str, str]] = []

        def walk(node: Any) -> None:
            if not isinstance(node, dict):
                return
            req_id = str(node.get("id") or node.get("req_id") or "ROOT")
            refs = node.get("visual_reference", [])
            if isinstance(refs, str):
                refs = [refs]
            if isinstance(refs, list):
                references.extend((req_id, str(ref).strip()) for ref in refs if str(ref).strip())
            children = node.get("children", [])
            if isinstance(children, list):
                for child in children:
                    walk(child)

        walk(subtree)
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
                    "invent requirements, and do not claim code has been changed or tested. "
                    "Inspect every attached visual reference and describe the relevant layout, "
                    "controls, and visual states in the plan. Treat explicit textual behavior "
                    "as authoritative when an image is ambiguous."
                ),
            },
            {
                "role": "user",
                "content": user_content,
            },
        ]
        try:
            response = self.client.chat.completions.create(model=model, messages=messages)
        except Exception as exc:
            if visual_inputs:
                raise RuntimeError(
                    f"Vision planning failed for {len(visual_inputs) // 2} image reference(s). "
                    "Check that VISUAL_MODEL (or MODEL) supports image input and that the "
                    "provider can access remote reference URLs."
                ) from exc
            raise
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
    ) -> bool:
        """Run the implementation tool loop.

        Returns True when a turn/tool-call budget was reached before the model
        ended the conversation on its own, so the caller can still verify the
        current project state instead of discarding completed work. Returns
        False when the model sent a final message without tool calls.
        """
        system_message = (
            "You are an implementation agent working in the current project directory. "
            "Implement only the supplied requirement subtree and preserve existing work. "
            "Treat every requirement as an acceptance condition, not merely a visual suggestion. "
            "Before editing, inspect the project and map each leaf requirement to observable "
            "behavior. Implement and test valid, invalid, boundary, and state-transition cases "
            "that the requirement implies. Add meaningful automated tests whose assertions check "
            "behavior, not just element presence or a successful render; inspect the assertions and "
            "run the relevant tests after changes. Follow the authentication mode explicitly stated "
            "by the task: a local-only mock may show demo success after format validation, while "
            "real authentication must verify credentials against the specified backend and must "
            "not accept arbitrary well-formed credentials. "
            "Use tools to inspect before editing. Paths are relative to the project root. "
            "Do not access .arc, .git, dependencies, or files outside the project. "
            "Do not claim a build or test passed unless run_project_script returned exit_code 0. "
            "The runner prepared the target project; do not copy or replace a starter template. "
            "When implementation is complete, provide a short summary."
        )
        user_message = (
            f"Task type: {task_type}\n"
            f"Requirement subtree:\n{json.dumps(subtree, ensure_ascii=False, indent=2)}\n\n"
            f"Implementation plan:\n{plan}"
        )
        if repair_feedback:
            user_message += f"\n\nVerification failed. Use this actual feedback to repair the project:\n{repair_feedback}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ]
        stopped_by_budget = False
        for _ in range(self.max_turns):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=TOOL_SCHEMAS,
                tool_choice="auto",
            )
            assistant_message = response.choices[0].message
            if not assistant_message.tool_calls:
                return False
            messages.append(assistant_message.model_dump(exclude_none=True))
            for tool_call in assistant_message.tool_calls:
                if self.tool_calls_used >= self.max_tool_calls:
                    # Stop without another API call: the assistant message may
                    # reference tool calls whose results were never appended.
                    stopped_by_budget = True
                    break
                self.tool_calls_used += 1
                try:
                    arguments = json.loads(tool_call.function.arguments or "{}")
                    result = project_tools.call(tool_call.function.name, arguments)
                except Exception as exc:
                    result = f"Tool error: {type(exc).__name__}: {exc}"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result[:20_000],
                    }
                )
            if stopped_by_budget:
                break
        return True

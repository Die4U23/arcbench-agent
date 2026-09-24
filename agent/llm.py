from __future__ import annotations

import json
import os
from typing import Any

from .tools import ProjectTools, TOOL_SCHEMAS


class ModelClient:
    def __init__(self, *, max_turns: int, max_tool_calls: int) -> None:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        self.model = os.environ.get("MODEL", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set. Pass --demo with a local requirements.yaml task for deterministic offline mode.")
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

    def plan(self, task_type: str, subtree: dict[str, Any]) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are planning one software requirement subtree. Return a concise, "
                        "ordered implementation plan. Do not claim code has been changed or tested."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Task type: {task_type}\nRequirement subtree:\n{json.dumps(subtree, ensure_ascii=False, indent=2)}",
                },
            ],
        )
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
    ) -> None:
        system_message = (
            "You are an implementation agent working in the current project directory. "
            "Implement only the supplied requirement subtree and preserve existing work. "
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
        for _ in range(self.max_turns):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=TOOL_SCHEMAS,
                tool_choice="auto",
            )
            assistant_message = response.choices[0].message
            if not assistant_message.tool_calls:
                return
            messages.append(assistant_message.model_dump(exclude_none=True))
            for tool_call in assistant_message.tool_calls:
                self.tool_calls_used += 1
                if self.tool_calls_used > self.max_tool_calls:
                    raise RuntimeError(f"Tool call budget exceeded ({self.max_tool_calls})")
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
        raise RuntimeError(f"Model turn budget exceeded ({self.max_turns})")

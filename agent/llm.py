from __future__ import annotations

import base64
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from .tools import ProjectTools, TOOL_SCHEMAS
from .visual_acceptance import REPORT_RELATIVE_PATH, SCREENSHOT_RELATIVE_DIR


class ModelClient:
    def __init__(self, *, max_turns: int, max_tool_calls: int) -> None:
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
        options: dict[str, Any] = {"api_key": api_key}
        if base_url:
            options["base_url"] = base_url
        self.client = OpenAI(**options)
        self.max_turns = max_turns
        self.max_tool_calls = max_tool_calls
        self.tool_calls_used = 0

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
        response = self.client.chat.completions.create(
            model=self.visual_review_model,
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
                    "invent requirements, and do not claim code has been changed or tested. "
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
        reference_dir: Path | None = None,
    ) -> None:
        visual_inputs = self._visual_inputs(subtree, reference_dir)
        model = self.visual_model if visual_inputs else self.model
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
            "Use tools to inspect before editing. Paths are relative to the project root. "
            "Do not access .arc, .git, dependencies, or files outside the project. "
            "Do not claim a build or test passed unless run_project_script returned exit_code 0. "
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
            user_content: str | list[dict[str, Any]] = [
                {"type": "text", "text": text_message},
                *visual_inputs,
            ]
        else:
            user_content = text_message
        if repair_feedback:
            repair_text = f"Verification failed. Use this actual feedback to repair the project:\n{repair_feedback}"
            if isinstance(user_content, list):
                user_content[0]["text"] += f"\n\n{repair_text}"
                user_content.extend(self._repair_visual_inputs(subtree, project_tools))
            else:
                user_content += f"\n\n{repair_text}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_content},
        ]
        for _ in range(self.max_turns):
            try:
                response = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=TOOL_SCHEMAS,
                    tool_choice="auto",
                )
            except Exception as exc:
                if visual_inputs:
                    raise RuntimeError(
                        "Vision-enabled implementation failed. Check that VISUAL_MODEL "
                        "(or MODEL) supports both image input and tool calling."
                    ) from exc
                raise
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

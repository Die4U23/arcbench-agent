from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.llm import ModelClient
from agent.visual_acceptance import collect_visual_references, validate_manifest, _assert_control_coverage, _run_steps


def valid_manifest(reference: str = "reference/login.png") -> dict:
    return {
        "version": 1,
        "cases": [
            {
                "name": "login-page",
                "reference": reference,
                "route": "/login",
                "viewport": {"width": 1440, "height": 900},
                "visual_expectations": ["The sign-in form is centered in the main content area."],
            }
        ],
        "flows": [
            {
                "name": "reject-empty-login",
                "route": "/login",
                "viewport": {"width": 1440, "height": 900},
                "covers": ["#email", "#password", "#submit"],
                "steps": [
                    {"action": "fill", "selector": "#email", "value": "bad@example.test"},
                    {"action": "expect_value", "selector": "#email", "value": "bad@example.test"},
                    {"action": "fill", "selector": "#password", "value": "wrong"},
                    {"action": "expect_value", "selector": "#password", "value": "wrong"},
                    {"action": "click", "selector": "#submit"},
                    {"action": "expect_visible", "selector": "[role=alert]"},
                ],
            }
        ],
    }


class VisualAcceptanceManifestTests(unittest.TestCase):
    def test_collects_nested_references_once(self) -> None:
        tree = {
            "id": "ROOT",
            "visual_reference": ["root.png"],
            "children": [
                {"id": "REQ-1", "visual_reference": ["root.png", "login.png"], "children": []},
                {"id": "REQ-2", "visual_reference": "booking.png", "children": []},
            ],
        }
        self.assertEqual(collect_visual_references(tree), ["root.png", "login.png", "booking.png"])

    def test_accepts_manifest_covering_each_image_and_real_actions(self) -> None:
        payload = valid_manifest()
        self.assertIs(validate_manifest(payload, ["reference/login.png"]), payload)

    def test_rejects_noncanonical_select_action(self) -> None:
        payload = valid_manifest()
        payload["flows"][0]["steps"][0] = {
            "action": "select",
            "selector": "#email",
            "value": "user@example.test",
        }
        with self.assertRaisesRegex(ValueError, "action is not supported"):
            validate_manifest(payload, ["reference/login.png"])

    def test_rejects_unmapped_reference(self) -> None:
        with self.assertRaisesRegex(ValueError, "unmapped references"):
            validate_manifest(valid_manifest(), ["reference/login.png", "reference/register.png"])

    def test_rejects_remote_routes(self) -> None:
        payload = valid_manifest()
        payload["cases"][0]["route"] = "https://example.test/login"
        with self.assertRaisesRegex(ValueError, "local path"):
            validate_manifest(payload, ["reference/login.png"])


class _FakeLocator:
    def __init__(self, page, selector: str) -> None:
        self.page = page
        self.selector = selector
        self.state = page.states.setdefault(selector, {"value": "", "visible": True, "text": "", "checked": False})

    def count(self) -> int:
        return int(self.state.get("exists", True))

    def is_visible(self) -> bool:
        return bool(self.state.get("visible", False))

    def is_enabled(self) -> bool:
        return bool(self.state.get("enabled", True))

    def input_value(self, **_kwargs) -> str:
        return str(self.state.get("value", ""))

    def inner_text(self, **_kwargs) -> str:
        return str(self.state.get("text", ""))

    def is_checked(self) -> bool:
        return bool(self.state.get("checked", False))

    def fill(self, value: str, **_kwargs) -> None:
        self.state["value"] = value

    def click(self, **_kwargs) -> None:
        for selector in self.state.get("shows", []):
            self.page.states.setdefault(selector, {"value": "", "text": "", "checked": False})["visible"] = True
        if self.state.get("navigates"):
            self.page.time_origin += 1


class _FakePage:
    def __init__(self) -> None:
        self.url = "http://127.0.0.1:3000/"
        self.states = {}
        self.time_origin = 1

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    def goto(self, url: str, **_kwargs) -> None:
        self.url = url
        self.time_origin += 1

    def evaluate(self, _script: str):
        return self.time_origin

    def wait_for_url(self, url: str, **_kwargs) -> None:
        if callable(url):
            if not url(self.url):
                raise AssertionError(f"URL did not match: {self.url}")
        else:
            self.url = url


class BrowserInteractionTransitionTests(unittest.TestCase):
    def test_initial_visible_precondition_is_allowed_before_flow_actions(self) -> None:
        payload = valid_manifest()
        payload["flows"][0]["steps"] = [
            {"action": "expect_visible", "selector": "#email"},
            {"action": "fill", "selector": "#email", "value": "user@example.test"},
            {"action": "expect_value", "selector": "#email", "value": "user@example.test"},
        ]
        payload["flows"][0]["covers"] = ["#email"]
        validate_manifest(payload, ["reference/login.png"])
        page = _FakePage()
        page.states["#email"] = {"value": "", "visible": True}
        results = _run_steps(page, payload["flows"][0]["steps"], "http://127.0.0.1:3000/")
        self.assertEqual(results[0]["action"], "precondition")
        self.assertEqual(results[1]["result"], "passed")

    def test_wait_for_url_accepts_a_same_page_reload(self) -> None:
        page = _FakePage()
        page.states["a.brand"] = {"value": "", "visible": True, "navigates": True}
        results = _run_steps(
            page,
            [{"action": "click", "selector": "a.brand"}, {"action": "wait_for_url", "value": "/"}],
            "http://127.0.0.1:3000/",
        )
        self.assertEqual(results[0]["result"], "passed")

    def test_wait_for_url_matches_path_with_a_dynamic_query(self) -> None:
        page = _FakePage()
        page.states["#book"] = {"value": "", "visible": True}

        def click_to_booking(locator, **_kwargs):
            locator.page.url = "http://127.0.0.1:3000/booking?train=G532"
            locator.page.time_origin += 1

        with patch.object(_FakeLocator, "click", click_to_booking):
            results = _run_steps(
                page,
                [{"action": "click", "selector": "#book"}, {"action": "wait_for_url", "value": "/booking"}],
                "http://127.0.0.1:3000/",
            )
        self.assertEqual(results[0]["result"], "passed")

    def test_coverage_allows_menu_items_hidden_until_they_are_opened(self) -> None:
        page = _FakePage()
        page.states["#menu-link"] = {"value": "", "visible": False}
        with patch.object(page, "evaluate", return_value=[]):
            uncovered = _assert_control_coverage(page, {"covers": ["#menu-link"]})
        self.assertEqual(uncovered, [])

    def test_hidden_control_fails_when_action_is_attempted(self) -> None:
        page = _FakePage()
        page.states["#menu-link"] = {"value": "", "visible": False}
        with self.assertRaisesRegex(AssertionError, "visible and enabled"):
            _run_steps(
                page,
                [{"action": "click", "selector": "#menu-link"}, {"action": "expect_navigation"}],
                "http://127.0.0.1:3000/",
            )

    def test_same_page_link_must_trigger_a_document_navigation(self) -> None:
        page = _FakePage()
        page.states["a[href='/login']"] = {"value": "", "visible": True, "text": "Login", "navigates": True}
        results = _run_steps(
            page,
            [
                {"action": "click", "selector": "a[href='/login']"},
                {"action": "expect_navigation"},
            ],
            "http://127.0.0.1:3000/",
        )
        self.assertEqual(results[0]["result"], "passed")

    def test_navigate_steps_reset_the_flow_to_another_local_route(self) -> None:
        payload = valid_manifest()
        payload["flows"][0]["covers"] = ["#email"]
        payload["flows"][0]["steps"] = [
            {"action": "navigate", "value": "/register"},
            {"action": "wait_for_url", "value": "/register"},
            {"action": "navigate", "value": "/login"},
            {"action": "wait_for_url", "value": "/login"},
            {"action": "fill", "selector": "#email", "value": "user@example.test"},
            {"action": "expect_value", "selector": "#email", "value": "user@example.test"},
        ]
        validate_manifest(payload, ["reference/login.png"])

        page = _FakePage()
        results = _run_steps(page, payload["flows"][0]["steps"], "http://127.0.0.1:3000/")

        self.assertEqual(page.url, "http://127.0.0.1:3000/login")
        self.assertEqual(len(results), 3)

    def test_fill_is_checked_against_a_changed_value(self) -> None:
        page = _FakePage()
        page.states["#email"] = {"value": "", "visible": True, "text": ""}
        results = _run_steps(
            page,
            [
                {"action": "fill", "selector": "#email", "value": "bad@example.test"},
                {"action": "expect_value", "selector": "#email", "value": "bad@example.test"},
            ],
            "http://127.0.0.1:3000/",
        )
        self.assertEqual(results[0]["result"], "passed")

    def test_click_must_cause_an_observable_state_change(self) -> None:
        page = _FakePage()
        page.states["#submit"] = {"value": "", "visible": True, "text": "", "shows": ["#error"]}
        page.states["#error"] = {"value": "", "visible": False, "text": "Invalid credentials"}
        _run_steps(
            page,
            [
                {"action": "click", "selector": "#submit"},
                {"action": "expect_visible", "selector": "#error"},
            ],
            "http://127.0.0.1:3000/",
        )

    def test_one_interaction_can_have_multiple_observable_assertions(self) -> None:
        payload = valid_manifest()
        payload["flows"][0]["steps"] = [
            {"action": "click", "selector": "#submit"},
            {"action": "expect_visible", "selector": "#error"},
            {"action": "expect_hidden", "selector": "#loading"},
        ]
        payload["flows"][0]["covers"] = ["#submit"]
        validate_manifest(payload, ["reference/login.png"])
        page = _FakePage()
        page.states["#submit"] = {"value": "", "visible": True, "shows": ["#error"]}
        page.states["#error"] = {"value": "", "visible": False}
        page.states["#loading"] = {"value": "", "visible": True}
        original_click = _FakeLocator.click

        def click_and_hide(locator, **kwargs):
            original_click(locator, **kwargs)
            locator.page.states["#loading"]["visible"] = False

        with patch.object(_FakeLocator, "click", click_and_hide):
            results = _run_steps(page, payload["flows"][0]["steps"], "http://127.0.0.1:3000/")
        self.assertEqual(len(results[0]["assertions"]), 2)

    def test_click_without_a_state_change_fails(self) -> None:
        page = _FakePage()
        page.states["#submit"] = {"value": "", "visible": True, "text": ""}
        page.states["#error"] = {"value": "", "visible": True, "text": "Already visible"}
        with self.assertRaisesRegex(AssertionError, "no observable state change"):
            _run_steps(
                page,
                [
                    {"action": "click", "selector": "#submit"},
                    {"action": "expect_visible", "selector": "#error"},
                ],
                "http://127.0.0.1:3000/",
            )

    def test_requires_an_observable_assertion_after_each_interaction(self) -> None:
        payload = valid_manifest()
        payload["flows"][0]["steps"] = [
            {"action": "fill", "selector": "#email", "value": "bad@example.test"},
            {"action": "click", "selector": "#submit"},
            {"action": "expect_visible", "selector": "[role=alert]"},
        ]
        with self.assertRaisesRegex(ValueError, "observable assertion for the previous interaction"):
            validate_manifest(payload, ["reference/login.png"])

    def test_rejects_covered_controls_without_a_real_action(self) -> None:
        payload = valid_manifest()
        payload["flows"][0]["covers"].append("#dead-menu")
        with self.assertRaisesRegex(ValueError, "no exercised action"):
            validate_manifest(payload, ["reference/login.png"])


class VisualImplementationContextTests(unittest.TestCase):
    def test_repair_reads_only_failed_screenshots_for_its_requirement(self) -> None:
        model = object.__new__(ModelClient)
        report = {
            "cases": [
                {
                    "name": "login",
                    "reference": "reference/login.png",
                    "route": "/login",
                    "verdict": "repair",
                    "screenshot": "artifacts/visual-acceptance/screenshots/login.png",
                },
                {
                    "name": "register",
                    "reference": "reference/register.png",
                    "verdict": "repair",
                    "screenshot": "artifacts/visual-acceptance/screenshots/register.png",
                },
                {
                    "name": "outside",
                    "reference": "reference/login.png",
                    "verdict": "repair",
                    "screenshot": "../../outside.png",
                },
            ]
        }
        project_tools = SimpleNamespace(project_dir=Path("project"))
        with patch.object(Path, "read_text", return_value=json.dumps(report)):
            with patch.object(Path, "read_bytes", return_value=b"\x89PNG\r\n\x1a\n"):
                content = model._repair_visual_inputs(
                    {"id": "REQ-LOGIN", "visual_reference": ["reference/login.png"]},
                    project_tools,
                )

        self.assertEqual(len(content), 2)
        self.assertIn("reference/login.png", content[0]["text"])
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_interaction_failure_also_attaches_the_page_screenshot(self) -> None:
        model = object.__new__(ModelClient)
        report = {
            "cases": [
                {
                    "name": "login",
                    "reference": "reference/login.png",
                    "route": "/login",
                    "verdict": "pass",
                    "screenshot": "artifacts/visual-acceptance/screenshots/login.png",
                }
            ],
            "flows": [{"name": "open-menu", "passed": False}],
        }
        with patch.object(Path, "read_text", return_value=json.dumps(report)):
            with patch.object(Path, "read_bytes", return_value=b"\x89PNG\r\n\x1a\n"):
                content = model._repair_visual_inputs(
                    {"id": "REQ-LOGIN", "visual_reference": ["reference/login.png"]},
                    SimpleNamespace(project_dir=Path("project")),
                )

        self.assertEqual([part["type"] for part in content], ["text", "image_url"])

    def test_repair_call_receives_reference_and_actual_screenshot(self) -> None:
        recorded: dict = {}

        def create(**kwargs):
            recorded.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=None))])

        model = object.__new__(ModelClient)
        model.model = "text-model"
        model.visual_model = "vision-model"
        model.max_turns = 1
        model.max_tool_calls = 10
        model.max_model_requests = 24
        model.max_total_tokens = 300_000
        model.tool_calls_used = 0
        model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        reference_payload = [
            {"type": "text", "text": "Visual reference for REQ-LOGIN: reference/login.png"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,cmVm"}},
        ]
        screenshot_payload = [
            {"type": "text", "text": "Actual browser screenshot for failed login case"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,YWN0dWFs"}},
        ]
        with patch.object(model, "_visual_inputs", return_value=reference_payload):
            with patch.object(model, "_repair_visual_inputs", return_value=screenshot_payload):
                model.implement(
                    task_type="web",
                    subtree={"id": "REQ-LOGIN", "visual_reference": ["reference/login.png"]},
                    plan="Match the login reference.",
                    project_tools=SimpleNamespace(project_dir=Path("project")),
                    repair_feedback="The form is too narrow and the menu did not open.",
                    reference_dir=Path("task"),
                )

        content = recorded["messages"][2]["content"]
        self.assertEqual(recorded["model"], "vision-model")
        self.assertEqual(sum(part["type"] == "image_url" for part in content), 2)
        self.assertIn("menu did not open", content[0]["text"])

    def test_implementation_call_receives_reference_image(self) -> None:
        recorded: dict = {}

        def create(**kwargs):
            recorded.update(kwargs)
            message = SimpleNamespace(tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        model = object.__new__(ModelClient)
        model.model = "text-model"
        model.visual_model = "vision-model"
        model.max_turns = 1
        model.max_tool_calls = 10
        model.max_model_requests = 24
        model.max_total_tokens = 300_000
        model.tool_calls_used = 0
        model.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        image_payload = [
            {"type": "text", "text": "Visual reference for REQ-1: reference/login.png"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,cG5n"}},
        ]
        with patch.object(model, "_visual_inputs", return_value=image_payload):
            model.implement(
                task_type="web",
                subtree={"id": "REQ-1", "visual_reference": ["reference/login.png"], "children": []},
                plan="Match the reference form.",
                project_tools=None,
                reference_dir=Path("task"),
            )

        self.assertEqual(recorded["model"], "vision-model")
        user_content = recorded["messages"][2]["content"]
        self.assertIsInstance(user_content, list)
        self.assertTrue(any(part.get("type") == "image_url" for part in user_content))
        self.assertIn("Every element styled or labeled", recorded["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()

"""Offline transport failures that used to wait indefinitely after HTTP 200."""
import asyncio
from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import httpx
from openai import AsyncOpenAI, InternalServerError

from agent.model_transport import CompletionClient, ModelRequestInterrupted
from agent.tools import ProjectTools
from agent.visual_acceptance import REPORT_RELATIVE_PATH, verify_visual_acceptance
from tests.test_llm_budget import _make_client
from tests.test_visual_acceptance import valid_manifest


def completion_payload():
    return {"id": "offline-completion", "object": "chat.completion", "created": 1,
            "model": "offline-model", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "complete"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


class KeepAliveBody(httpx.AsyncByteStream):
    def __init__(self, prefix=b""):
        self.prefix = prefix
        self.chunks = 0
        self.cancelled = False
        self.closed = False

    async def __aiter__(self):
        try:
            if self.prefix:
                yield self.prefix
            while True:
                await asyncio.sleep(0.003)
                self.chunks += 1
                yield b"\n"  # Network reads succeed, but the JSON response never completes.
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self):
        self.closed = True


@contextmanager
def mock_transport(handler):
    """Keep the real SDK parser/cancellation, replacing only the HTTP transport."""
    clients = []
    requests = []

    def handle(request):
        requests.append(request)
        return handler(request)

    def factory(**options):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                   timeout=options["timeout"])
        clients.append(client)
        return AsyncOpenAI(**options, http_client=client)

    with patch("openai.AsyncOpenAI", side_effect=factory):
        yield clients, requests


def completion_client(deadline=0.08):
    return CompletionClient(api_key="offline-test-key", base_url="https://offline.invalid/v1",
                            deadline_seconds=deadline)


class CompletionTransportTests(unittest.TestCase):
    def test_real_local_socket_keepalive_disconnects_after_deadline(self):
        disconnected = threading.Event()
        finished = threading.Event()
        stopping = threading.Event()
        requests = []
        chunks = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    while not stopping.wait(0.003):
                        self.wfile.write(b"1\r\n\n\r\n")
                        self.wfile.flush()
                        chunks.append(1)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    disconnected.set()
                finally:
                    finished.set()
                    self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01),
                                  name="offline-model-server")
        thread.start()
        http_client = httpx.AsyncClient(trust_env=False)
        client = CompletionClient(api_key="offline-test-key", deadline_seconds=1.0,
            base_url=f"http://127.0.0.1:{server.server_port}/v1", http_client=http_client)
        try:
            with self.assertRaises(ModelRequestInterrupted) as caught:
                client.create(model="offline-model", messages=[])
            self.assertEqual(caught.exception.reason, "deadline")
            self.assertTrue(http_client.is_closed)
            self.assertTrue(disconnected.wait(2), f"No disconnect observed; server requests={requests}")
            self.assertTrue(finished.wait(2))
            self.assertEqual(requests, ["/v1/chat/completions"])
            self.assertGreater(len(chunks), 1)
        finally:
            stopping.set()
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_http_200_keepalive_is_cancelled_and_closed_without_retry(self):
        body = KeepAliveBody()
        with mock_transport(lambda _: httpx.Response(200, stream=body)) as (clients, requests):
            started = time.monotonic()
            with self.assertRaises(ModelRequestInterrupted) as caught:
                completion_client().create(model="offline-model", messages=[])
            self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(caught.exception.reason, "deadline")
        self.assertGreater(body.chunks, 1)
        self.assertTrue(body.cancelled)
        self.assertTrue(body.closed)
        self.assertEqual(len(requests), 1)
        self.assertTrue(all(client.is_closed for client in clients))

    def test_connection_error_is_unknown_usage_without_sdk_retry(self):
        def unavailable(request):
            raise httpx.ConnectError("Offline simulated connection failure", request=request)
        with mock_transport(unavailable) as (clients, requests):
            with self.assertRaises(ModelRequestInterrupted) as caught:
                completion_client().create(model="offline-model", messages=[])
        self.assertEqual(caught.exception.reason, "transport_error")
        self.assertFalse(caught.exception.report()["usage_complete"])
        self.assertEqual(len(requests), 1)
        self.assertTrue(all(client.is_closed for client in clients))

    def test_retryable_http_status_is_not_automatically_resent(self):
        with mock_transport(lambda _: httpx.Response(500, json={"error": "offline failure"})) as (clients, requests):
            with self.assertRaises(InternalServerError):
                completion_client().create(model="offline-model", messages=[])
        self.assertEqual(len(requests), 1)
        self.assertTrue(all(client.is_closed for client in clients))

    def test_complete_responses_preserve_usage_across_fresh_event_loops(self):
        with mock_transport(lambda _: httpx.Response(200, json=completion_payload())) as (clients, requests):
            client = completion_client(deadline=2)
            first = client.create(model="offline-model", messages=[])
            second = client.create(model="offline-model", messages=[])
        self.assertEqual(first.choices[0].message.content, "complete")
        self.assertEqual(second.usage.total_tokens, 120)
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(clients), 2)
        self.assertTrue(all(client.is_closed for client in clients))

    def test_real_sync_playwright_loop_cancels_request_and_joins_worker(self):
        from playwright.sync_api import sync_playwright
        body = KeepAliveBody()
        responses = [httpx.Response(200, json=completion_payload()), httpx.Response(200, stream=body)]
        with sync_playwright(), mock_transport(lambda _: responses.pop(0)) as (clients, requests):
            self.assertTrue(asyncio.get_running_loop().is_running())
            client = completion_client()
            complete = client.create(model="offline-model", messages=[])
            self.assertEqual(complete.usage.total_tokens, 120)
            with self.assertRaises(ModelRequestInterrupted) as caught:
                client.create(model="offline-model", messages=[])
            self.assertEqual(caught.exception.reason, "deadline")
            self.assertEqual(len(requests), 2)
            self.assertTrue(all(client.is_closed for client in clients))
            self.assertTrue(body.cancelled and body.closed)
            self.assertFalse(any(thread.name.startswith("arc-model-request")
                                 for thread in threading.enumerate()))

    def test_invalid_deadlines_and_streaming_are_rejected_before_http(self):
        for deadline in (0, -1, float("nan"), float("inf"), 901):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                completion_client(deadline)
        with self.assertRaises(ValueError):
            completion_client().create(model="offline-model", messages=[], stream=True)

    def test_partial_tool_arguments_never_execute_and_latch_prevents_replay(self):
        body = KeepAliveBody(b'{"choices":[{"message":{"tool_calls":[{"function":'
                             b'{"name":"write_file","arguments":"{\\"path\\":')
        model = _make_client(None, None, [])
        model.client = completion_client()
        with TemporaryDirectory() as directory, mock_transport(
                lambda _: httpx.Response(200, stream=body)) as (clients, requests):
            project = ProjectTools(Path(directory))
            with patch.object(project, "write_file") as write:
                with self.assertRaises(ModelRequestInterrupted) as caught:
                    model.implement(task_type="web", subtree={"id": "ROOT", "name": "Offline"},
                                    plan="Implement original requirements", project_tools=project)
                write.assert_not_called()
                self.assertEqual(list(Path(directory).rglob("*.jsx")), [])
            self.assertEqual(caught.exception.stage, "implementation")
            self.assertEqual(caught.exception.request, 1)
            self.assertEqual(model.model_requests_used, 1)
            self.assertEqual(model.prompt_tokens_used + model.completion_tokens_used, 0)
            with self.assertRaises(ModelRequestInterrupted):
                model.plan("web", {"id": "ROOT", "name": "Offline"})
            self.assertEqual(model.model_requests_used, 1)
            self.assertEqual(len(requests), 1)
            with self.assertLogs("agent.llm", level="WARNING") as logged:
                model.log_usage()
            self.assertIn("token_counts=unavailable", "\n".join(logged.output))
        self.assertTrue(body.cancelled and body.closed)
        self.assertTrue(all(client.is_closed for client in clients))

    def test_known_usage_is_retained_before_incomplete_second_request(self):
        body = KeepAliveBody()
        responses = [httpx.Response(200, json=completion_payload()), httpx.Response(200, stream=body)]
        model = _make_client(None, None, [])
        model.client = completion_client()
        with mock_transport(lambda _: responses.pop(0)) as (_, requests):
            self.assertEqual(model.plan("web", {"id": "ROOT", "name": "Offline"}), "complete")
            with self.assertRaises(ModelRequestInterrupted):
                model.plan("web", {"id": "ROOT", "name": "Offline"})
            with self.assertRaises(ModelRequestInterrupted):
                model._record_model_request()
        self.assertEqual(len(requests), 2)
        self.assertEqual(model.model_requests_used, 2)
        self.assertEqual((model.prompt_tokens_used, model.completion_tokens_used), (100, 20))
        self.assertEqual(model.last_request_interruption.request, 2)

    def test_visual_interruption_saves_partial_evidence_and_closes_browser_server(self):
        failure = ModelRequestInterrupted(reason="deadline", deadline_seconds=300,
                                          stage="visual-case-review", request=7)
        manifest = valid_manifest()
        manifest["cases"].append(dict(manifest["cases"][0], name="unrun-case"))
        browser = MagicMock()
        context = browser.new_context.return_value
        context.new_page.return_value.screenshot.return_value = b"offline screenshot"
        model = SimpleNamespace(review_visual_case=MagicMock(side_effect=failure))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("agent.visual_acceptance._read_manifest", return_value=manifest), \
                 patch("agent.visual_acceptance._find_package_root", return_value=root), \
                 patch("agent.visual_acceptance._reserve_port", return_value=12345), \
                 patch("agent.visual_acceptance._start_server") as start, \
                 patch("agent.visual_acceptance._wait_for_server", return_value="http://127.0.0.1:12345"), \
                 patch("agent.visual_acceptance._ensure_chromium", return_value=browser), \
                 patch("agent.visual_acceptance._stop_server") as stop, \
                 patch("playwright.sync_api.sync_playwright", return_value=MagicMock()):
                with self.assertRaises(ModelRequestInterrupted) as caught:
                    verify_visual_acceptance(root, root, ["reference/login.png"], model)
            self.assertIs(caught.exception, failure)
            model.review_visual_case.assert_called_once()
            browser.new_context.assert_called_once()
            context.close.assert_called_once()
            browser.close.assert_called_once()
            stop.assert_called_once_with(start.return_value)
            report = json.loads((root / REPORT_RELATIVE_PATH).read_text(encoding="utf-8"))
            self.assertFalse(report["passed"])
            self.assertEqual(report["cases"][0]["verdict"], "interrupted")
            self.assertEqual(report["flows"], [])
            self.assertTrue((root / report["cases"][0]["screenshot"]).is_file())
            self.assertFalse(report["model_request_interruption"]["usage_complete"])

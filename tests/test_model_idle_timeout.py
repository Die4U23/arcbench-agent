"""Real SDK/socket regression for slow complete bodies and interruption evidence."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from agent.model_transport import CompletionClient, ModelRequestInterrupted
from tests.test_llm_budget import _make_client
from tests.test_model_transport import completion_payload


@contextmanager
def delayed_json_server():
    requests = []
    body = json.dumps(completion_payload()).encode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            requests.append(self.path)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.flush()
            # Scale minutes to milliseconds while keeping real HTTPX/SDK reads.
            time.sleep(0.2)
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda:server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@contextmanager
def scaled_read_client(*, old_idle_cap=False):
    clients = []

    def factory(**options):
        timeout = options['timeout']
        read = min(60, timeout.read) if old_idle_cap else timeout.read
        options['timeout'] = httpx.Timeout(read / 600, connect=timeout.connect,
                                         write=timeout.write, pool=timeout.pool)
        options['http_client'] = httpx.AsyncClient(trust_env=False, timeout=options['timeout'])
        clients.append(options['http_client'])
        return AsyncOpenAI(**options)

    with patch('openai.AsyncOpenAI', side_effect=factory):
        yield clients


class IdleTimeoutTests(unittest.TestCase):
    def test_body_pause_within_total_deadline_returns_complete_usage_once(self):
        with delayed_json_server() as (url, requests), scaled_read_client() as clients:
            client = CompletionClient(api_key='offline-test-key', base_url=url, deadline_seconds=300)
            result = client.create(model='offline-model', messages=[])
            self.assertEqual(result.usage.total_tokens, 120)
            self.assertEqual(client.deadline_seconds, 300)
            self.assertEqual(requests, ['/v1/chat/completions'])
            self.assertTrue(all(client.is_closed for client in clients))

    def test_old_idle_cap_is_rejected_with_cause_preserved_through_controller_latch(self):
        with delayed_json_server() as (url, requests), scaled_read_client(old_idle_cap=True) as clients:
            model = _make_client(None, None, [])
            model.client = CompletionClient(api_key='offline-test-key', base_url=url, deadline_seconds=300)
            model._record_model_request()
            with self.assertRaises(ModelRequestInterrupted) as caught:
                model._create_completion('implementation', model='offline-model', messages=[])
            report = caught.exception.report()
            self.assertEqual(report['reason'], 'transport_error')
            self.assertEqual(report['request'], 1)
            self.assertEqual(report['stage'], 'implementation')
            self.assertIn('APITimeoutError', report['cause_types'])
            self.assertIn('ReadTimeout', report['cause_types'])
            self.assertFalse(report['usage_complete'])
            self.assertNotIn(url, str(caught.exception))
            self.assertEqual(model.prompt_tokens_used + model.completion_tokens_used, 0)
            with self.assertRaises(ModelRequestInterrupted):
                model._create_completion('implementation', model='offline-model', messages=[])
            self.assertEqual(requests, ['/v1/chat/completions'])
            self.assertEqual(model.model_requests_used, 1)
            self.assertTrue(all(client.is_closed for client in clients))


if __name__ == '__main__':
    unittest.main()

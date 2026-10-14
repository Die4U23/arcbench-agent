"""Stream transport exposes only finished completions with provider usage."""
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from agent.model_transport import CompletionClient, ModelRequestInterrupted
from agent.tools import ProjectTools
from tests.test_llm_budget import _make_client
from tests.test_model_transport import mock_transport


def event(delta=None, finish=None, usage=None):
    choice = [] if usage is not None else [dict(index=0, delta=delta or {}, finish_reason=finish)]
    data = dict(id='offline-stream', object='chat.completion.chunk', created=1,
                model='offline-model', choices=choice, usage=usage)
    return b'data: ' + json.dumps(data).encode() + b'\n\n'


USAGE = dict(prompt_tokens=100, completion_tokens=20, total_tokens=120,
             prompt_cache_hit_tokens=95, prompt_cache_miss_tokens=5,
             completion_tokens_details=dict(reasoning_tokens=8))
TOOLS = [event(dict(role='assistant', reasoning_content='Think ')),
         event(dict(reasoning_content='before tools.', tool_calls=[dict(index=0, id='call-a',
             type='function', function=dict(name='write_file', arguments='{"path":"frontend/'))])),
         event(dict(tool_calls=[dict(index=1, id='call-b', type='function',
             function=dict(name='read_file', arguments='{"path":"other.txt"}')),
             dict(index=0, function=dict(arguments='note.txt","content":"complete"}'))])),
         event(finish='tool_calls'), event(usage=USAGE), b'data: [DONE]\n\n']


class SSEBody(httpx.AsyncByteStream):
    def __init__(self, chunks, *, error=False, keepalive=False):
        self.chunks, self.error, self.keepalive = chunks, error, keepalive
        self.closed = self.cancelled = False

    async def __aiter__(self):
        try:
            for chunk in self.chunks:
                await asyncio.sleep(0.002)
                yield chunk
            if self.error:
                raise httpx.ReadError('Offline connection closed mid-response')
            while self.keepalive:
                await asyncio.sleep(0.002)
                yield b': keep-alive\n\n'
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self):
        self.closed = True


def stream_client(deadline=2):
    return CompletionClient(api_key='offline-test-key', base_url='https://offline.invalid/v1',
                            deadline_seconds=deadline, assemble_stream=True)


class CompleteStreamTests(unittest.TestCase):
    def test_real_socket_stream_returns_only_after_final_usage_and_closes(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                try:
                    for data in [b': keep-alive\n\n', *TOOLS]:
                        self.wfile.write(f'{len(data):x}\r\n'.encode() + data + b'\r\n')
                        self.wfile.flush()
                        time.sleep(0.015)
                    self.wfile.write(b'0\r\n\r\n')
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                self.close_connection = True

        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=lambda:server.serve_forever(poll_interval=0.01))
        thread.start()
        http_client = httpx.AsyncClient(trust_env=False)
        client = CompletionClient(api_key='offline-test-key',
            base_url=f'http://127.0.0.1:{server.server_port}/v1',http_client=http_client,
            deadline_seconds=2,assemble_stream=True)
        try:
            result = client.create(model='offline-model',messages=[])
            self.assertEqual(result.usage.total_tokens,120)
            self.assertEqual(result.choices[0].message.reasoning_content,'Think before tools.')
            self.assertEqual(len(requests),1)
            self.assertTrue(requests[0]['stream_options']['include_usage'])
            self.assertTrue(http_client.is_closed)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_reasoning_interleaved_tools_and_final_usage_preserve_exact_content(self):
        body = SSEBody(TOOLS)
        with mock_transport(lambda _:httpx.Response(200, stream=body,
                headers={'content-type':'text/event-stream'})) as (clients, requests):
            result = stream_client().create(model='offline-model', messages=[])
        message = result.choices[0].message
        self.assertEqual(message.reasoning_content, 'Think before tools.')
        self.assertEqual([tool.id for tool in message.tool_calls], ['call-a', 'call-b'])
        self.assertEqual(json.loads(message.tool_calls[0].function.arguments),
                         dict(path='frontend/note.txt', content='complete'))
        self.assertEqual(result.usage.total_tokens, 120)
        self.assertEqual(result.usage.prompt_cache_hit_tokens, 95)
        self.assertEqual(result.usage.completion_tokens_details.reasoning_tokens, 8)
        payload = json.loads(requests[0].content)
        self.assertTrue(payload['stream'])
        self.assertTrue(payload['stream_options']['include_usage'])
        self.assertEqual(len(requests), 1)
        self.assertTrue(body.closed and all(client.is_closed for client in clients))

    def test_partial_tool_disconnect_never_executes_and_latch_prevents_resend(self):
        body = SSEBody(TOOLS[:2], error=True)
        model = _make_client(None, None, [])
        model.client = stream_client()
        with TemporaryDirectory() as directory, mock_transport(lambda _:httpx.Response(200,
                stream=body, headers={'content-type':'text/event-stream'})) as (clients, requests):
            tools = ProjectTools(Path(directory))
            with patch.object(tools, 'write_file') as write:
                with self.assertRaises(ModelRequestInterrupted) as caught:
                    model.implement(task_type='web', subtree=dict(id='ROOT'), plan='Original task',
                                    project_tools=tools)
                write.assert_not_called()
            self.assertEqual(caught.exception.reason, 'transport_error')
            self.assertIn('ReadError', caught.exception.cause_types)
            self.assertEqual(model.prompt_tokens_used + model.completion_tokens_used, 0)
            with self.assertRaises(ModelRequestInterrupted):
                model.plan('web',dict(id='ROOT'))
            self.assertEqual(len(requests), 1)
            self.assertTrue(body.closed and all(client.is_closed for client in clients))

    def test_finish_marker_without_final_provider_usage_is_unknown_not_success(self):
        body = SSEBody(TOOLS[:4] + [b'data: [DONE]\n\n'])
        with mock_transport(lambda _:httpx.Response(200, stream=body,
                headers={'content-type':'text/event-stream'})) as (clients, requests):
            with self.assertRaises(ModelRequestInterrupted) as caught:
                stream_client().create(model='offline-model',messages=[])
        self.assertEqual(caught.exception.reason, 'incomplete_stream')
        self.assertFalse(caught.exception.report()['usage_complete'])
        self.assertEqual(len(requests), 1)
        self.assertTrue(body.closed and all(client.is_closed for client in clients))

    def test_keepalive_and_partial_arguments_obey_absolute_deadline_and_close(self):
        body = SSEBody(TOOLS[:2], keepalive=True)
        with mock_transport(lambda _:httpx.Response(200, stream=body,
                headers={'content-type':'text/event-stream'})) as (clients, requests):
            with self.assertRaises(ModelRequestInterrupted) as caught:
                stream_client(deadline=0.04).create(model='offline-model', messages=[])
        self.assertEqual(caught.exception.reason, 'deadline')
        self.assertEqual(len(requests), 1)
        self.assertTrue(body.cancelled and body.closed)
        self.assertTrue(all(client.is_closed for client in clients))

    def test_complete_length_limited_response_remains_usage_known_for_controller_recovery(self):
        chunks = TOOLS[:2] + [event(finish='length'),event(usage=USAGE),b'data: [DONE]\n\n']
        body = SSEBody(chunks)
        with mock_transport(lambda _:httpx.Response(200, stream=body,
                headers={'content-type':'text/event-stream'})) as (_, requests):
            result = stream_client().create(model='offline-model',messages=[])
        self.assertEqual(result.choices[0].finish_reason, 'length')
        self.assertEqual(result.usage.total_tokens, 120)
        self.assertEqual(len(requests), 1)


if __name__ == '__main__':
    unittest.main()

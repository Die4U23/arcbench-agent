"""Provider rejection must survive an image-enabled implementation unchanged."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from openai import APIStatusError

from agent.llm import ModelClient
from tests.test_model_transport import completion_client, mock_transport


class VisualProviderErrorTests(unittest.TestCase):
    def visual_client(self, create):
        model = object.__new__(ModelClient)
        model.model = 'offline-text'
        model.visual_model = 'offline-vision'
        model.max_turns = 1
        model.max_tool_calls = 10
        model.max_model_requests = 24
        model.max_total_tokens = 300_000
        model.tool_calls_used = 0
        model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return model

    def implement(self, model):
        image = [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,cG5n'}}]
        with patch.object(model, '_visual_inputs', return_value=image):
            model.implement(task_type='web', subtree={'id': 'REQ-1', 'children': []},
                            plan='Inspect reference.', project_tools=None, reference_dir=Path('task'))

    def test_http_errors_are_preserved_without_retry_or_false_vision_diagnosis(self):
        for status in (400, 401, 402, 422, 429, 500, 503):
            with self.subTest(status=status):
                error = APIStatusError('Provider rejected request', response=httpx.Response(
                    status, request=httpx.Request('POST', 'https://offline.invalid/completions')),
                    body={'error': {'message': 'Provider rejected request'}})
                create = Mock(side_effect=error)
                model = self.visual_client(create)
                with self.assertRaises(APIStatusError) as caught:
                    self.implement(model)
                self.assertIs(caught.exception, error)
                self.assertEqual(create.call_count, 1)
                self.assertEqual(model.model_requests_used, 1)
                self.assertEqual(getattr(model, 'prompt_tokens_used', 0), 0)
                self.assertEqual(getattr(model, 'completion_tokens_used', 0), 0)

    def test_real_sdk_402_closes_connection_and_preserves_balance_message(self):
        body = {'error': {'message': 'Insufficient Balance', 'type': 'unknown_error', 'code': 'invalid_request_error'}}
        with mock_transport(lambda _: httpx.Response(402, json=body)) as (clients, requests):
            model = self.visual_client(completion_client(deadline=2).create)
            with self.assertRaises(APIStatusError) as caught:
                self.implement(model)
        self.assertEqual(caught.exception.status_code, 402)
        self.assertIn('Insufficient Balance', str(caught.exception))
        self.assertNotIn('Vision-enabled implementation failed', str(caught.exception))
        self.assertEqual(len(requests), 1)
        self.assertTrue(all(client.is_closed for client in clients))

    def test_non_http_visual_failure_retains_existing_hint(self):
        error = ValueError('Offline malformed image')
        create = Mock(side_effect=error)
        with self.assertRaisesRegex(RuntimeError, 'Vision-enabled implementation failed') as caught:
            self.implement(self.visual_client(create))
        self.assertIs(caught.exception.__cause__, error)
        self.assertEqual(create.call_count, 1)


if __name__ == '__main__':
    unittest.main()

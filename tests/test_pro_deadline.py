import os
import unittest
from unittest.mock import patch

from agent.llm import ModelClient
from agent.model_transport import default_model_deadline


class ProDeadlineTests(unittest.TestCase):
    def test_only_official_pro_receives_longer_finite_deadline(self):
        for model, url, expected in (
            ('deepseek-v4-pro', 'https://api.deepseek.com', 900),
            ('deepseek-v4-pro', 'https://api.deepseek.com/beta', 900),
            ('deepseek-flash', 'https://api.deepseek.com', 300),
            ('deepseek-v4-pro', 'https://proxy.example/v1', 300),
            ('offline-model', 'http://127.0.0.1:1', 300),
        ):
            with self.subTest(model=model, url=url):
                self.assertEqual(default_model_deadline(model, url), expected)

    def test_controller_applies_pro_read_total_bound_without_retries_or_budget_increase(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY':'offline-test-key',
                'MODEL':'deepseek-v4-pro','OPENAI_BASE_URL':'https://api.deepseek.com',
                'ARCBENCH_MAX_TOTAL_TOKENS':'10000000','ARCBENCH_MAX_MODEL_REQUESTS':'250'},clear=True):
            model=ModelClient()
        self.assertEqual(model.client.deadline_seconds, 900)
        self.assertEqual(model.client._options['timeout'].read, 900)
        self.assertEqual(model.client._options['max_retries'], 0)
        self.assertTrue(model.client.assemble_stream)
        self.assertEqual(model.max_model_requests, 250)
        self.assertEqual(model.max_total_tokens, 10000000)

    def test_explicit_deadline_stays_authoritative_and_cannot_exceed_900(self):
        environment={'OPENAI_API_KEY':'offline-test-key','MODEL':'deepseek-v4-pro',
                     'OPENAI_BASE_URL':'https://api.deepseek.com','ARCBENCH_MODEL_DEADLINE_SECONDS':'120'}
        with patch.dict(os.environ,environment,clear=True):
            model=ModelClient()
        self.assertEqual(model.client.deadline_seconds, 120)
        self.assertEqual(model.client._options['timeout'].read, 120)
        environment['ARCBENCH_MODEL_DEADLINE_SECONDS']='901'
        with patch.dict(os.environ,environment,clear=True), self.assertRaises(ValueError):
            ModelClient()


if __name__ == '__main__':
    unittest.main()

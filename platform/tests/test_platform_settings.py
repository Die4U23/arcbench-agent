from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "platform-agent"))
sys.path.insert(0, str(root / "platform-agent/arcbench-agent-runtime/src"))

from agent.competition_model import same_campaign_model
from agent.platform_settings import PlatformSettings, model_allowed, models_equivalent


FORBIDDEN_IMPORTS = (
    "agent.ticketbooking_contract",
    "agent.booking_completion",
    "agent.station_units",
    "agent.repair_policy",
    "agent.ticketbooking_probes",
    "agent.generation_contract",
)


class PlatformSettingsTests(unittest.TestCase):
    def test_defaults_match_current_platform_constants(self):
        settings = PlatformSettings()
        self.assertEqual(settings.allowed_models, ("deepseek-flash", "deepseek-v4-flash"))
        self.assertEqual(
            settings.model_aliases,
            (frozenset({"deepseek-flash", "deepseek-v4-flash", "deepseek-v4.1-flash-1017"}),),
        )
        self.assertEqual(settings.max_input_tokens, 1048576)
        self.assertEqual(settings.max_output_tokens, 16384)
        self.assertEqual(settings.tool_limit, 2000)
        self.assertEqual(settings.max_tool_calls, 2000)
        self.assertEqual(settings.context_window_tokens, 1048576)
        self.assertEqual(settings.request_allowance, 28)
        self.assertEqual(settings.implement_request_allowance, 64)
        self.assertEqual(settings.repair_request_allowance, 16)
        self.assertEqual(settings.packet_chars, 750000)
        self.assertEqual(settings.context_chars, 48000)
        self.assertEqual(settings.request_limit, 400)
        self.assertTrue(settings.runtime_check)
        self.assertEqual(settings.runtime_rounds, 4)
        self.assertEqual(settings.runtime_seconds, 3600)
        self.assertEqual(settings.runtime_test_timeout_ms, 10000)
        self.assertIsNone(settings.token_limit)
        self.assertTrue(model_allowed(settings, "deepseek-flash"))
        self.assertTrue(model_allowed(settings, "deepseek-v4-flash"))
        self.assertFalse(model_allowed(settings, "deepseek-v4-pro"))

    def test_from_env_overrides(self):
        settings = PlatformSettings.from_env({
            "ARC_ALLOWED_MODELS": "alpha, beta",
            "ARC_TOKEN_LIMIT": "2048",
            "ARC_MAX_INPUT_TOKENS": "4096",
            "ARC_REQUEST_LIMIT": "12",
            "ARC_TOOL_LIMIT": "9",
            "ARC_MAX_OUTPUT_TOKENS": "256",
            "ARC_IMPLEMENT_REQUESTS": "7",
            "ARC_REPAIR_REQUESTS": "3",
            "ARC_PACKET_CHARS": "4000",
            "ARC_CONTEXT_CHARS": "1500",
            "ARC_RUNTIME_CHECK": "0",
            "ARC_RUNTIME_ROUNDS": "5",
            "ARC_RUNTIME_SECONDS": "900",
            "ARC_RUNTIME_TEST_TIMEOUT_MS": "2500",
            "OPENAI_API_KEY": "should-not-be-read",
        })
        self.assertEqual(settings.runtime_test_timeout_ms, 2500)
        self.assertFalse(settings.runtime_check)
        self.assertEqual(settings.runtime_rounds, 5)
        self.assertEqual(settings.runtime_seconds, 900)
        self.assertTrue(PlatformSettings.from_env({"ARC_RUNTIME_CHECK": "1"}).runtime_check)
        self.assertEqual(settings.allowed_models, ("alpha", "beta"))
        self.assertEqual(settings.token_limit, 2048)
        self.assertEqual(settings.max_input_tokens, 4096)
        self.assertEqual(settings.context_window_tokens, 4096)
        only_run = PlatformSettings.from_env({"ARC_TOKEN_LIMIT": "999"})
        self.assertEqual((only_run.token_limit, only_run.max_input_tokens), (999, 1048576))
        self.assertEqual(settings.request_limit, 12)
        self.assertEqual(settings.tool_limit, 9)
        self.assertEqual(settings.max_tool_calls, 9)
        self.assertEqual(settings.max_output_tokens, 256)
        self.assertEqual(settings.implement_request_allowance, 7)
        self.assertEqual(settings.repair_request_allowance, 3)
        self.assertEqual(settings.packet_chars, 4000)
        self.assertEqual(settings.context_chars, 1500)
        self.assertEqual(settings.request_allowance, 28)
        self.assertNotIn("should-not-be-read", settings.__dict__.values())

    def test_invalid_env_is_rejected_with_variable_name(self):
        cases = {
            "ARC_TOKEN_LIMIT": "0",
            "ARC_REQUEST_LIMIT": "-1",
            "ARC_TOOL_LIMIT": "nope",
            "ARC_MAX_OUTPUT_TOKENS": "1.5",
            "ARC_IMPLEMENT_REQUESTS": "",
            "ARC_REPAIR_REQUESTS": "1000000001",
            "ARC_PACKET_CHARS": "abc",
            "ARC_CONTEXT_CHARS": "0",
            "ARC_ALLOWED_MODELS": "",
            "ARC_RUNTIME_CHECK": "yes",
            "ARC_RUNTIME_ROUNDS": "0",
            "ARC_RUNTIME_SECONDS": "86401",
            "ARC_MAX_INPUT_TOKENS": "x",
            "ARC_RUNTIME_TEST_TIMEOUT_MS": "0",
        }
        for name, value in cases.items():
            with self.subTest(name=name, value=value):
                with self.assertRaises(ValueError) as ctx:
                    PlatformSettings.from_env({name: value})
                self.assertIn(name, str(ctx.exception))

    def test_alias_equivalence_matches_campaign_proxy(self):
        settings = PlatformSettings()
        self.assertTrue(models_equivalent(settings, "deepseek-v4-flash", "deepseek-flash"))
        self.assertTrue(same_campaign_model("deepseek-v4-flash", "deepseek-flash"))
        self.assertTrue(same_campaign_model("deepseek-flash", "deepseek-v4.1-flash-1017"))
        self.assertFalse(models_equivalent(settings, "deepseek-v4-flash", "deepseek-v4-pro"))
        self.assertFalse(same_campaign_model("deepseek-v4-flash", "deepseek-v4-pro"))
        self.assertTrue(same_campaign_model("same", "same"))
        custom = PlatformSettings(model_aliases=(frozenset({"one", "two"}),))
        self.assertTrue(models_equivalent(custom, "ONE", "two"))
        self.assertFalse(models_equivalent(custom, "one", "deepseek-flash"))

    def test_star_allows_any_nonempty_model(self):
        settings = PlatformSettings.from_env({"ARC_ALLOWED_MODELS": "*"})
        self.assertIsNone(settings.allowed_models)
        self.assertTrue(model_allowed(settings, "deepseek-v4-pro"))
        self.assertTrue(model_allowed(settings, "any-model"))
        self.assertFalse(model_allowed(settings, ""))
        self.assertFalse(model_allowed(settings, "   "))

    def test_platform_generation_import_does_not_load_ticketbooking(self):
        probe = (
            "import sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(root / 'platform-agent')!r})\n"
            "import agent.platform_generation\n"
            "forbidden = {\n"
            + "".join(f"    {name!r},\n" for name in FORBIDDEN_IMPORTS)
            + "}\n"
            "found = sorted(name for name in forbidden if name in sys.modules)\n"
            "if found:\n"
            "    raise SystemExit('unexpected modules: ' + ', '.join(found))\n"
            "print('ok')\n"
        )
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "probe_imports.py"
            script.write_text(probe, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_closure_script_reports_platform_generation_required(self):
        script = root / "platform-agent" / "tools" / "platform_module_closure.py"
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("agent.platform_generation", payload["required"])
        self.assertIn("required", payload)
        self.assertIn("lazy_only", payload)
        self.assertIn("unused", payload)


if __name__ == "__main__":
    unittest.main()

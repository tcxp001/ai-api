import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ai_api_dashboard", ROOT / "dashboard.py")
dashboard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(dashboard)


class DashboardCodexRetryTests(unittest.TestCase):
    def test_validate_provider_sets_codex_retry_defaults(self):
        provider = dashboard.validate_provider(
            {
                "name": "regular",
                "base_url": "https://example.invalid/v1",
                "api_mode": "codex_responses",
            },
            1,
        )

        self.assertEqual(provider["request_max_retries"], 4)
        self.assertEqual(provider["stream_max_retries"], 5)

    def test_validate_provider_sets_high_retry_defaults_for_any_providers(self):
        for name in ("any", "any2"):
            with self.subTest(name=name):
                provider = dashboard.validate_provider(
                    {
                        "name": name,
                        "base_url": "https://example.invalid/v1",
                        "api_mode": "codex_responses",
                    },
                    1,
                )

                self.assertEqual(provider["request_max_retries"], 20)
                self.assertEqual(provider["stream_max_retries"], 50)

    def test_generated_codex_provider_block_writes_retry_settings(self):
        block = dashboard.generated_codex_provider_block(
            [
                {
                    "name": "any",
                    "request_max_retries": 20,
                    "stream_max_retries": 50,
                },
                {
                    "name": "regular",
                },
            ],
            "http://127.0.0.1:18006",
        )

        self.assertIn('[model_providers."any"]', block)
        self.assertIn("request_max_retries = 20", block)
        self.assertIn("stream_max_retries = 50", block)
        self.assertIn('[model_providers."regular"]', block)
        self.assertIn("request_max_retries = 4", block)
        self.assertIn("stream_max_retries = 5", block)

    def test_validate_provider_rejects_retry_values_over_codex_max(self):
        with self.assertRaisesRegex(ValueError, "request_max_retries"):
            dashboard.validate_provider(
                {
                    "name": "regular",
                    "base_url": "https://example.invalid/v1",
                    "api_mode": "codex_responses",
                    "request_max_retries": 101,
                },
                1,
            )


if __name__ == "__main__":
    unittest.main()

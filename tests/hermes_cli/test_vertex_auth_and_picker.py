import unittest
from pathlib import Path
from unittest.mock import patch

from agent.vertex_adapter import has_vertex_credentials
from hermes_cli.auth import get_auth_status, is_provider_explicitly_configured
from hermes_cli.models import _PROVIDER_MODELS


class TestVertexAuthAndPicker(unittest.TestCase):
    def test_has_vertex_credentials_with_adc_file(self):
        with patch("pathlib.Path.exists", return_value=True):
            self.assertTrue(has_vertex_credentials())

    def test_get_vertex_auth_status_logged_in(self):
        with patch("pathlib.Path.exists", return_value=True):
            status = get_auth_status("vertex")
            self.assertEqual(status.get("provider"), "vertex")
            self.assertTrue(status.get("logged_in"))

    def test_vertex_in_fallback_chain_is_explicitly_configured(self):
        fake_cfg = {
            "fallback_providers": [
                {"provider": "openai-codex", "model": "gpt-5.6-luna"},
                {"provider": "vertex", "model": "google/gemini-3.7-flash"},
            ]
        }
        with patch("hermes_cli.config.load_config", return_value=fake_cfg):
            self.assertTrue(is_provider_explicitly_configured("vertex"))
            self.assertFalse(is_provider_explicitly_configured("unknown-provider"))

    def test_vertex_curated_models_contains_gemini_37_flash(self):
        vertex_models = _PROVIDER_MODELS.get("vertex", [])
        self.assertIn("google/gemini-3.7-flash", vertex_models)
        self.assertEqual(vertex_models[0], "google/gemini-3.7-flash")


if __name__ == "__main__":
    unittest.main()

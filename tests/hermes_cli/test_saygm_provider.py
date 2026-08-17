"""Tests for saygm provider plugin wiring.

SayGM (https://api.saygm.com/v1) is a multi-model OpenAI-compatible gateway.
These tests verify the auto-wired surfaces: PROVIDER_REGISTRY, the model
picker, the default main model, credential resolution, and the provider:model
syntax.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hermes_cli.auth import (
    PROVIDER_REGISTRY,
    resolve_provider,
    resolve_api_key_provider_credentials,
)
from hermes_cli.models import (
    CANONICAL_PROVIDERS,
    _PROVIDER_MODELS,
    get_default_model_for_provider,
    provider_model_ids,
)
from providers import get_provider_profile


_SAYGM_BASE_URL = "https://api.saygm.com/v1"

_OTHER_PROVIDER_KEYS = (
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "DASHSCOPE_API_KEY", "XAI_API_KEY", "KIMI_API_KEY", "KIMI_CN_API_KEY",
    "MINIMAX_API_KEY", "MINIMAX_CN_API_KEY", "AI_GATEWAY_API_KEY",
    "KILOCODE_API_KEY", "HF_TOKEN", "GLM_API_KEY", "ZAI_API_KEY",
    "XIAOMI_API_KEY", "OPENROUTER_API_KEY", "GMI_API_KEY",
    "ARCEEAI_API_KEY", "TOKENHUB_API_KEY", "OLLAMA_API_KEY",
)


class TestSayGMProviderRegistry:
    """Verify saygm is auto-registered in PROVIDER_REGISTRY from the plugin."""

    def test_registered(self):
        assert "saygm" in PROVIDER_REGISTRY

    def test_auth_type_api_key(self):
        assert PROVIDER_REGISTRY["saygm"].auth_type == "api_key"

    def test_inference_base_url(self):
        assert PROVIDER_REGISTRY["saygm"].inference_base_url == _SAYGM_BASE_URL

    def test_api_key_env_vars(self):
        assert "SAYGM_API_KEY" in PROVIDER_REGISTRY["saygm"].api_key_env_vars


class TestSayGMProfile:
    """The plugin profile declares the OpenAI-compatible surface."""

    def test_profile_loaded(self):
        profile = get_provider_profile("saygm")
        assert profile is not None

    def test_base_url_and_api_mode(self):
        profile = get_provider_profile("saygm")
        assert profile.base_url == _SAYGM_BASE_URL
        assert profile.api_mode == "chat_completions"
        assert profile.auth_type == "api_key"

    def test_default_aux_model(self):
        profile = get_provider_profile("saygm")
        assert profile.default_aux_model == "gpt-5.4-mini"


class TestSayGMAliases:
    @pytest.mark.parametrize("alias", ["saygm", "SayGM"])
    def test_alias_resolves(self, alias, monkeypatch):
        for key in _OTHER_PROVIDER_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("SAYGM_API_KEY", "saygm-test-key")
        assert resolve_provider(alias) == "saygm"


class TestSayGMOptionalEnvVars:
    def test_saygm_api_key_in_optional_env_vars(self):
        from hermes_cli.config import OPTIONAL_ENV_VARS

        assert "SAYGM_API_KEY" in OPTIONAL_ENV_VARS
        assert OPTIONAL_ENV_VARS["SAYGM_API_KEY"]["category"] == "provider"
        assert OPTIONAL_ENV_VARS["SAYGM_API_KEY"]["password"] is True


class TestSayGMModelCatalog:
    def test_static_model_list_exists(self):
        assert "saygm" in _PROVIDER_MODELS
        assert len(_PROVIDER_MODELS["saygm"]) >= 1

    def test_default_main_model(self):
        assert get_default_model_for_provider("saygm") == "gpt-5.4"

    def test_canonical_provider_entry(self):
        slugs = [p.slug for p in CANONICAL_PROVIDERS]
        assert "saygm" in slugs

    def test_provider_model_ids_no_key_returns_curated(self, monkeypatch):
        """Without a key the picker shows the curated cc-servable list."""
        for key in _OTHER_PROVIDER_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("SAYGM_API_KEY", raising=False)

        ids = provider_model_ids("saygm")
        assert ids[0] == "gpt-5.4"
        assert "gpt-5.5" in ids

    def test_provider_model_ids_merges_filtered_live_models(self, monkeypatch):
        """With a key, filtered live results merge after the curated list."""
        monkeypatch.setenv("SAYGM_API_KEY", "saygm-live-key")
        monkeypatch.setattr(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            lambda provider_id: {
                "provider": provider_id,
                "api_key": "saygm-live-key",
                "base_url": _SAYGM_BASE_URL,
                "source": "SAYGM_API_KEY",
            },
        )

        profile = get_provider_profile("saygm")
        monkeypatch.setattr(
            profile,
            "fetch_models",
            lambda *args, **kwargs: [
                "gpt-5.4",
                "gpt-5.4-mini",
            ],
        )

        ids = provider_model_ids("saygm")
        assert ids[0] == "gpt-5.4"
        assert "gpt-5.4-mini" in ids


class TestSayGMReasoningEffort:
    """gpt-5.6-sol needs reasoning_effort='none' for cc function tools."""

    def test_sol_pins_reasoning_effort_none(self):
        profile = get_provider_profile("saygm")
        _, top_level = profile.build_api_kwargs_extras(model="gpt-5.6-sol")
        assert top_level == {"reasoning_effort": "none"}

    def test_other_models_emit_no_reasoning_effort(self):
        profile = get_provider_profile("saygm")
        for model in ("gpt-5.4", "gpt-5.5", "gpt-5.6-terra", "o3"):
            _, top_level = profile.build_api_kwargs_extras(model=model)
            assert top_level == {}, model


class TestSayGMMaxTokens:
    """o3/o4-mini cap below SayGM's 100,003 completion-token limit."""

    def test_o3_and_o4_mini_capped(self):
        profile = get_provider_profile("saygm")
        assert profile.get_max_tokens("o3") == 100000
        assert profile.get_max_tokens("o4-mini") == 100000

    def test_other_models_use_default_cap(self):
        profile = get_provider_profile("saygm")
        assert profile.get_max_tokens("gpt-5.4") == 128000
        assert profile.get_max_tokens("gpt-5.6-sol") == 128000


class TestSayGMCredentials:
    def test_resolve_credentials(self, monkeypatch):
        for key in _OTHER_PROVIDER_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("SAYGM_API_KEY", "saygm-test-key-1234")
        creds = resolve_api_key_provider_credentials("saygm")
        assert creds["api_key"] == "saygm-test-key-1234"
        assert creds["base_url"] == _SAYGM_BASE_URL


class TestSayGMApiMode:
    def test_determine_api_mode_is_chat_completions(self):
        from hermes_cli.providers import determine_api_mode

        assert determine_api_mode("saygm") == "chat_completions"


class TestSayGMUrlMapping:
    def test_url_to_provider(self):
        from agent.model_metadata import _URL_TO_PROVIDER

        assert _URL_TO_PROVIDER.get("api.saygm.com") == "saygm"


class TestSayGMContextLength:
    def test_default_model_has_registered_context_length(self):
        from agent.model_metadata import get_model_context_length

        ctx = get_model_context_length("gpt-5.4")
        assert isinstance(ctx, int)
        assert ctx >= 4096

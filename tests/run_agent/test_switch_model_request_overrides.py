"""Regression tests for provider-scoped overrides during live model switches."""

from unittest.mock import MagicMock, patch

from run_agent import AIAgent


OLLAMA_URL = "http://localhost:11434/v1"
CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"


def _make_agent(*, provider, requested_provider, model, base_url, overrides, owned, providers):
    agent = AIAgent.__new__(AIAgent)
    agent.provider = provider
    agent.requested_provider = requested_provider
    agent.model = model
    agent.base_url = base_url
    agent.api_key = "source-key"
    agent.api_mode = "chat_completions"
    agent.client = MagicMock(name="source-client")
    agent._client_kwargs = {"api_key": "source-key", "base_url": base_url}
    agent.request_overrides = overrides
    agent._custom_provider_extra_body = owned
    agent._custom_providers = providers
    agent.context_compressor = None
    agent._anthropic_api_key = ""
    agent._anthropic_base_url = None
    agent._anthropic_client = None
    agent._is_anthropic_oauth = False
    agent._cached_system_prompt = "cached"
    agent._primary_runtime = {}
    agent._fallback_activated = False
    agent._fallback_index = 0
    agent._fallback_chain = []
    agent._fallback_model = None
    agent._config_context_length = None
    agent._credential_pool = None
    agent._credential_pool_entry_id = None
    agent._transport_cache = {}
    agent._use_prompt_caching = False
    agent._use_native_cache_layout = False
    agent.reasoning_config = None
    agent._consecutive_stale_streams = 0
    agent._create_openai_client = MagicMock(return_value=MagicMock(name="destination-client"))
    agent._apply_client_headers_for_base_url = MagicMock()
    agent._anthropic_prompt_cache_policy = MagicMock(return_value=(False, False))
    agent._ensure_lmstudio_runtime_loaded = MagicMock(return_value=None)
    agent._lmstudio_load_was_unverified = MagicMock(return_value=False)
    agent._effective_lmstudio_context_length = MagicMock(return_value=None)
    return agent


def _switch(agent, providers, *, model, provider, base_url):
    cfg = {"custom_providers": providers, "agent": {}}
    with (
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("agent.credential_pool.load_pool", return_value=None),
        patch("hermes_cli.timeouts.get_provider_request_timeout", return_value=None),
    ):
        agent.switch_model(
            new_model=model,
            new_provider=provider,
            api_key="destination-key",
            base_url=base_url,
            api_mode="codex_responses" if provider == "openai-codex" else "chat_completions",
        )


def test_ollama_to_codex_drops_provider_fields_but_preserves_caller_overrides():
    providers = [
        {
            "provider_key": "ollama-local",
            "base_url": OLLAMA_URL,
            "model": "qwen3.5",
            "extra_body": {"think": False, "num_ctx": 65536},
        }
    ]
    agent = _make_agent(
        provider="custom:ollama-local",
        requested_provider="custom:ollama-local",
        model="qwen3.5",
        base_url=OLLAMA_URL,
        overrides={
            "service_tier": "priority",
            "extra_body": {"think": False, "num_ctx": 65536, "caller_only": True},
        },
        owned={"think": False, "num_ctx": 65536},
        providers=providers,
    )

    _switch(agent, providers, model="gpt-5.6", provider="openai-codex", base_url=CODEX_URL)

    assert agent.provider == "openai-codex"
    assert agent.request_overrides == {
        "service_tier": "priority",
        "extra_body": {"caller_only": True},
    }
    assert agent._custom_provider_extra_body == {}


def test_switch_between_named_custom_providers_replaces_provider_defaults():
    providers = [
        {
            "provider_key": "a",
            "name": "a",
            "base_url": "https://a.test/v1",
            "model": "model-a",
            "extra_body": {"think": False},
        },
        {
            "provider_key": "b",
            "name": "b",
            "base_url": "https://b.test/v1",
            "model": "model-b",
            "extra_body": {"enable_thinking": True},
        },
    ]
    agent = _make_agent(
        provider="custom:a",
        requested_provider="custom:a",
        model="model-a",
        base_url="https://a.test/v1",
        overrides={"extra_body": {"think": False, "caller_only": 1}},
        owned={"think": False},
        providers=providers,
    )

    _switch(agent, providers, model="model-b", provider="b", base_url="https://b.test/v1")

    assert agent.request_overrides == {
        "extra_body": {"enable_thinking": True, "caller_only": 1}
    }
    assert agent._custom_provider_extra_body == {"enable_thinking": True}


def test_same_custom_endpoint_model_switch_refreshes_model_defaults():
    providers = [
        {
            "provider_key": "local",
            "name": "local",
            "base_url": "https://shared.test/v1",
            "model": "model-a",
            "extra_body": {"mode_a": True},
        },
        {
            "provider_key": "local",
            "name": "local",
            "base_url": "https://shared.test/v1",
            "model": "model-b",
            "extra_body": {"mode_b": True},
        },
    ]
    agent = _make_agent(
        provider="custom:local",
        requested_provider="custom:local",
        model="model-a",
        base_url="https://shared.test/v1",
        overrides={"extra_body": {"mode_a": True}},
        owned={"mode_a": True},
        providers=providers,
    )

    _switch(
        agent,
        providers,
        model="model-b",
        provider="custom:local",
        base_url="https://shared.test/v1",
    )

    assert agent.request_overrides == {"extra_body": {"mode_b": True}}
    assert agent._custom_provider_extra_body == {"mode_b": True}


def test_primary_runtime_override_snapshot_is_independent():
    providers = [
        {
            "provider_key": "ollama-local",
            "base_url": OLLAMA_URL,
            "model": "qwen3.5",
            "extra_body": {"num_ctx": 65536},
        }
    ]
    agent = _make_agent(
        provider="custom:ollama-local",
        requested_provider="custom:ollama-local",
        model="qwen3.5",
        base_url=OLLAMA_URL,
        overrides={"extra_body": {"num_ctx": 65536}},
        owned={"num_ctx": 65536},
        providers=providers,
    )

    _switch(agent, providers, model="gpt-5.6", provider="openai-codex", base_url=CODEX_URL)

    assert agent._primary_runtime["request_overrides"] == agent.request_overrides
    assert agent._primary_runtime["custom_provider_extra_body"] == {}
    agent.request_overrides.setdefault("extra_body", {})["later"] = True
    agent._custom_provider_extra_body["later"] = True
    assert "later" not in agent._primary_runtime["request_overrides"].get("extra_body", {})
    assert "later" not in agent._primary_runtime["custom_provider_extra_body"]


def test_config_refresh_failure_still_removes_source_owned_fields():
    providers = [
        {
            "provider_key": "ollama-local",
            "base_url": OLLAMA_URL,
            "model": "qwen3.5",
            "extra_body": {"think": False, "num_ctx": 65536},
        }
    ]
    agent = _make_agent(
        provider="custom:ollama-local",
        requested_provider="custom:ollama-local",
        model="qwen3.5",
        base_url=OLLAMA_URL,
        overrides={"extra_body": {"think": False, "num_ctx": 65536, "caller": 1}},
        owned={"think": False, "num_ctx": 65536},
        providers=providers,
    )
    cfg = {"custom_providers": providers, "agent": {}}

    with (
        patch("hermes_cli.config.load_config_readonly", side_effect=OSError("unreadable")),
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("agent.credential_pool.load_pool", return_value=None),
        patch("hermes_cli.timeouts.get_provider_request_timeout", return_value=None),
    ):
        agent.switch_model(
            new_model="gpt-5.6",
            new_provider="openai-codex",
            api_key="destination-key",
            base_url=CODEX_URL,
            api_mode="codex_responses",
        )

    assert agent.request_overrides == {"extra_body": {"caller": 1}}
    assert agent._custom_provider_extra_body == {}

"""Regression tests for provider-scoped overrides across fallback routes."""

import copy
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _client(base_url, api_key="fallback-key"):
    client = MagicMock()
    client.base_url = base_url
    client.api_key = api_key
    return client


def _make_agent(*, source_provider, source_model, source_url, overrides, owned, providers, chain):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="source-key",
            base_url=source_url,
            provider=source_provider,
            model=source_model,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=chain,
        )
    agent.client = MagicMock(name="source-client")
    agent.provider = source_provider
    agent.requested_provider = source_provider
    agent.model = source_model
    agent.base_url = source_url
    agent.api_mode = "chat_completions"
    agent._client_kwargs = {"api_key": "source-key", "base_url": source_url}
    agent.request_overrides = copy.deepcopy(overrides)
    agent._custom_provider_extra_body = copy.deepcopy(owned)
    agent._custom_providers = copy.deepcopy(providers)
    agent._primary_runtime.update(
        {
            "model": source_model,
            "provider": source_provider,
            "requested_provider": source_provider,
            "base_url": source_url,
            "api_mode": "chat_completions",
            "api_key": "source-key",
            "client_kwargs": dict(agent._client_kwargs),
            "request_overrides": copy.deepcopy(overrides),
            "custom_provider_extra_body": copy.deepcopy(owned),
        }
    )
    agent._fallback_chain = copy.deepcopy(chain)
    agent._fallback_index = 0
    agent._fallback_activated = False
    agent._unavailable_fallback_keys = set()
    agent._rate_limited_until = 0
    return agent


def _activate(agent, clients):
    with (
        patch(
            "agent.chat_completion_helpers._fallback_entry_unavailable_without_network",
            return_value=None,
        ),
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=clients),
        patch("agent.credential_pool.load_pool", return_value=None),
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda model, _provider: model,
        ),
    ):
        return agent._try_activate_fallback()


def test_custom_primary_to_native_fallback_removes_only_provider_owned_fields():
    providers = [
        {
            "provider_key": "ollama-local",
            "name": "ollama-local",
            "base_url": "http://localhost:11434/v1",
            "model": "qwen",
            "extra_body": {"think": False, "num_ctx": 65536},
        }
    ]
    chain = [{"provider": "openai-codex", "model": "gpt-5.6"}]
    agent = _make_agent(
        source_provider="custom:ollama-local",
        source_model="qwen",
        source_url="http://localhost:11434/v1",
        overrides={
            "service_tier": "priority",
            "extra_body": {"think": False, "num_ctx": 65536, "caller_only": True},
        },
        owned={"think": False, "num_ctx": 65536},
        providers=providers,
        chain=chain,
    )

    assert _activate(
        agent,
        [(_client("https://chatgpt.com/backend-api/codex/responses"), "gpt-5.6")],
    ) is True

    assert agent.request_overrides == {
        "service_tier": "priority",
        "extra_body": {"caller_only": True},
    }
    assert agent._custom_provider_extra_body == {}


def test_native_primary_to_custom_fallback_applies_destination_defaults():
    providers = [
        {
            "provider_key": "b",
            "name": "b",
            "base_url": "https://b.test/v1",
            "model": "model-b",
            "extra_body": {"enable_thinking": True},
        }
    ]
    chain = [
        {
            "provider": "custom:b",
            "model": "model-b",
            "base_url": "https://b.test/v1",
        }
    ]
    agent = _make_agent(
        source_provider="openrouter",
        source_model="openai/gpt-5",
        source_url="https://openrouter.ai/api/v1",
        overrides={"extra_body": {"caller_only": 1}},
        owned={},
        providers=providers,
        chain=chain,
    )

    assert _activate(agent, [(_client("https://b.test/v1"), "model-b")]) is True

    assert agent.request_overrides == {
        "extra_body": {"enable_thinking": True, "caller_only": 1}
    }
    assert agent._custom_provider_extra_body == {"enable_thinking": True}


def test_fallback_chain_rebases_custom_a_to_b_to_native():
    providers = [
        {
            "provider_key": "a",
            "name": "a",
            "base_url": "https://a.test/v1",
            "model": "model-a",
            "extra_body": {"mode_a": True},
        },
        {
            "provider_key": "b",
            "name": "b",
            "base_url": "https://b.test/v1",
            "model": "model-b",
            "extra_body": {"mode_b": True},
        },
    ]
    chain = [
        {"provider": "custom:b", "model": "model-b", "base_url": "https://b.test/v1"},
        {"provider": "openai-codex", "model": "gpt-5.6"},
    ]
    agent = _make_agent(
        source_provider="custom:a",
        source_model="model-a",
        source_url="https://a.test/v1",
        overrides={"extra_body": {"mode_a": True, "caller_only": 1}},
        owned={"mode_a": True},
        providers=providers,
        chain=chain,
    )

    assert _activate(agent, [(_client("https://b.test/v1"), "model-b")]) is True
    assert agent.request_overrides == {
        "extra_body": {"mode_b": True, "caller_only": 1}
    }
    assert _activate(
        agent,
        [(_client("https://chatgpt.com/backend-api/codex/responses"), "gpt-5.6")],
    ) is True
    assert agent.request_overrides == {"extra_body": {"caller_only": 1}}
    assert agent._custom_provider_extra_body == {}


def test_primary_restore_recovers_exact_override_state():
    providers = [
        {
            "provider_key": "a",
            "name": "a",
            "base_url": "https://a.test/v1",
            "model": "model-a",
            "extra_body": {"mode_a": True},
        }
    ]
    primary_overrides = {
        "service_tier": "priority",
        "extra_body": {"mode_a": True, "caller_only": 1},
    }
    agent = _make_agent(
        source_provider="custom:a",
        source_model="model-a",
        source_url="https://a.test/v1",
        overrides=primary_overrides,
        owned={"mode_a": True},
        providers=providers,
        chain=[{"provider": "openai-codex", "model": "gpt-5.6"}],
    )

    assert _activate(
        agent,
        [(_client("https://chatgpt.com/backend-api/codex/responses"), "gpt-5.6")],
    ) is True
    agent._rate_limited_until = 0
    with (
        patch.object(agent, "_create_openai_client", return_value=MagicMock()),
        patch("agent.credential_pool.load_pool", return_value=None),
    ):
        assert agent._restore_primary_runtime() is True

    assert agent.request_overrides == primary_overrides
    assert agent._custom_provider_extra_body == {"mode_a": True}


def test_legacy_primary_snapshot_without_override_keys_rebases_safely():
    providers = [
        {
            "provider_key": "a",
            "name": "a",
            "base_url": "https://a.test/v1",
            "model": "model-a",
            "extra_body": {"mode_a": True},
        }
    ]
    agent = _make_agent(
        source_provider="custom:a",
        source_model="model-a",
        source_url="https://a.test/v1",
        overrides={"extra_body": {"mode_a": True, "caller": 1}},
        owned={"mode_a": True},
        providers=providers,
        chain=[],
    )
    agent._primary_runtime.pop("request_overrides")
    agent._primary_runtime.pop("custom_provider_extra_body")
    agent._fallback_activated = True
    agent.request_overrides = {"extra_body": {"mode_b": True, "caller": 1}}
    agent._custom_provider_extra_body = {"mode_b": True}

    with (
        patch.object(agent, "_create_openai_client", return_value=MagicMock()),
        patch("agent.credential_pool.load_pool", return_value=None),
    ):
        assert agent._restore_primary_runtime() is True

    assert agent.request_overrides == {
        "extra_body": {"mode_a": True, "caller": 1}
    }
    assert agent._custom_provider_extra_body == {"mode_a": True}


def test_transient_primary_recovery_restores_override_snapshot():
    providers = [
        {
            "provider_key": "a",
            "name": "a",
            "base_url": "https://a.test/v1",
            "model": "model-a",
            "extra_body": {"mode_a": True},
        }
    ]
    primary = {"extra_body": {"mode_a": True, "caller": 1}}
    agent = _make_agent(
        source_provider="custom:a",
        source_model="model-a",
        source_url="https://a.test/v1",
        overrides=primary,
        owned={"mode_a": True},
        providers=providers,
        chain=[],
    )
    agent.request_overrides = {"extra_body": {"stale": True}}
    agent._custom_provider_extra_body = {"stale": True}
    error = type("ConnectError", (Exception,), {})("temporary")

    with (
        patch.object(agent, "_create_openai_client", return_value=MagicMock()),
        patch("agent.agent_runtime_helpers.time.sleep", return_value=None),
    ):
        assert agent._try_recover_primary_transport(
            error, retry_count=3, max_retries=3
        ) is True

    assert agent.request_overrides == primary
    assert agent._custom_provider_extra_body == {"mode_a": True}

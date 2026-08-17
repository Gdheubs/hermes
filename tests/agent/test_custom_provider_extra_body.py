import sys
from types import SimpleNamespace
from unittest.mock import patch

from agent.agent_init import _merge_custom_provider_extra_body




def test_custom_provider_extra_body_preserves_caller_override():
    agent = SimpleNamespace(
        provider="custom",
        model="google/gemma-4-31b-it",
        base_url="https://example.test/v1",
        request_overrides={
            "extra_body": {
                "reasoning_effort": "low",
                "caller_only": True,
            }
        },
    )

    _merge_custom_provider_extra_body(
        agent,
        [
            {
                "name": "gemma",
                "base_url": "https://example.test/v1",
                "model": "google/gemma-4-31b-it",
                "extra_body": {
                    "enable_thinking": True,
                    "reasoning_effort": "high",
                },
            }
        ],
    )

    assert agent.request_overrides["extra_body"] == {
        "enable_thinking": True,
        "reasoning_effort": "low",
        "caller_only": True,
    }
    assert agent._custom_provider_extra_body == {"enable_thinking": True}




def test_named_custom_provider_extra_body_matches_provider_key():
    agent = SimpleNamespace(
        provider="custom:zai-coding-plan",
        model="glm-5.2",
        base_url="https://api.z.ai/api/coding/paas/v4",
        request_overrides={},
    )

    _merge_custom_provider_extra_body(
        agent,
        [
            {
                "provider_key": "other-provider",
                "name": "Other Provider",
                "base_url": "https://api.z.ai/api/coding/paas/v4",
                "model": "glm-5.2",
                "extra_body": {"enable_thinking": True},
            },
            {
                "provider_key": "zai-coding-plan",
                "name": "Z.AI Coding Plan",
                "base_url": "https://api.z.ai/api/coding/paas/v4/",
                "model": "glm-5.2",
                "extra_body": {"enable_thinking": False},
            },
        ],
    )

    assert agent.request_overrides == {"extra_body": {"enable_thinking": False}}


def test_custom_to_native_removes_only_provider_owned_extra_body():
    providers = [
        {
            "provider_key": "ollama-local",
            "base_url": "http://localhost:11434/v1",
            "model": "qwen3.5",
            "extra_body": {"think": False, "num_ctx": 65536},
        }
    ]
    agent = SimpleNamespace(
        provider="custom:ollama-local",
        requested_provider="custom:ollama-local",
        model="qwen3.5",
        base_url="http://localhost:11434/v1",
        request_overrides={
            "service_tier": "priority",
            "extra_body": {"caller_only": True},
        },
    )

    _merge_custom_provider_extra_body(agent, providers)
    assert agent._custom_provider_extra_body == {"think": False, "num_ctx": 65536}

    agent.provider = "openai-codex"
    agent.requested_provider = "openai-codex"
    agent.model = "gpt-5.6"
    agent.base_url = "https://chatgpt.com/backend-api/codex/responses"
    _merge_custom_provider_extra_body(agent, providers)

    assert agent.request_overrides == {
        "service_tier": "priority",
        "extra_body": {"caller_only": True},
    }
    assert agent._custom_provider_extra_body == {}


def test_custom_a_to_b_replaces_defaults_and_preserves_caller_collision():
    providers = [
        {
            "provider_key": "a",
            "base_url": "https://a.test/v1",
            "model": "model-a",
            "extra_body": {"think": False, "shared": "a"},
        },
        {
            "provider_key": "b",
            "base_url": "https://b.test/v1",
            "model": "model-b",
            "extra_body": {"enable_thinking": True, "shared": "b"},
        },
    ]
    agent = SimpleNamespace(
        provider="custom",
        requested_provider="custom:a",
        model="model-a",
        base_url="https://a.test/v1",
        request_overrides={"extra_body": {"shared": "caller", "caller_only": 1}},
    )

    _merge_custom_provider_extra_body(agent, providers)
    assert agent.request_overrides["extra_body"] == {
        "think": False,
        "shared": "caller",
        "caller_only": 1,
    }
    assert agent._custom_provider_extra_body == {"think": False}

    # Live switches may carry the configured provider key as a bare identity.
    agent.provider = "b"
    agent.requested_provider = "b"
    agent.model = "model-b"
    agent.base_url = "https://b.test/v1"
    _merge_custom_provider_extra_body(agent, providers)

    assert agent.request_overrides["extra_body"] == {
        "enable_thinking": True,
        "shared": "caller",
        "caller_only": 1,
    }
    assert agent._custom_provider_extra_body == {"enable_thinking": True}


def test_equal_value_caller_collision_is_never_provider_owned():
    providers = [
        {
            "provider_key": "local",
            "base_url": "http://localhost:11434/v1",
            "extra_body": {"think": False},
        }
    ]
    agent = SimpleNamespace(
        provider="custom:local",
        requested_provider="custom:local",
        model="qwen",
        base_url="http://localhost:11434/v1",
        request_overrides={"extra_body": {"think": False}},
    )

    _merge_custom_provider_extra_body(agent, providers)
    assert agent._custom_provider_extra_body == {}

    agent.provider = "openai-codex"
    agent.requested_provider = "openai-codex"
    agent.base_url = "https://chatgpt.com/backend-api/codex/responses"
    _merge_custom_provider_extra_body(agent, providers)

    assert agent.request_overrides == {"extra_body": {"think": False}}


def test_changed_provider_owned_value_becomes_caller_owned():
    providers = [
        {
            "provider_key": "local",
            "base_url": "http://localhost:11434/v1",
            "extra_body": {"num_ctx": 65536},
        }
    ]
    agent = SimpleNamespace(
        provider="custom:local",
        requested_provider="custom:local",
        model="qwen",
        base_url="http://localhost:11434/v1",
        request_overrides={},
    )

    _merge_custom_provider_extra_body(agent, providers)
    assert agent._custom_provider_extra_body == {"num_ctx": 65536}
    agent.request_overrides["extra_body"]["num_ctx"] = 32768

    agent.provider = "openai-codex"
    agent.requested_provider = "openai-codex"
    agent.base_url = "https://chatgpt.com/backend-api/codex/responses"
    _merge_custom_provider_extra_body(agent, providers)

    assert agent.request_overrides == {"extra_body": {"num_ctx": 32768}}
    assert agent._custom_provider_extra_body == {}


def test_native_provider_identity_wins_over_colliding_custom_name():
    providers = [
        {
            "provider_key": "openrouter",
            "name": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "extra_body": {"think": True},
        }
    ]
    agent = SimpleNamespace(
        provider="openrouter",
        requested_provider="openrouter",
        model="some-model",
        base_url="https://openrouter.ai/api/v1",
        request_overrides={},
    )

    _merge_custom_provider_extra_body(agent, providers)

    assert agent.request_overrides == {}
    assert agent._custom_provider_extra_body == {}


def test_catalog_import_failure_cannot_turn_native_name_into_custom_route():
    providers = [
        {
            "provider_key": "openrouter",
            "name": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "extra_body": {"think": True},
        }
    ]
    agent = SimpleNamespace(
        provider="openrouter",
        requested_provider="openrouter",
        model="some-model",
        base_url="https://openrouter.ai/api/v1",
        request_overrides={},
    )

    with patch.dict(sys.modules, {"hermes_cli.models": None}):
        _merge_custom_provider_extra_body(agent, providers)

    assert agent.request_overrides == {}
    assert agent._custom_provider_extra_body == {}


def test_nested_provider_fields_are_removed_without_losing_caller_fields():
    agent = SimpleNamespace(
        provider="openai-codex",
        requested_provider="openai-codex",
        model="gpt-5.5",
        base_url="https://chatgpt.com/backend-api/codex",
        request_overrides={
            "extra_body": {
                "options": {
                    "mode": "a",
                    "num_ctx": 65536,
                    "caller_flag": True,
                }
            }
        },
        _custom_provider_extra_body={
            "options": {"mode": "a", "num_ctx": 65536}
        },
    )

    _merge_custom_provider_extra_body(agent, [])

    assert agent.request_overrides == {
        "extra_body": {"options": {"caller_flag": True}}
    }
    assert agent._custom_provider_extra_body == {}

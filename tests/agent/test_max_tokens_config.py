"""Focused regressions for model/provider output-token cap validation."""

from unittest.mock import patch

import pytest


def _agent(config, *, max_tokens=None, requested_provider="custom:llamacpp"):
    from run_agent import AIAgent

    with patch("hermes_cli.config.load_config_readonly", return_value=config):
        return AIAgent(
            provider="custom",
            requested_provider=requested_provider,
            base_url="http://127.0.0.1:18080/v1",
            api_key="local",
            model="qwen-local",
            max_tokens=max_tokens,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )


@pytest.mark.parametrize("invalid_cap", [True, 0, -1, 12000.5])
def test_invalid_caller_and_model_caps_fall_through_to_named_custom_provider(
    invalid_cap,
):
    """Invalid CLI-originated caps must not suppress the provider fallback."""
    agent = _agent(
        {
            "model": {"max_tokens": invalid_cap},
            "providers": {"llamacpp": {"max_output_tokens": 9000}},
        },
        max_tokens=invalid_cap,
    )

    assert agent.max_tokens == 9000


@pytest.mark.parametrize(
    ("caller_cap", "model_cap", "provider_cap", "expected"),
    [
        (8000, 16000, 12000, 8000),
        (None, "16000", 12000, 16000),
        (None, None, "12000", 12000),
        (None, None, 12000.5, None),
    ],
)
def test_output_cap_precedence_and_strict_provider_validation(
    caller_cap, model_cap, provider_cap, expected
):
    agent = _agent(
        {
            "model": {"max_tokens": model_cap},
            "providers": {"llamacpp": {"max_output_tokens": provider_cap}},
        },
        max_tokens=caller_cap,
    )

    assert agent.max_tokens == expected


@pytest.mark.parametrize("requested_provider", ["custom:LLAMACPP", "custom:Local Llama"])
def test_named_custom_aliases_preserve_provider_cap(requested_provider):
    agent = _agent(
        {
            "providers": {
                "llamacpp": {
                    "name": "Local Llama",
                    "max_output_tokens": 9000,
                }
            }
        },
        requested_provider=requested_provider,
    )

    assert agent.max_tokens == 9000

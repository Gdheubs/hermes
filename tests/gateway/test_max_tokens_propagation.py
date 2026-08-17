"""Regression tests for max_tokens propagation from config.yaml to AIAgent.

Covers #20741: `model.max_tokens` was silently dropped before reaching the
gateway-spawned agent, so providers without a hardcoded default (OpenRouter
free models, Ollama Cloud, custom OpenAI-compatible endpoints) truncated long
generations with `finish_reason="length"`.

Precedence verified here:
    HERMES_MAX_TOKENS env  >  model.max_tokens  >  per-provider
    max_output_tokens  >  None
"""

import importlib
import os
import sys
import textwrap

import pytest


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a writable config.yaml and a clean module cache.

    These tests deliberately re-import ``hermes_cli`` / ``gateway`` so each
    config write is read fresh. To avoid leaking that purge into sibling test
    files in the same worker (which breaks their import-time mocks), we snapshot
    the affected modules and restore them on teardown.
    """
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_MAX_TOKENS", raising=False)

    _saved = {
        k: v
        for k, v in sys.modules.items()
        if k.startswith(("hermes_cli", "gateway"))
    }

    def write_cfg(body: str) -> None:
        (hermes_home / "config.yaml").write_text(textwrap.dedent(body))

    def fresh_gateway():
        for mod in list(sys.modules.keys()):
            if mod.startswith(("hermes_cli", "gateway")):
                del sys.modules[mod]
        return importlib.import_module("gateway.run")

    try:
        yield write_cfg, fresh_gateway
    finally:
        # Drop anything we (re)imported, then restore the pre-test snapshot so
        # the next test file sees the module objects it was loaded with.
        for k in list(sys.modules.keys()):
            if k.startswith(("hermes_cli", "gateway")):
                del sys.modules[k]
        sys.modules.update(_saved)


def test_top_level_max_tokens_propagates(isolated_home):
    """model.max_tokens is read into the gateway runtime kwargs (#20741)."""
    write_cfg, fresh_gateway = isolated_home
    write_cfg(
        """
        model:
          default: glm-5.1
          provider: openrouter
          max_tokens: 16384
        """
    )
    grun = fresh_gateway()
    kw = grun._resolve_runtime_agent_kwargs()
    assert kw["max_tokens"] == 16384


def test_per_provider_max_output_tokens_fallback(isolated_home):
    """A custom provider's max_output_tokens fills in when no global is set."""
    write_cfg, fresh_gateway = isolated_home
    write_cfg(
        """
        model:
          default: glm-5.1
          provider: mylocal
        providers:
          mylocal:
            api: http://localhost:11434/v1
            api_key: sk-test
            default_model: glm-5.1
            max_output_tokens: 12000
        """
    )
    grun = fresh_gateway()
    kw = grun._resolve_runtime_agent_kwargs()
    assert kw["max_tokens"] == 12000


def test_auth_fallback_uses_fallback_provider_output_cap(isolated_home, monkeypatch):
    """Fallback runtime cap wins when environment and model caps are absent."""
    write_cfg, fresh_gateway = isolated_home
    write_cfg(
        """
        model:
          default: primary-model
          provider: primary
        fallback_model:
          provider: fallback
          model: fallback-model
        """
    )
    grun = fresh_gateway()
    from hermes_cli.auth import AuthError

    def _resolve_runtime_provider(*, requested=None, **_kwargs):
        if requested is None:
            raise AuthError("expired", code="auth_failed")
        return {
            "api_key": "fallback-key",
            "base_url": "https://fallback.example/v1",
            "provider": "fallback",
            "requested_provider": requested,
            "api_mode": "chat_completions",
            "command": None,
            "args": ("--fallback",),
            "credential_pool": "pool",
            "max_output_tokens": 12000,
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        _resolve_runtime_provider,
    )

    assert grun._resolve_runtime_agent_kwargs()["max_tokens"] == 12000


def test_auth_fallback_model_reaches_session_runtime(isolated_home, monkeypatch):
    """A fallback provider's distinct model replaces the primary model."""
    write_cfg, fresh_gateway = isolated_home
    write_cfg(
        """
        model:
          default: primary-model
          provider: primary
        fallback_model:
          provider: fallback
          model: fallback-model
        """
    )
    grun = fresh_gateway()
    from hermes_cli.auth import AuthError

    def _resolve_runtime_provider(*, requested=None, **_kwargs):
        if requested is None:
            raise AuthError("expired", code="auth_failed")
        return {
            "api_key": "fallback-key",
            "base_url": "https://fallback.example/v1",
            "provider": "fallback",
            "requested_provider": requested,
            "api_mode": "chat_completions",
            "command": None,
            "args": (),
            "credential_pool": None,
            "max_output_tokens": 12000,
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        _resolve_runtime_provider,
    )
    runner = object.__new__(grun.GatewayRunner)
    runner._session_model_overrides = {}

    model, _runtime = runner._resolve_session_agent_runtime(
        user_config={"model": {"default": "primary-model"}}
    )

    assert model == "fallback-model"


def test_explicit_provider_keeps_its_output_cap(isolated_home):
    """A channel override must pass its provider output cap to the agent."""
    write_cfg, fresh_gateway = isolated_home
    write_cfg(
        """
        model:
          default: cloud-model
          provider: openai-codex
        providers:
          llamacpp:
            api: http://127.0.0.1:18080/v1
            api_key: local
            default_model: qwen3.8-27b-q4_k_m-128k
            max_output_tokens: 12000
        """
    )
    grun = fresh_gateway()
    kw = grun._resolve_runtime_agent_kwargs_for_provider("llamacpp")
    assert kw["max_tokens"] == 12000
    # The actual resolver canonicalizes the OpenAI-compatible endpoint to
    # ``custom`` but preserves the configured alias for presentation and
    # provider-specific config lookup.
    assert kw["provider"] == "custom"
    assert kw["requested_provider"] == "llamacpp"
    assert kw["base_url"] == "http://127.0.0.1:18080/v1"


def test_explicit_provider_honors_environment_output_cap(isolated_home, monkeypatch):
    """A one-off cap must override channel-provider and global defaults."""
    write_cfg, fresh_gateway = isolated_home
    write_cfg(
        """
        model:
          default: cloud-model
          provider: openai-codex
          max_tokens: 16000
        providers:
          llamacpp:
            api: http://127.0.0.1:18080/v1
            api_key: local
            default_model: qwen3.8-27b-q4_k_m-128k
            max_output_tokens: 12000
        """
    )
    monkeypatch.setenv("HERMES_MAX_TOKENS", "8000")
    grun = fresh_gateway()

    kw = grun._resolve_runtime_agent_kwargs_for_provider("llamacpp")

    assert kw["max_tokens"] == 8000


def test_invalid_environment_cap_falls_through_to_model_cap(isolated_home, monkeypatch):
    """An unusable HERMES_MAX_TOKENS must not skip model.max_tokens."""
    write_cfg, fresh_gateway = isolated_home
    write_cfg(
        """
        model:
          default: cloud-model
          provider: openai-codex
          max_tokens: 16000
        providers:
          llamacpp:
            api: http://127.0.0.1:18080/v1
            default_model: qwen3.8-27b-q4_k_m-128k
            max_output_tokens: 12000
        """
    )
    monkeypatch.setenv("HERMES_MAX_TOKENS", "not-a-number")

    assert fresh_gateway()._resolve_runtime_agent_kwargs_for_provider("llamacpp")["max_tokens"] == 16000


@pytest.mark.parametrize(
    ("model_cap", "provider_cap", "expected"),
    [
        (True, 12000, 12000),
        (False, True, None),
        (12000.5, 9000, 9000),
        (None, 9000.5, None),
    ],
)
def test_only_positive_integer_caps_are_accepted(
    isolated_home, model_cap, provider_cap, expected
):
    """Booleans and floats are invalid; decimal integer strings are accepted."""
    write_cfg, fresh_gateway = isolated_home
    model_line = "" if model_cap is None else f"max_tokens: {str(model_cap).lower()}"
    write_cfg(
        f"""
        model:
          default: cloud-model
          provider: openai-codex
          {model_line}
        providers:
          llamacpp:
            api: http://127.0.0.1:18080/v1
            default_model: qwen3.8-27b-q4_k_m-128k
            max_output_tokens: {str(provider_cap).lower() if isinstance(provider_cap, bool) else provider_cap}
        """
    )

    assert fresh_gateway()._resolve_runtime_agent_kwargs_for_provider("llamacpp")["max_tokens"] == expected


def test_gateway_cap_parser_accepts_decimal_integer_strings(isolated_home):
    _, fresh_gateway = isolated_home

    assert fresh_gateway()._positive_output_token_cap(" 9000 ") == 9000



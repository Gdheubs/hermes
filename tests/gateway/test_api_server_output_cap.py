from unittest.mock import patch

import pytest

from gateway.platforms.api_server import _resolve_request_runtime_agent_kwargs


def _resolve_cap(monkeypatch, *, env=None, model=None, provider=None):
    if env is None:
        monkeypatch.delenv("HERMES_MAX_TOKENS", raising=False)
    else:
        monkeypatch.setenv("HERMES_MAX_TOKENS", env)
    runtime = {"provider": "custom", "max_output_tokens": provider}
    with patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider", return_value=runtime
    ), patch(
        "hermes_cli.runtime_provider._get_model_config",
        return_value={"max_tokens": model},
    ):
        return _resolve_request_runtime_agent_kwargs("llamacpp")["max_tokens"]


def test_api_server_output_cap_precedence(monkeypatch):
    assert _resolve_cap(monkeypatch, env="8000", model=16000, provider=12000) == 8000
    assert _resolve_cap(monkeypatch, model=16000, provider=12000) == 16000
    assert _resolve_cap(monkeypatch, provider=12000) == 12000


@pytest.mark.parametrize("env", ["invalid", "0", "-1", "1.5", "true", ""])
def test_api_server_invalid_environment_cap_falls_through(monkeypatch, env):
    assert _resolve_cap(monkeypatch, env=env, model=16000, provider=12000) == 16000


@pytest.mark.parametrize("invalid", [True, False, 0, -1, 1.5, "1.5"])
def test_api_server_rejects_non_positive_or_non_integral_caps(monkeypatch, invalid):
    assert _resolve_cap(monkeypatch, model=invalid, provider=12000) == 12000
    assert _resolve_cap(monkeypatch, model=None, provider=invalid) is None

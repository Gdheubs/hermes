"""Regression tests for personality overlays in ``hermes -z`` mode."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from hermes_cli import oneshot
from hermes_cli.personality import BUILTIN_PERSONALITIES


class _FakeAgent:
    init_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).init_kwargs = kwargs

    def run_conversation(self, prompt):
        return {"final_response": "ok", "failed": False, "completed": True}

    def shutdown_memory_provider(self, *args, **kwargs):
        return None

    def close(self):
        return None


def _run_oneshot(monkeypatch, config):
    hermes_home = Path(os.environ["HERMES_HOME"])
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(config),
        encoding="utf-8",
    )

    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: {
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "provider": "test-provider",
            "requested_provider": "test-provider",
            "api_mode": "chat_completions",
            "credential_pool": None,
        },
    )
    monkeypatch.setattr(
        "hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(oneshot, "_create_session_db_for_oneshot", lambda: None)

    response, _ = oneshot._run_agent("hello", use_config_toolsets=False)

    assert response == "ok"
    return _FakeAgent.init_kwargs


def test_oneshot_passes_configured_personality_to_agent(monkeypatch):
    kwargs = _run_oneshot(
        monkeypatch,
        {
            "model": {"default": "test-model", "provider": "test-provider"},
            "display": {"personality": "catgirl"},
        },
    )

    assert kwargs["ephemeral_system_prompt"] == BUILTIN_PERSONALITIES["catgirl"]


def test_oneshot_prefers_environment_overlay_to_personality(monkeypatch):
    monkeypatch.setenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", "environment overlay")

    kwargs = _run_oneshot(
        monkeypatch,
        {
            "model": {"default": "test-model", "provider": "test-provider"},
            "display": {"personality": "catgirl"},
        },
    )

    assert kwargs["ephemeral_system_prompt"] == "environment overlay"


def test_oneshot_uses_manual_prompt_without_personality(monkeypatch):
    kwargs = _run_oneshot(
        monkeypatch,
        {
            "model": {"default": "test-model", "provider": "test-provider"},
            "display": {"personality": "none"},
            "agent": {"system_prompt": "manual overlay"},
        },
    )

    assert kwargs["ephemeral_system_prompt"] == "manual overlay"

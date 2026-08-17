"""Per-job cron reasoning policy must be explicit and fail closed."""

from unittest.mock import MagicMock, patch

import pytest


def test_create_and_update_persist_only_canonical_explicit_reasoning(
    tmp_path, monkeypatch
):
    from cron import jobs

    monkeypatch.setattr(jobs, "_compute_provider_model_snapshots", lambda **_kw: (None, None))
    with jobs.use_cron_store(tmp_path):
        plain = jobs.create_job("plain", "* * * * *")
        assert "reasoning_effort" not in plain

        created = jobs.create_job(
            "reasoned", "* * * * *", reasoning_effort=" HIGH "
        )
        assert created["reasoning_effort"] == "high"

        updated = jobs.update_job(created["id"], {"reasoning_effort": "none"})
        assert updated["reasoning_effort"] == "none"

        cleared = jobs.update_job(created["id"], {"reasoning_effort": None})
        assert "reasoning_effort" not in cleared


@pytest.mark.parametrize("value", [True, "", "   ", "unknown"])
def test_create_and_update_reject_invalid_reasoning(tmp_path, monkeypatch, value):
    from cron import jobs

    monkeypatch.setattr(jobs, "_compute_provider_model_snapshots", lambda **_kw: (None, None))
    with jobs.use_cron_store(tmp_path):
        with pytest.raises(ValueError, match="reasoning_effort"):
            jobs.create_job("bad", "* * * * *", reasoning_effort=value)

        created = jobs.create_job("plain", "* * * * *")
        with pytest.raises(ValueError, match="reasoning_effort"):
            jobs.update_job(created["id"], {"reasoning_effort": value})


def test_runtime_resolver_preserves_default_and_fails_closed():
    from cron.scheduler import _resolve_job_reasoning_config

    config = {"agent": {"reasoning_effort": "low"}}
    assert _resolve_job_reasoning_config({}, config, "model-a") == {
        "enabled": True,
        "effort": "low",
    }
    assert _resolve_job_reasoning_config(
        {"reasoning_effort": False}, config, "model-a"
    ) == {"enabled": False}
    with pytest.raises(ValueError, match="reasoning_effort"):
        _resolve_job_reasoning_config(
            {"reasoning_effort": "invalid"}, config, "model-a"
        )


def test_run_job_passes_job_reasoning_override_to_agent(tmp_path):
    from cron.scheduler import run_job

    (tmp_path / "config.yaml").write_text(
        "agent:\n  reasoning_effort: low\n", encoding="utf-8"
    )
    job = {
        "id": "reasoning-job",
        "name": "reasoning-job",
        "prompt": "synthetic",
        "reasoning_effort": "high",
    }
    fake_db = MagicMock()
    fake_agent = MagicMock()
    fake_agent.run_conversation.return_value = {"final_response": "ok"}

    with patch("cron.scheduler._hermes_home", tmp_path), patch(
        "cron.scheduler._resolve_origin", return_value=None
    ), patch("hermes_cli.env_loader.load_hermes_dotenv"), patch(
        "hermes_cli.env_loader.reset_secret_source_cache"
    ), patch("hermes_state.SessionDB", return_value=fake_db), patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={
            "api_key": "synthetic-key",
            "base_url": "https://example.invalid/v1",
            "provider": "test",
            "api_mode": "chat_completions",
        },
    ), patch("tools.mcp_tool.discover_mcp_tools", return_value=[]), patch(
        "run_agent.AIAgent", return_value=fake_agent
    ) as agent_cls:
        success, _, final, error = run_job(job)

    assert success is True
    assert error is None
    assert final == "ok"
    assert agent_cls.call_args.kwargs["reasoning_config"] == {
        "enabled": True,
        "effort": "high",
    }

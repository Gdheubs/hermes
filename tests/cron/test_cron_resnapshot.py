"""``resnapshot`` control flag on cron job updates (#75375).

An UNPINNED job follows the global default at fire time; the #44585 drift
guard snapshots that default at job creation and fails closed when the global
default later changes. ``update_job(..., {"resnapshot": True})`` re-baselines
the snapshots to the CURRENT global default without pinning the job, so it
keeps following ``cron.model`` / ``model.default``.

The flag is user-owned (CLI/dashboard/programmatic callers only — it is
deliberately absent from the agent-facing ``cronjob`` tool schema, mirroring
the model/provider pin policy) and is never persisted to the job record.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is importable.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.jobs import load_jobs, update_job

DEFAULT_MODEL = "deepseek/deepseek-v4-flash-0731"
DEFAULT_PROVIDER = "nous"


def _write_config(home: Path) -> None:
    (home / "config.yaml").write_text(
        "model:\n"
        f"  default: {DEFAULT_MODEL}\n"
        f"  provider: {DEFAULT_PROVIDER}\n",
        encoding="utf-8",
    )


def _write_jobs(home: Path, jobs: list) -> None:
    cron_dir = home / "cron"
    cron_dir.mkdir(exist_ok=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps({"jobs": jobs}), encoding="utf-8"
    )


def _base_job(**overrides) -> dict:
    job = {
        "id": "resnap-test",
        "name": "resnapshot test",
        "prompt": "hello",
        "skills": [],
        "skill": None,
        "model": None,
        "provider": None,
        "provider_snapshot": "openai-codex",
        "model_snapshot": "gpt-5.6-terra",
        "base_url": None,
        "no_agent": False,
        "script": None,
        "schedule": {"kind": "cron", "expr": "0 9 * * *", "display": "0 9 * * *"},
        "schedule_display": "0 9 * * *",
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "next_run_at": "2026-08-15T09:00:00+00:00",
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    job.update(overrides)
    return job


def _resolved_provider(*args, **kwargs):
    """Deterministic runtime-provider resolution for snapshot capture."""
    return {
        "api_key": "test-key",
        "base_url": "https://example.invalid/v1",
        "provider": DEFAULT_PROVIDER,
        "api_mode": "chat_completions",
    }


class TestResnapshot:
    def test_unpinned_job_resnapshot_refreshes_snapshots(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_config(tmp_path)
        _write_jobs(tmp_path, [_base_job()])

        with patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            side_effect=_resolved_provider,
        ):
            updated = update_job("resnap-test", {"resnapshot": True})

        assert updated is not None
        assert updated["model"] is None
        assert updated["provider"] is None
        assert updated["provider_snapshot"] == DEFAULT_PROVIDER
        assert updated["model_snapshot"] == DEFAULT_MODEL

        # The control flag must never be persisted.
        stored = load_jobs()[0]
        assert "resnapshot" not in stored

    def test_unpinned_job_without_resnapshot_keeps_stale_snapshots(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_config(tmp_path)
        _write_jobs(tmp_path, [_base_job()])

        with patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            side_effect=_resolved_provider,
        ):
            updated = update_job("resnap-test", {"name": "renamed"})

        assert updated["name"] == "renamed"
        assert updated["provider_snapshot"] == "openai-codex"
        assert updated["model_snapshot"] == "gpt-5.6-terra"

    def test_pinned_job_resnapshot_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_config(tmp_path)
        _write_jobs(
            tmp_path,
            [
                _base_job(
                    model="anthropic/claude-sonnet-4",
                    provider="anthropic",
                )
            ],
        )

        with patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            side_effect=_resolved_provider,
        ):
            updated = update_job("resnap-test", {"resnapshot": True})

        # Pinned axes carry no snapshot; the flag must not unpin the job.
        assert updated["model"] == "anthropic/claude-sonnet-4"
        assert updated["provider"] == "anthropic"
        assert updated["provider_snapshot"] is None
        assert updated["model_snapshot"] is None

    def test_no_agent_job_resnapshot_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_config(tmp_path)
        _write_jobs(tmp_path, [_base_job(no_agent=True, script="watch.py")])

        with patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            side_effect=_resolved_provider,
        ):
            updated = update_job("resnap-test", {"resnapshot": True})

        assert updated["no_agent"] is True
        assert updated["provider_snapshot"] is None
        assert updated["model_snapshot"] is None

    def test_resnapshot_true_false_values(self, tmp_path, monkeypatch):
        """Only a truthy flag triggers a re-baseline; false leaves state alone."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_config(tmp_path)
        _write_jobs(tmp_path, [_base_job()])

        with patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            side_effect=_resolved_provider,
        ):
            updated = update_job("resnap-test", {"resnapshot": False})

        assert updated["provider_snapshot"] == "openai-codex"
        assert updated["model_snapshot"] == "gpt-5.6-terra"

    def test_programmatic_tool_call_supports_resnapshot(self, tmp_path, monkeypatch):
        """cronjob(action='update', resnapshot=True) works for programmatic callers."""
        from tools.cronjob_tools import CRONJOB_SCHEMA, cronjob

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_config(tmp_path)
        _write_jobs(tmp_path, [_base_job()])

        with patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            side_effect=_resolved_provider,
        ):
            result = json.loads(
                cronjob(action="update", job_id="resnap-test", resnapshot=True)
            )

        assert result.get("success") is True
        job = result["job"]
        assert job["model"] is None
        # The tool's formatted output omits snapshot fields; verify the store.
        stored = load_jobs()[0]
        assert stored["provider_snapshot"] == DEFAULT_PROVIDER
        assert stored["model_snapshot"] == DEFAULT_MODEL
        assert "resnapshot" not in stored

        # The agent-facing schema must NOT expose the flag (user-owned, like
        # model/provider pins): an agent must not silently re-baseline a
        # drift guard that is blocking unattended spend.
        assert "resnapshot" not in CRONJOB_SCHEMA["parameters"]["properties"]

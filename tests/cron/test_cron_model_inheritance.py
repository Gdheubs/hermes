"""Cron global-model resolution honors opt-in root→named-profile inheritance.

Both cron paths that resolve the GLOBAL (unpinned) model read the raw profile
``config.yaml`` and previously bypassed ``load_config()``'s opt-in root-primary
inheritance:

* ``cron/scheduler.py`` ``run_job`` — the fire-time global model, compared
  against the drift snapshot via ``resolve_cron_model_drift_defaults``.
* ``cron/jobs.py`` ``_resolve_default_model_snapshot`` — the create-time snapshot
  an unpinned job stores so a later global swap fails closed.

Both now route through the single canonical helper
``hermes_cli.config.build_cron_effective_config`` (raw user config → opt-in
root inheritance → managed overlay → env expansion, in canonical precedence),
so an opted-in *unpinned* worker snapshots and fires on the SAME inherited root
primary model. Explicit cron overrides — a per-job model pin and the
``cron.model`` fleet default — are applied by the callers and stay unchanged.

Generic placeholder ids only — never a real model/provider.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from hermes_cli import config as cfgmod


ROOT_MODEL = "root-model-alpha"
ROOT_PROVIDER = "provider-root"
LOCAL_MODEL = "local-model-beta"
LOCAL_PROVIDER = "provider-local"
CRON_FLEET_MODEL = "cron-fleet-model-gamma"
MANAGED_MODEL = "managed-model-delta"


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def cron_profile_env(tmp_path, monkeypatch):
    """HERMES_HOME points at ``<root>/profiles/coder`` so
    ``get_default_hermes_root()`` resolves ``<root>`` and cron reads the named
    profile config."""
    root = tmp_path / "root"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.delenv("HERMES_MODEL", raising=False)  # config, not env, decides
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfgmod._RAW_CONFIG_CACHE.clear()
    yield {
        "root_cfg": root / "config.yaml",
        "profile_cfg": profile / "config.yaml",
    }
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfgmod._RAW_CONFIG_CACHE.clear()


# ── Canonical helper: build_cron_effective_config ───────────────────────────

def test_build_cron_effective_config_opted_in_inherits_root_primary(cron_profile_env):
    _write_yaml(cron_profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(cron_profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    cfg = cfgmod.build_cron_effective_config(cron_profile_env["profile_cfg"])

    assert cfg["model"]["default"] == ROOT_MODEL
    assert cfg["model"]["provider"] == ROOT_PROVIDER


def test_build_cron_effective_config_default_off_stays_isolated(cron_profile_env):
    _write_yaml(cron_profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(cron_profile_env["profile_cfg"], {"model": {"default": LOCAL_MODEL, "provider": LOCAL_PROVIDER}})

    cfg = cfgmod.build_cron_effective_config(cron_profile_env["profile_cfg"])

    assert cfg["model"]["default"] == LOCAL_MODEL
    assert cfg["model"]["provider"] == LOCAL_PROVIDER


def test_build_cron_effective_config_managed_overlay_wins_over_inherited_root(
    cron_profile_env, tmp_path, monkeypatch
):
    """Canonical precedence: an administrator-pinned managed model still wins
    over the inherited root value."""
    _write_yaml(cron_profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(cron_profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    managed_dir = tmp_path / "managed"
    _write_yaml(managed_dir / "config.yaml", {"model": {"default": MANAGED_MODEL}})
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    from hermes_cli import managed_scope
    managed_scope.invalidate_managed_cache()

    cfg = cfgmod.build_cron_effective_config(cron_profile_env["profile_cfg"])

    assert cfg["model"]["default"] == MANAGED_MODEL  # managed wins over inherited root
    managed_scope.invalidate_managed_cache()


# ── run_job's global-model expression (resolve_cron_model_drift_defaults) ────

def test_run_job_global_model_expression_uses_inherited_root(cron_profile_env):
    """The exact expression run_job evaluates for the unpinned global model —
    ``resolve_cron_model_drift_defaults(build_cron_effective_config(...))`` —
    resolves the inherited root primary for an opted-in profile."""
    _write_yaml(cron_profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(cron_profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    cfg = cfgmod.build_cron_effective_config(cron_profile_env["profile_cfg"])
    provider, model = cfgmod.resolve_cron_model_drift_defaults(cfg)

    assert model == ROOT_MODEL
    assert provider == ROOT_PROVIDER


# ── create-time snapshot: _resolve_default_model_snapshot ────────────────────

def test_snapshot_resolves_inherited_root_model_for_opted_in_profile(cron_profile_env):
    from cron.jobs import _resolve_default_model_snapshot

    _write_yaml(cron_profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(cron_profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    assert _resolve_default_model_snapshot() == ROOT_MODEL


def test_snapshot_default_off_stays_isolated(cron_profile_env):
    from cron.jobs import _resolve_default_model_snapshot

    _write_yaml(cron_profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(cron_profile_env["profile_cfg"], {"model": {"default": LOCAL_MODEL}})

    assert _resolve_default_model_snapshot() == LOCAL_MODEL


def test_snapshot_cron_fleet_default_still_wins_over_inherited_root(cron_profile_env):
    """Explicit cron override unchanged: the ``cron.model`` fleet default beats
    the (now inheritance-aware) global chat model for unpinned cron jobs."""
    from cron.jobs import _resolve_default_model_snapshot

    _write_yaml(cron_profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(
        cron_profile_env["profile_cfg"],
        {"model": {"inherit_root_primary": True}, "cron": {"model": CRON_FLEET_MODEL}},
    )

    assert _resolve_default_model_snapshot() == CRON_FLEET_MODEL

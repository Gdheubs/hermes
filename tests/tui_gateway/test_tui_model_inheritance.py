"""Desktop/TUI + bridge primary-model resolution honors opt-in root inheritance.

``tui_gateway/server.py`` reads config via ``_load_cfg`` (raw user file +
managed overlay + ``${VAR}`` expansion), a bypass of ``load_config()``.
``_resolve_model`` and ``_config_model_target`` both read ``_load_cfg()["model"]``.
This proves that former bypass now routes the shared
``hermes_cli.config.apply_root_primary_model_inheritance`` resolver, so an
opted-in named profile's desktop/TUI/bridge model reflects the two inherited
root scalars — while the write-back primitive ``_load_cfg_raw`` stays raw
(never persisting inherited values), opted-out stays isolated, and root-only
edits are observed without restart.

Generic placeholder ids only — never a real model/provider.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from hermes_cli import config as cfgmod
import tui_gateway.server as server


ROOT_MODEL = "root-model-alpha"
ROOT_PROVIDER = "provider-root"
LOCAL_MODEL = "local-model-beta"
LOCAL_PROVIDER = "provider-local"
MANAGED_MODEL = "managed-model-delta"


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns + 1_000_000_000, st.st_mtime_ns + 1_000_000_000))


def _reset_cfg_cache():
    server._cfg_cache = None
    server._cfg_mtime = None
    server._cfg_path = None


@pytest.fixture
def tui_profile_env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(server, "_hermes_home", str(profile))
    # No per-session override in these tests.
    monkeypatch.setattr(server, "get_hermes_home_override", lambda: None)
    _reset_cfg_cache()
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfgmod._RAW_CONFIG_CACHE.clear()
    yield {
        "root_cfg": root / "config.yaml",
        "profile_cfg": profile / "config.yaml",
    }
    _reset_cfg_cache()
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfgmod._RAW_CONFIG_CACHE.clear()


def test_tui_opted_in_profile_inherits_root_primary(tui_profile_env):
    _write_yaml(tui_profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(tui_profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    assert server._resolve_model() == ROOT_MODEL
    assert server._config_model_target() == (ROOT_MODEL, ROOT_PROVIDER)


def test_tui_load_cfg_raw_stays_raw(tui_profile_env):
    """The write-back primitive must NOT carry inherited values (else a save
    would persist root's model into the profile file)."""
    _write_yaml(tui_profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(tui_profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    raw = server._load_cfg_raw()
    assert "default" not in raw.get("model", {})
    assert "provider" not in raw.get("model", {})


def test_tui_default_off_stays_isolated(tui_profile_env):
    _write_yaml(tui_profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(tui_profile_env["profile_cfg"], {"model": {"default": LOCAL_MODEL, "provider": LOCAL_PROVIDER}})

    assert server._resolve_model() == LOCAL_MODEL
    assert server._config_model_target() == (LOCAL_MODEL, LOCAL_PROVIDER)


def test_tui_managed_overlay_wins_over_inherited_root(tui_profile_env, tmp_path, monkeypatch):
    """Canonical precedence: an administrator-pinned managed model.default must
    win over an inherited root value, while an un-pinned managed leaf
    (model.provider) still fills from the inherited root."""
    _write_yaml(tui_profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(tui_profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    managed_dir = tmp_path / "managed"
    _write_yaml(managed_dir / "config.yaml", {"model": {"default": MANAGED_MODEL}})
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    from hermes_cli import managed_scope
    managed_scope.invalidate_managed_cache()

    assert server._resolve_model() == MANAGED_MODEL  # managed wins
    assert server._config_model_target() == (MANAGED_MODEL, ROOT_PROVIDER)  # provider inherited
    managed_scope.invalidate_managed_cache()


def test_tui_root_change_observed_without_restart(tui_profile_env):
    _write_yaml(tui_profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(tui_profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    assert server._resolve_model() == ROOT_MODEL

    _write_yaml(tui_profile_env["root_cfg"], {"model": {"default": "root-model-changed", "provider": ROOT_PROVIDER}})
    _bump_mtime(tui_profile_env["root_cfg"])

    assert server._resolve_model() == "root-model-changed"

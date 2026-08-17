"""Gateway primary-model resolution honors the opt-in root inheritance.

``gateway/run.py`` reads config via ``_load_gateway_config`` (a raw-YAML +
managed-overlay bypass of ``load_config()``). This test proves that former
bypass now routes through the shared
``hermes_cli.config.apply_root_primary_model_inheritance`` resolver, so an
opted-in named profile's gateway model tier (``_resolve_gateway_model`` /
``_load_gateway_config``) reflects the two inherited root scalars — while
opted-out and the default/root profile stay isolated, and root-only edits are
observed without restart.

All model ids/providers are generic placeholders — never a real model/provider.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from hermes_cli import config as cfgmod
import gateway.run as gateway_run


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


@pytest.fixture
def gw_profile_env(tmp_path, monkeypatch):
    """HERMES_HOME + gateway ``_hermes_home`` point at ``<root>/profiles/coder``
    so ``get_default_hermes_root()`` resolves ``<root>``."""
    root = tmp_path / "root"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(gateway_run, "_hermes_home", profile)
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfgmod._RAW_CONFIG_CACHE.clear()
    yield {
        "root_cfg": root / "config.yaml",
        "profile_cfg": profile / "config.yaml",
    }
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfgmod._RAW_CONFIG_CACHE.clear()


def test_gateway_opted_in_profile_inherits_root_primary(gw_profile_env):
    _write_yaml(gw_profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(gw_profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    assert gateway_run._resolve_gateway_model() == ROOT_MODEL
    cfg = gateway_run._load_gateway_config()
    assert cfg["model"]["default"] == ROOT_MODEL
    assert cfg["model"]["provider"] == ROOT_PROVIDER


def test_gateway_default_off_stays_isolated(gw_profile_env):
    _write_yaml(gw_profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(gw_profile_env["profile_cfg"], {"model": {"default": LOCAL_MODEL, "provider": LOCAL_PROVIDER}})

    assert gateway_run._resolve_gateway_model() == LOCAL_MODEL
    cfg = gateway_run._load_gateway_config()
    assert cfg["model"]["provider"] == LOCAL_PROVIDER


def test_gateway_root_change_observed_without_restart(gw_profile_env):
    _write_yaml(gw_profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(gw_profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    assert gateway_run._resolve_gateway_model() == ROOT_MODEL

    _write_yaml(gw_profile_env["root_cfg"], {"model": {"default": "root-model-changed", "provider": ROOT_PROVIDER}})
    _bump_mtime(gw_profile_env["root_cfg"])

    assert gateway_run._resolve_gateway_model() == "root-model-changed"


def test_gateway_managed_overlay_wins_over_inherited_root(gw_profile_env, tmp_path, monkeypatch):
    """Canonical precedence: an administrator-pinned managed model.default must
    win over an inherited root value, while a leaf managed does NOT pin
    (model.provider) still fills from the inherited root."""
    _write_yaml(gw_profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(gw_profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    managed_dir = tmp_path / "managed"
    _write_yaml(managed_dir / "config.yaml", {"model": {"default": MANAGED_MODEL}})
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    from hermes_cli import managed_scope
    managed_scope.invalidate_managed_cache()

    assert gateway_run._resolve_gateway_model() == MANAGED_MODEL  # managed wins
    cfg = gateway_run._load_gateway_config()
    assert cfg["model"]["default"] == MANAGED_MODEL
    assert cfg["model"]["provider"] == ROOT_PROVIDER  # inherited fills the un-pinned leaf
    managed_scope.invalidate_managed_cache()


def test_gateway_missing_root_fails_safe(gw_profile_env):
    # No root config; opted-in profile keeps its own value, no broadening.
    _write_yaml(gw_profile_env["profile_cfg"],
                {"model": {"inherit_root_primary": True, "default": LOCAL_MODEL, "provider": LOCAL_PROVIDER}})

    assert gateway_run._resolve_gateway_model() == LOCAL_MODEL
    cfg = gateway_run._load_gateway_config()
    assert cfg["model"]["provider"] == LOCAL_PROVIDER

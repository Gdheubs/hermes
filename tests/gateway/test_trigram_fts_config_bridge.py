"""config.yaml sessions.trigram_fts bridge (config-authoritative).

Modeled directly on test_cjk_fts_config_bridge.py — adaptation C wires the
trigram index gate through the same config->env bridge as sessions.cjk_fts.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

import gateway.run as gateway_run


def _write_home(tmp_path: Path, sessions_cfg: dict, env_text: str = "") -> Path:
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"sessions": sessions_cfg}), encoding="utf-8"
    )
    (hermes_home / ".env").write_text(env_text, encoding="utf-8")
    return hermes_home


def test_trigram_fts_bridged_from_config(tmp_path, monkeypatch):
    home = _write_home(tmp_path, {"trigram_fts": False})
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    monkeypatch.setenv("HERMES_TRIGRAM_FTS", "1")
    gateway_run._reload_runtime_env_preserving_config_authority()
    assert os.environ["HERMES_TRIGRAM_FTS"] == "False"


def test_trigram_fts_documented_default_enabled():
    """Enabled-by-default upstream parity, same as sessions.cjk_fts."""
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["sessions"]["trigram_fts"] is True

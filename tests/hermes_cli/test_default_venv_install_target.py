"""Tests for :func:`hermes_cli.main._default_venv_install_target`.

Regression for the ``_default_venv_install_target`` path bug: the
``VIRTUAL_ENV`` env var was hardcoded to ``<PROJECT_ROOT>/venv`` instead of the
live venv, so lazy-refresh-recovery / dependency-repair on a ``.venv`` install
targeted a nonexistent directory and logged false-positive health warnings.
"""

from __future__ import annotations

import os

import hermes_cli.main as m


def test_install_target_points_at_dot_venv(tmp_path, monkeypatch):
    """VIRTUAL_ENV must follow the live venv, not a hardcoded ``venv`` dir."""
    (tmp_path / ".venv").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(m, "_is_termux_env", lambda env: False)
    monkeypatch.setattr(
        "hermes_cli.managed_uv.ensure_uv", lambda: "uv", raising=True
    )
    # Force _default_live_venv to resolve to the .venv layout (no venv dir).
    monkeypatch.setattr(
        "hermes_cli.managed_uv._default_live_venv",
        lambda root: root / ".venv",
    )

    cmd, env = m._default_venv_install_target()
    assert cmd == ["uv", "pip"]
    assert env is not None
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")


def test_install_target_env_is_clean(tmp_path, monkeypatch):
    """The env returned carries VIRTUAL_ENV and no stale PYTHONPATH on POSIX."""
    (tmp_path / ".venv").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(m, "_is_termux_env", lambda env: False)
    monkeypatch.setattr(
        "hermes_cli.managed_uv.ensure_uv", lambda: "uv", raising=True
    )
    monkeypatch.setattr(
        "hermes_cli.managed_uv._default_live_venv",
        lambda root: root / ".venv",
    )
    monkeypatch.setenv("PYTHONPATH", "/some/existing/path")

    _cmd, env = m._default_venv_install_target()
    assert env is not None
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")
    # PYTHONPATH is only stripped in a Termux env; POSIX keeps it.
    assert env.get("PYTHONPATH") == os.environ.get("PYTHONPATH")

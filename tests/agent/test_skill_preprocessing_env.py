"""SKILL.md inline-shell snippets must use the central subprocess env factory.

``!`cmd``` expansion used to call ``subprocess.run()`` with no ``env`` at all,
so — unlike every terminal/tool spawn — the snippet inherited the raw process
environment: no session-context stamps, no Desktop connection-mode stamp, and
no scrub of a ``HERMES_DESKTOP_CONNECTION_MODE`` value inherited from the
user's shell (#82187 follow-up review, item 2).
"""

import os
import subprocess
from types import SimpleNamespace

import pytest

import gateway.session_context as sc
from gateway.session_context import (
    DESKTOP_CONNECTION_MODE_ENV as MODE_ENV,
    _VAR_MAP,
    set_desktop_connection_mode,
    set_session_vars,
)

from agent.skill_preprocessing import run_inline_shell


@pytest.fixture(autouse=True)
def _isolate_session_context():
    """Clean ContextVar + os.environ + engaged-latch slate per test, restored."""
    tracked = list(_VAR_MAP.keys()) + [MODE_ENV]
    saved_env = {k: os.environ.get(k) for k in tracked}
    saved_ctx = {name: var.get() for name, var in _VAR_MAP.items()}
    saved_mode = sc._DESKTOP_CONNECTION_MODE.get()
    saved_engaged = sc._session_context_engaged
    for var in _VAR_MAP.values():
        var.set(sc._UNSET)
    sc._DESKTOP_CONNECTION_MODE.set(sc._UNSET)
    sc._session_context_engaged = False
    try:
        yield
    finally:
        for var, val in zip(_VAR_MAP.values(), saved_ctx.values()):
            var.set(val)
        sc._DESKTOP_CONNECTION_MODE.set(saved_mode)
        sc._session_context_engaged = saved_engaged
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _capture_spawn_env(monkeypatch) -> dict:
    """Run one inline snippet with subprocess.run stubbed; return its env kwarg."""
    captured = {}

    def _fake_run(argv, **kwargs):
        captured.update({"argv": argv, "env": kwargs.get("env")})
        return SimpleNamespace(stdout="ok\n", stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert run_inline_shell("echo hi", None, timeout=5) == "ok"
    return captured


def test_inline_shell_passes_a_factory_built_env(monkeypatch):
    """The spawn must supply an explicit env, not inherit the raw process one."""
    captured = _capture_spawn_env(monkeypatch)
    assert captured["env"] is not None


def test_live_mode_is_stamped_for_the_snippet(monkeypatch):
    set_session_vars(session_key="k", source="desktop")
    set_desktop_connection_mode("remote")
    captured = _capture_spawn_env(monkeypatch)
    assert captured["env"][MODE_ENV] == "remote"


def test_live_mode_overrides_an_ambient_shell_value(monkeypatch):
    """A live remote ContextVar wins over HERMES_DESKTOP_CONNECTION_MODE=local
    inherited from the user's shell."""
    monkeypatch.setenv(MODE_ENV, "local")
    set_session_vars(session_key="k", source="desktop")
    set_desktop_connection_mode("remote")
    captured = _capture_spawn_env(monkeypatch)
    assert captured["env"][MODE_ENV] == "remote"


def test_ambient_value_is_stripped_when_no_mode_is_bound(monkeypatch):
    """Engaged session context with no bound mode: the inherited shell value is
    stripped rather than honored (write-only stamp contract)."""
    monkeypatch.setenv(MODE_ENV, "remote")
    set_session_vars(session_key="k", source="tui")
    captured = _capture_spawn_env(monkeypatch)
    assert MODE_ENV not in captured["env"]


def test_snippet_is_not_spawned_when_the_env_factory_fails(monkeypatch):
    """A sanitizer that cannot be built must fail CLOSED, not fall back to env=None.

    ``subprocess.run(env=None)`` inherits the raw parent environment, so the
    old ``except: _run_env = None`` fallback handed the snippet an ambient
    ``HERMES_DESKTOP_CONNECTION_MODE=local`` verbatim — reopening exactly the
    spoofing path the scrub exists to close, and only on the error branch where
    nobody would look for it (#82187 follow-up review, item 3).
    """
    monkeypatch.setenv(MODE_ENV, "local")
    set_session_vars(session_key="k", source="desktop")
    set_desktop_connection_mode("remote")

    import tools.environments.local as local_env

    def _boom():
        raise RuntimeError("sanitizer unavailable")

    monkeypatch.setattr(local_env, "build_subprocess_env", _boom)

    spawned = []

    def _record(argv, **kwargs):
        spawned.append({"argv": argv, "env": kwargs.get("env")})
        return SimpleNamespace(stdout="ok\n", stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", _record)

    result = run_inline_shell("echo hi", None, timeout=5)

    # The strongest assertion available: the child never existed, so there is
    # no environment for it to have inherited.
    assert spawned == [], (
        "the snippet was spawned with a non-sanitized environment: "
        f"{spawned[0]['env'] if spawned else None}"
    )
    assert "inline-shell error" in result

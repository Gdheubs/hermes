"""The skill-facing read path for the Desktop connection mode (#82140).

Skills branch on ``HERMES_DESKTOP_CONNECTION_MODE`` from their helper scripts to
decide whether a gateway-side artifact is already on the user's machine or has
to be transferred first. The subprocess bridge stamps it — **write-only**, on
every spawn — which is precisely what keeps the issue's "no user-configurable
``HERMES_*`` env var" criterion true: a value inherited from the user's shell is
stripped rather than honored, and the contextvar remains the only source.
"""

import os

import pytest

import gateway.session_context as sc
from gateway.session_context import (
    DESKTOP_CONNECTION_MODE_ENV as MODE_ENV,
    _VAR_MAP,
    clear_session_vars,
    set_desktop_connection_mode,
    set_session_vars,
)
from tools.environments.local import _make_run_env

SESSION_VARS = list(_VAR_MAP.keys())


@pytest.fixture(autouse=True)
def _isolate_session_context():
    """Clean ContextVar + os.environ + engaged-latch slate per test, restored."""
    tracked = SESSION_VARS + [MODE_ENV]
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


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_bound_mode_is_stamped_for_the_child(mode):
    set_desktop_connection_mode(mode)
    assert _make_run_env({})[MODE_ENV] == mode


def test_remote_like_saved_mode_is_stamped_normalized():
    set_desktop_connection_mode("ssh")
    assert _make_run_env({})[MODE_ENV] == "remote"


def test_unbound_session_stamps_nothing():
    """CLI, TUI, messaging, cron: the var is simply absent."""
    assert MODE_ENV not in _make_run_env({})


def test_inherited_env_value_is_stripped_when_no_mode_is_bound(monkeypatch):
    """The criterion: a user-set value is NOT a source of truth.

    A user who exports HERMES_DESKTOP_CONNECTION_MODE=local in their shell must
    not be able to convince a CLI-session skill that gateway files are sitting
    on a Desktop machine.
    """
    monkeypatch.setenv(MODE_ENV, "local")
    assert MODE_ENV not in _make_run_env({})


def test_inherited_env_value_cannot_override_the_live_mode(monkeypatch):
    """A remote Desktop session stays remote no matter what the shell says."""
    monkeypatch.setenv(MODE_ENV, "local")
    set_desktop_connection_mode("remote")
    assert _make_run_env({})[MODE_ENV] == "remote"


def test_stale_value_from_a_previous_turn_does_not_survive(monkeypatch):
    """Each spawn re-derives; a cleared session strips rather than lingers."""
    tokens = set_session_vars(source="desktop", desktop_connection_mode="local")
    assert _make_run_env({})[MODE_ENV] == "local"
    monkeypatch.setenv(MODE_ENV, "local")  # simulate a leaked process-global
    clear_session_vars(tokens)
    assert MODE_ENV not in _make_run_env({})


def test_set_session_vars_carries_the_mode_through_to_the_child():
    tokens = set_session_vars(source="desktop", desktop_connection_mode="remote")
    try:
        assert _make_run_env({})[MODE_ENV] == "remote"
    finally:
        clear_session_vars(tokens)


def test_no_connection_details_are_ever_stamped():
    """Only the mode crosses the boundary — never a URL, host, or token."""
    set_desktop_connection_mode("remote")
    env = _make_run_env({})
    leaked = [
        key
        for key in env
        if key.startswith("HERMES_DESKTOP") and key != MODE_ENV
    ]
    assert leaked == []
    assert env[MODE_ENV] in {"local", "remote"}

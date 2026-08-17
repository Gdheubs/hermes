"""CLI session-source ContextVar binding regressions."""

from types import SimpleNamespace


def test_bind_cli_session_source_sets_task_local_surface(monkeypatch):
    from gateway.session_context import (
        clear_session_vars,
        get_session_var,
        reset_session_vars,
    )
    from hermes_cli.main import _bind_cli_session_source

    reset_session_vars()
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "stale-environment-value")
    tokens = _bind_cli_session_source(SimpleNamespace(source="cli"))
    try:
        assert get_session_var("HERMES_SESSION_SOURCE") == "cli"
    finally:
        clear_session_vars(tokens)

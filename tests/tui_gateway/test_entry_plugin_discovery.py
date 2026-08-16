"""Regression: the TUI gateway entry point must discover lifecycle plugins.

The TUI gateway previously only discovered plugins lazily (for unresolved
toolsets), which left fail-closed lifecycle hooks — e.g. intent-router's
``pre_llm_call`` routing gate (``enforce_before_first_llm``) — unregistered, so
the parent model ran unconstrained on coding tasks instead of being gated to a
single tool-free SOL review. ``entry._discover_plugins`` must invoke
``hermes_cli.plugins.discover_plugins`` so those hooks are present before the
first agent turn.
"""

from tui_gateway import entry


def test_discover_plugins_invokes_plugin_discovery(monkeypatch):
    calls = []

    monkeypatch.setattr(
        "hermes_cli.plugins.discover_plugins",
        lambda *a, **k: calls.append(True),
    )

    entry._discover_plugins()

    assert calls


def test_discover_plugins_failure_is_non_fatal(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("plugin discovery exploded")

    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", _boom)

    # Must not raise — a discovery failure should not take down the TUI.
    entry._discover_plugins()

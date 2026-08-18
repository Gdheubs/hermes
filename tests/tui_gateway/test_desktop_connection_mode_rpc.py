"""The TUI gateway's Desktop connection-mode plumbing (#82140).

The Desktop shell already resolves ``local``/``remote`` via
``window.hermesDesktop.getConnection()``. These tests pin the server side of
that announcement: where it is stored, when it is refreshed, and which sessions
are allowed to have one at all.

The helpers under test are pure dict/param transforms, so they run without
standing up a gateway.
"""

import threading

import pytest

from gateway.session_context import _DESKTOP_CONNECTION_MODE, _UNSET, _VAR_MAP


def _srv():
    import tui_gateway.server as srv

    return srv


@pytest.fixture(autouse=True)
def _reset_contextvars():
    yield
    for var in _VAR_MAP.values():
        var.set(_UNSET)
    _DESKTOP_CONNECTION_MODE.set(_UNSET)


def _desktop_session(**extra) -> dict:
    return {"session_key": "k", "source": "desktop", **extra}


class TestNormalizeParam:
    def test_reads_and_normalizes_the_param(self):
        assert _srv()._normalize_connection_mode_param({"connection_mode": "cloud"}) == "remote"

    @pytest.mark.parametrize("params", [None, {}, {"connection_mode": ""}, {"connection_mode": "nope"}])
    def test_missing_or_unknown_is_none(self, params):
        assert _srv()._normalize_connection_mode_param(params) is None


class TestSessionConnectionMode:
    def test_desktop_session_reports_its_mode(self):
        session = _desktop_session(connection_mode="remote")
        assert _srv()._session_connection_mode(session) == "remote"

    @pytest.mark.parametrize("source", ["tui", "telegram", "cli", "kanban"])
    def test_non_desktop_sources_never_report_a_mode(self, source):
        """A stray connection_mode from a non-Desktop client must not be honored."""
        session = {"session_key": "k", "source": source, "connection_mode": "local"}
        assert _srv()._session_connection_mode(session) is None

    def test_missing_session_is_none(self):
        assert _srv()._session_connection_mode(None) is None

    def test_desktop_session_without_an_announcement_is_none(self):
        assert _srv()._session_connection_mode(_desktop_session()) is None


class TestRememberConnectionMode:
    def test_refreshes_the_stored_mode(self):
        """This is what makes a mid-session connection switch land."""
        session = _desktop_session(connection_mode="local")
        _srv()._remember_connection_mode(session, {"connection_mode": "remote"})
        assert session["connection_mode"] == "remote"

    def test_omitted_param_leaves_the_stored_mode_alone(self):
        """An older Desktop build must not erase a mode a newer one announced."""
        session = _desktop_session(connection_mode="remote")
        _srv()._remember_connection_mode(session, {"text": "hello"})
        assert session["connection_mode"] == "remote"

    def test_explicit_unknown_value_clears_to_none(self):
        """Explicitly unknown is 'I don't know', not 'keep believing local'."""
        session = _desktop_session(connection_mode="local")
        _srv()._remember_connection_mode(session, {"connection_mode": "banana"})
        assert session["connection_mode"] is None

    def test_no_session_is_a_noop(self):
        _srv()._remember_connection_mode(None, {"connection_mode": "remote"})


class TestBindSessionContext:
    """``_set_session_context`` is what every turn runs through."""

    def test_binds_a_desktop_session_mode(self, monkeypatch):
        from gateway.session_context import desktop_connection_mode

        srv = _srv()
        session = _desktop_session(connection_mode="remote")
        monkeypatch.setattr(srv, "_sessions", {"s1": session}, raising=False)
        srv._set_session_context("k")
        assert desktop_connection_mode() == "remote"

    def test_non_desktop_session_binds_none(self, monkeypatch):
        from gateway.session_context import desktop_connection_mode

        srv = _srv()
        session = {"session_key": "k", "source": "tui", "connection_mode": "local"}
        monkeypatch.setattr(srv, "_sessions", {"s1": session}, raising=False)
        srv._set_session_context("k")
        assert desktop_connection_mode() is None

    def test_unknown_session_key_binds_none(self, monkeypatch):
        from gateway.session_context import desktop_connection_mode

        srv = _srv()
        monkeypatch.setattr(srv, "_sessions", {}, raising=False)
        srv._set_session_context("no-such-key")
        assert desktop_connection_mode() is None

    def test_explicit_mode_wins_for_ephemeral_ids(self, monkeypatch):
        """bg_*/preview_* task IDs aren't session keys; the caller-supplied
        parent mode must bind instead of the (empty) lookup result."""
        from gateway.session_context import desktop_connection_mode

        srv = _srv()
        monkeypatch.setattr(srv, "_sessions", {}, raising=False)
        srv._set_session_context("bg_abc123", connection_mode="remote")
        assert desktop_connection_mode() == "remote"

    def test_explicit_none_is_not_second_guessed(self, monkeypatch):
        """An explicit None ('parent has no Desktop mode') must not be
        overridden by a coincidental session-map hit."""
        from gateway.session_context import desktop_connection_mode

        srv = _srv()
        session = _desktop_session(connection_mode="remote")
        monkeypatch.setattr(srv, "_sessions", {"s1": session}, raising=False)
        srv._set_session_context("k", connection_mode=None)
        assert desktop_connection_mode() is None


def test_new_session_records_carry_a_connection_mode_slot():
    """Both live-session record shapes must have the field _set_session_context reads."""
    srv = _srv()
    record = srv._deferred_session_record(
        "key", cols=80, cwd="", history=[], lease=None, source="desktop",
        connection_mode="remote",
    )
    assert record["connection_mode"] == "remote"


def _host():
    import io

    from tui_gateway.compute_host import ComputeHost

    return ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)


def _live_session(**extra) -> dict:
    return {
        "session_key": "k",
        "source": "desktop",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "attached_images": [],
        "cols": 80,
        "cwd": "/w",
        **extra,
    }


class TestComputeHostBoundary:
    """Dashboard turn isolation must not erase the Desktop connection mode.

    The compute-host child rebuilds the session from the ``turn.start`` frame,
    so the frame must carry the parent's resolved mode and
    ``_ensure_server_session`` must apply it on create and refresh it on reuse
    — otherwise every isolated Desktop turn binds ``None`` and skills/MCP lose
    the announcement (#82140).
    """

    def test_turn_frame_carries_the_resolved_mode(self):
        frame = _srv()._compute_host_turn_frame(
            "rid", "s1", _live_session(connection_mode="remote"), "hi"
        )
        assert frame["connection_mode"] == "remote"

    def test_turn_frame_for_non_desktop_session_carries_none(self):
        """A stray mode on a non-Desktop session must not cross the boundary."""
        frame = _srv()._compute_host_turn_frame(
            "rid", "s1", _live_session(source="tui", connection_mode="local"), "hi"
        )
        assert frame["connection_mode"] is None

    def test_child_new_session_receives_the_frame_mode(self, monkeypatch):
        """The create path hands the frame mode to _init_session."""
        srv = _srv()
        host = _host()
        received = {}

        def _fake_init_session(sid, key, agent, history, **kwargs):
            received.update(kwargs)
            srv._sessions[sid] = {
                "agent": agent,
                "session_key": key,
                "history": list(history),
                "history_lock": threading.Lock(),
                "source": srv._resolve_session_source(kwargs.get("source")),
                "connection_mode": kwargs.get("connection_mode"),
            }

        monkeypatch.setattr(srv, "_sessions", {}, raising=False)
        monkeypatch.setattr(srv, "_make_agent", lambda *a, **k: object())
        monkeypatch.setattr(srv, "_transfer_db_to_agent", lambda *a, **k: False)
        monkeypatch.setattr(srv, "_init_session", _fake_init_session)
        frame = _srv()._compute_host_turn_frame(
            "rid", "s1", _live_session(connection_mode="remote"), "hi"
        )
        session = host._ensure_server_session(srv, frame)
        assert received["connection_mode"] == "remote"
        assert session["connection_mode"] == "remote"

    def test_child_fallback_session_keeps_the_frame_mode(self, monkeypatch):
        """The minimal host-owned session (init machinery unavailable) too."""
        srv = _srv()
        host = _host()

        def _boom(*a, **k):
            raise RuntimeError("slash worker unavailable")

        monkeypatch.setattr(srv, "_sessions", {}, raising=False)
        monkeypatch.setattr(srv, "_make_agent", lambda *a, **k: object())
        monkeypatch.setattr(srv, "_transfer_db_to_agent", lambda *a, **k: False)
        monkeypatch.setattr(srv, "_init_session", _boom)
        frame = _srv()._compute_host_turn_frame(
            "rid", "s1", _live_session(connection_mode="remote"), "hi"
        )
        session = host._ensure_server_session(srv, frame)
        assert session["connection_mode"] == "remote"

    def test_child_reuse_refreshes_the_mode_and_binds_it(self, monkeypatch):
        """A remote turn, then a switch to local: the reused child session must
        refresh and the child's own turn context must observe the new mode."""
        from gateway.session_context import desktop_connection_mode

        srv = _srv()
        host = _host()
        child_session = _live_session(connection_mode="remote")
        monkeypatch.setattr(srv, "_sessions", {"s1": child_session}, raising=False)

        frame = srv._compute_host_turn_frame(
            "rid", "s1", _live_session(connection_mode="local"), "hi"
        )
        reused = host._ensure_server_session(srv, frame)
        assert reused is child_session
        assert reused["connection_mode"] == "local"

        # What _run_prompt_submit's context bind now sees in the child.
        srv._set_session_context("k")
        assert desktop_connection_mode() == "local"

    def test_child_reuse_with_an_older_parent_frame_keeps_the_mode(self, monkeypatch):
        """A frame without the key (older parent) must not erase the mode."""
        srv = _srv()
        host = _host()
        child_session = _live_session(connection_mode="remote")
        monkeypatch.setattr(srv, "_sessions", {"s1": child_session}, raising=False)
        host._ensure_server_session(srv, {"sid": "s1", "session_key": "k"})
        assert child_session["connection_mode"] == "remote"


class TestEphemeralAgentInheritance:
    """Background and preview agents must inherit the parent Desktop mode.

    prompt.background and preview.restart bind fresh ``bg_*`` / ``preview_*``
    task IDs that are not in ``_sessions``, so the lookup-based derivation in
    ``_set_session_context`` finds nothing; the handlers must hand the parent
    session's resolved mode across explicitly (#82187 follow-up review, item 1).
    Each probe reads all three surfaces INSIDE the detached agent thread: the
    Python accessor, the subprocess env stamp, and the MCP per-call ``_meta``.
    """

    def _capture_inside_detached_agent(self, monkeypatch, method_name, params, session):
        import queue

        import run_agent

        srv = _srv()
        captured: queue.Queue = queue.Queue()

        class _ProbeAgent:
            def __init__(self, **kwargs):
                pass

            def run_conversation(self, **kwargs):
                from gateway.session_context import (
                    DESKTOP_CONNECTION_MODE_ENV,
                    desktop_connection_mode,
                )
                from tools.environments.local import _make_run_env
                from tools.mcp_tool import _call_tool_meta

                captured.put(
                    {
                        "accessor": desktop_connection_mode(),
                        "env": _make_run_env({}).get(DESKTOP_CONNECTION_MODE_ENV),
                        "meta": _call_tool_meta(),
                    }
                )
                return {"final_response": "done"}

        monkeypatch.setattr(run_agent, "AIAgent", _ProbeAgent)
        monkeypatch.setattr(srv, "_sessions", {"s1": session}, raising=False)
        monkeypatch.setattr(
            srv, "_background_agent_kwargs", lambda agent, task_id: {}, raising=False
        )
        monkeypatch.setattr(
            srv, "_ephemeral_preview_agent_kwargs", lambda agent, task_id: {}, raising=False
        )
        monkeypatch.setattr(
            srv, "_preview_restart_callbacks", lambda parent, task_id: {}, raising=False
        )
        monkeypatch.setattr(srv, "_emit", lambda *a, **k: None, raising=False)
        resp = srv._methods[method_name]("rid", {"session_id": "s1", **params})
        assert resp.get("error") is None, resp
        return captured.get(timeout=15)

    def _parent(self, **extra) -> dict:
        return {
            "session_key": "k",
            "source": "desktop",
            "agent": object(),
            "history": [],
            "history_lock": threading.Lock(),
            "cwd": "",
            **extra,
        }

    def test_background_agent_sees_the_parent_mode(self, monkeypatch):
        from tools.mcp_tool import MCP_DESKTOP_CONNECTION_MODE_META_KEY

        seen = self._capture_inside_detached_agent(
            monkeypatch,
            "prompt.background",
            {"text": "hi"},
            self._parent(connection_mode="remote"),
        )
        assert seen["accessor"] == "remote"
        assert seen["env"] == "remote"
        assert seen["meta"] == {MCP_DESKTOP_CONNECTION_MODE_META_KEY: "remote"}

    def test_preview_agent_sees_the_parent_mode(self, monkeypatch):
        from tools.mcp_tool import MCP_DESKTOP_CONNECTION_MODE_META_KEY

        seen = self._capture_inside_detached_agent(
            monkeypatch,
            "preview.restart",
            {"url": "http://localhost:3000"},
            self._parent(connection_mode="remote"),
        )
        assert seen["accessor"] == "remote"
        assert seen["env"] == "remote"
        assert seen["meta"] == {MCP_DESKTOP_CONNECTION_MODE_META_KEY: "remote"}

    def test_non_desktop_parent_spawns_modeless_children(self, monkeypatch):
        """A TUI parent's stray connection_mode must not leak into children."""
        seen = self._capture_inside_detached_agent(
            monkeypatch,
            "prompt.background",
            {"text": "hi"},
            self._parent(source="tui", connection_mode="local"),
        )
        assert seen["accessor"] is None
        assert seen["meta"] is None

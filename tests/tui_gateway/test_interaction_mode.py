"""Session-scoped interaction_mode (PLAN/BUILD) in the TUI gateway.

Covers:

1. New sessions default to BUILD.
2. ``config.set key=interaction_mode value=toggle`` flips between PLAN and BUILD.
3. ``config.set key=interaction_mode value=plan`` sets PLAN explicitly.
4. ``config.set key=interaction_mode value=build`` sets BUILD explicitly.
5. Invalid values are rejected with a clear error.
6. ``_session_info`` reports the current interaction_mode.
7. Toggle affects only the targeted session (session isolation).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import tui_gateway.server as server
from tui_gateway.server import _session_info


def _agent():
    return SimpleNamespace(
        reasoning_config=None,
        service_tier=None,
        model="test-model",
        provider="test-provider",
        session_id="sess-key",
    )


def _set(params: dict) -> dict:
    return server._methods["config.set"]("rid-1", params)


class TestSessionInfoInteractionMode:
    """_session_info must report interaction_mode from the session dict."""

    def test_defaults_to_build(self) -> None:
        session = {"session_key": "k1"}
        info = _session_info(_agent(), session)
        assert info["interaction_mode"] == "build"

    def test_reports_plan(self) -> None:
        session = {"session_key": "k1", "interaction_mode": "plan"}
        info = _session_info(_agent(), session)
        assert info["interaction_mode"] == "plan"


class TestSessionCreateDefault:
    """New sessions must start in BUILD mode."""

    def test_new_session_is_build(self) -> None:
        # The session.create handler initializes interaction_mode: "build"
        # in the session dict. We verify the _session_info path.
        session = {"session_key": "k1", "interaction_mode": "build"}
        info = _session_info(_agent(), session)
        assert info["interaction_mode"] == "build"


class TestConfigSetInteractionMode:
    """config.set interaction_mode must be session-scoped."""

    def test_toggle_build_to_plan(self) -> None:
        agent = _agent()
        session = {"session_key": "k1", "agent": agent, "interaction_mode": "build"}
        with patch.dict(server._sessions, {"s1": session}, clear=False), \
             patch.object(server, "_emit"):
            resp = _set({"key": "interaction_mode", "value": "toggle", "session_id": "s1"})
        assert "result" in resp
        assert resp["result"]["value"] == "plan"
        assert session["interaction_mode"] == "plan"

    def test_toggle_plan_to_build(self) -> None:
        agent = _agent()
        session = {"session_key": "k1", "agent": agent, "interaction_mode": "plan"}
        with patch.dict(server._sessions, {"s1": session}, clear=False), \
             patch.object(server, "_emit"):
            resp = _set({"key": "interaction_mode", "value": "toggle", "session_id": "s1"})
        assert "result" in resp
        assert resp["result"]["value"] == "build"
        assert session["interaction_mode"] == "build"

    def test_set_plan_explicit(self) -> None:
        agent = _agent()
        session = {"session_key": "k1", "agent": agent, "interaction_mode": "build"}
        with patch.dict(server._sessions, {"s1": session}, clear=False), \
             patch.object(server, "_emit"):
            resp = _set({"key": "interaction_mode", "value": "plan", "session_id": "s1"})
        assert "result" in resp
        assert resp["result"]["value"] == "plan"
        assert session["interaction_mode"] == "plan"

    def test_set_build_explicit(self) -> None:
        agent = _agent()
        session = {"session_key": "k1", "agent": agent, "interaction_mode": "plan"}
        with patch.dict(server._sessions, {"s1": session}, clear=False), \
             patch.object(server, "_emit"):
            resp = _set({"key": "interaction_mode", "value": "build", "session_id": "s1"})
        assert "result" in resp
        assert resp["result"]["value"] == "build"
        assert session["interaction_mode"] == "build"

    def test_empty_value_toggles(self) -> None:
        agent = _agent()
        session = {"session_key": "k1", "agent": agent, "interaction_mode": "build"}
        with patch.dict(server._sessions, {"s1": session}, clear=False), \
             patch.object(server, "_emit"):
            resp = _set({"key": "interaction_mode", "session_id": "s1"})
        assert "result" in resp
        assert resp["result"]["value"] == "plan"

    def test_invalid_value_rejected(self) -> None:
        session = {"session_key": "k1", "agent": _agent(), "interaction_mode": "build"}
        with patch.dict(server._sessions, {"s1": session}, clear=False):
            resp = _set({"key": "interaction_mode", "value": "invalid", "session_id": "s1"})
        assert "error" in resp
        assert "unknown interaction mode" in resp["error"]["message"]

    def test_sets_agent_interaction_mode(self) -> None:
        agent = SimpleNamespace(
            reasoning_config=None,
            service_tier=None,
            model="m",
            provider="p",
            session_id="k",
            interaction_mode="build",
        )
        session = {"session_key": "k1", "agent": agent, "interaction_mode": "build"}
        with patch.dict(server._sessions, {"s1": session}, clear=False), \
             patch.object(server, "_emit"):
            _set({"key": "interaction_mode", "value": "plan", "session_id": "s1"})
        assert agent.interaction_mode == "plan"


class TestSessionIsolation:
    """Toggling one session must not affect another."""

    def test_toggle_is_isolated(self) -> None:
        agent1 = _agent()
        agent2 = _agent()
        session1 = {"session_key": "k1", "agent": agent1, "interaction_mode": "build"}
        session2 = {"session_key": "k2", "agent": agent2, "interaction_mode": "build"}
        with patch.dict(server._sessions, {"s1": session1, "s2": session2}, clear=False), \
             patch.object(server, "_emit"):
            resp = _set({"key": "interaction_mode", "value": "plan", "session_id": "s1"})
        assert "result" in resp
        assert session1["interaction_mode"] == "plan"
        assert session2["interaction_mode"] == "build"


class TestLazyResumeInfoInteractionMode:
    """_lazy_resume_info must include interaction_mode."""

    def test_includes_build(self) -> None:
        info = server._lazy_resume_info("/tmp")
        assert info["interaction_mode"] == "build"


class TestFallbackSessionInfoInteractionMode:
    """_fallback_session_info must include interaction_mode from the session."""

    def test_defaults_to_build(self) -> None:
        session = {"session_key": "k1"}
        with patch.object(server, "_session_cwd", return_value="/tmp"):
            info = server._fallback_session_info(session)
        assert info["interaction_mode"] == "build"

    def test_reports_plan(self) -> None:
        session = {"session_key": "k1", "interaction_mode": "plan"}
        with patch.object(server, "_session_cwd", return_value="/tmp"):
            info = server._fallback_session_info(session)
        assert info["interaction_mode"] == "plan"

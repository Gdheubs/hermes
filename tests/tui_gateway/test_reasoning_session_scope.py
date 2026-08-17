"""Reasoning-effort session scoping in the TUI gateway (desktop backend).

Covers the "desktop reverts thinking to medium after one turn" report:

1. ``_session_info`` must report ``reasoning_effort: "none"`` when reasoning
   is disabled — reporting ``""`` (indistinguishable from "unset") made the
   desktop adopt the empty value after the first turn, wiping its sticky
   "thinking off" pick so every later chat reverted to the default effort.

2. ``config.set key=reasoning`` with a live session must be session-scoped:
   it must NOT rewrite the global ``agent.reasoning_effort`` in config.yaml
   (the desktop model menu applies a per-model preset on every selection,
   which was silently clobbering the user's configured value), and it must
   land on ``create_reasoning_override`` so lazily-built sessions (agent not
   constructed until the first prompt) don't drop the change.

3. ``_load_reasoning_config`` must honor a YAML boolean False
   (``reasoning_effort: false`` / ``off`` / ``no``) as thinking-disabled.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import tui_gateway.server as server
from tui_gateway.server import _session_info


def _agent(reasoning_config):
    return SimpleNamespace(
        reasoning_config=reasoning_config,
        service_tier=None,
        model="glm-5",
        provider="zai",
        session_id="sess-key",
    )


class TestSessionInfoReasoningEffort:
    """Disabled reasoning must be reported as 'none', never ''."""

    def test_disabled_reports_none(self) -> None:
        info = _session_info(_agent({"enabled": False}))
        assert info["reasoning_effort"] == "none"

    def test_enabled_reports_effort(self) -> None:
        info = _session_info(_agent({"enabled": True, "effort": "high"}))
        assert info["reasoning_effort"] == "high"

    def test_unset_reports_empty(self) -> None:
        info = _session_info(_agent(None))
        assert info["reasoning_effort"] == ""


class TestConfigSetReasoningSessionScope:
    """Session-targeted reasoning changes must not touch global config."""

    def _dispatch(self, params: dict) -> dict:
        handler = server._methods["config.set"]
        return handler("rid-1", params)

    def test_session_scoped_set_skips_global_write(self) -> None:
        agent = _agent(None)
        session = {"session_key": "k1", "agent": agent}
        with patch.dict(server._sessions, {"s1": session}, clear=False), \
                patch.object(server, "_write_config_key") as write_key, \
                patch.object(server, "_persist_live_session_runtime"), \
                patch.object(server, "_emit"):
            resp = self._dispatch(
                {"key": "reasoning", "session_id": "s1", "value": "none"}
            )
        assert resp["result"]["value"] == "none"
        assert agent.reasoning_config == {"enabled": False}
        write_key.assert_not_called()


    def test_live_change_refreshes_the_primary_runtime_snapshot(self) -> None:
        """The desktop reuses the running agent, and ``_primary_runtime`` is
        otherwise only written at agent init / model switch. Without this, a
        fallback later in the session restores the effort the session STARTED
        at and the prompt then advertises a tier nobody selected."""
        agent = _agent({"enabled": True, "effort": "medium"})
        agent._fallback_activated = False
        agent._primary_runtime = {
            "model": "glm-5",
            "provider": "zai",
            "reasoning_config": {"enabled": True, "effort": "medium"},
        }
        session = {"session_key": "k1", "agent": agent}
        with patch.dict(server._sessions, {"s1": session}, clear=False), \
                patch.object(server, "_write_config_key"), \
                patch.object(server, "_persist_live_session_runtime"), \
                patch.object(server, "_emit"):
            resp = self._dispatch(
                {"key": "reasoning", "session_id": "s1", "value": "ultra"}
            )

        assert resp["result"]["value"] == "ultra"
        assert agent.reasoning_config == {"enabled": True, "effort": "ultra"}
        assert agent._primary_runtime["reasoning_config"] == {
            "enabled": True, "effort": "ultra",
        }

    def test_live_change_during_fallback_preserves_the_saved_primary(self) -> None:
        """While a fallback answers, ``agent.reasoning_config`` describes the
        FALLBACK — copying it into the snapshot would destroy the only record
        of what the primary must come back to."""
        agent = _agent({"enabled": True, "effort": "medium"})
        agent._fallback_activated = True
        agent._primary_runtime = {
            "model": "glm-5",
            "provider": "zai",
            "reasoning_config": {"enabled": True, "effort": "medium"},
        }
        session = {"session_key": "k1", "agent": agent}
        with patch.dict(server._sessions, {"s1": session}, clear=False), \
                patch.object(server, "_write_config_key"), \
                patch.object(server, "_persist_live_session_runtime"), \
                patch.object(server, "_emit"):
            self._dispatch({"key": "reasoning", "session_id": "s1", "value": "ultra"})

        assert agent.reasoning_config == {"enabled": True, "effort": "ultra"}
        assert agent._primary_runtime["reasoning_config"] == {
            "enabled": True, "effort": "medium",
        }

    def test_live_change_leaves_the_cached_system_prompt_untouched(self) -> None:
        """🔴 CACHE INVARIANT: the system prompt is byte-stable for a
        conversation and only compaction may rebuild it. A mid-session
        reasoning change updates live + persisted runtime state and
        ``session.info``; the agent-visible correction rides that turn's
        message sidecar (``build_runtime_reasoning_note``) instead."""
        agent = _agent({"enabled": True, "effort": "medium"})
        prompt = (
            "You are Hermes Agent.\n\n"
            "Conversation started: Tuesday, June 16, 2026\n"
            "Model: glm-5\nProvider: zai\n"
            "Reasoning effort: medium"
        )
        agent._cached_system_prompt = prompt
        session = {"session_key": "k1", "agent": agent}
        with patch.dict(server._sessions, {"s1": session}, clear=False), \
                patch.object(server, "_write_config_key"), \
                patch.object(server, "_persist_live_session_runtime") as persist, \
                patch.object(server, "_emit") as emit:
            resp = self._dispatch(
                {"key": "reasoning", "session_id": "s1", "value": "ultra"}
            )

        assert resp["result"]["value"] == "ultra"
        assert agent.reasoning_config == {"enabled": True, "effort": "ultra"}
        assert agent._cached_system_prompt == prompt
        persist.assert_called()
        assert any(call.args[0] == "session.info" for call in emit.call_args_list)
        assert _session_info(agent)["reasoning_effort"] == "ultra"

    def test_no_session_persists_globally(self) -> None:
        with patch.object(server, "_write_config_key") as write_key:
            resp = self._dispatch({"key": "reasoning", "value": "low"})
        assert resp["result"]["value"] == "low"
        write_key.assert_called_once_with("agent.reasoning_effort", "low")

    def test_unknown_value_rejected(self) -> None:
        resp = self._dispatch({"key": "reasoning", "value": "bogus"})
        assert "error" in resp


class TestLoadReasoningConfigYamlBoolean:
    """YAML `reasoning_effort: false` means disabled, not default."""

    def test_boolean_false_disables(self) -> None:
        with patch.object(
            server, "_load_cfg", return_value={"agent": {"reasoning_effort": False}}
        ):
            assert server._load_reasoning_config() == {"enabled": False}

    def test_string_false_disables(self) -> None:
        with patch.object(
            server, "_load_cfg", return_value={"agent": {"reasoning_effort": "false"}}
        ):
            assert server._load_reasoning_config() == {"enabled": False}


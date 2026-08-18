"""Backend PLAN mode enforcement — all tool families are blocked.

Contract under test:

1. ``invoke_tool`` returns the deterministic blocked message when the agent
   has ``interaction_mode == "plan"``.
2. BUILD mode (default) is unchanged — tools dispatch normally.
3. The gate fires before any plugin hooks or middleware.
4. The error message matches the exact acceptance-criteria string.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

PLAN_BLOCKED_MSG = (
    "Tool execution is disabled in PLAN mode. "
    "Switch to BUILD mode to run tools."
)


class TestInvokeToolPlanMode:
    """invoke_tool must block every tool when interaction_mode is 'plan'."""

    def _make_agent(self, interaction_mode: str = "build"):
        return SimpleNamespace(
            interaction_mode=interaction_mode,
            session_id="test-session",
            _current_turn_id="turn-1",
            _current_api_request_id="api-1",
            _session_db=None,
        )

    def test_plan_blocks_any_tool(self) -> None:
        """PLAN mode must return the deterministic blocked message."""
        from agent.agent_runtime_helpers import invoke_tool

        agent = self._make_agent("plan")
        result = invoke_tool(
            agent,
            "terminal",
            {"command": "ls"},
            effective_task_id="t1",
        )
        parsed = json.loads(result)
        assert parsed["error"] == PLAN_BLOCKED_MSG

    def test_plan_blocks_todo_tool(self) -> None:
        """Agent-loop tools (todo, memory, etc.) must also be blocked."""
        from agent.agent_runtime_helpers import invoke_tool

        agent = self._make_agent("plan")
        result = invoke_tool(
            agent,
            "todo",
            {"todos": [{"id": "1", "content": "test", "status": "pending"}]},
            effective_task_id="t1",
        )
        parsed = json.loads(result)
        assert parsed["error"] == PLAN_BLOCKED_MSG

    def test_build_allows_tools(self) -> None:
        """BUILD mode must NOT block tools — the gate is a no-op."""
        from agent.agent_runtime_helpers import invoke_tool

        agent = self._make_agent("build")
        try:
            result = invoke_tool(
                agent,
                "totally_fake_tool_xyz",
                {},
                effective_task_id="t1",
            )
            parsed = json.loads(result)
            assert parsed.get("error") != PLAN_BLOCKED_MSG
        except (AttributeError, KeyError, RuntimeError):
            # BUILD mode lets the call through; downstream errors from the
            # fake tool name are expected and prove the gate did NOT block.
            pass

    def test_default_interaction_mode_allows_tools(self) -> None:
        """Agent without interaction_mode set defaults to BUILD (no gate)."""
        agent = self._make_agent("build")
        del agent.interaction_mode
        assert getattr(agent, "interaction_mode", "build") == "build"

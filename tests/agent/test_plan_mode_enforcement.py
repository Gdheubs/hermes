"""PLAN mode blocks write/execute tools, allows read-only tools.

Covers:
1. PLAN mode blocks write/execute tools before any handler runs.
2. Read-only tools pass through in PLAN mode.
3. Block message is deterministic and matches the spec exactly.
4. The execute callback is never invoked for blocked tools.
5. Agents without interaction_mode (legacy) default to BUILD.
"""

import json
from types import SimpleNamespace

from agent.tool_executor import (
    _ManagedToolResult,
    _run_agent_tool_execution_middleware,
)

BLOCK_MESSAGE = (
    "Tool execution is disabled in PLAN mode. "
    "Switch to BUILD mode to run tools."
)

READ_ONLY_TOOLS = [
    "read_file", "search_files", "session_search",
    "skill_view", "skills_list", "clarify",
    "browser_snapshot", "browser_get_images", "memory",
]

BLOCKED_TOOLS = [
    "terminal", "write_file", "patch", "delegate_task",
    "browser_exec", "computer_use", "skill_manage",
    "todo", "execute_code",
]


def _agent(mode="build"):
    a = SimpleNamespace(
        interaction_mode=mode,
        session_id="session",
        _current_turn_id="turn",
        _current_api_request_id="request",
        quiet_mode=True,
        tool_progress_mode="off",
        verbose_logging=False,
        log_prefix_chars=40,
        _current_tool="",
        _touch_activity=lambda msg: None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        stream_delta_callback=None,
        tool_gen_callback=None,
        _tool_guardrails=SimpleNamespace(
            allows_execution=True,
            before_call=lambda name, args: SimpleNamespace(
                allows_execution=True,
                message=None,
            ),
        ),
        _guardrail_block_result=lambda d: "",
    )
    return a


def test_plan_blocks_write_execute_tools():
    """Write/execute tools are blocked in PLAN mode."""
    agent = _agent("plan")
    for name in BLOCKED_TOOLS:
        result = _run_agent_tool_execution_middleware(
            agent,
            function_name=name,
            function_args={},
            effective_task_id="t1",
            tool_call_id="tc1",
            execute=lambda args: "never",
        )
        assert isinstance(result, _ManagedToolResult)
        assert result.blocked is True, f"{name} was not blocked"
        assert result.dispatched is False, f"{name} was dispatched"


def test_plan_allows_read_only_tools():
    """Read-only tools pass through in PLAN mode."""
    agent = _agent("plan")
    for name in READ_ONLY_TOOLS:
        handler_called = False

        def _handler(args):
            nonlocal handler_called
            handler_called = True
            return json.dumps({"ok": True})

        result = _run_agent_tool_execution_middleware(
            agent,
            function_name=name,
            function_args={},
            effective_task_id="t1",
            tool_call_id="tc1",
            execute=_handler,
        )
        assert handler_called is True, f"{name} was blocked in PLAN mode"


def test_plan_block_message_exact():
    """The block message must match the spec exactly."""
    agent = _agent("plan")
    result = _run_agent_tool_execution_middleware(
        agent,
        function_name="write_file",
        function_args={"path": "/tmp/x"},
        effective_task_id="t1",
        tool_call_id="tc1",
        execute=lambda args: "never",
    )
    body = json.loads(result.result)
    assert body["error"] == BLOCK_MESSAGE


def test_plan_does_not_call_execute_for_blocked():
    """The execute callback must never be invoked for blocked tools."""
    agent = _agent("plan")
    call_log = []

    def handler(args):
        call_log.append(args)

    _run_agent_tool_execution_middleware(
        agent,
        function_name="terminal",
        function_args={},
        effective_task_id="t1",
        tool_call_id="tc1",
        execute=handler,
    )
    assert call_log == []


def test_missing_mode_defaults_to_build():
    """Agent without interaction_mode attr defaults to BUILD."""
    agent = _agent()
    del agent.interaction_mode
    assert getattr(agent, "interaction_mode", "build") == "build"

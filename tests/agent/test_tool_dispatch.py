"""Tests for agent-owned tool dispatch extraction."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import tool_dispatch
from agent.tool_dispatch import (
    AGENT_RUNTIME_POST_HOOK_TOOL_NAMES,
    agent_runtime_owns_post_tool_hook,
    dispatch_delegate_task,
    invoke_tool,
)
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


@pytest.fixture()
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        return a


def test_canonical_module_owns_invoke_tool():
    assert invoke_tool is tool_dispatch.invoke_tool
    assert invoke_tool.__module__ == "agent.tool_dispatch"


def test_agent_runtime_helpers_reexports_invoke_tool():
    from agent.agent_runtime_helpers import invoke_tool as legacy_invoke_tool

    assert legacy_invoke_tool is invoke_tool


def test_agent_runtime_helpers_reexports_post_hook_symbols():
    from agent.agent_runtime_helpers import (
        AGENT_RUNTIME_POST_HOOK_TOOL_NAMES as legacy_names,
    )
    from agent.agent_runtime_helpers import (
        agent_runtime_owns_post_tool_hook as legacy_predicate,
    )

    assert legacy_names is AGENT_RUNTIME_POST_HOOK_TOOL_NAMES
    assert legacy_predicate is agent_runtime_owns_post_tool_hook


def test_agent_invoke_tool_forwarder_reaches_extracted_impl(agent):
    with patch("agent.tool_dispatch.invoke_tool", return_value="ok") as mock_invoke:
        result = agent._invoke_tool("web_search", {"q": "test"}, "task-1")

    mock_invoke.assert_called_once_with(
        agent,
        "web_search",
        {"q": "test"},
        "task-1",
        None,
        None,
        False,
        False,
        None,
        False,
    )
    assert result == "ok"


def test_agent_dispatch_delegate_task_forwarder_reaches_extracted_impl():
    import run_agent

    parent = SimpleNamespace(_delegate_depth=0)
    captured = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return "{}"

    with patch("tools.delegate_tool.delegate_task", fake_delegate_task):
        run_agent.AIAgent._dispatch_delegate_task(
            parent,
            {
                "goal": "test",
                "context": "ctx",
                "tasks": None,
                "max_iterations": 10,
                "role": "leaf",
                "action": None,
                "subagent_id": None,
                "message": None,
            },
        )

    assert captured["goal"] == "test"
    assert captured["background"] is True
    assert captured["parent_agent"] is parent


def test_dispatch_delegate_task_background_false_for_subagent():
    parent = SimpleNamespace(_delegate_depth=1)
    captured = {}

    with patch("tools.delegate_tool.delegate_task", lambda **kwargs: captured.update(kwargs) or "{}"):
        dispatch_delegate_task(parent, {"goal": "nested"})

    assert captured["background"] is False


def test_invoke_tool_honors_run_agent_handle_function_call_patch_seam(agent):
    with patch("run_agent.handle_function_call", return_value="registry-result") as mock_hfc:
        result = invoke_tool(agent, "web_search", {"q": "test"}, "task-1")

    mock_hfc.assert_called_once()
    assert result == "registry-result"


def test_post_hook_ownership_contract_uses_canonical_module(agent):
    for tool_name in (
        "todo",
        "session_search",
        "memory",
        "clarify",
        "delegate_task",
        "read_terminal",
        "read_preview",
        "read_window_below",
        "setup_mcp",
    ):
        assert agent_runtime_owns_post_tool_hook(agent, tool_name) is True

    agent._context_engine_tool_names = {"context_query"}
    assert agent_runtime_owns_post_tool_hook(agent, "context_query") is True

    agent._memory_manager = SimpleNamespace(has_tool=lambda name: name == "memory_extra")
    assert agent_runtime_owns_post_tool_hook(agent, "memory_extra") is True
    assert agent_runtime_owns_post_tool_hook(agent, "web_search") is False


def test_post_hook_tool_names_reexported_from_legacy_import_path():
    from agent.agent_runtime_helpers import AGENT_RUNTIME_POST_HOOK_TOOL_NAMES as legacy

    assert legacy is AGENT_RUNTIME_POST_HOOK_TOOL_NAMES

"""Agent-owned single-tool routing and dispatch policy."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _ra():
    """Lazy ``run_agent`` reference for test-patch routing."""
    import run_agent
    return run_agent


AGENT_RUNTIME_POST_HOOK_TOOL_NAMES = frozenset(
    {"todo", "session_search", "memory", "clarify", "read_terminal", "read_preview", "read_window_below", "setup_mcp", "delegate_task"}
)


def agent_runtime_owns_post_tool_hook(agent: Any, function_name: str) -> bool:
    """Return True when an agent-level tool path emits its own post hook."""
    if function_name in AGENT_RUNTIME_POST_HOOK_TOOL_NAMES:
        return True
    if getattr(agent, "_context_engine_tool_names", None) and function_name in agent._context_engine_tool_names:
        return True
    memory_manager = getattr(agent, "_memory_manager", None)
    return bool(memory_manager and memory_manager.has_tool(function_name))


def dispatch_delegate_task(agent, function_args: dict) -> str:
    """Single call site for delegate_task dispatch.

    New DELEGATE_TASK_SCHEMA fields only need to be added here to reach all
    invocation paths (concurrent, sequential, inline).
    """
    from tools.delegate_tool import (
        _strip_model_hidden_task_fields,
        delegate_task as _delegate_task,
    )
    # Delegations from the top-level MODEL always run in the background —
    # the model does not get to choose. delegate_task returns immediately
    # with a handle (one per task) and each subagent's result re-enters the
    # conversation as a new message when it finishes. This applies to BOTH
    # a single task and a fan-out batch (each task becomes its own
    # independent background subagent). The one exception:
    #   - A delegation from an ORCHESTRATOR SUBAGENT (depth > 0) stays
    #     synchronous: the orchestrator needs its workers' results within
    #     its own turn to compose a summary, and a subagent doesn't own the
    #     gateway session the async result would route back to.
    # The schema-level `background` param is intentionally ignored here.
    _is_subagent = getattr(agent, "_delegate_depth", 0) > 0
    return _delegate_task(
        goal=function_args.get("goal"),
        context=function_args.get("context"),
        tasks=_strip_model_hidden_task_fields(function_args.get("tasks")),
        max_iterations=function_args.get("max_iterations"),
        role=function_args.get("role"),
        background=(not _is_subagent),
        action=function_args.get("action"),
        subagent_id=function_args.get("subagent_id"),
        message=function_args.get("message"),
        parent_agent=agent,
    )


def invoke_tool(agent, function_name: str, function_args: dict, effective_task_id: str,
                 tool_call_id: Optional[str] = None, messages: list = None,
                 pre_tool_block_checked: bool = False,
                 skip_tool_request_middleware: bool = False,
                 tool_request_middleware_trace: Optional[List[Dict[str, Any]]] = None,
                 skip_tool_execution_middleware: bool = False) -> str:
    """Invoke a single tool and return the result string. No display logic.

    Handles both agent-level tools (todo, memory, etc.) and registry-dispatched
    tools. Used by the concurrent execution path; the sequential path retains
    its own inline invocation for backward-compatible display handling.
    """
    if not isinstance(function_args, dict):
        function_args = {}

    _tool_middleware_trace = list(tool_request_middleware_trace or [])
    try:
        from hermes_cli.middleware import apply_tool_request_middleware

        if not skip_tool_request_middleware:
            _tool_request_mw = apply_tool_request_middleware(
                function_name,
                function_args,
                task_id=effective_task_id or "",
                session_id=getattr(agent, "session_id", "") or "",
                tool_call_id=tool_call_id or "",
                turn_id=getattr(agent, "_current_turn_id", "") or "",
                api_request_id=getattr(agent, "_current_api_request_id", "") or "",
            )
            function_args = _tool_request_mw.payload
            _tool_middleware_trace = _tool_request_mw.trace
    except Exception as _mw_err:
        logger.debug("tool_request middleware error: %s", _mw_err)

    # Check plugin hooks for a block or approval directive before executing.
    block_message: Optional[str] = None
    if not pre_tool_block_checked:
        try:
            from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
            block_message, modified_args = _dispatch_pre_tool_call_hooks(
                function_name, function_args, task_id=effective_task_id or "",
                session_id=getattr(agent, "session_id", "") or "",
                tool_call_id=tool_call_id or "",
                turn_id=getattr(agent, "_current_turn_id", "") or "",
                api_request_id=getattr(agent, "_current_api_request_id", "") or "",
                middleware_trace=list(_tool_middleware_trace),
            )
            if modified_args is not None:
                function_args = modified_args
        except Exception:
            block_message = None
    if block_message is not None:
        result = json.dumps({"error": block_message}, ensure_ascii=False)
        try:
            from model_tools import _emit_post_tool_call_hook
            _emit_post_tool_call_hook(
                function_name=function_name,
                function_args=function_args,
                result=result,
                task_id=effective_task_id or "",
                session_id=getattr(agent, "session_id", "") or "",
                tool_call_id=tool_call_id or "",
                turn_id=getattr(agent, "_current_turn_id", "") or "",
                api_request_id=getattr(agent, "_current_api_request_id", "") or "",
                status="blocked",
                error_type="plugin_block",
                error_message=block_message,
                middleware_trace=list(_tool_middleware_trace),
            )
        except Exception:
            pass
        return result

    tool_start_time = time.monotonic()

    def _finish_agent_tool(result: Any, observed_args: Optional[dict] = None) -> Any:
        hook_args = observed_args if isinstance(observed_args, dict) else function_args
        try:
            from model_tools import _emit_post_tool_call_hook
            _emit_post_tool_call_hook(
                function_name=function_name,
                function_args=hook_args,
                result=result,
                task_id=effective_task_id or "",
                session_id=getattr(agent, "session_id", "") or "",
                tool_call_id=tool_call_id or "",
                turn_id=getattr(agent, "_current_turn_id", "") or "",
                api_request_id=getattr(agent, "_current_api_request_id", "") or "",
                duration_ms=int((time.monotonic() - tool_start_time) * 1000),
                middleware_trace=list(_tool_middleware_trace),
            )
        except Exception:
            pass
        return result

    if function_name == "todo":
        def _execute(next_args: dict) -> Any:
            from tools.todo_tool import todo_tool as _todo_tool
            return _finish_agent_tool(
                _todo_tool(
                    todos=next_args.get("todos"),
                    merge=next_args.get("merge", False),
                    store=agent._todo_store,
                ),
                next_args,
            )
    elif function_name == "session_search":
        def _execute(next_args: dict) -> Any:
            session_db = agent._get_session_db_for_recall()
            if not session_db:
                from hermes_state import format_session_db_unavailable
                return _finish_agent_tool(json.dumps({"success": False, "error": format_session_db_unavailable()}), next_args)
            from tools.session_search_tool import session_search as _session_search
            return _finish_agent_tool(
                _session_search(
                    query=next_args.get("query", ""),
                    role_filter=next_args.get("role_filter"),
                    limit=next_args.get("limit", 3),
                    session_id=next_args.get("session_id"),
                    around_message_id=next_args.get("around_message_id"),
                    window=next_args.get("window", 5),
                    sort=next_args.get("sort"),
                    detail=next_args.get("detail", "adaptive"),
                    db=session_db,
                    current_session_id=agent.session_id,
                ),
                next_args,
            )
    elif function_name == "memory":
        def _execute(next_args: dict) -> Any:
            target = next_args.get("target", "memory")
            operations = next_args.get("operations")
            from tools.memory_tool import memory_tool as _memory_tool
            result = _memory_tool(
                action=next_args.get("action"),
                target=target,
                content=next_args.get("content"),
                old_text=next_args.get("old_text"),
                operations=operations,
                store=agent._memory_store,
            )
            # Mirror successful built-in memory writes to external providers.
            # All gating/op-expansion lives behind the manager interface
            # (MemoryManager.notify_memory_tool_write).
            if agent._memory_manager:
                agent._memory_manager.notify_memory_tool_write(
                    result,
                    next_args,
                    build_metadata=lambda: agent._build_memory_write_metadata(
                        task_id=effective_task_id,
                        tool_call_id=tool_call_id,
                    ),
                )
            return _finish_agent_tool(result, next_args)
    elif agent._memory_manager and agent._memory_manager.has_tool(function_name):
        def _execute(next_args: dict) -> Any:
            return _finish_agent_tool(agent._memory_manager.handle_tool_call(function_name, next_args), next_args)
    elif function_name == "clarify":
        def _execute(next_args: dict) -> Any:
            from tools.clarify_tool import clarify_tool as _clarify_tool
            return _finish_agent_tool(
                _clarify_tool(
                    question=next_args.get("question", ""),
                    choices=next_args.get("choices"),
                    multi_select=next_args.get("multi_select", False),
                    callback=agent.clarify_callback,
                ),
                next_args,
            )
    elif function_name == "read_terminal":
        def _execute(next_args: dict) -> Any:
            from tools.read_terminal_tool import read_terminal_tool as _read_terminal_tool
            return _finish_agent_tool(
                _read_terminal_tool(
                    start_line=next_args.get("start_line"),
                    count=next_args.get("count"),
                    callback=getattr(agent, "read_terminal_callback", None),
                ),
                next_args,
            )
    elif function_name == "read_preview":
        def _execute(next_args: dict) -> Any:
            from tools.read_preview_tool import read_preview_tool as _read_preview_tool
            return _finish_agent_tool(
                _read_preview_tool(
                    start=next_args.get("start"),
                    count=next_args.get("count"),
                    callback=getattr(agent, "read_preview_callback", None),
                ),
                next_args,
            )
    elif function_name == "read_window_below":
        def _execute(next_args: dict) -> Any:
            from tools.read_window_tool import read_window_below_tool as _read_window_below_tool
            return _finish_agent_tool(
                _read_window_below_tool(
                    callback=getattr(agent, "read_window_below_callback", None),
                ),
                next_args,
            )
    elif function_name == "setup_mcp":
        def _execute(next_args: dict) -> Any:
            from tools.setup_mcp_tool import setup_mcp_tool as _setup_mcp_tool
            return _finish_agent_tool(
                _setup_mcp_tool(
                    server=next_args.get("server", ""),
                    action=next_args.get("action", "install"),
                    reason=next_args.get("reason", ""),
                    callback=getattr(agent, "setup_mcp_callback", None),
                ),
                next_args,
            )
    elif function_name == "delegate_task":
        def _execute(next_args: dict) -> Any:
            return _finish_agent_tool(dispatch_delegate_task(agent, next_args), next_args)
    else:
        def _execute(next_args: dict) -> Any:
            dispatch_kwargs = dict(
                tool_call_id=tool_call_id,
                session_id=agent.session_id or "",
                turn_id=getattr(agent, "_current_turn_id", "") or "",
                api_request_id=getattr(agent, "_current_api_request_id", "") or "",
                enabled_tools=list(agent.valid_tool_names) if agent.valid_tool_names else None,
                skip_pre_tool_call_hook=True,
                skip_tool_request_middleware=True,
                enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                disabled_toolsets=getattr(agent, "disabled_toolsets", None),
                tool_request_middleware_trace=list(_tool_middleware_trace),
            )
            if skip_tool_execution_middleware:
                dispatch_kwargs["skip_tool_execution_middleware"] = True
            return _ra().handle_function_call(
                function_name,
                next_args,
                effective_task_id,
                **dispatch_kwargs,
            )

    if skip_tool_execution_middleware:
        return _execute(function_args)

    from hermes_cli.middleware import run_tool_execution_middleware

    return run_tool_execution_middleware(
        function_name,
        function_args,
        lambda next_args: _execute(next_args if isinstance(next_args, dict) else function_args),
        original_args=function_args,
        task_id=effective_task_id or "",
        session_id=getattr(agent, "session_id", "") or "",
        tool_call_id=tool_call_id or "",
        turn_id=getattr(agent, "_current_turn_id", "") or "",
        api_request_id=getattr(agent, "_current_api_request_id", "") or "",
    )

"""The async tool-batch dispatch: real threads, loop-time gate timeout,
model-order results, interrupt drain, and the critical result-append contract.
The middleware is mocked; this pins the async coordination + the append."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.tool_executor import _execute_tool_batch_async, _ManagedToolResult


def _agent():
    return SimpleNamespace(_interrupt_requested=False,
                           _tool_worker_threads_lock=__import__("threading").Lock(),
                           _tool_worker_threads=set())


def _call(name, delay=0.0):
    return dict(function_name=name, function_args={}, effective_task_id="t",
                tool_call_id=name, execute=lambda: None, _delay=delay)


def _managed_result(*_a, **kw):
    name = kw.get("function_name", "?")
    return _ManagedToolResult(result=f"res:{name}", args={}, middleware_trace=[],
                              blocked=False, dispatched=True)


def test_batch_runs_all_and_preserves_order():
    agent = _agent()
    agent._tool_result_content_for_active_model = lambda name, res: res
    calls = [_call("a"), _call("b"), _call("c")]
    messages = []
    with patch("agent.tool_executor._run_agent_tool_execution_middleware",
               side_effect=_managed_result), \
         patch("agent.tool_executor._flush_session_db_after_tool_progress",
               return_value=True):
        ordered = asyncio.run(_execute_tool_batch_async(
            agent, calls, timeout_s=5.0, _messages=messages))
    assert [c["function_name"] for c, *_ in ordered] == ["a", "b", "c"]
    assert all(out == "ok" for _, out, _ in ordered)
    # the critical regression: the managed results MUST land in the messages
    # in the model order (the middleware only returns them; the append is ours)
    assert [m.get("tool_call_id") for m in messages] == ["a", "b", "c"], messages


def test_wedged_call_bounded_by_loop_time_timeout():
    agent = _agent()
    calls = [_call("fast"), _call("wedged")]

    def _mw(*a, **kw):
        if kw["function_name"] == "wedged":
            time.sleep(30.0)
        return _managed_result(kw["function_name"])
    t = time.perf_counter()
    with patch("agent.tool_executor._run_agent_tool_execution_middleware", side_effect=_mw):
        ordered = asyncio.run(_execute_tool_batch_async(agent, calls, timeout_s=0.2))
    wall = time.perf_counter() - t
    statuses = [out for _, out, _ in ordered]
    assert statuses[0] == "ok" and statuses[1] == "timed_out"
    assert wall < 5.0, f"the gate timeout must bound the wedged call (wall={wall:.2f}s)"


def test_interrupt_drains_remaining_calls():
    agent = _agent()
    calls = [_call("a"), _call("b")]
    with patch("agent.tool_executor._run_agent_tool_execution_middleware", side_effect=_managed_result):
        async def _interrupt_during():
            agent._interrupt_requested = True
            return await _execute_tool_batch_async(agent, calls, timeout_s=5.0)
        ordered = asyncio.run(_interrupt_during())
    assert ordered and all(out in ("ok", "interrupted") for _, out, _ in ordered)


def test_mid_batch_interrupt_drains_pending():
    agent = _agent()
    calls = [_call("slow")]

    def _mw(*a, **kw):
        time.sleep(30.0)
        return _managed_result("slow")

    async def _interrupt_after():
        async def _set_soon():
            await asyncio.sleep(0.05)
            agent._interrupt_requested = True
        asyncio.get_running_loop().create_task(_set_soon())
        t = time.perf_counter()
        ordered = await _execute_tool_batch_async(agent, calls, timeout_s=30.0)
        return ordered, time.perf_counter() - t

    with patch("agent.tool_executor._run_agent_tool_execution_middleware", side_effect=_mw):
        ordered, wall = asyncio.run(_interrupt_after())
    assert wall < 5.0, f"the mid-batch interrupt must drain (wall={wall:.2f}s)"


def test_per_call_error_is_reported_not_raised():
    agent = _agent()
    calls = [_call("bad"), _call("good")]
    with patch("agent.tool_executor._run_agent_tool_execution_middleware",
               side_effect=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
               if kw["function_name"] == "bad" else _managed_result("good")):
        ordered = asyncio.run(_execute_tool_batch_async(agent, calls, timeout_s=5.0))
    by_name = {c["function_name"]: (out, res) for c, out, res in ordered}
    assert by_name["bad"][0] == "error"
    assert by_name["good"][0] == "ok"


def test_timeout_none_still_drains_on_interrupt():
    agent = _agent()
    calls = [_call("slow")]

    def _mw(*a, **kw):
        time.sleep(30.0)
        return _managed_result("slow")

    async def _run():
        async def _set_soon():
            await asyncio.sleep(0.05)
            agent._interrupt_requested = True
        asyncio.get_running_loop().create_task(_set_soon())
        return await _execute_tool_batch_async(agent, calls, timeout_s=None)

    with patch("agent.tool_executor._run_agent_tool_execution_middleware", side_effect=_mw):
        ordered = asyncio.run(_run())
    assert ordered and ordered[0][1] in ("interrupted", "timed_out")


def test_empty_batch_is_a_noop():
    assert asyncio.run(_execute_tool_batch_async(_agent(), [], timeout_s=1.0)) == []


def test_dispatcher_async_path_runs_parallel_segment(monkeypatch):
    """End-to-end wiring: with HERMES_TOOL_EXEC_ASYNC the dispatcher routes a
    parallel segment through the async batch — the middleware runs + the
    results land in the messages."""
    import os
    from run_agent import AIAgent

    monkeypatch.setenv("HERMES_TOOL_EXEC_ASYNC", "1")
    calls = [SimpleNamespace(function=SimpleNamespace(name=n, arguments="{}"), id=n)
             for n in ("mcp__srv__list_files", "mcp__srv__get_info")]

    agent = _agent()
    agent._invoke_tool = MagicMock(side_effect=lambda fn, args, tid: {"ok": fn})
    agent._tool_result_content_for_active_model = lambda name, res: res
    agent.log_prefix = ""
    agent.quiet_mode = True
    agent._current_tool = None
    agent._interrupt_requested = False
    from run_agent import AIAgent as _AIAgent
    agent._execute_tool_calls_async_segment = _AIAgent._execute_tool_calls_async_segment.__get__(agent)
    agent._execute_tool_calls_concurrent = lambda *a, **k: None
    agent._execute_tool_calls_sequential = lambda *a, **k: None

    msg = SimpleNamespace(tool_calls=calls)
    with patch("agent.tool_dispatch_helpers._is_mcp_tool_parallel_safe", return_value=True), \
         patch("run_agent.get_active_env", return_value=SimpleNamespace(cwd="/tmp")), \
         patch("agent.tool_executor._run_agent_tool_execution_middleware",
               side_effect=_managed_result) as _mw:
        AIAgent._execute_tool_calls(agent, msg, [], "task", 0)
    assert _mw.call_count == 2, "the async path must route the parallel segment through the middleware"


def test_interrupt_appends_cancelled_markers():
    """The sync's pre-flight contract: interrupted calls get the cancelled
    markers in the messages, not a silent skip."""
    agent = _agent()
    agent._tool_result_content_for_active_model = lambda name, res: res
    calls = [_call("a")]
    messages = []
    with patch("agent.tool_executor._run_agent_tool_execution_middleware",
               side_effect=_managed_result):
        async def _interrupt():
            agent._interrupt_requested = True
            return await _execute_tool_batch_async(
                agent, calls, timeout_s=5.0, _messages=messages)
        asyncio.run(_interrupt())
    joined = " ".join(str(m.get("content") or "") for m in messages)
    assert "cancelled" in joined, messages


def test_ok_results_flush_to_the_session_db():
    """Each appended result triggers the per-result persistence flush."""
    agent = _agent()
    agent._tool_result_content_for_active_model = lambda name, res: res
    calls = [_call("a"), _call("b")]
    messages = []
    with patch("agent.tool_executor._run_agent_tool_execution_middleware",
               side_effect=_managed_result), \
         patch("agent.tool_executor._flush_session_db_after_tool_progress",
               return_value=True) as _flush:
        asyncio.run(_execute_tool_batch_async(
            agent, calls, timeout_s=5.0, _messages=messages))
    assert _flush.call_count == 2, "one flush per ok result"


def test_progress_event_emitted_per_ok_result():
    """The sync's display surface: each ok result emits tool.completed."""
    agent = _agent()
    agent._tool_result_content_for_active_model = lambda name, res: res
    events = []
    agent.tool_progress_callback = lambda *a, **kw: events.append((a, kw))
    calls = [_call("a"), _call("b")]
    messages = []
    with patch("agent.tool_executor._run_agent_tool_execution_middleware",
               side_effect=_managed_result), \
         patch("agent.tool_executor._flush_session_db_after_tool_progress",
               return_value=True):
        asyncio.run(_execute_tool_batch_async(
            agent, calls, timeout_s=5.0, _messages=messages))
    assert [e[0][1] for e in events] == ["a", "b"], events

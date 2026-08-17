"""The async tool-batch dispatch: real threads, loop-time gate timeout,
model-order results, interrupt drain. The middleware is mocked (its real
behavior is the sync path's coverage); this pins the async coordination."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

from agent.tool_executor import _execute_tool_batch_async


def _agent():
    return SimpleNamespace(_interrupt_requested=False,
                           _tool_worker_threads_lock=__import__("threading").Lock(),
                           _tool_worker_threads=set())


def _call(name, delay=0.0):
    return dict(function_name=name, function_args={}, effective_task_id="t",
                tool_call_id=name, execute=lambda: None, _delay=delay)


def test_batch_runs_all_and_preserves_order():
    agent = _agent()
    calls = [_call("a"), _call("b"), _call("c")]
    with patch("agent.tool_executor._run_agent_tool_execution_middleware",
               side_effect=lambda *a, **kw: f"ok:{kw['function_name']}"):
        ordered = asyncio.run(_execute_tool_batch_async(agent, calls, timeout_s=5.0))
    assert [c["function_name"] for c, *_ in ordered] == ["a", "b", "c"]
    assert all(out == "ok" for _, out, _ in ordered)


def test_wedged_call_bounded_by_loop_time_timeout():
    agent = _agent()
    calls = [_call("fast"), _call("wedged")]
    def _mw(*a, **kw):
        if kw["function_name"] == "wedged":
            time.sleep(30.0)
        return "ok"
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
    with patch("agent.tool_executor._run_agent_tool_execution_middleware",
               side_effect=lambda *a, **kw: "ok"):
        async def _interrupt_during():
            agent._interrupt_requested = True
            return await _execute_tool_batch_async(agent, calls, timeout_s=5.0)
        ordered = asyncio.run(_interrupt_during())
    # the interrupt is captured per call by the worker wrapper
    assert ordered and all(out in ("ok", "interrupted") for _, out, _ in ordered)


def test_mid_batch_interrupt_drains_pending():
    """A mid-batch interrupt releases the pending calls immediately (the
    loop-time poll), not after the workers finish."""
    agent = _agent()
    calls = [_call("slow")]

    def _mw(*a, **kw):
        time.sleep(30.0)  # wedged worker
        return "ok"

    async def _interrupt_after():
        import asyncio
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
    """A failing middleware reports the error outcome instead of raising —
    the batch returns the other calls' results."""
    agent = _agent()
    calls = [_call("bad"), _call("good")]
    with patch("agent.tool_executor._run_agent_tool_execution_middleware",
               side_effect=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
               if kw["function_name"] == "bad" else "ok"):
        ordered = asyncio.run(_execute_tool_batch_async(agent, calls, timeout_s=5.0))
    by_name = {c["function_name"]: (out, res) for c, out, res in ordered}
    assert by_name["bad"][0] == "error"
    assert by_name["good"][0] == "ok"


def test_timeout_none_still_drains_on_interrupt():
    """timeout_s=None (no deadline) must not mean 'no interrupt checks' —
    the mid-batch interrupt still releases the batch."""
    agent = _agent()
    calls = [_call("slow")]

    def _mw(*a, **kw):
        time.sleep(30.0)
        return "ok"

    async def _run():
        async def _set_soon():
            import asyncio
            await asyncio.sleep(0.05)
            agent._interrupt_requested = True
        asyncio.get_running_loop().create_task(_set_soon())
        return await _execute_tool_batch_async(agent, calls, timeout_s=None)

    with patch("agent.tool_executor._run_agent_tool_execution_middleware", side_effect=_mw):
        ordered = asyncio.run(_run())
    assert ordered and ordered[0][1] in ("interrupted", "timed_out")

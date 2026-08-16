"""Regression tests for background review agent cleanup."""

from __future__ import annotations

import run_agent as run_agent_module
from run_agent import AIAgent


def _bare_agent() -> AIAgent:
    agent = object.__new__(AIAgent)
    agent.model = "fake-model"
    agent.platform = "telegram"
    agent.provider = "openai"
    agent.base_url = ""
    agent.api_key = ""
    agent.api_mode = ""
    agent.session_id = "test-session"
    agent._parent_session_id = ""
    agent._credential_pool = None
    agent._memory_store = object()
    agent._memory_enabled = True
    agent._user_profile_enabled = False
    agent._cached_system_prompt = "test-cached-system-prompt"
    import datetime as _dt
    agent.session_start = _dt.datetime(2026, 1, 1, 12, 0, 0)
    agent._MEMORY_REVIEW_PROMPT = "review memory"
    agent._SKILL_REVIEW_PROMPT = "review skills"
    agent._COMBINED_REVIEW_PROMPT = "review both"
    agent.background_review_callback = None
    agent.status_callback = None
    agent._safe_print = lambda *_args, **_kwargs: None
    import threading as _threading
    agent._background_review_agent = None
    agent._background_review_lock = _threading.Lock()
    agent._background_review_generation = 0
    agent._background_review_request_done = _threading.Event()
    agent._background_review_request_done.set()
    agent._active_children = []
    agent._active_children_lock = _threading.Lock()
    return agent


class ImmediateThread:
    def __init__(self, *, target, daemon=None, name=None):
        self._target = target

    def start(self):
        self._target()


def test_background_review_shuts_down_memory_provider_before_close(monkeypatch):
    events = []

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            events.append(("init", kwargs))
            self._session_messages = []

        def run_conversation(self, **kwargs):
            events.append(("run_conversation", kwargs))

        def shutdown_memory_provider(self):
            events.append(("shutdown_memory_provider", None))

        def close(self):
            events.append(("close", None))

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)

    agent = _bare_agent()

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=[{"role": "user", "content": "hello"}],
        review_memory=True,
    )

    assert [name for name, _payload in events] == [
        "init",
        "run_conversation",
        "shutdown_memory_provider",
        "close",
    ]


def test_background_review_fork_opts_out_of_session_finalization(monkeypatch):
    """The review fork shares the parent's live session_id, so it must set
    ``_end_session_on_close = False``. Otherwise close() (now finalizing owned
    session rows) would end the still-active parent session mid-conversation
    every time the review fires (~every 10 turns). Regression for #12029.
    """
    seen = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []
            # Default matches AIAgent.__init__ (agent_init.py): owns its row.
            self._end_session_on_close = True

        def __setattr__(self, name, value):
            object.__setattr__(self, name, value)
            if name == "_end_session_on_close":
                seen["end_session_on_close"] = value

        def run_conversation(self, **kwargs):
            # By the time the fork runs, the opt-out must already be applied.
            seen["at_run_time"] = self._end_session_on_close

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)

    agent = _bare_agent()

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=[{"role": "user", "content": "hello"}],
        review_memory=True,
    )

    assert seen.get("end_session_on_close") is False
    assert seen.get("at_run_time") is False


def test_background_review_skipped_in_delegation_subagent(monkeypatch):
    """The automatic post-turn review must NOT fire inside a delegation
    subagent (``_delegate_depth > 0``).

    Regression for #85859: the fork inherits the subagent's live model, so in
    a delegation subagent running a premium model it replayed the whole
    conversation at premium rates. Subagents are already barred from writing
    shared MEMORY.md, so there is nothing for the review to persist here.
    """
    forks = []

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            forks.append(kwargs)

        def run_conversation(self, **kwargs):
            pass

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)

    agent = _bare_agent()
    agent._delegate_depth = 1  # this agent IS a delegation subagent

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=[{"role": "user", "content": "hello"}],
        review_memory=True,
        review_skills=True,
    )

    assert forks == [], "no review fork should be spawned inside a subagent"


def test_background_review_runs_at_top_level(monkeypatch):
    """Sibling guard for the subagent skip: at ``_delegate_depth == 0`` the
    review still fires exactly as before (the cost guard is subagent-only)."""
    forks = []

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            forks.append(kwargs)

        def run_conversation(self, **kwargs):
            pass

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)

    agent = _bare_agent()
    agent._delegate_depth = 0  # top-level agent

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=[{"role": "user", "content": "hello"}],
        review_memory=True,
    )

    assert len(forks) == 1, "top-level review must still spawn the fork"


def test_background_review_explicit_focus_runs_even_in_subagent(monkeypatch):
    """An explicit ``/refine`` (``focus`` set) is a deliberate user request and
    is honored regardless of depth — only the automatic post-turn review is
    suppressed in subagents."""
    forks = []

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            forks.append(kwargs)

        def run_conversation(self, **kwargs):
            pass

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)

    agent = _bare_agent()
    agent._delegate_depth = 2

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=[{"role": "user", "content": "hello"}],
        review_skills=True,
        focus="save the deploy workflow as a skill",
    )

    assert len(forks) == 1, "explicit focus review must run even in a subagent"


def test_background_review_registers_on_active_children_for_interrupt(monkeypatch):
    """The review fork must be added to the parent's ``_active_children`` so
    ``AIAgent.interrupt()`` (which fans out to that list) can reach it, and
    to ``_background_review_agent`` so the NEXT live turn can proactively
    cancel a still-running review. Regression for the doubled-token-
    accounting / Ctrl+C-proof lockup that a review racing a new live turn
    against the same session_id/credentials can cause.
    """
    seen = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []

        def run_conversation(self, **kwargs):
            # While run_conversation is "in flight", both tracking slots on
            # the parent must already point at this fork.
            seen["active_children_during_run"] = list(agent._active_children)
            seen["background_review_agent_during_run"] = agent._background_review_agent

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)

    agent = _bare_agent()

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=[{"role": "user", "content": "hello"}],
        review_memory=True,
    )

    fork = seen["background_review_agent_during_run"]
    assert fork is not None
    assert seen["active_children_during_run"] == [fork]

    # After the review completes, both tracking slots must be cleared —
    # otherwise a later interrupt() would try to cancel an already-closed
    # agent, or the next turn would wait on a review that no longer exists.
    assert agent._background_review_agent is None
    assert agent._active_children == []


def test_new_live_turn_cancels_still_running_background_review(monkeypatch):
    """Cancel helper must interrupt a still-running background review and wait
    for its request phase to exit before the live turn continues — not
    fire-and-forget (#84423 residual race after #82418).
    """
    import threading
    import time

    from agent.background_review import cancel_in_flight_background_review

    calls = []
    request_active = threading.Event()
    request_active.set()

    class FakeReviewAgent:
        def interrupt(self, message=None):
            calls.append(message)

            def _finish():
                # Simulate provider abort + run_conversation finally releasing
                # the request-done slot a beat after interrupt returns.
                time.sleep(0.05)
                request_active.clear()
                agent._background_review_agent = None
                agent._background_review_request_done.set()

            threading.Thread(target=_finish, daemon=True).start()

    agent = _bare_agent()
    agent._background_review_agent = FakeReviewAgent()
    agent._background_review_request_done.clear()

    assert request_active.is_set()
    cancel_in_flight_background_review(agent, wait_seconds=2.0)

    assert calls == ["superseded by a new live turn"]
    assert not request_active.is_set()
    assert agent._background_review_request_done.is_set()


def test_live_turn_cancel_is_bounded_when_abort_is_slow():
    """Cancel wait must not deadlock if the review abort path is slow (#84423)."""
    import time

    from agent.background_review import cancel_in_flight_background_review

    class SlowReviewAgent:
        def interrupt(self, message=None):
            return None  # never releases request_done

    agent = _bare_agent()
    agent._background_review_agent = SlowReviewAgent()
    agent._background_review_request_done.clear()

    started = time.monotonic()
    cancel_in_flight_background_review(agent, wait_seconds=0.1)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    # Still uncleared — live turn proceeded without waiting forever.
    assert not agent._background_review_request_done.is_set()


def test_startup_generation_blocks_first_provider_call(monkeypatch):
    """A live turn that begins during review-thread startup must prevent that
    review from making its first provider call (#84423 TOCTOU).
    """
    from agent.background_review import (
        cancel_in_flight_background_review,
        claim_background_review_slot,
        _run_review_in_thread,
    )

    ran = {"run_conversation": False}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []
            self._memory_enabled = True
            self._user_profile_enabled = False

        def run_conversation(self, **kwargs):
            ran["run_conversation"] = True

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)

    agent = _bare_agent()
    # Simulate spawn claiming the slot, then a live turn cancelling before
    # the review worker reaches run_conversation.
    generation = claim_background_review_slot(agent)
    assert generation is not None
    assert not agent._background_review_request_done.is_set()

    cancel_in_flight_background_review(agent, wait_seconds=0.01)
    _run_review_in_thread(
        agent,
        messages_snapshot=[{"role": "user", "content": "hello"}],
        prompt="review",
        generation=generation,
    )

    assert ran["run_conversation"] is False
    assert agent._background_review_request_done.is_set()
    assert agent._background_review_agent is None


# ---------------------------------------------------------------------------
# memory_notifications mode: off | on | verbose
# ---------------------------------------------------------------------------

import json as _json

from agent.background_review import summarize_background_review_actions


def _memory_add_review():
    """A minimal review transcript: one memory add (assistant call + tool result)."""
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_mem1",
                    "function": {
                        "name": "memory",
                        "arguments": _json.dumps(
                            {
                                "action": "add",
                                "target": "memory",
                                "content": "User prefers terse replies",
                            }
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_mem1",
            "content": _json.dumps(
                {"success": True, "message": "Entry added.", "target": "memory"}
            ),
        },
    ]


def _skill_patch_review():
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_skill1",
                    "function": {
                        "name": "skill_manage",
                        "arguments": _json.dumps(
                            {"action": "patch", "name": "demo", "old_string": "a", "new_string": "b"}
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_skill1",
            "content": _json.dumps(
                {
                    "success": True,
                    "message": "Patched SKILL.md in skill 'demo' (1 replacement).",
                    "_change": {"old": "a", "new": "b"},
                }
            ),
        },
    ]


def test_memory_notifications_off_returns_nothing():
    actions = summarize_background_review_actions(
        _memory_add_review(), [], notification_mode="off"
    )
    assert actions == []








def test_skill_patch_off_silent_verbose_shows_diff():
    assert (
        summarize_background_review_actions(
            _skill_patch_review(), [], notification_mode="off"
        )
        == []
    )
    verbose = summarize_background_review_actions(
        _skill_patch_review(), [], notification_mode="verbose"
    )
    assert len(verbose) == 1
    assert "demo" in verbose[0] and "→" in verbose[0]

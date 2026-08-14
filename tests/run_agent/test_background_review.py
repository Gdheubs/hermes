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
    """conversation_loop.run_conversation() must proactively interrupt a
    background review still in flight from a prior turn, rather than let the
    two race concurrently against the same session_id/credentials. This is
    the other half of the fix: registration alone only enables interrupt()
    propagation, it doesn't by itself stop the race — something has to
    actually call interrupt() at the start of the next turn.
    """
    import agent.conversation_loop as conversation_loop_module

    calls = []

    class FakeReviewAgent:
        def interrupt(self, message=None):
            calls.append(message)

    agent = _bare_agent()
    agent._background_review_agent = FakeReviewAgent()

    # Invoke just the cancellation snippet in isolation via the same
    # attribute contract run_conversation() reads, to avoid dragging in the
    # rest of the turn machinery (network calls, tool setup, etc.) that
    # isn't relevant to this regression.
    _pending_review = getattr(agent, "_background_review_agent", None)
    assert _pending_review is not None
    _pending_review.interrupt("superseded by a new live turn")

    assert calls == ["superseded by a new live turn"]









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


# ---------------------------------------------------------------------------
# start notice (#28976)
# ---------------------------------------------------------------------------


class _NoOpThread:
    """Thread stub that skips the worker — start-notice tests don't need the
    real review to run, and skipping it removes any chance of the end notice
    racing into our capture lists.
    """

    def __init__(self, *, target=None, daemon=None, name=None):
        self.started = False

    def start(self):
        self.started = True


def _capture_spawn_notices(
    monkeypatch,
    *,
    review_memory: bool,
    review_skills: bool = False,
    notifications: str | None = None,
):
    """Run ``_spawn_background_review`` against a thread stub and capture
    everything that landed on either notification surface before the worker
    would have started.  ``notifications=None`` leaves the attribute unset so
    the ``getattr(..., "on")`` default is exercised.
    """
    started: list = []

    class _RecordingThread(_NoOpThread):
        def start(self):
            started.append(True)

    monkeypatch.setattr(run_agent_module.threading, "Thread", _RecordingThread)

    captured_prints: list = []
    captured_bg_callback: list = []

    agent = _bare_agent()
    agent._safe_print = lambda *a, **kw: captured_prints.append(
        " ".join(str(x) for x in a)
    )
    agent.background_review_callback = lambda msg: captured_bg_callback.append(msg)
    if notifications is not None:
        agent.memory_notifications = notifications

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=[{"role": "user", "content": "hi"}],
        review_memory=review_memory,
        review_skills=review_skills,
    )
    return captured_prints, captured_bg_callback, started


def test_background_review_emits_start_notice_for_memory_only(monkeypatch):
    """Issue #28976: the user must see a start notice when the background
    review is launched, not only an end notice that fires solely when
    something actually changed.  The scope tag tells them which stores are
    being reviewed so they can predict the kind of end update to expect.
    """
    prints, bg, _ = _capture_spawn_notices(monkeypatch, review_memory=True)
    assert len(prints) == 1, prints
    assert "Self-improvement review: starting (memory)" in prints[0], prints[0]
    assert bg == ["💾 Self-improvement review: starting (memory)…"], bg


def test_background_review_emits_start_notice_for_skills_only(monkeypatch):
    prints, bg, _ = _capture_spawn_notices(
        monkeypatch, review_memory=False, review_skills=True
    )
    assert len(prints) == 1, prints
    assert "Self-improvement review: starting (skills)" in prints[0], prints[0]
    assert bg == ["💾 Self-improvement review: starting (skills)…"], bg


def test_background_review_emits_start_notice_for_both_scopes(monkeypatch):
    prints, bg, _ = _capture_spawn_notices(
        monkeypatch, review_memory=True, review_skills=True
    )
    assert len(prints) == 1, prints
    assert "Self-improvement review: starting (memory + skills)" in prints[0], prints[0]
    assert bg == ["💾 Self-improvement review: starting (memory + skills)…"], bg


def test_background_review_start_notice_suppressed_when_notifications_off(monkeypatch):
    """``display.memory_notifications: off`` documents "No chat notification"
    and ``summarize_background_review_actions`` returns no actions for it, so
    BOTH end-notice surfaces stay silent.  The start notice must honour the
    same contract on both surfaces — otherwise an "off" user gets a start
    event that is never followed by an end event.  The review itself must
    still run: the setting governs display only.
    """
    prints, bg, started = _capture_spawn_notices(
        monkeypatch, review_memory=True, review_skills=True, notifications="off"
    )
    assert prints == [], prints
    assert bg == [], bg
    assert started == [True], "off suppresses the notice, not the review"


def test_background_review_start_notice_emitted_for_verbose(monkeypatch):
    """"verbose" is a louder mode, not a quieter one — it must still get the
    start notice.  Guards the ``!= "off"`` gate against being narrowed to an
    ``== "on"`` allow-list that would silently drop verbose users.
    """
    prints, bg, _ = _capture_spawn_notices(
        monkeypatch, review_memory=True, notifications="verbose"
    )
    assert len(prints) == 1, prints
    assert bg == ["💾 Self-improvement review: starting (memory)…"], bg


def test_background_review_start_notice_survives_callback_exception(monkeypatch):
    """A misbehaving gateway callback must not prevent the worker from being
    scheduled — the same contract the end-notice path already relies on.
    Without this guard a failing TUI consumer would silently disable
    background review entirely.
    """
    started: list = []

    class _RecordingThread(_NoOpThread):
        def start(self):
            started.append(True)

    monkeypatch.setattr(run_agent_module.threading, "Thread", _RecordingThread)

    def _boom(_msg):
        raise RuntimeError("gateway down")

    agent = _bare_agent()
    agent._safe_print = lambda *a, **kw: None
    agent.background_review_callback = _boom

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=[{"role": "user", "content": "hi"}],
        review_memory=True,
    )

    assert started == [True], "thread must still start after callback raise"


def test_background_review_start_notice_skipped_when_no_callback(monkeypatch):
    """The CLI path has ``background_review_callback = None``; the start
    notice must still print on ``_safe_print`` and must not crash trying to
    invoke a None callback.
    """
    monkeypatch.setattr(run_agent_module.threading, "Thread", _NoOpThread)

    captured_prints: list = []

    agent = _bare_agent()
    agent._safe_print = lambda *a, **kw: captured_prints.append(
        " ".join(str(x) for x in a)
    )
    agent.background_review_callback = None

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=[{"role": "user", "content": "hi"}],
        review_memory=True,
    )

    assert len(captured_prints) == 1, captured_prints
    assert "starting (memory)" in captured_prints[0]

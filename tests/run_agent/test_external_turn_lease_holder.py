"""A caller that already owns the turn lease can hand its holder to the engine.

``AIAgent.run_conversation`` accepts ``session_turn_lease_holder`` from a front
end that acquired the conversation's durable turn lease itself. The turn must
then arm the transcript-write fence with that holder and leave acquisition,
refresh, and release to the caller. Every test here also pins the other half of
the contract: with the parameter absent, the internal lease path is unchanged.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent

_REFRESHER_THREAD_NAME = "session-turn-lease-refresh"


def _external_holder(turn: str = "front-end") -> str:
    """Build a holder whose PID is alive so no reclaim probe can steal it.

    ``try_acquire_session_turn_lease`` reclaims a row whose structured holder
    PID is gone, so a synthetic PID would make "the engine did not release it"
    unfalsifiable.
    """
    return f"pid={os.getpid()}:turn={turn}:platform=remote"


class _DB:
    """Session-store double that records every turn-lease call it receives."""

    def __init__(self, session_exists=True, acquire_result=True):
        self.events = []
        self.session_exists = session_exists
        self.acquire_result = acquire_result

    def get_session(self, session_id):
        return {"id": session_id} if self.session_exists else None

    def acquire_session_turn_lease(self, session_id, holder, **kwargs):
        self.events.append(("acquire", session_id, holder))
        on_wait = kwargs.get("on_wait")
        if on_wait is not None and self.acquire_result is False:
            on_wait(0.0)
        return self.acquire_result

    def resolve_resume_session_id(self, session_id):
        self.events.append(("resolve", session_id))
        return "compressed-tip"

    def get_messages_as_conversation(self, session_id, **kwargs):
        self.events.append(("reload", session_id, kwargs))
        return [{"role": "user", "content": "durable latest"}]

    def refresh_session_turn_lease(self, session_id, holder, **kwargs):
        self.events.append(("refresh", session_id, holder))
        return True

    def release_session_turn_lease(self, session_id, holder):
        self.events.append(("release", session_id, holder))


def _agent_with_db(db, *, session_id="shared", platform="desktop"):
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = session_id
    agent.platform = platform
    agent.model = "test-model"
    agent._session_db = db
    agent._session_db_created = True
    agent._persist_disabled = False
    agent._parent_session_id = None
    agent._relay_pending_turn_id = None
    agent._reset_activity_labels_after_turn = lambda: None
    agent._conversation_root_id = lambda: session_id
    agent.log_prefix = ""
    agent._vprint = lambda *a, **k: None
    agent.status_callback = None
    agent._interrupt_requested = False
    agent._interrupt_message = None
    agent._pending_redirect = None
    agent._execution_thread_id = None
    agent._interrupt_thread_signal_pending = False
    return agent


def _arm_flush(agent):
    """Add the bookkeeping the real persist path reads, nothing more."""
    agent._session_persist_lock = None
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._db_flush_scan_prefix = None
    agent._pending_cli_user_message = None
    agent._persist_user_message_idx = None
    agent._persist_user_message_override = None
    agent._persist_user_message_timestamp = None
    agent._last_persistence_error_cause = None
    return agent


def test_external_holder_skips_engine_acquire(monkeypatch):
    """A supplied holder suppresses acquire, resolve, reload, and release."""
    db = _DB()
    agent = _agent_with_db(db)
    holder = _external_holder()
    observed = {}

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        observed["history"] = history
        observed["holder"] = getattr(
            _agent, "_active_session_turn_lease_holder", None
        )
        observed["ttl"] = getattr(
            _agent, "_active_session_turn_lease_ttl_seconds", None
        )
        return {"final_response": "ok", "messages": history, "failed": False}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    seed = [{"role": "user", "content": "caller seed"}]
    result = AIAgent.run_conversation(
        agent,
        "new message",
        conversation_history=seed,
        session_turn_lease_holder=holder,
        session_turn_lease_ttl_seconds=45.0,
    )

    assert result["final_response"] == "ok"
    # No acquire, so also no contended resolve/reload and no release.
    assert db.events == []
    # The engine deliberately performs no reload on this path: the reload
    # after a contended wait belongs to its own acquire, so a caller whose
    # acquire waited must refresh its own snapshot before calling. This pins
    # only that the engine does not replace the caller-provided history.
    assert observed["history"] is seed
    # The fence input the persist path reads is the caller's holder.
    assert observed["holder"] == holder
    assert observed["ttl"] == 45.0
    # Cleared on exit so a cached agent cannot fence the next turn with it.
    assert getattr(agent, "_active_session_turn_lease_holder", None) is None
    assert getattr(agent, "_active_session_turn_lease_ttl_seconds", None) is None


def test_external_holder_defaults_ttl_when_unspecified(monkeypatch):
    """Omitting the TTL leaves the same 300s default the internal path uses."""
    db = _DB()
    agent = _agent_with_db(db)
    observed = {}

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        observed["ttl"] = getattr(
            _agent, "_active_session_turn_lease_ttl_seconds", None
        )
        return {"final_response": "ok", "messages": history, "failed": False}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    AIAgent.run_conversation(
        agent,
        "new message",
        conversation_history=[{"role": "user", "content": "seed"}],
        session_turn_lease_holder=_external_holder(),
    )

    assert observed["ttl"] == 300.0
    assert db.events == []


def test_external_holder_is_not_released_by_engine(tmp_path, monkeypatch):
    """The caller's lease row survives the turn, still owned by the caller."""
    path = tmp_path / "state.db"
    db = SessionDB(path)
    other = SessionDB(path)
    try:
        db.create_session("shared", source="test")
        holder = _external_holder()
        assert db.try_acquire_session_turn_lease("shared", holder, ttl_seconds=30)

        agent = _agent_with_db(db)

        def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
            return {"final_response": "ok", "messages": history, "failed": False}

        monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
        result = AIAgent.run_conversation(
            agent,
            "new message",
            conversation_history=[{"role": "user", "content": "seed"}],
            session_turn_lease_holder=holder,
            session_turn_lease_ttl_seconds=30.0,
        )
        assert result["final_response"] == "ok"

        # Still held: a contender cannot take the conversation.
        assert (
            other.try_acquire_session_turn_lease(
                "shared", _external_holder("contender"), ttl_seconds=5
            )
            is False
        )
        # Still held by the caller specifically: a holder-qualified refresh
        # matches, which a released or reclaimed row could not do.
        assert (
            db.refresh_session_turn_lease("shared", holder, ttl_seconds=30) is True
        )
        db.release_session_turn_lease("shared", holder)
    finally:
        db.close()
        other.close()


def test_external_holder_arms_write_fence(tmp_path, monkeypatch):
    """Transcript writes carry the caller's holder into the in-txn fence.

    Four arms in one turn, because the discriminator is which writes are
    admitted and which are refused. If the engine did not arm the fence, the
    persist path would pass ``turn_lease_holder=None``, the guard would not run
    at all, and the fenced arm would be admitted like the rest.
    """
    path = tmp_path / "state.db"
    db = SessionDB(path)
    thief_db = SessionDB(path)
    ttl = 0.3
    try:
        db.create_session("shared", source="test")
        holder = _external_holder()
        assert db.try_acquire_session_turn_lease("shared", holder, ttl_seconds=ttl)

        agent = _arm_flush(_agent_with_db(db))
        agent._ensure_db_session = lambda: None
        observed = {}

        def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
            observed["armed"] = getattr(
                _agent, "_active_session_turn_lease_holder", None
            )
            # Arm 1: live lease, own holder. Admitted.
            observed["owned"] = _agent._flush_messages_to_session_db(
                [{"role": "user", "content": "owned"}], []
            )
            # Arm 2: lease lapsed but uncontested. The fence revives the row
            # for its own holder, so a turn running without a refresher is not
            # locked out of its own transcript.
            time.sleep(ttl + 0.25)
            observed["revived"] = _agent._flush_messages_to_session_db(
                [{"role": "assistant", "content": "expired-but-owned"}], []
            )
            # Arm 3: the revival used the caller's TTL, not a hardcoded one,
            # so the row lapses again on the same short clock and a contender
            # takes the conversation.
            time.sleep(ttl + 0.25)
            observed["stolen"] = thief_db.try_acquire_session_turn_lease(
                "shared", _external_holder("thief"), ttl_seconds=30
            )
            # Arm 4: foreign holder now owns the row. Refused.
            observed["fenced"] = _agent._flush_messages_to_session_db(
                [{"role": "assistant", "content": "after-steal"}], []
            )
            observed["cause"] = getattr(
                _agent, "_last_persistence_error_cause", None
            )
            return {"final_response": "ok", "messages": history, "failed": False}

        monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
        AIAgent.run_conversation(
            agent,
            "new message",
            conversation_history=[{"role": "user", "content": "seed"}],
            session_turn_lease_holder=holder,
            session_turn_lease_ttl_seconds=ttl,
        )

        assert observed["armed"] == holder
        assert observed["owned"] is True
        assert observed["revived"] is True
        assert observed["stolen"] is True
        assert observed["fenced"] is False
        assert observed["cause"] == "turn_lease"
        assert [m["content"] for m in thief_db.get_messages("shared")] == [
            "owned",
            "expired-but-owned",
        ]
    finally:
        db.close()
        thief_db.close()


def test_external_holder_starts_no_refresher(monkeypatch):
    """No lease refresher runs on the external path, but one still runs without it."""
    seen = {}

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        seen.setdefault("threads", []).append(
            [t.name for t in threading.enumerate() if t.is_alive()]
        )
        return {"final_response": "ok", "messages": history, "failed": False}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)

    # Control arm first: the internal path does start the refresher, so the
    # assertion below is about the external path and not about the literal
    # having drifted out of the code.
    control_db = _DB()
    AIAgent.run_conversation(
        _agent_with_db(control_db),
        "new message",
        conversation_history=[{"role": "user", "content": "seed"}],
    )
    assert _REFRESHER_THREAD_NAME in seen["threads"][0]

    external_db = _DB()
    AIAgent.run_conversation(
        _agent_with_db(external_db),
        "new message",
        conversation_history=[{"role": "user", "content": "seed"}],
        session_turn_lease_holder=_external_holder(),
    )
    assert _REFRESHER_THREAD_NAME not in seen["threads"][1]
    assert external_db.events == []
    assert [event[0] for event in control_db.events] == ["acquire", "release"]


def test_external_holder_ignored_without_session_id(caplog, monkeypatch):
    """Without a session id there is no conversation to fence, so nothing arms.

    Dropping the holder is the contract, but dropping it silently is not: a
    caller that believes the lease it acquired covers this turn is running
    unfenced, and only the log can tell it so.
    """
    db = _DB()
    agent = _agent_with_db(db, session_id="")
    holder = _external_holder()
    observed = {}

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        observed["holder"] = getattr(
            _agent, "_active_session_turn_lease_holder", None
        )
        observed["threads"] = [
            t.name for t in threading.enumerate() if t.is_alive()
        ]
        return {"final_response": "ok", "messages": history, "failed": False}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    with caplog.at_level(logging.WARNING, logger="run_agent"):
        result = AIAgent.run_conversation(
            agent,
            "new message",
            conversation_history=[{"role": "user", "content": "seed"}],
            session_turn_lease_holder=holder,
        )

    assert result["final_response"] == "ok"
    assert observed["holder"] is None
    assert _REFRESHER_THREAD_NAME not in observed["threads"]
    assert db.events == []
    # Named, not just counted: the operator has to know which holder was
    # dropped to find the caller that supplied it.
    assert any(
        "session turn lease holder supplied without a session id"
        in record.getMessage()
        and holder in record.getMessage()
        for record in caplog.records
    )


def test_external_holder_cleared_on_interrupted_exit(monkeypatch):
    """The fence input is cleared on the exits that skip a normal completion.

    The clear lives in the outermost ``finally`` chain rather than beside the
    return, so it has to survive both an exception leaving the turn through
    ``except BaseException`` and an early return reporting an interrupt. Only
    the completed-turn exit was pinned before. An agent that kept the holder
    after either of these would fence its next turn with a lease the caller
    has since released, and the fence would then admit writes the next
    conversation's real owner should have refused.
    """
    holder = _external_holder()
    observed = {}

    def _record_armed(_agent):
        observed.setdefault("armed", []).append(
            getattr(_agent, "_active_session_turn_lease_holder", None)
        )

    # Arm 1: the turn leaves by raising. KeyboardInterrupt and not a plain
    # Exception, because the real interrupt is a BaseException and the clear
    # must not be sitting behind a handler that never sees one.
    def raising_run(_agent, _message, _system, _history, *_args, **_kwargs):
        _record_armed(_agent)
        raise KeyboardInterrupt("stopped mid-turn")

    monkeypatch.setattr("agent.conversation_loop.run_conversation", raising_run)
    raised_db = _DB()
    raised_agent = _agent_with_db(raised_db)
    with pytest.raises(KeyboardInterrupt):
        AIAgent.run_conversation(
            raised_agent,
            "new message",
            conversation_history=[{"role": "user", "content": "seed"}],
            session_turn_lease_holder=holder,
            session_turn_lease_ttl_seconds=45.0,
        )

    # Armed while the turn was running, so the clear below is not vacuous.
    assert observed["armed"] == [holder]
    assert getattr(raised_agent, "_active_session_turn_lease_holder", None) is None
    assert (
        getattr(raised_agent, "_active_session_turn_lease_ttl_seconds", None) is None
    )
    # Clearing is not releasing: the row stays the caller's on this exit too.
    assert raised_db.events == []

    # Arm 2: the turn returns early reporting an interrupt instead of raising,
    # which is the shape the interrupt and redirect paths actually take.
    def interrupted_run(_agent, _message, _system, history, *_args, **_kwargs):
        _record_armed(_agent)
        return {
            "final_response": "",
            "messages": history,
            "failed": False,
            "interrupted": True,
        }

    monkeypatch.setattr("agent.conversation_loop.run_conversation", interrupted_run)
    returned_db = _DB()
    returned_agent = _agent_with_db(returned_db)
    result = AIAgent.run_conversation(
        returned_agent,
        "new message",
        conversation_history=[{"role": "user", "content": "seed"}],
        session_turn_lease_holder=holder,
        session_turn_lease_ttl_seconds=45.0,
    )

    assert result["interrupted"] is True
    assert observed["armed"] == [holder, holder]
    assert getattr(returned_agent, "_active_session_turn_lease_holder", None) is None
    assert (
        getattr(returned_agent, "_active_session_turn_lease_ttl_seconds", None) is None
    )
    assert returned_db.events == []


def test_absent_param_is_behavior_neutral(monkeypatch):
    """The internal lease path is untouched when the new parameters are absent.

    This is the regression guard on converting the existing acquire gate from
    ``if`` to ``elif``: same call sequence, same minted holder shape, same
    contended reload, same release.
    """
    db = _DB()
    agent = _agent_with_db(db, session_id="stale-parent")
    agent._conversation_root_id = lambda: "stale-parent"
    status_events = []
    agent.status_callback = lambda kind, text=None: status_events.append(
        (kind, text)
    )
    observed = {}

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        observed["history"] = history
        observed["session_id"] = _agent.session_id
        observed["holder"] = getattr(
            _agent, "_active_session_turn_lease_holder", None
        )
        observed["ttl"] = getattr(
            _agent, "_active_session_turn_lease_ttl_seconds", None
        )
        return {"final_response": "ok", "messages": history, "failed": False}

    # Simulate a contended wait so the resolve+reload path is exercised.
    def acquire_with_wait(session_id, holder, **kwargs):
        db.events.append(("acquire", session_id, holder))
        on_wait = kwargs.get("on_wait")
        if on_wait is not None:
            on_wait(0.0)
        return True

    db.acquire_session_turn_lease = acquire_with_wait

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    result = AIAgent.run_conversation(
        agent,
        "new message",
        conversation_history=[{"role": "user", "content": "stale"}],
    )

    assert result["final_response"] == "ok"
    assert observed["history"] == [{"role": "user", "content": "durable latest"}]
    assert observed["session_id"] == "compressed-tip"
    assert observed["holder"].startswith(f"pid={os.getpid()}:turn=")
    assert observed["ttl"] == 300.0
    assert [event[0] for event in db.events] == [
        "acquire",
        "resolve",
        "reload",
        "release",
    ]
    # Released under exactly the holder that was acquired and armed.
    acquired = next(e for e in db.events if e[0] == "acquire")[2]
    released = next(e for e in db.events if e[0] == "release")[2]
    assert acquired == released == observed["holder"]
    assert getattr(agent, "_active_session_turn_lease_holder", None) is None
    assert any(
        kind == "lifecycle" and text and "waiting for it to finish" in text
        for kind, text in status_events
    )

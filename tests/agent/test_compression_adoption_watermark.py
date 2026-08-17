"""Regression: durable-parent adoption must be gated by a monotonic row-id
watermark, not a bare length comparison.

``compress_context`` re-reads the durable parent after acquiring the
per-session compression lock and adopts it when it "grew" — historically
``len(durable_parent) > len(messages)``. Length conflates count with
freshness: active-row COUNT is not monotonic (rotation ends a parent and
publishes a child whose rows are fewer but strictly NEWER — new
AUTOINCREMENT ids), while row ID is. The bare length gate is therefore
directionally blind:

- W1 class A — an exception AFTER a committed rotation leaves the caller
  holding the stale pre-rotation transcript while ``agent.session_id`` points
  at the committed child; the length gate refuses to reconcile and the same
  content is re-summarized into a second child.
- W1 class B — a session ended by a NON-compression path (``/new``, gateway
  hygiene, timeout) between the rotated-parent gate (compression-reason-only)
  and the durable read is not deflected; its full transcript plus orphan
  appends can be adopted.
- W1 class C — with ``_persist_user_message_idx`` unset, the legacy path
  ASSUMES full durability; an un-persisted live tail is silently dropped.

The fix replaces the length comparison with a monotonic max-id watermark
(adopt iff the durable reload's max row id strictly exceeds the snapshot's
max known row id), guarded by a same-window liveness re-check and a
fail-closed preflush.
"""

from __future__ import annotations

from pathlib import Path

from hermes_state import SessionDB

from tests.agent.test_compression_adoption_preserves_live_tail import (
    _build_agent_with_db,
)


def test_retry_after_committed_rotation_adopts_child_not_stale_parent(tmp_path: Path) -> None:
    """W1 class A: an exception AFTER a successful rotation commit leaves the
    caller holding the pre-rotation transcript while agent.session_id points at
    the committed child. The retry must be re-anchored to the child rows, not
    re-summarize the same content into a second child."""
    db = SessionDB(db_path=tmp_path / "state.db")
    parent_sid = "CRASH_AFTER_COMMIT_PARENT"
    child_sid = "CRASH_AFTER_COMMIT_CHILD"
    db.create_session(parent_sid, source="webui")
    for i in range(100):
        db.append_message(parent_sid, "user", f"p{i}")

    # The rotation that already committed (mirrors L3488-3501): parent ended,
    # child holds the 10-row compressed summary with strictly newer row ids.
    child_rows = [
        {"role": "user", "content": f"child summary {i}"} for i in range(10)
    ]
    db.publish_compression_child(
        parent_session_id=parent_sid,
        child_session_id=child_sid,
        source="test",
        messages=child_rows,
        require_compression_lease=False,
    )

    # The caller kept its pre-compression list (L3071-3086 re-raises without
    # restoring it); agent.session_id is already the child (L3502).
    stale = db.get_messages_as_conversation(parent_sid, include_row_ids=True)
    assert len(stale) == 100
    agent = _build_agent_with_db(db, child_sid)

    agent._compress_context(stale, "sys", approx_tokens=120_000)

    compress_input = agent.context_compressor.compress.call_args.args[0]
    assert [m["content"] for m in compress_input] == [
        f"child summary {i}" for i in range(10)
    ], (
        "Retry after a committed rotation must compress the CHILD rows, not "
        "re-summarize the stale parent transcript (W1 class A). "
        f"Compress input: {[m.get('content') for m in compress_input]!r}"
    )


def test_ended_by_other_path_parent_not_adopted(tmp_path: Path) -> None:
    """W1 class B: a session ended by a NON-compression path between the
    rotated-parent gate (L2709, compression-reason-only) and the durable read
    (L2834) is not deflected; its full transcript plus orphan appends must
    never become the compress input."""
    db = SessionDB(db_path=tmp_path / "state.db")
    parent_sid = "ENDED_BY_TIMEOUT"
    db.create_session(parent_sid, source="webui")
    db.append_message(parent_sid, "user", "persisted question")
    db.append_message(parent_sid, "assistant", "persisted answer")

    snapshot = db.get_messages_as_conversation(parent_sid, include_row_ids=True)
    db.end_session(parent_sid, "timeout")          # non-compression end
    db.append_message(parent_sid, "assistant", "orphan row 3")  # orphan append

    agent = _build_agent_with_db(db, parent_sid)
    agent._compress_context(snapshot, "sys", approx_tokens=120_000)

    compress_input = agent.context_compressor.compress.call_args.args[0]
    assert "orphan row 3" not in [m.get("content") for m in compress_input], (
        "Adoption must not feed an ended session's rows (incl. orphan "
        "appends) to the summarizer (W1 class B). "
        f"Compress input: {[m.get('content') for m in compress_input]!r}"
    )


def test_unset_flush_anchor_fails_closed_keeps_live_tail(tmp_path: Path) -> None:
    """W1 class C: with _persist_user_message_idx unset (cold-resumed preflight
    agent), the in-memory list may still hold an un-persisted live tail. The
    legacy adopt-directly assumption must fail closed so the live input is not
    dropped from the compress input."""
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "UNSET_ANCHOR_TAIL"
    db.create_session(session_id, source="desktop")
    db.append_message(session_id, "user", "persisted question")
    db.append_message(session_id, "assistant", "persisted answer")

    snapshot = db.get_messages_as_conversation(session_id, include_row_ids=True)
    messages = [*snapshot, {"role": "user", "content": "LIVE TAIL"}]  # no _row_id
    # _persist_user_message_idx stays None (never anchored).

    db.append_message(session_id, "assistant", "concurrent row 3")
    db.append_message(session_id, "assistant", "concurrent row 4")

    agent = _build_agent_with_db(db, session_id)
    agent._compress_context(messages, "sys", approx_tokens=120_000)

    compress_input = agent.context_compressor.compress.call_args.args[0]
    assert "LIVE TAIL" in [m.get("content") for m in compress_input], (
        "With an unset flush anchor and an un-persisted live tail, adoption "
        "must be skipped so the live input reaches the summarizer (W1 class C). "
        f"Compress input: {[m.get('content') for m in compress_input]!r}"
    )

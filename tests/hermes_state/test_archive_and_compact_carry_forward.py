"""Compaction must not leave a duplicate of every carried-forward tail message.

``archive_and_compact`` soft-archives the whole live set and re-inserts the
compacted set as fresh rows. The compacted set is ``[summary] + [verbatim
tail]``, so each tail message ends up stored twice: once as the pre-compaction
row and once as the freshly inserted live row.

Both copies satisfy ``search_messages``'s ``(active = 1 OR compacted = 1)``
filter, so recall returns the same message N+1 times after N compactions, and
``compacted = 1`` -- which the session_search tool renders as "summarized
away" -- gets stamped on content that is still live.

The archived copy of a carried-forward message is a superseded duplicate, not
a summarized turn, so it belongs in the rewind/undo state (``active = 0,
compacted = 0``): still on disk, still reachable via
``get_messages(include_inactive=True)``, but hidden from recall.
"""

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _seed(db, session_id="s1", n=5):
    db.create_session(session_id, source="cli")
    for i in range(n):
        db.append_message(session_id, "user", content=f"turn-{i} unique-token-{i}")
    return session_id


def _rows(db, session_id):
    """(content, active, compacted) for every row, oldest first."""
    with db._read_ctx() as conn:
        return [
            (r[0], r[1], r[2])
            for r in conn.execute(
                "SELECT content, active, compacted FROM messages"
                " WHERE session_id = ? ORDER BY id",
                (session_id,),
            )
        ]


def _recall_visible(db, session_id):
    """Rows session_search can reach: active live rows plus archived turns."""
    with db._read_ctx() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT content FROM messages WHERE session_id = ?"
                " AND (active = 1 OR compacted = 1) ORDER BY id",
                (session_id,),
            )
        ]


class TestCarryForwardIsNotDuplicated:
    def test_carried_forward_tail_appears_once_in_recall(self, db):
        sid = _seed(db, n=5)
        tail = [
            {"role": "user", "content": "turn-3 unique-token-3"},
            {"role": "user", "content": "turn-4 unique-token-4"},
        ]
        db.archive_and_compact(sid, [{"role": "user", "content": "SUMMARY"}] + tail)

        visible = _recall_visible(db, sid)
        for token in ("unique-token-3", "unique-token-4"):
            hits = [c for c in visible if token in c]
            assert len(hits) == 1, f"{token} visible {len(hits)}x to recall, expected 1"

    def test_summarized_turns_stay_discoverable(self, db):
        sid = _seed(db, n=5)
        tail = [{"role": "user", "content": "turn-4 unique-token-4"}]
        db.archive_and_compact(sid, [{"role": "user", "content": "SUMMARY"}] + tail)

        visible = _recall_visible(db, sid)
        # turns 0-2 were genuinely summarized away -- recall must still find them.
        for token in ("unique-token-0", "unique-token-1", "unique-token-2"):
            assert any(token in c for c in visible), f"{token} lost from recall"

    def test_superseded_copy_is_retained_on_disk(self, db):
        sid = _seed(db, n=3)
        tail = [{"role": "user", "content": "turn-2 unique-token-2"}]
        db.archive_and_compact(sid, [{"role": "user", "content": "SUMMARY"}] + tail)

        # Nothing is deleted: the superseded row is still there, just hidden.
        all_rows = _rows(db, sid)
        assert sum(1 for c, _, _ in all_rows if "unique-token-2" in c) == 2
        superseded = [
            (a, cp) for c, a, cp in all_rows if "unique-token-2" in c and a == 0
        ]
        assert superseded == [(0, 0)], (
            "superseded duplicate must use the rewind state (active=0, compacted=0),"
            f" got {superseded}"
        )

    def test_live_context_is_unchanged(self, db):
        sid = _seed(db, n=5)
        compacted = [
            {"role": "user", "content": "SUMMARY"},
            {"role": "user", "content": "turn-3 unique-token-3"},
            {"role": "user", "content": "turn-4 unique-token-4"},
        ]
        db.archive_and_compact(sid, compacted)

        live = [m["content"] for m in db.get_messages(sid)]
        assert live == [m["content"] for m in compacted]

    def test_repeated_compaction_does_not_accumulate_duplicates(self, db):
        sid = _seed(db, n=6)
        for _ in range(3):
            db.archive_and_compact(
                sid,
                [
                    {"role": "user", "content": "SUMMARY"},
                    {"role": "user", "content": "turn-5 unique-token-5"},
                ],
            )

        visible = _recall_visible(db, sid)
        hits = [c for c in visible if "unique-token-5" in c]
        assert len(hits) == 1, f"tail visible {len(hits)}x after 3 compactions"

    def test_no_carry_forward_still_archives_everything(self, db):
        """A compaction that keeps no verbatim tail behaves exactly as before."""
        sid = _seed(db, n=3)
        db.archive_and_compact(sid, [{"role": "user", "content": "SUMMARY ONLY"}])

        visible = _recall_visible(db, sid)
        for token in ("unique-token-0", "unique-token-1", "unique-token-2"):
            assert any(token in c for c in visible), f"{token} lost from recall"
        assert any("SUMMARY ONLY" in c for c in visible)

    def test_matching_content_outside_the_tail_is_still_archived(self, db):
        """Only the contiguous tail suffix carries forward.

        An older message that happens to repeat the tail's text was genuinely
        summarized away and must keep ``compacted = 1`` -- matching by content
        alone, without anchoring to the suffix, would wrongly hide it.
        """
        sid = "s1"
        db.create_session(sid, source="cli")
        db.append_message(sid, "user", content="echo unique-token-x")
        db.append_message(sid, "user", content="filler")
        db.append_message(sid, "user", content="echo unique-token-x")

        db.archive_and_compact(
            sid,
            [
                {"role": "user", "content": "SUMMARY"},
                {"role": "user", "content": "echo unique-token-x"},
            ],
        )

        rows = _rows(db, sid)
        archived = [(a, cp) for c, a, cp in rows if "unique-token-x" in c and a == 0]
        # oldest copy: summarized away (compacted=1). newest: superseded (0, 0).
        assert archived == [(0, 1), (0, 0)], archived

    def test_tool_calls_inserted_through_append_match_the_carried_tail(self, db):
        sid = _seed(db, n=1)
        tool_calls = [{"id": "call-1", "type": "function"}]
        db.append_message(
            sid,
            "assistant",
            content=None,
            tool_calls=tool_calls,
            tool_call_id="call-1",
        )
        tail = {
            "role": "assistant",
            "content": None,
            "tool_calls": tool_calls,
            "tool_call_id": "call-1",
        }

        db.archive_and_compact(sid, [{"role": "user", "content": "SUMMARY"}, tail])

        with db._read_ctx() as conn:
            rows = conn.execute(
                "SELECT active, compacted FROM messages "
                "WHERE session_id = ? AND tool_call_id = ? ORDER BY id",
                (sid, "call-1"),
            ).fetchall()
        assert [(row[0], row[1]) for row in rows] == [(0, 0), (1, 0)]

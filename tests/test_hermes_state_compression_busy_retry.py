"""Appends flow freely during compression; the commit preserves them (#75316).

HISTORY: ``append_message`` used to refuse while another writer held the
session's compression lock, with a short busy-wait (#75264 → #75083). That
fenced ordinary transcript writes behind a lease whose real job is stopping
two COMPRESSIONS colliding — turns died as ``session_persistence_failed``
whenever a slow provider summary overlapped an incoming message (#74568,
#77386), and a stale lock from a dead PID blocked writes for the full TTL.

CURRENT CONTRACT (watermark commit): appends never check compression_locks.
``archive_and_compact()`` takes a watermark captured at compression start and
re-sequences every row that arrived after it (the concurrent tail) back into
the live transcript, atomically, instead of archiving it with the snapshot.
The commit itself is holder-fenced: a compression whose lease was lost cannot
publish.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_state import (
    CompressionSessionBusyError,
    SessionCompressionInProgressError,
    SessionDB,
)


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    d = SessionDB(tmp_path / "state.db")
    d.create_session("sess1", source="test")
    return d


def test_append_is_never_blocked_by_a_foreign_compression_lock(db: SessionDB) -> None:
    """The classic race: a steer lands while compression owns the session.

    Old behavior: busy-wait then land (or die on timeout). New behavior: the
    append lands IMMEDIATELY — the watermark commit is what protects it.
    """
    assert db.try_acquire_compression_lock("sess1", "compressor") is True

    started = time.monotonic()
    db.append_message("sess1", role="user", content="steered mid-compression")
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, "append must not wait on a compression lease"
    rows = db.get_messages("sess1")
    assert any(r["content"] == "steered mid-compression" for r in rows)


def test_append_is_never_blocked_by_a_stale_dead_pid_lock(db: SessionDB) -> None:
    """A crashed compressor's unexpired lock must not fence writes (#74568)."""
    assert db.try_acquire_compression_lock(
        "sess1", "pid-9999999-long-gone", ttl_seconds=3600
    ) is True
    started = time.monotonic()
    db.append_message("sess1", role="user", content="lands despite stale lock")
    assert time.monotonic() - started < 0.5
    rows = db.get_messages("sess1")
    assert any(r["content"] == "lands despite stale lock" for r in rows)


def test_the_lock_owner_append_still_works(db: SessionDB) -> None:
    assert db.try_acquire_compression_lock("sess1", "compressor") is True
    db.append_message(
        "sess1",
        role="assistant",
        content="written by the compressor",
        compression_lock_holder="compressor",
    )
    rows = db.get_messages("sess1")
    assert any(r["content"] == "written by the compressor" for r in rows)


def test_transient_error_is_a_subclass_of_the_original(db: SessionDB) -> None:
    """Existing `except CompressionSessionBusyError` handlers must still catch."""
    assert issubclass(SessionCompressionInProgressError, CompressionSessionBusyError)


def test_no_lock_means_no_delay(db: SessionDB) -> None:
    started = time.monotonic()
    db.append_message("sess1", role="user", content="uncontended")
    assert time.monotonic() - started < 0.5


def test_a_lost_compression_lease_still_fails_fast(db: SessionDB) -> None:
    """``publish_compression_child`` with a lost lease is permanent — no retry."""
    started = time.monotonic()
    with pytest.raises(CompressionSessionBusyError):
        db.publish_compression_child(
            parent_session_id="sess1",
            child_session_id="child1",
            source="test",
            messages=[{"role": "user", "content": "compacted"}],
            compression_lock_holder="not-the-holder",
            require_compression_lease=True,
        )
    assert time.monotonic() - started < 0.5, (
        "a lost lease is permanent and must not spend the retry budget"
    )


def test_compression_busy_wait_budget_is_not_capped_by_write_patience(
    db: SessionDB, monkeypatch
) -> None:
    """The compression wait budget must be independent of the write-lock
    deadline (#77386).

    Previously the compression deadline was ``min(now + busy_wait, write_deadline)``,
    which clamped the compression wait to the (shorter) write patience even
    after the budget was raised. Now the compression deadline stands on its
    own, so a long compression wait survives even when the write patience
    would have expired first.
    """
    # Set a tiny compression budget so the test is fast, but set an even
    # tinier write patience to prove the compression budget is NOT capped
    # by it.
    monkeypatch.setattr(SessionDB, "_COMPRESSION_BUSY_WAIT_S", 1.0)
    monkeypatch.setattr(SessionDB, "_TRANSCRIPT_WRITE_PATIENCE_S", 0.3)
    assert db.try_acquire_compression_lock("sess1", "compressor") is True

    started = time.monotonic()
    with pytest.raises(CompressionSessionBusyError):
        db.append_message("sess1", role="user", content="waits past write patience")
    elapsed = time.monotonic() - started

    # If the min() clamp were still in place, the wait would give up at
    # ~0.3 s (write patience). With the fix, it spends the full 1.0 s
    # compression budget.
    assert elapsed >= 0.8, (
        f"compression wait was cut short at {elapsed:.2f}s — "
        "the write-deadline min() clamp may still be present"
    )
    assert elapsed < 10, "did not give up within a bounded time"


def test_compression_busy_wait_default_covers_real_compression_durations(
    db: SessionDB,
) -> None:
    """The default _COMPRESSION_BUSY_WAIT_S must be large enough to cover
    observed real-world compression durations (129 s, 144 s, 148 s reported
    in #77386 and confirmed by @ruizanthony on a live Linux/WebUI install),
    while staying below the 600 s total ceiling.
    """
    assert SessionDB._COMPRESSION_BUSY_WAIT_S >= 300.0, (
        f"_COMPRESSION_BUSY_WAIT_S={SessionDB._COMPRESSION_BUSY_WAIT_S} "
        "must be >= 300 s to cover real LLM-streamed compression durations"
    )
    assert SessionDB._COMPRESSION_BUSY_WAIT_S <= 600.0, (
        f"_COMPRESSION_BUSY_WAIT_S={SessionDB._COMPRESSION_BUSY_WAIT_S} "
        "must be <= 600 s (the compression total ceiling)"
    )

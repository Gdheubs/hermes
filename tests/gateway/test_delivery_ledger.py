"""Tests for the gateway delivery-obligation ledger (gateway/delivery_ledger.py).

State machine, dead-owner claiming, attempts cap, stale cutoff, retention,
id stability, and the startup redelivery sweep's contract:
- pending rows redeliver plainly (send never started, no dup risk)
- attempting/failed rows carry the recovered-reply marker (honest
  at-least-once; ambiguity is labeled, never silently resent)
- rows owned by a LIVE process are never claimed
- poison rows abandon at the attempts cap / stale cutoff
"""

import os
import time
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway import delivery_ledger as dl


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """Isolated state.db per test (autouse HERMES_HOME isolation already
    redirects get_hermes_home; make the redirect explicit and per-test)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")
    yield


def _record(oid="ob-1", session_key="agent:main:slack:channel:C1", **kw):
    dl.record_obligation(
        obligation_id=oid,
        session_key=session_key,
        platform=kw.get("platform", "slack"),
        chat_id=kw.get("chat_id", "C1"),
        thread_id=kw.get("thread_id", "171.001"),
        content=kw.get("content", "the final answer"),
    )


def _row(oid):
    with dl._connect() as conn:
        r = conn.execute(
            """SELECT state, attempts, owner_pid, content
               FROM delivery_obligations WHERE obligation_id=?""",
            (oid,),
        ).fetchone()
    return None if r is None else {
        "state": r[0], "attempts": r[1], "owner_pid": r[2], "content": r[3],
    }


def _blocking_probe():
    """Return a blocking ledger call and an event-loop progress witness."""
    ledger_started = threading.Event()
    event_loop_progressed = threading.Event()
    blocked_event_loop = []

    def _slow_ledger_call(*args, **kwargs):
        ledger_started.set()
        # Generous timeout: a genuinely blocked loop can never set the event
        # (the witness coroutine cannot run), so a longer wait only guards
        # against loaded-CI scheduling flake, not against missing the bug.
        if not event_loop_progressed.wait(timeout=5.0):
            blocked_event_loop.append(True)

    async def _event_loop_witness():
        import asyncio

        deadline = asyncio.get_running_loop().time() + 10
        while not ledger_started.is_set():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("ledger call never started")
            await asyncio.sleep(0)
        event_loop_progressed.set()

    return _slow_ledger_call, _event_loop_witness, blocked_event_loop


def _orphan(oid):
    """Make the row look like it belongs to a dead process."""
    with dl._connect() as conn:
        conn.execute(
            "UPDATE delivery_obligations SET owner_pid=999999999, "
            "owner_started_at=1 WHERE obligation_id=?",
            (oid,),
        )


class TestStateMachine:
    def test_record_starts_pending(self):
        _record()
        assert _row("ob-1")["state"] == "pending"


class TestObligationId:
    def test_stable_and_distinct(self):
        a = dl.compute_obligation_id("sk1", "msg1", "hello")
        assert a == dl.compute_obligation_id("sk1", "msg1", "hello")
        # Different thread (baked into session_key) → different id. This is
        # the cron-topic collision class from the earlier outbox attempt.
        assert a != dl.compute_obligation_id("sk1:threadB", "msg1", "hello")
        assert a != dl.compute_obligation_id("sk1", "msg2", "hello")
        assert a != dl.compute_obligation_id("sk1", "msg1", "other")
        assert len(a) == 24


class TestSweep:
    def test_live_owner_rows_never_claimed(self):
        _record()  # owner = this (live) process
        assert dl.sweep_recoverable() == []

    def test_dead_owner_pending_claimed_without_marker(self):
        _record()
        _orphan("ob-1")
        claimed = dl.sweep_recoverable()
        assert len(claimed) == 1
        assert claimed[0]["needs_marker"] is False
        assert claimed[0]["attempts"] == 1
        # Claim re-stamps ownership: a second sweep in the same (live)
        # process must not double-claim.
        assert dl.sweep_recoverable() == []


class TestPrune:
    def test_old_delivered_rows_pruned(self):
        _record()
        dl.mark_delivered("ob-1")
        with dl._connect() as conn:
            conn.execute(
                "UPDATE delivery_obligations SET updated_at=? WHERE obligation_id=?",
                (time.time() - dl._RETENTION_SECONDS - 60, "ob-1"),
            )
        dl._prune()
        assert _row("ob-1") is None


class TestLedgerEnabled:
    def test_default_on(self):
        assert dl.ledger_enabled({}) is True
        assert dl.ledger_enabled({"gateway": {}}) is True


class TestGatewayRedeliverySweep:
    """Drive the real GatewayRunner._redeliver_pending_obligations."""

    @staticmethod
    def _runner(adapter=None):
        from gateway.config import Platform
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.adapters = {Platform.SLACK: adapter} if adapter else {}
        _store = MagicMock()
        _store.clear_resume_pending = AsyncMock()
        _store._store = None
        runner.session_store = None
        runner._async_session_store = _store
        return runner

    @staticmethod
    def _adapter(success=True):
        adapter = MagicMock()
        adapter.send = AsyncMock(
            return_value=MagicMock(success=success, error="" if success else "nope")
        )
        return adapter

    @pytest.mark.asyncio
    async def test_pending_redelivers_plain_and_clears_resume(self):
        _record()  # pending
        _orphan("ob-1")
        adapter = self._adapter()
        runner = self._runner(adapter)

        n = await runner._redeliver_pending_obligations()

        assert n == 1
        sent = adapter.send.call_args.kwargs
        assert sent["content"] == "the final answer"  # no marker
        assert sent["metadata"] == {"thread_id": "171.001"}
        assert _row("ob-1")["state"] == "delivered"
        runner._async_session_store.clear_resume_pending.assert_awaited_once_with(
            "agent:main:slack:channel:C1"
        )

    @pytest.mark.asyncio
    async def test_attempting_redelivers_with_marker(self):
        _record()
        dl.mark_attempting("ob-1")
        _orphan("ob-1")
        adapter = self._adapter()
        runner = self._runner(adapter)

        await runner._redeliver_pending_obligations()

        sent = adapter.send.call_args.kwargs
        assert sent["content"].startswith(dl.RECOVERED_MARKER)
        assert sent["content"].endswith("the final answer")

    @pytest.mark.parametrize(
        ("send_success", "ledger_method"),
        [(True, "mark_delivered"), (False, "mark_failed")],
    )
    @pytest.mark.asyncio
    async def test_slow_state_update_does_not_block_event_loop(
        self, send_success, ledger_method
    ):
        import asyncio

        _record()
        _orphan("ob-1")
        runner = self._runner(self._adapter(success=send_success))
        slow_update, event_loop_witness, blocked_event_loop = _blocking_probe()

        with patch.object(dl, ledger_method, side_effect=slow_update):
            await asyncio.gather(
                runner._redeliver_pending_obligations(), event_loop_witness()
            )

        assert blocked_event_loop == []


class TestRuntimeFailedSweep:
    """``sweep_failed_for_runtime``: the runtime redelivery path that fires
    after a platform reconnects, without waiting for a restart.

    The startup sweep only claims rows owned by a DEAD process, so a final
    response definitively rejected while this gateway stayed alive (e.g. the
    network outage that also dropped the adapter) would otherwise sit in the
    ledger until the next boot. This sweep claims exactly those rows for a
    platform that just reconnected. The attempts cap and stale cutoff still
    bound poison rows, and a different live process's rows are never stolen.
    """

    def _failed(self, oid="ob-1", **kw):
        _record(oid=oid, **kw)
        dl.mark_failed(oid, "send_path_degraded")

    def test_failed_row_claimed_for_platform_with_marker(self):
        self._failed()

        claimed = dl.sweep_failed_for_runtime("slack")

        assert len(claimed) == 1
        assert claimed[0]["obligation_id"] == "ob-1"
        # A previous rejection means a resend MIGHT duplicate — the marker
        # keeps the at-least-once contract honest (same as the startup path).
        assert claimed[0]["needs_marker"] is True
        assert claimed[0]["attempts"] == 1
        row = _row("ob-1")
        assert row["state"] == "attempting"  # claimed → about to send
        # Claim re-stamps ownership: a second sweep must not double-claim.
        assert dl.sweep_failed_for_runtime("slack") == []

    def test_other_platform_failed_rows_not_claimed(self):
        self._failed(oid="ob-slack", platform="slack")
        self._failed(
            oid="ob-tg", platform="telegram",
            session_key="agent:main:telegram:group:1",
        )

        claimed = dl.sweep_failed_for_runtime("slack")

        assert [c["obligation_id"] for c in claimed] == ["ob-slack"]
        assert _row("ob-tg")["state"] == "failed"

    def test_other_live_process_rows_not_stolen(self):
        """A failed row owned by a DIFFERENT live process is left alone —
        that process owns the retry budget and will redeliver (or abandon)
        it. Only rows owned by ourselves or by a dead process are claimed."""
        self._failed()
        with dl._connect() as conn:
            conn.execute(
                "UPDATE delivery_obligations SET owner_pid=?, owner_started_at=NULL "
                "WHERE obligation_id=?",
                (os.getppid(), "ob-1"),  # our parent is alive for this test
            )

        assert dl.sweep_failed_for_runtime("slack") == []
        assert _row("ob-1")["state"] == "failed"

    def test_rows_over_attempts_cap_abandoned_not_claimed(self):
        self._failed()
        with dl._connect() as conn:
            conn.execute(
                "UPDATE delivery_obligations SET attempts=? WHERE obligation_id=?",
                (dl.MAX_ATTEMPTS, "ob-1"),
            )

        assert dl.sweep_failed_for_runtime("slack") == []
        assert _row("ob-1")["state"] == "abandoned"

    def test_stale_failed_row_abandoned_not_claimed(self):
        self._failed()
        with dl._connect() as conn:
            conn.execute(
                "UPDATE delivery_obligations SET created_at=? WHERE obligation_id=?",
                (time.time() - dl.STALE_AFTER_SECONDS - 60, "ob-1"),
            )

        assert dl.sweep_failed_for_runtime("slack") == []
        assert _row("ob-1")["state"] == "abandoned"


class TestRuntimeFailedRedelivery:
    """Drive GatewayRunner._redeliver_failed_obligations_for_platform — the
    runtime counterpart of the startup sweep, fired when a platform
    reconnects after an outage. Uses the same real ledger + real adapter
    harness as TestGatewayRedeliverySweep."""

    @staticmethod
    def _runner(adapter=None):
        from gateway.config import Platform
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.adapters = {Platform.SLACK: adapter} if adapter else {}
        _store = MagicMock()
        _store.clear_resume_pending = AsyncMock()
        _store._store = None
        runner.session_store = None
        runner._async_session_store = _store
        return runner

    @staticmethod
    def _adapter(success=True):
        adapter = MagicMock()
        adapter.send = AsyncMock(
            return_value=MagicMock(success=success, error="" if success else "nope")
        )
        return adapter

    @pytest.mark.asyncio
    async def test_failed_row_redelivered_with_marker(self):
        from gateway.config import Platform

        _record()  # slack, pending
        dl.mark_failed("ob-1", "send_path_degraded")
        adapter = self._adapter()
        runner = self._runner(adapter)

        n = await runner._redeliver_failed_obligations_for_platform(Platform.SLACK)

        assert n == 1
        sent = adapter.send.call_args.kwargs
        assert sent["content"].startswith(dl.RECOVERED_MARKER)
        assert sent["content"].endswith("the final answer")
        assert _row("ob-1")["state"] == "delivered"
        runner._async_session_store.clear_resume_pending.assert_awaited_once_with(
            "agent:main:slack:channel:C1"
        )

    @pytest.mark.asyncio
    async def test_failed_send_marks_failed_again_without_crash(self):
        from gateway.config import Platform

        _record()
        dl.mark_failed("ob-1", "send_path_degraded")
        adapter = self._adapter(success=False)
        runner = self._runner(adapter)

        n = await runner._redeliver_failed_obligations_for_platform(Platform.SLACK)

        assert n == 0
        assert _row("ob-1")["state"] == "failed"

    @pytest.mark.asyncio
    async def test_other_platform_failed_rows_not_redelivered(self):
        from gateway.config import Platform

        _record(oid="ob-slack", platform="slack")
        _record(
            oid="ob-tg", platform="telegram",
            session_key="agent:main:telegram:group:1",
        )
        dl.mark_failed("ob-slack", "x")
        dl.mark_failed("ob-tg", "x")
        adapter = self._adapter()
        runner = self._runner(adapter)  # only the slack adapter is connected

        n = await runner._redeliver_failed_obligations_for_platform(Platform.SLACK)

        assert n == 1
        assert _row("ob-slack")["state"] == "delivered"
        assert _row("ob-tg")["state"] == "failed"


class TestAttemptsOnlySpentOnRealSends:
    """``attempts`` is the redelivery budget — it must buy a send.

    ``self.adapters`` only holds a platform after its ``connect()`` succeeded,
    and the sweep claimed every dead-owner row regardless. A platform that
    failed to connect this boot therefore burned one attempt per boot while
    the caller's ``adapter is None`` branch skipped it without sending — so
    after MAX_ATTEMPTS boots the row abandoned having never been sent once,
    losing exactly the response the ledger exists to guarantee. That failure
    correlates with the crash that created the obligation: the network
    trouble that killed the send tends to still be there on the next boot.
    """

    def test_absent_platform_does_not_burn_attempts(self):
        _record(platform="telegram")
        dl.mark_attempting("ob-1")

        for _ in range(dl.MAX_ATTEMPTS + 2):
            _orphan("ob-1")
            assert dl.sweep_recoverable(deliverable_platforms={"discord"}) == []

        row = dl.debug_rows()
        assert "abandoned" not in row
        with dl._connect() as conn:
            state, attempts = conn.execute(
                "SELECT state, attempts FROM delivery_obligations "
                "WHERE obligation_id=?", ("ob-1",),
            ).fetchone()
        assert attempts == 0, "an unsendable boot must not spend the budget"
        assert state == "attempting"

    def test_row_still_delivers_once_its_platform_returns(self):
        _record(platform="telegram")
        for _ in range(dl.MAX_ATTEMPTS + 2):
            _orphan("ob-1")
            dl.sweep_recoverable(deliverable_platforms={"discord"})

        _orphan("ob-1")
        claimed = dl.sweep_recoverable(deliverable_platforms={"telegram"})
        assert len(claimed) == 1
        assert claimed[0]["attempts"] == 1


class TestUnconnectedPlatformKeepsItsBudget:
    """End-to-end through the real runner: boots where the platform failed to
    connect must not consume the row's redelivery budget."""

    @staticmethod
    def _runner_without_slack():
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.adapters = {}  # slack failed to connect this boot
        _store = MagicMock()
        _store.clear_resume_pending = AsyncMock()
        _store._store = None
        runner.session_store = None
        runner._async_session_store = _store
        return runner

    @pytest.mark.asyncio
    async def test_row_survives_boots_where_its_platform_is_down(self):
        _record(platform="slack")
        dl.mark_attempting("ob-1")

        for _ in range(dl.MAX_ATTEMPTS + 1):
            _orphan("ob-1")
            runner = self._runner_without_slack()
            assert await runner._redeliver_pending_obligations() == 0

        assert _row("ob-1")["state"] != "abandoned", (
            "the obligation was abandoned without a single send being attempted"
        )
        assert _row("ob-1")["attempts"] == 0



class TestOwnerAlivePidProbe:
    """_owner_alive's no-start-time fallback must route through
    gateway.status._pid_exists, never a raw ``os.kill(pid, 0)`` probe.

    On Windows ``os.kill(pid, 0)`` is NOT a no-op: CPython maps sig=0 to
    ``GenerateConsoleCtrlEvent(0, pid)`` (bpo-14484), so probing a LIVE pid
    whose start time psutil could not read would Ctrl+C its console group.
    Pattern per the windows-native-support reference: patch
    ``gateway.status._pid_exists``, not ``os.kill``.
    """

    def _no_start_time(self, monkeypatch):
        from gateway import status

        monkeypatch.setattr(status, "get_process_start_time", lambda pid: None)

    def test_alive_when_pid_exists(self, monkeypatch):
        from gateway import status

        self._no_start_time(monkeypatch)
        monkeypatch.setattr(status, "_pid_exists", lambda pid: True)
        assert dl._owner_alive(12345, 999) is True

    def test_dead_when_pid_gone(self, monkeypatch):
        from gateway import status

        self._no_start_time(monkeypatch)
        monkeypatch.setattr(status, "_pid_exists", lambda pid: False)
        assert dl._owner_alive(12345, 999) is False

    def test_raw_os_kill_probe_never_used(self, monkeypatch):
        """Regression guard: the probe must not touch os.kill when
        gateway.status._pid_exists is importable (i.e. always in-tree)."""
        from gateway import status

        self._no_start_time(monkeypatch)
        calls = []
        monkeypatch.setattr(status, "_pid_exists", lambda pid: calls.append(pid) or True)
        monkeypatch.setattr(
            dl.os, "kill", lambda *a, **k: (_ for _ in ()).throw(AssertionError("raw os.kill probe used"))
        )
        assert dl._owner_alive(4242, 999) is True
        assert calls == [4242]

    def test_probe_exception_means_dead(self, monkeypatch):
        from gateway import status

        self._no_start_time(monkeypatch)

        def boom(pid):
            raise RuntimeError("probe blew up")

        monkeypatch.setattr(status, "_pid_exists", boom)
        assert dl._owner_alive(12345, 999) is False

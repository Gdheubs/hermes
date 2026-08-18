from hermes_cli.resource_guard import ProcessMemoryGuard, ResourceGuardSettings


def test_hard_limit_captures_evidence_and_calls_restart_once(monkeypatch):
    snapshot = {
        "rss_bytes": 9 * 1024 * 1024,
        "descendant_rss_bytes": 0,
        "descendant_count": 0,
    }
    callbacks = []
    evidence = []
    monkeypatch.setattr(
        "hermes_cli.resource_guard.collect_process_memory_snapshot",
        lambda _metrics: dict(snapshot),
    )
    monkeypatch.setattr(
        "hermes_cli.resource_guard._append_telemetry", lambda row: None
    )
    monkeypatch.setattr(
        "hermes_cli.resource_guard._write_evidence_snapshot",
        lambda row, reason: evidence.append(reason) or None,
    )
    guard = ProcessMemoryGuard(
        component="test",
        settings=ResourceGuardSettings(
            poll_seconds=15,
            telemetry_seconds=60,
            warn_rss_mb=2,
            snapshot_rss_mb=4,
            hard_rss_mb=8,
            descendant_warn_rss_mb=8,
            descendant_hard_rss_mb=24,
            snapshot_cooldown_seconds=300,
        ),
        on_hard_limit=lambda row: callbacks.append(row),
    )

    guard._sample()
    guard._sample()

    assert callbacks == [callbacks[0]]
    assert "hard-parent" in evidence


def test_descendant_hard_limit_is_independent_of_parent_rss(monkeypatch):
    snapshot = {
        "rss_bytes": 1 * 1024 * 1024,
        "descendant_rss_bytes": 25 * 1024 * 1024,
        "descendant_count": 2,
    }
    callbacks = []
    monkeypatch.setattr(
        "hermes_cli.resource_guard.collect_process_memory_snapshot",
        lambda _metrics: dict(snapshot),
    )
    monkeypatch.setattr(
        "hermes_cli.resource_guard._append_telemetry", lambda row: None
    )
    monkeypatch.setattr(
        "hermes_cli.resource_guard._write_evidence_snapshot", lambda row, reason: None
    )
    guard = ProcessMemoryGuard(
        component="test",
        settings=ResourceGuardSettings(
            warn_rss_mb=2,
            snapshot_rss_mb=4,
            hard_rss_mb=8,
            descendant_warn_rss_mb=8,
            descendant_hard_rss_mb=24,
        ),
        on_hard_limit=lambda row: callbacks.append(row),
    )

    guard._sample()
    guard._sample()

    assert len(callbacks) == 1
    assert callbacks[0]["descendant_rss_bytes"] == 25 * 1024 * 1024


def test_single_transient_spike_does_not_fire_hard_limit(monkeypatch):
    """A lone over-cap sample must not fire; the guard requires consecutive
    confirmations so a transient allocation spike cannot trigger a restart."""
    snapshot = {
        "rss_bytes": 9 * 1024 * 1024,
        "descendant_rss_bytes": 0,
        "descendant_count": 0,
    }
    callbacks = []
    monkeypatch.setattr(
        "hermes_cli.resource_guard.collect_process_memory_snapshot",
        lambda _metrics: dict(snapshot),
    )
    monkeypatch.setattr(
        "hermes_cli.resource_guard._append_telemetry", lambda row: None
    )
    monkeypatch.setattr(
        "hermes_cli.resource_guard._write_evidence_snapshot", lambda row, reason: None
    )
    guard = ProcessMemoryGuard(
        component="test",
        settings=ResourceGuardSettings(
            warn_rss_mb=2,
            snapshot_rss_mb=4,
            hard_rss_mb=8,
            descendant_warn_rss_mb=8,
            descendant_hard_rss_mb=24,
            hard_limit_confirmations=3,
        ),
        on_hard_limit=lambda row: callbacks.append(row),
    )

    guard._sample()
    guard._sample()
    assert callbacks == []

    guard._sample()
    assert len(callbacks) == 1


def test_recovery_resets_hard_limit_debounce(monkeypatch):
    """A sample back under the cap resets the confirmation counter."""
    state = {
        "rss_bytes": 9 * 1024 * 1024,
        "descendant_rss_bytes": 0,
        "descendant_count": 0,
    }
    callbacks = []
    monkeypatch.setattr(
        "hermes_cli.resource_guard.collect_process_memory_snapshot",
        lambda _metrics: dict(state),
    )
    monkeypatch.setattr(
        "hermes_cli.resource_guard._append_telemetry", lambda row: None
    )
    monkeypatch.setattr(
        "hermes_cli.resource_guard._write_evidence_snapshot", lambda row, reason: None
    )
    guard = ProcessMemoryGuard(
        component="test",
        settings=ResourceGuardSettings(
            warn_rss_mb=2,
            snapshot_rss_mb=4,
            hard_rss_mb=8,
            descendant_warn_rss_mb=8,
            descendant_hard_rss_mb=24,
            hard_limit_confirmations=2,
        ),
        on_hard_limit=lambda row: callbacks.append(row),
    )

    guard._sample()  # over cap -> violations = 1
    state["rss_bytes"] = 1 * 1024 * 1024  # recover
    guard._sample()  # under cap -> violations = 0
    state["rss_bytes"] = 9 * 1024 * 1024  # over cap again
    guard._sample()  # violations = 1
    assert callbacks == []
    guard._sample()  # violations = 2 -> fires
    assert len(callbacks) == 1


def test_telemetry_is_opt_in(monkeypatch):
    """Telemetry rows are written only when telemetry_enabled is set."""
    snapshot = {
        "rss_bytes": 1 * 1024 * 1024,
        "descendant_rss_bytes": 0,
        "descendant_count": 0,
    }
    rows = []
    monkeypatch.setattr(
        "hermes_cli.resource_guard.collect_process_memory_snapshot",
        lambda _metrics: dict(snapshot),
    )
    monkeypatch.setattr(
        "hermes_cli.resource_guard._append_telemetry", lambda row: rows.append(row)
    )
    monkeypatch.setattr(
        "hermes_cli.resource_guard._write_evidence_snapshot", lambda row, reason: None
    )

    off = ProcessMemoryGuard(
        component="test",
        settings=ResourceGuardSettings(
            warn_rss_mb=2,
            snapshot_rss_mb=4,
            hard_rss_mb=8,
            descendant_warn_rss_mb=8,
            descendant_hard_rss_mb=24,
            telemetry_enabled=False,
            telemetry_seconds=0,
        ),
        on_hard_limit=lambda row: None,
    )
    off._sample()
    assert rows == []

    on = ProcessMemoryGuard(
        component="test",
        settings=ResourceGuardSettings(
            warn_rss_mb=2,
            snapshot_rss_mb=4,
            hard_rss_mb=8,
            descendant_warn_rss_mb=8,
            descendant_hard_rss_mb=24,
            telemetry_enabled=True,
            telemetry_seconds=0,
        ),
        on_hard_limit=lambda row: None,
    )
    on._sample()
    assert len(rows) == 1

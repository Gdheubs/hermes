"""Tests for gateway.shutdown_forensics — fast snapshot + async diag spawn."""

from __future__ import annotations

import io
import json
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import shutdown_forensics as sf


# ---------------------------------------------------------------------------
# _signal_name
# ---------------------------------------------------------------------------

class TestSignalName:

    def test_unknown_int_returns_signal_num_token(self):
        # Pick an integer extremely unlikely to ever be a real signal alias
        assert sf._signal_name(9999) == "signal#9999"


# ---------------------------------------------------------------------------
# snapshot_shutdown_context
# ---------------------------------------------------------------------------

class TestSnapshotShutdownContext:

    def test_handles_none_signal(self):
        ctx = sf.snapshot_shutdown_context(None)
        assert ctx["signal"] == "UNKNOWN"
        assert ctx["signal_num"] is None

    def test_includes_timestamps(self):
        before = time.time()
        ctx = sf.snapshot_shutdown_context(signal.SIGTERM)
        after = time.time()
        assert before <= ctx["ts"] <= after
        assert isinstance(ctx["ts_monotonic"], float)


    def test_under_systemd_false_without_invocation_id_and_normal_ppid(
        self, monkeypatch
    ):
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        # We can't actually change ppid; skip if we happen to be reaped
        # by init (e.g. running under tini).
        if os.getppid() == 1:
            pytest.skip("test process is reaped by init")
        ctx = sf.snapshot_shutdown_context(signal.SIGTERM)
        assert ctx["under_systemd"] is False


    def test_detects_takeover_marker_for_self(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        marker = tmp_path / ".gateway-takeover.json"
        marker.write_text(
            f'{{"target_pid": {os.getpid()}, "replacer_pid": 99999}}',
            encoding="utf-8",
        )
        ctx = sf.snapshot_shutdown_context(signal.SIGTERM)
        assert "takeover_marker" in ctx
        assert ctx["takeover_marker_for_self"] is True


# ---------------------------------------------------------------------------
# format_context_for_log / context_as_json
# ---------------------------------------------------------------------------

class TestFormatters:


    def test_context_as_json_handles_unserialisable_values(self):
        ctx = {"signal": "SIGTERM", "weird": object()}
        payload = sf.context_as_json(ctx)
        # default=str means objects get repr'd, JSON stays valid
        decoded = json.loads(payload)
        assert decoded["signal"] == "SIGTERM"
        assert "weird" in decoded


# ---------------------------------------------------------------------------
# spawn_async_diagnostic
# ---------------------------------------------------------------------------

class TestSpawnAsyncDiagnostic:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only diagnostic")
    def test_spawns_subprocess_and_writes_output(self, tmp_path):
        log_path = tmp_path / "diag.log"
        pid = sf.spawn_async_diagnostic(log_path, "SIGTERM", timeout_seconds=3.0)
        assert pid is not None and pid > 0

        # Wait briefly for the subprocess to write — bounded by its own timeout.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if log_path.exists() and log_path.stat().st_size > 0:
                # Wait a touch longer for the script to finish writing
                time.sleep(0.2)
                break
            time.sleep(0.1)

        # Reap the subprocess so it doesn't show up as a zombie.
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, OSError):
            pass

        assert log_path.exists()
        contents = log_path.read_text(encoding="utf-8", errors="replace")
        assert "shutdown diagnostic" in contents
        assert "SIGTERM" in contents


# ---------------------------------------------------------------------------
# _parse_systemd_duration_to_us
# ---------------------------------------------------------------------------

class TestParseSystemdDuration:
    def test_seconds(self):
        assert sf._parse_systemd_duration_to_us("90s") == 90 * 1_000_000

    def test_minutes(self):
        assert sf._parse_systemd_duration_to_us("3min") == 180 * 1_000_000


# ---------------------------------------------------------------------------
# check_systemd_timing_alignment
# ---------------------------------------------------------------------------

class TestCheckSystemdTimingAlignment:

    def test_returns_none_when_unit_undeterminable(self, monkeypatch):
        monkeypatch.setenv("INVOCATION_ID", "abc")
        # /proc/self/cgroup likely doesn't end in .service for the test runner
        result = sf.check_systemd_timing_alignment(180.0)
        # Either None (we couldn't find a unit) or a dict with mismatch info
        # for whatever unit pytest IS in.  Both are valid; we just ensure
        # the function doesn't raise.
        assert result is None or isinstance(result, dict)


class TestCheckSystemdTimingAlignmentScope:
    """Scope detection: query the manager that actually owns the unit.

    Regression test for #85117 — `systemctl --user show` on a unit unknown
    to the user manager returns rc=0 with the user manager's own
    DefaultTimeoutStopUSec (typically 90s) instead of failing, so the old
    "try --user first, trust rc=0" logic produced false "stale unit" warnings
    on system-level installs (hermes gateway install --system).
    """

    SYSTEM_CGROUP = "0::/system.slice/hermes-gateway.service"
    USER_CGROUP = (
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
        "hermes-gateway.service"
    )

    @staticmethod
    def _stub_cgroup(monkeypatch, path: str) -> None:
        real_open = open

        def fake_open(file, *a, **kw):
            if file == "/proc/self/cgroup":
                return io.StringIO(path + "\n")
            return real_open(file, *a, **kw)

        monkeypatch.setattr("builtins.open", fake_open)

    @staticmethod
    def _stub_systemctl(monkeypatch, by_scope):
        calls = []

        def fake_run(cmd, **kwargs):
            scope = "user" if "--user" in cmd else "system"
            calls.append(scope)
            return by_scope[scope]

        monkeypatch.setattr(sf.subprocess, "run", fake_run)
        return calls

    def test_system_install_queries_system_scope_first(self, monkeypatch):
        # Issue #85117 scenario: unit installed with `--system`, running under
        # /system.slice/.  The system manager holds the real value (210s);
        # the user manager would answer rc=0 with its 90s default.
        monkeypatch.setenv("INVOCATION_ID", "abc123")
        self._stub_cgroup(monkeypatch, self.SYSTEM_CGROUP)
        calls = self._stub_systemctl(monkeypatch, {
            "system": SimpleNamespace(
                returncode=0, stdout="LoadState=loaded\nTimeoutStopUSec=3min 30s\n"
            ),
            "user": SimpleNamespace(
                returncode=0, stdout="LoadState=loaded\nTimeoutStopUSec=90s\n"
            ),
        })
        result = sf.check_systemd_timing_alignment(180.0)
        # System scope consulted first and used; user manager never queried.
        assert calls == ["system"]
        assert result is not None
        assert result["unit"] == "hermes-gateway.service"
        assert result["timeout_stop_sec"] == 210.0
        assert result["mismatch"] is False  # 210 >= 180 + 30 headroom

    def test_user_install_queries_user_scope_first(self, monkeypatch):
        # The common case must keep working: a user-scope unit is read from
        # the user manager without touching the system manager.
        monkeypatch.setenv("INVOCATION_ID", "abc123")
        self._stub_cgroup(monkeypatch, self.USER_CGROUP)
        calls = self._stub_systemctl(monkeypatch, {
            "user": SimpleNamespace(
                returncode=0, stdout="LoadState=loaded\nTimeoutStopUSec=90s\n"
            ),
            "system": SimpleNamespace(
                returncode=0, stdout="LoadState=loaded\nTimeoutStopUSec=90s\n"
            ),
        })
        result = sf.check_systemd_timing_alignment(60.0)
        assert calls == ["user"]
        assert result is not None
        assert result["timeout_stop_sec"] == 90.0
        assert result["mismatch"] is False  # 90 >= 60 + 30

    def test_falls_back_when_primary_scope_does_not_own_unit(self, monkeypatch):
        # If the primary scope reports LoadState=not-found (rc=0 but the unit
        # isn't owned there), the other scope must be tried instead of
        # trusting the manager default.
        monkeypatch.setenv("INVOCATION_ID", "abc123")
        self._stub_cgroup(monkeypatch, self.USER_CGROUP)
        calls = self._stub_systemctl(monkeypatch, {
            "user": SimpleNamespace(
                returncode=0, stdout="LoadState=not-found\nTimeoutStopUSec=90s\n"
            ),
            "system": SimpleNamespace(
                returncode=0, stdout="LoadState=loaded\nTimeoutStopUSec=210s\n"
            ),
        })
        result = sf.check_systemd_timing_alignment(180.0)
        assert calls == ["user", "system"]
        assert result is not None
        assert result["timeout_stop_sec"] == 210.0
        assert result["mismatch"] is False

    def test_reports_mismatch_when_system_timeout_below_drain(self, monkeypatch):
        # A genuine mismatch in system scope must still be reported.
        monkeypatch.setenv("INVOCATION_ID", "abc123")
        self._stub_cgroup(monkeypatch, self.SYSTEM_CGROUP)
        calls = self._stub_systemctl(monkeypatch, {
            "system": SimpleNamespace(
                returncode=0, stdout="LoadState=loaded\nTimeoutStopUSec=90s\n"
            ),
            "user": SimpleNamespace(
                returncode=0, stdout="LoadState=loaded\nTimeoutStopUSec=90s\n"
            ),
        })
        result = sf.check_systemd_timing_alignment(180.0)
        assert calls == ["system"]
        assert result is not None
        assert result["mismatch"] is True  # 90 < 180 + 30

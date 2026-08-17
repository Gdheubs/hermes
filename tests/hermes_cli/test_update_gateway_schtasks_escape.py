"""Tests for the Windows gateway post-update job-object escape (issue #84185).

The bug: ``_cold_start_windows_gateway_after_update`` and
``_spawn_gateway_restart_watcher`` both spawn the gateway via
``subprocess.Popen`` + ``CREATE_BREAKAWAY_FROM_JOB``, which CreateProcess
accepts silently even when the parent job denies breakaway. The spawned
child lands inside the parent's job and is hard-killed when the updater
exits. The printed ✓ is therefore a lie.

The fix routes both spawn points through the Scheduled Task when one is
registered — ``schtasks /Run`` goes through the Task Scheduler service and
is never a child of any job containing the updater. Falls back to the
direct spawn only when no Scheduled Task exists, and even then reports
survival honestly.

Follow-up (issue #84185 review): the task is re-registered before /Run so it
never replays a stale Python path from task-creation time, and the post-
trigger poll checks only for NEW gateway PIDs (not one that was already
running) so a pre-update gateway draining in the background does not satisfy
the check on its own.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

MODULE_UPDATE_CMD = "hermes_cli.update_cmd"

@pytest.fixture
def cold_start_mocks(monkeypatch):
    """Patch the three gateway_windows helpers the cold-start path uses.

    Returns a namespace with the three mocks. The low-level spawn helpers
    on ``hermes_cli.gateway_windows`` are patched directly (same pattern
    as ``tests/hermes_cli/test_gateway_windows.py``);
    ``hermes_cli.gateway.find_gateway_pids`` is stubbed to report nothing
    running; and ``update_cmd._m`` is patched to report Windows.
    """
    from hermes_cli import gateway, gateway_windows, update_cmd

    m_main = MagicMock(name="hermes_cli.main")
    m_main._is_windows.return_value = True

    spawn_via_schtasks = MagicMock(name="_spawn_via_scheduled_task", return_value=False)
    spawn_detached = MagicMock(name="_spawn_detached", return_value=0)
    wait_for_ready = MagicMock(name="_wait_for_gateway_ready", return_value=[])

    monkeypatch.setattr(gateway_windows, "_spawn_via_scheduled_task", spawn_via_schtasks)
    monkeypatch.setattr(gateway_windows, "_spawn_detached", spawn_detached)
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", wait_for_ready)
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kw: [])
    monkeypatch.setattr(update_cmd, "_m", lambda: m_main)

    class _NS:
        pass

    ns = _NS()
    ns.spawn_via_schtasks = spawn_via_schtasks
    ns.spawn_detached = spawn_detached
    ns.wait_for_ready = wait_for_ready
    return ns

class TestColdStartEscape:
    """_cold_start_windows_gateway_after_update must prefer the Scheduled Task."""

    def test_cold_start_via_scheduled_task_when_task_exists(
        self, capsys, cold_start_mocks
    ):
        """schtasks path spawns and survives → prints task-based ✓ and returns."""
        from hermes_cli import update_cmd

        cold_start_mocks.spawn_via_schtasks.return_value = True
        update_cmd._cold_start_windows_gateway_after_update()

        out = capsys.readouterr().out
        cold_start_mocks.spawn_via_schtasks.assert_called_once()
        cold_start_mocks.spawn_detached.assert_not_called()
        assert "Scheduled Task" in out
        assert "✓" in out
        assert "did not survive" not in out

    def test_cold_start_falls_back_to_spawn_detached_when_no_task(
        self, capsys, cold_start_mocks
    ):
        """No Scheduled Task registered → fall back to _spawn_detached + survival check."""
        from hermes_cli import update_cmd

        cold_start_mocks.spawn_via_schtasks.return_value = False
        cold_start_mocks.spawn_detached.return_value = 54321
        cold_start_mocks.wait_for_ready.return_value = [54321]
        update_cmd._cold_start_windows_gateway_after_update()

        out = capsys.readouterr().out
        cold_start_mocks.spawn_via_schtasks.assert_called_once()
        cold_start_mocks.spawn_detached.assert_called_once()
        cold_start_mocks.wait_for_ready.assert_called_once()
        assert "54321" in out
        assert "did not survive" not in out

    def test_cold_start_reports_failure_when_spawn_does_not_survive(
        self, capsys, cold_start_mocks
    ):
        """Direct spawn returns a PID but the gateway never comes up → ✗, no ✓."""
        from hermes_cli import update_cmd

        cold_start_mocks.spawn_via_schtasks.return_value = False
        cold_start_mocks.spawn_detached.return_value = 54321
        cold_start_mocks.wait_for_ready.return_value = []
        update_cmd._cold_start_windows_gateway_after_update()

        out = capsys.readouterr().out
        cold_start_mocks.spawn_detached.assert_called_once()
        cold_start_mocks.wait_for_ready.assert_called_once()
        assert "did not survive" in out
        assert "hermes gateway start" in out
        assert "✓ Starting Windows gateway after update" not in out

class TestSpawnViaScheduledTaskHelper:
    """_spawn_via_scheduled_task returns False unless a NEW gateway actually shows up."""

    def test_returns_false_when_no_task_registered(self, monkeypatch):
        from hermes_cli import gateway_windows

        monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
        monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: False)
        exec_mock = MagicMock()
        monkeypatch.setattr(gateway_windows, "_exec_schtasks", exec_mock)
        assert gateway_windows._spawn_via_scheduled_task() is False
        exec_mock.assert_not_called()

    def test_returns_false_when_schtasks_run_fails(self, monkeypatch):
        from hermes_cli import gateway_windows

        wait_mock = MagicMock(return_value=[])
        write_mock = MagicMock(return_value=Path("/fake/script.cmd"))
        install_mock = MagicMock(return_value=(True, "created"))
        monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
        monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: True)
        monkeypatch.setattr(gateway_windows, "_write_task_script", write_mock)
        monkeypatch.setattr(gateway_windows, "_install_scheduled_task", install_mock)
        monkeypatch.setattr(
            gateway_windows, "_exec_schtasks", lambda *a, **kw: (1, "", "error")
        )
        monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", wait_mock)
        assert gateway_windows._spawn_via_scheduled_task() is False
        write_mock.assert_called_once()
        install_mock.assert_called_once()
        wait_mock.assert_not_called()  # didn't even wait — run failed

    def test_returns_false_when_task_triggered_but_no_new_pid_appears(self, monkeypatch):
        from hermes_cli import gateway, gateway_windows

        write_mock = MagicMock(return_value=Path("/fake/script.cmd"))
        install_mock = MagicMock(return_value=(True, "created"))
        monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
        monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: True)
        monkeypatch.setattr(gateway_windows, "_write_task_script", write_mock)
        monkeypatch.setattr(gateway_windows, "_install_scheduled_task", install_mock)
        monkeypatch.setattr(
            gateway_windows, "_exec_schtasks", lambda *a, **kw: (0, "", "")
        )
        monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", lambda **kw: [])
        # find_gateway_pids returns empty set before and after
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kw: [])
        assert gateway_windows._spawn_via_scheduled_task() is False

    def test_returns_true_when_task_triggered_and_new_pid_appears(self, monkeypatch):
        from hermes_cli import gateway, gateway_windows

        write_mock = MagicMock(return_value=Path("/fake/script.cmd"))
        install_mock = MagicMock(return_value=(True, "created"))
        monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
        monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: True)
        monkeypatch.setattr(gateway_windows, "_write_task_script", write_mock)
        monkeypatch.setattr(gateway_windows, "_install_scheduled_task", install_mock)
        monkeypatch.setattr(
            gateway_windows, "_exec_schtasks", lambda *a, **kw: (0, "", "")
        )
        # Pre-trigger: no gateway; post-trigger: PID 12345 appears (new).
        wait_mock = MagicMock(return_value=[12345])
        monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", wait_mock)
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kw: [])
        assert gateway_windows._spawn_via_scheduled_task() is True
        write_mock.assert_called_once()
        install_mock.assert_called_once()

    def test_returns_false_when_only_preexisting_gateway_detected(self, monkeypatch):
        """The pre-update gateway still draining must NOT satisfy the check."""
        from hermes_cli import gateway, gateway_windows

        write_mock = MagicMock(return_value=Path("/fake/script.cmd"))
        install_mock = MagicMock(return_value=(True, "created"))
        monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
        monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: True)
        monkeypatch.setattr(gateway_windows, "_write_task_script", write_mock)
        monkeypatch.setattr(gateway_windows, "_install_scheduled_task", install_mock)
        monkeypatch.setattr(
            gateway_windows, "_exec_schtasks", lambda *a, **kw: (0, "", "")
        )
        # find_gateway_pids returns the SAME PID before and after → no new gateway.
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kw: [9999])
        wait_mock = MagicMock(return_value=[9999])
        monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", wait_mock)
        assert gateway_windows._spawn_via_scheduled_task() is False

    def test_returns_false_when_script_write_fails(self, monkeypatch):
        """If _write_task_script fails, _spawn_via_scheduled_task must bail."""
        from hermes_cli import gateway_windows

        monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
        monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: True)
        monkeypatch.setattr(
            gateway_windows, "_write_task_script", MagicMock(side_effect=OSError("disk full"))
        )
        assert gateway_windows._spawn_via_scheduled_task() is False

    def test_returns_false_when_task_registration_fails(self, monkeypatch):
        """If _install_scheduled_task fails, _spawn_via_scheduled_task must bail."""
        from hermes_cli import gateway_windows

        monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
        monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: True)
        monkeypatch.setattr(
            gateway_windows, "_write_task_script", MagicMock(return_value=Path("/fake/script.cmd"))
        )
        monkeypatch.setattr(
            gateway_windows, "_install_scheduled_task", MagicMock(return_value=(False, "error"))
        )
        assert gateway_windows._spawn_via_scheduled_task() is False


class TestWatcherSchtasksBlock:
    """The watcher inline script must refresh the task and check for NEW pids."""

    def test_watcher_script_contains_task_refresh_and_pid_snapshot(self):
        """The embedded watcher script refreshes the task scripts and snapshots
        pre-existing gateway PIDs so it only counts NEW processes."""
        from hermes_cli import gateway

        # The watcher is a textwrap.dedent string frozen inside the function.
        # We verify its contents by calling _spawn_gateway_restart_watcher
        # and checking the generated argv.  Since we can't easily capture the
        # internal watcher source without spawning a process, we instead
        # assert on the module-level source by extracting the script via
        # a controlled invocation on a non-Windows platform (no-op).
        # For a content-level check, read the raw function source.
        import inspect
        source = inspect.getsource(gateway._spawn_gateway_restart_watcher)

        # 1. Task scripts must be refreshed before triggering.
        assert "_write_task_script" in source
        assert "_install_scheduled_task" in source

        # 2. Pre-existing PIDs must be snapshotted before the poll.
        assert "_pre_pids = set(_fgp())" in source
        assert "_new = set(_fgp()) - _pre_pids" in source
        assert "_started_via_task = _ok" in source

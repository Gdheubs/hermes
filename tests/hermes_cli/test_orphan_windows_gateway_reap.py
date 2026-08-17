"""Windows gateway-orphan reaper tests (#87278 review point 3).

The gateway reaper must match the TUI node reaper's safety model:
- scan-time (create_time, exe) identity bound to the process_iter snapshot,
- PID-reuse-aware orphan check (child create time handed to _is_alive_parent),
- identity revalidated immediately before ``taskkill /T /F``.
"""

from unittest.mock import patch

from hermes_cli import dashboard_procs as dp


GW_CMD = "C:\\x\\python.exe -m hermes_cli.main gateway run --replace"
IDENT = (1000.0, "c:\\x\\python.exe")


class _FakeProc:
    def __init__(self, info):
        self.info = info


def _fake_process_iter(entries):
    def iter_proc(_attrs):
        for e in entries:
            yield _FakeProc(e)

    return iter_proc


# --- Scanner -------------------------------------------------------------------


def test_scan_windows_gateway_returns_identity_triples():
    import psutil

    entries = [
        {"pid": 111, "name": "python.exe", "cmdline": ["python.exe", "-m", "hermes_cli.main", "gateway", "run", "--replace"], "create_time": 1000.0, "exe": "C:\\x\\python.exe"},
        {"pid": 222, "name": "pythonw.exe", "cmdline": ["pythonw.exe", "-m", "hermes_cli.main", "gateway", "run", "--replace"], "create_time": 2000.0, "exe": "C:\\x\\pythonw.exe"},
        {"pid": 333, "name": "python.exe", "cmdline": ["python.exe", "-m", "hermes_cli.main", "gateway", "status"], "create_time": 3000.0, "exe": "C:\\x\\python.exe"},  # not run --replace
        {"pid": 444, "name": "node.exe", "cmdline": ["node.exe", "-m", "hermes_cli.main", "gateway", "run", "--replace"], "create_time": 4000.0, "exe": "C:\\x\\node.exe"},  # not python
        {"pid": 555, "name": "python.exe", "cmdline": ["python.exe", "-m", "hermes_cli.main", "gateway", "run", "--replace"], "create_time": None, "exe": None},  # identity unavailable
    ]
    with patch.object(psutil, "process_iter", _fake_process_iter(entries)), patch(
        "sys.platform", "win32"
    ):
        found = dp._scan_windows_gateway_processes(exclude_pids=set())

    by_pid = {pid: (cmd, ident) for pid, cmd, ident in found}
    assert sorted(by_pid) == [111, 222, 555]
    assert by_pid[111][1] == (1000.0, "c:\\x\\python.exe")
    assert by_pid[222][1] == (2000.0, "c:\\x\\pythonw.exe")
    assert by_pid[555][1] is None  # unavailable identity carried as None


def test_scan_windows_gateway_skips_excluded_pids():
    import psutil

    entries = [
        {"pid": 111, "name": "python.exe", "cmdline": ["python.exe", "-m", "hermes_cli.main", "gateway", "run", "--replace"], "create_time": 1000.0, "exe": "C:\\x\\python.exe"},
    ]
    with patch.object(psutil, "process_iter", _fake_process_iter(entries)), patch(
        "sys.platform", "win32"
    ):
        assert dp._scan_windows_gateway_processes(exclude_pids={111}) == []


# --- Reaper --------------------------------------------------------------------


def _reap_with(scanned, *, alive_parent=False, current_identity=IDENT, ppid=1):
    """Run the reaper with standard mocks; returns (result, taskkill_calls)."""
    taskkills = []

    class _Result:
        returncode = 0

    def fake_run(argv, **_kw):
        taskkills.append(argv)
        return _Result()

    with patch("sys.platform", "win32"), patch.object(
        dp, "_exclude_pids_from_env", return_value=set()
    ), patch.object(
        dp, "_lock_owned_serve_pids", return_value=set()
    ), patch.object(
        dp, "_scan_windows_gateway_processes", return_value=scanned
    ), patch.object(
        dp, "_process_ppid", return_value=ppid
    ), patch.object(
        dp, "_is_alive_parent", return_value=alive_parent
    ) as alive_mock, patch.object(
        dp, "_current_process_identity", return_value=current_identity
    ), patch.object(
        dp.subprocess, "run", side_effect=fake_run
    ):
        result = dp._reap_orphaned_windows_gateway_processes()

    return result, taskkills, alive_mock


def test_reap_kills_orphan_gateway_with_matching_identity():
    scanned = [(111, GW_CMD, IDENT)]
    result, taskkills, _ = _reap_with(scanned)

    assert result["matched"] == [111]
    assert result["killed"] == [111]
    assert result["failed"] == []
    assert len(taskkills) == 1
    assert taskkills[0][:2] == ["taskkill", "/T"]


def test_reap_passes_child_create_time_to_parent_check():
    """The orphan check must be PID-reuse-aware: the scanned child's create
    time is handed to _is_alive_parent so a reused parent PID cannot shield
    an orphan."""
    scanned = [(111, GW_CMD, IDENT)]
    _result, _kills, alive_mock = _reap_with(scanned)

    alive_mock.assert_called_once_with(1, 1000.0)  # (ppid, child create_time)


def test_reap_skips_live_parent():
    scanned = [(111, GW_CMD, IDENT)]
    result, taskkills, _ = _reap_with(scanned, alive_parent=True)
    assert result["matched"] == []
    assert taskkills == []


def test_reap_skips_pid_reused_between_scan_and_kill():
    """If the PID was reused after the scan (identity changed), taskkill must
    never fire at the replacement process."""
    scanned = [(111, GW_CMD, IDENT)]
    reused_identity = (9999.0, "c:\\elsewhere\\python.exe")
    result, taskkills, _ = _reap_with(scanned, current_identity=reused_identity)
    assert result["matched"] == [111]  # selected as an orphan target...
    assert result["killed"] == []  # ...but never killed
    assert taskkills == []


def test_reap_skips_gone_pid_before_kill():
    scanned = [(111, GW_CMD, IDENT)]
    result, taskkills, _ = _reap_with(scanned, current_identity=None)
    assert result["matched"] == [111]
    assert result["killed"] == []
    assert taskkills == []


def test_reap_skips_unidentifiable_scan_entry():
    """No scan-time identity -> cannot bind the PID to the scanned process;
    fail closed and skip."""
    scanned = [(111, GW_CMD, None)]
    result, taskkills, _ = _reap_with(scanned)
    assert result["matched"] == []
    assert taskkills == []


def test_reap_skips_unknown_parent():
    scanned = [(111, GW_CMD, IDENT)]
    with patch("sys.platform", "win32"), patch.object(
        dp, "_exclude_pids_from_env", return_value=set()
    ), patch.object(
        dp, "_lock_owned_serve_pids", return_value=set()
    ), patch.object(
        dp, "_scan_windows_gateway_processes", return_value=scanned
    ), patch.object(
        dp, "_process_ppid", return_value=None
    ):
        result = dp._reap_orphaned_windows_gateway_processes()
    assert result["matched"] == []

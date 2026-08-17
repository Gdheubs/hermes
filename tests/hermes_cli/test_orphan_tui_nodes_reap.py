"""Orphaned TUI node reap at TUI launch.

``hermes desktop`` / ``hermes --tui`` spawn ``node ui-tui/dist/entry.js``
trees via :func:`hermes_cli.main._launch_tui`. When the parent ``hermes``
process dies uncleanly the node child survives and keeps emitting
prompt_toolkit status chrome to a shared terminal, producing the stacked /
repeated status frames observed on Windows.

This suite pins the two safety gates the reaper must enforce:

1. **Verified-Node gate (Greptile P1):** a process whose *cmdline merely
   mentions* ``ui-tui/dist/entry`` but whose executable is NOT node (a
   ``python`` shell, a ``notepad.exe``) must never be selected for reaping.
   This is tested against the REAL scanner by faking only the ``ps``/``wmic``
   subprocess output.
2. **Orphan gate:** only node processes whose launcher parent has already
   exited (reparented to init) are reaped; a concurrently-running, live-parent
   TUI is spared.

The reaper is exercised with mocked kill so no real process is touched.
"""

import os
import sys
from unittest.mock import patch

import pytest

import hermes_cli.dashboard_procs as dp

# Stable (create_time, exe) identity snapshot a scanner would capture for a node
# process. Used by reaper-level tests whose scanners are mocked.
IDENT = (1.0, "node")
WINDOWS_IDENT = (1.0, "node.exe")


def _norm(path):
    from pathlib import Path

    try:
        return str(Path(path).resolve()).lower()
    except Exception:
        return path.lower()


# --- Node-identity gate (P1) -------------------------------------------------


def test_is_node_exe_accepts_node_only():
    assert dp._is_node_exe("node ui-tui/dist/entry.js") is True
    assert dp._is_node_exe("node.exe ui-tui/dist/entry.js") is True
    assert dp._is_node_exe("/usr/bin/node ui-tui/dist/entry.js") is True
    assert dp._is_node_exe('"node.exe" ui-tui') is True
    assert dp._is_node_exe('"/usr/bin/node" ui-tui/dist/entry.js') is True


def test_is_node_exe_rejects_non_node():
    for cmd in (
        "python ui-tui/dist/entry.js",
        "notepad.exe --open ui-tui/dist/entry.js",
        "/usr/bin/node.cmd ui-tui/dist/entry.js",
        "node.bat ui-tui/dist/entry.js",
        "grep -r ui-tui/dist/entry .",
        "",
    ):
        assert dp._is_node_exe(cmd) is False


def test_tui_cmdline_matches_only_approved_entry_paths():
    """Greptile P1: generic `--expose-gc` + `dist/entry.js` must not match an
    unrelated node app. Only entry paths Hermes actually launches are approved."""
    approved = {
        _norm("/x/ui-tui/dist/entry.js"),
        _norm("/opt/hermes-tui/dist/entry.js"),
        _norm("/lib/python3/site-packages/hermes_cli/tui_dist/entry.js"),
    }
    with patch.object(dp, "_APPROVED_TUI_ENTRY_PATHS", approved):
        # Real Hermes launches (exact approved paths) match.
        assert dp._is_tui_node_cmdline("node --expose-gc /x/ui-tui/dist/entry.js --session 1") is True
        assert dp._is_tui_node_cmdline("node --expose-gc /opt/hermes-tui/dist/entry.js") is True
        assert dp._is_tui_node_cmdline("node --expose-gc /lib/python3/site-packages/hermes_cli/tui_dist/entry.js") is True
        # Greptile repro: unrelated node with the same generic fragment -> reject.
        assert dp._is_tui_node_cmdline("node --expose-gc /tmp/unrelated/dist/entry.js") is False
        # No --expose-gc launcher marker -> reject (even if path looks right).
        assert dp._is_tui_node_cmdline("node /x/ui-tui/dist/entry.js") is False
        # Path not in the approved set -> reject.
        assert dp._is_tui_node_cmdline("node --expose-gc /some/other/app/dist/entry.js") is False


def _fake_process_iter(entries):
    """Patch psutil.process_iter to yield fake procs from *entries*.

    *entries* is a list of dicts each with keys pid/cmdline/create_time/exe.
    Each fake proc exposes ``.info`` returning that dict (single observation,
    matching how the production scanner binds identity to cmdline).
    """

    class _Proc:
        def __init__(self, info):
            self.info = info

    class _Iter:
        def __iter__(self):
            return (self._make(e) for e in entries)

        @staticmethod
        def _make(e):
            return _Proc(e)

    return _Iter()


# Real launcher cmdlines always carry --expose-gc (see _launch_tui in main.py).
POSIX_ENTRIES = [
    {"pid": 111, "cmdline": ["node", "--expose-gc", "/x/ui-tui/dist/entry.js", "--session", "1"], "create_time": 1.0, "exe": "/usr/bin/node"},  # checkout
    {"pid": 112, "cmdline": ["node", "--expose-gc", "/opt/hermes-tui/dist/entry.js"], "create_time": 1.1, "exe": "/usr/bin/node"},  # HERMES_TUI_DIR
    {"pid": 113, "cmdline": ["node", "--expose-gc", "/lib/python3/site-packages/hermes_cli/tui_dist/entry.js"], "create_time": 1.2, "exe": "/usr/bin/node"},  # wheel bundle
    {"pid": 222, "cmdline": ["python", "-c", "read('ui-tui/dist/entry.js')"], "create_time": 2.0, "exe": "/usr/bin/python"},  # non-Node
    {"pid": 333, "cmdline": ["notepad.exe", "ui-tui\\dist\\entry"], "create_time": 3.0, "exe": "notepad.exe"},  # non-Node
    {"pid": 444, "cmdline": ["node", "/usr/bin/vim", "notes", "about", "ui-tui"], "create_time": 4.0, "exe": "/usr/bin/node"},  # no TUI pattern
    {"pid": 555, "cmdline": ["node", "/some/other/app/dist/entry.js"], "create_time": 5.0, "exe": "/usr/bin/node"},  # node but NOT Hermes (no --expose-gc)
]


def test_scan_posix_rejects_non_node_with_pattern():
    """Drive the REAL POSIX scanner via psutil.process_iter (Greptile P1 PoC)."""
    import psutil

    approved = {
        _norm("/x/ui-tui/dist/entry.js"),
        _norm("/opt/hermes-tui/dist/entry.js"),
        _norm("/lib/python3/site-packages/hermes_cli/tui_dist/entry.js"),
    }
    with patch.object(dp, "_APPROVED_TUI_ENTRY_PATHS", approved), patch.object(
        psutil, "process_iter", return_value=_fake_process_iter(POSIX_ENTRIES)
    ), patch("sys.platform", "darwin"):
        found = dp._scan_posix_node_processes(set())
    # All three supported Hermes TUI layouts (111/112/113) are selected; the
    # python and notepad lines that merely contain the pattern are rejected,
    # and a node process serving a non-Hermes dist/entry.js (555, no
    # --expose-gc / not an approved path) is rejected (Greptile P1: broad match).
    assert sorted(pid for pid, _c, _i in found) == [111, 112, 113]


WINDOWS_ENTRIES = [
    {"pid": 111, "cmdline": ["node.exe", "--expose-gc", "C:\\x\\ui-tui\\dist\\entry.js"], "create_time": 1.0, "exe": "C:\\node.exe"},  # checkout
    {"pid": 112, "cmdline": ["node.exe", "--expose-gc", "C:\\hermes-tui\\dist\\entry.js"], "create_time": 1.1, "exe": "C:\\node.exe"},  # HERMES_TUI_DIR
    {"pid": 222, "cmdline": ["python.exe", "-m", "hermes", "--tui", "ui-tui/dist/entry"], "create_time": 2.0, "exe": "python.exe"},  # non-Node
    {"pid": 333, "cmdline": ["notepad.exe", "ui-tui\\dist\\entry"], "create_time": 3.0, "exe": "notepad.exe"},  # non-Node
    {"pid": 444, "cmdline": ["node.exe", "C:\\some\\app\\dist\\entry.js"], "create_time": 4.0, "exe": "node.exe"},  # node, non-Hermes (no --expose-gc)
]


def test_scan_windows_rejects_non_node_with_pattern():
    """Drive the REAL Windows scanner via psutil.process_iter (Greptile P1 PoC)."""
    import psutil

    approved = {
        _norm("c:\\x\\ui-tui\\dist\\entry.js"),
        _norm("c:\\hermes-tui\\dist\\entry.js"),
    }
    with patch.object(dp, "_APPROVED_TUI_ENTRY_PATHS", approved), patch.object(
        psutil, "process_iter", return_value=_fake_process_iter(WINDOWS_ENTRIES)
    ), patch("sys.platform", "win32"):
        found = dp._scan_windows_node_processes(set())
    # Both Hermes TUI layouts (111 checkout, 112 HERMES_TUI_DIR) selected;
    # python/notepad and the non-Hermes node app (444) rejected.
    assert sorted(pid for pid, _c, _i in found) == [111, 112]


# --- Orphan gate (reaper-level) ---------------------------------------------


def _fake_kill_collector():
    terms: list[int] = []
    live: set[int] = set()

    def fake_kill(pid, sig):
        if sig == 0:
            if pid in live:
                return None
            raise ProcessLookupError()
        if sig == 15:
            terms.append(pid)
            live.discard(pid)
            return None
        if sig == 9:
            terms.append(pid)
            live.discard(pid)
            return None
        return None

    return terms, live, fake_kill


def test_reap_only_kills_orphan_tui_nodes():
    scanned = [
        (111, "node --expose-gc /x/ui-tui/dist/entry.js --session 1", IDENT),  # orphan (ppid 1)
        (222, "node --expose-gc /x/ui-tui/dist/entry.js --session 2", IDENT),  # live parent
        (333, "node --expose-gc /x/ui-tui/dist/entry.js --session 3", IDENT),  # orphan (ppid 1)
    ]
    ppids = {111: 1, 222: 50, 333: 1, 444: 1}
    terms, _live, fake_kill = _fake_kill_collector()
    import psutil

    class _Ident:
        def create_time(self):
            return 1.0

        def exe(self):
            return "node"

    with patch.object(dp, "_scan_posix_node_processes", return_value=scanned), patch(
        "os.kill", side_effect=fake_kill
    ), patch("sys.platform", "darwin"), patch.object(
        dp, "_process_ppid", side_effect=lambda pid: ppids.get(pid)
    ), patch.object(
        dp, "_is_alive_parent", side_effect=lambda ppid, ctime=None: ppid not in (0, 1)
    ), patch.object(psutil, "Process", return_value=_Ident()):
        result = dp._reap_orphaned_tui_nodes(
            sleep_fn=lambda _s: None, signal_term=15, signal_kill=9
        )
    # matched = selected-for-reaping targets (orphans only).
    assert set(result["matched"]) == {111, 333}
    assert set(terms) == {111, 333}
    assert set(result["killed"]) == {111, 333}


def test_substring_ui_tui_in_other_cmdline_is_not_a_tui_node():
    scanned = [(555, "grep -rl ui-tui/dist/entry ./src", IDENT)]
    with patch.object(dp, "_scan_posix_node_processes", return_value=scanned), patch(
        "sys.platform", "darwin"
    ):
        result = dp._reap_orphaned_tui_nodes(sleep_fn=lambda _s: None)
    assert result["matched"] == []
    assert result["killed"] == []


def test_unknown_parent_lookup_is_skipped_safely():
    """If the parent PID cannot be resolved, we cannot prove orphanhood, so the
    process is spared (never kill on unknown)."""
    scanned = [(111, "node --expose-gc /x/ui-tui/dist/entry.js", IDENT)]
    with patch.object(dp, "_scan_posix_node_processes", return_value=scanned), patch(
        "os.kill"
    ) as mock_kill, patch("sys.platform", "darwin"), patch.object(
        dp, "_process_ppid", return_value=None
    ):
        result = dp._reap_orphaned_tui_nodes(sleep_fn=lambda _s: None)
    assert result["matched"] == []
    mock_kill.assert_not_called()


def test_empty_scan_no_reap():
    with patch.object(dp, "_scan_posix_node_processes", return_value=[]), patch(
        "sys.platform", "darwin"
    ):
        result = dp._reap_orphaned_tui_nodes(sleep_fn=lambda _s: None)
    assert result == {"matched": [], "killed": [], "failed": []}


# --- Regression: Greptile P1s -------------------------------------------------


def test_windows_unknown_parent_is_not_reaped():
    """P1: a Windows TUI node whose parent lookup fails (None) must NOT be
    tree-killed. Failing closed matches the POSIX branch."""
    scanned = [(424242, "node.exe --expose-gc C:\\x\\ui-tui\\dist\\entry.js", WINDOWS_IDENT)]
    called = []

    def fake_kill(pid, sig):
        called.append((pid, sig))
        return None

    with patch.object(dp, "_scan_windows_node_processes", return_value=scanned), patch(
        "os.kill", side_effect=fake_kill
    ), patch("sys.platform", "win32"), patch.object(
        dp, "_process_ppid", return_value=None
    ):
        result = dp._reap_orphaned_tui_nodes(sleep_fn=lambda _s: None)
    assert result["matched"] == []
    assert called == []  # no taskkill issued for unknown-parent node


def test_posix_pid_reuse_skips_sigkill():
    """P1: if a SIGTERM'd node exits and its PID is reused before the grace
    period ends, the SIGKILL must NOT be delivered to the replacement."""
    import psutil

    scanned = [(111, "node --expose-gc /x/ui-tui/dist/entry.js", IDENT)]

    class _Proc:
        def __init__(self, ctime, exe):
            self._ctime = ctime
            self._exe = exe

        def create_time(self):
            return self._ctime

        def exe(self):
            return self._exe

    original = _Proc(1.0, "node")  # matches scanner baseline IDENT
    reused = _Proc(2000.0, "node")  # same exe, different instance (reused PID)

    sigterms = []
    proc_states = {"after_sleep": False}

    def fake_kill(pid, sig):
        if sig == 15:
            sigterms.append(pid)
        return None

    def fake_process(pid):
        # Before the grace sleep the selected PID is the original process; after
        # the sleep a *different* process occupies the same PID.
        return reused if proc_states["after_sleep"] else original

    with patch.object(dp, "_scan_posix_node_processes", return_value=scanned), patch(
        "os.kill", side_effect=fake_kill
    ), patch("sys.platform", "darwin"), patch.object(
        dp, "_process_ppid", return_value=1
    ), patch.object(
        dp, "_is_alive_parent", return_value=False
    ), patch.object(
        psutil, "Process", side_effect=fake_process
    ):
        def sleep(_s):
            proc_states["after_sleep"] = True

        result = dp._reap_orphaned_tui_nodes(sleep_fn=sleep)
    # SIGTERM was attempted; SIGKILL must have been withheld (PID reused).
    assert sigterms == [111]
    assert 111 not in result["killed"]


def test_windows_orphan_node_reaches_taskkill():
    """Positive counterpart to the unknown-parent test: a Windows TUI node whose
    launcher parent is confirmed dead (ppid 1 via psutil) IS reaped by taskkill.
    (Greptile P1: Windows cleanup must be reachable, not always skipped.)"""
    import psutil

    scanned = [(111, "node.exe --expose-gc C:\\x\\ui-tui\\dist\\entry.js", WINDOWS_IDENT)]
    taskkills = []

    class _Proc:
        def ppid(self):
            return 1  # orphaned

        def create_time(self):
            return 1.0

        def exe(self):
            return "node.exe"

    def fake_run(cmd, **kw):
        if cmd[:1] == ["taskkill"]:
            taskkills.append(cmd)
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        raise AssertionError(f"unexpected subprocess: {cmd}")

    with patch.object(dp, "_scan_windows_node_processes", return_value=scanned), patch(
        "subprocess.run", side_effect=fake_run
    ), patch("sys.platform", "win32"), patch.object(
        dp, "_process_ppid", return_value=1
    ), patch.object(
        psutil, "Process", return_value=_Proc()
    ):
        result = dp._reap_orphaned_tui_nodes(sleep_fn=lambda _s: None)
    assert result["matched"] == [111]
    # taskkill /T /F /PID 111 was issued for the orphaned Windows node.
    assert taskkills and "111" in taskkills[0]


def test_windows_pid_reuse_skips_taskkill():
    """P1: if the Node process exits and its PID is reused before taskkill, the
    replacement must NOT be tree-killed."""
    import psutil

    scanned = [(111, "node.exe --expose-gc C:\\x\\ui-tui\\dist\\entry.js", WINDOWS_IDENT)]
    taskkills = []

    class _Proc:
        def __init__(self, ctime):
            self._ctime = ctime

        def create_time(self):
            return self._ctime

        def exe(self):
            return "node.exe"

        def ppid(self):
            return 1

    original = _Proc(1000.0)
    reused = _Proc(2000.0)  # same PID, different instance
    calls = {"n": 0}

    def fake_run(cmd, **kw):
        if cmd[:1] == ["taskkill"]:
            taskkills.append(cmd)
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        raise AssertionError(f"unexpected subprocess: {cmd}")

    def fake_process(pid):
        # First identity query (at selection) sees the original; the revalidation
        # before taskkill sees a different process at the same PID (reuse).
        calls["n"] += 1
        return reused if calls["n"] > 1 else original

    with patch.object(dp, "_scan_windows_node_processes", return_value=scanned), patch(
        "subprocess.run", side_effect=fake_run
    ), patch("sys.platform", "win32"), patch.object(
        dp, "_process_ppid", return_value=1
    ), patch.object(
        psutil, "Process", side_effect=fake_process
    ):
        result = dp._reap_orphaned_tui_nodes(sleep_fn=lambda _s: None)
    assert taskkills == []  # no taskkill issued for the reused PID
    assert 111 not in result["killed"]


def test_greptile_prebaseline_reuse_424242():
    """Exact repro of Greptile's P1 harness: a verified-Node TUI (PID 424242)
    exits and its PID is reused *before* the kill. The replacement must NOT be
    signalled. Identity is captured at scan time, so the reused PID's differing
    live identity fails the pre-destructive-op check on both branches."""
    import psutil

    ident = (1.0, "node")
    scanned = [(424242, "node --expose-gc /x/ui-tui/dist/entry.js", ident)]

    class _Proc:
        def __init__(self, ctime, exe):
            self._ctime = ctime
            self._exe = exe

        def create_time(self):
            return self._ctime

        def exe(self):
            return self._exe

    original = _Proc(1.0, "node")
    reused = _Proc(9.9, "python")  # replacement: different ctime AND exe
    calls = {"n": 0}

    sigkills = []

    def fake_kill(pid, sig):
        if sig == 9:
            sigkills.append(pid)
        return None

    def fake_process(pid):
        calls["n"] += 1
        # First call (selection/idle) sees the original; at destruction the PID
        # is already reused by an unrelated python process.
        return reused if calls["n"] > 1 else original

    with patch.object(dp, "_scan_posix_node_processes", return_value=scanned), patch(
        "os.kill", side_effect=fake_kill
    ), patch("sys.platform", "darwin"), patch.object(
        dp, "_process_ppid", return_value=1
    ), patch.object(
        dp, "_is_alive_parent", return_value=False
    ), patch.object(
        psutil, "Process", side_effect=fake_process
    ):
        result = dp._reap_orphaned_tui_nodes(sleep_fn=lambda _s: None)
    # 424242 was selected by the scanner (verified node, orphaned) and so appears
    # in matched — that is expected. The safety property Greptile flagged is that
    # the reused PID must never receive SIGKILL (the escalation). SIGTERM to the
    # still-original process at t0 is correct; SIGKILL at t1 (when reused) is the
    # bug, and it is prevented by the baseline re-validation.
    assert 424242 in result["matched"]
    assert 424242 not in result["killed"]
    assert sigkills == []  # no SIGKILL to PID 424242 (replacement was reused)


def test_windows_scanner_binds_identity_to_observation():
    """P1: Windows discovery must not depend on wmic, and identity must be bound
    to the same process_iter observation as the cmdline (no separate
    psutil.Process(pid) lookup that PID reuse could poison)."""
    import psutil

    # A real Hermes TUI node, plus a node process that merely contains a
    # ui-tui-style fragment but is NOT a Hermes launch (no --expose-gc).
    entries = [
        {"pid": 111, "cmdline": ["node.exe", "--expose-gc", "C:\\x\\ui-tui\\dist\\entry.js"], "create_time": 1.0, "exe": "C:\\node.exe"},
        {"pid": 444, "cmdline": ["node.exe", "C:\\some\\app\\dist\\entry.js"], "create_time": 4.0, "exe": "node.exe"},
    ]
    captured = {}

    def _iter():
        for e in entries:
            class _Proc:
                info = e
            captured.setdefault("used", 0)
            captured["used"] += 1
            yield _Proc()

    with patch.object(
        dp, "_APPROVED_TUI_ENTRY_PATHS", {_norm("C:\\x\\ui-tui\\dist\\entry.js")}
    ), patch.object(psutil, "process_iter", return_value=_iter()), patch(
        "sys.platform", "win32"
    ):
        found = dp._scan_windows_node_processes(set())
    # Only the Hermes-launched node (111) is selected; the lookalike (444) is
    # rejected because it lacks the --expose-gc launcher marker. Identity is
    # taken from the same `info` dict as cmdline (no second Process() call).
    assert [pid for pid, _c, _i in found] == [111]
    for _pid, _c, identity in found:
        assert identity == (1.0, "c:\\node.exe")



# --- Parent-PID reuse detection (child-create-time bound) ---------------------


def _win_reap_with_parent_age(parent_create_time, child_create_time=1.0, ppid=50):
    """Run the Windows reaper with a fake parent whose create_time is
    *parent_create_time*; returns the reaper result."""
    import psutil

    scanned = [(777, "node.exe --expose-gc C:\\x\\ui-tui\\dist\\entry.js", (child_create_time, "node.exe"))]

    class _Proc:
        def __init__(self, ctime, exe):
            self._ctime = ctime
            self._exe = exe

        def create_time(self):
            return self._ctime

        def exe(self):
            return self._exe

    class _ParentProc(_Proc):
        pass

    procs = {777: _Proc(child_create_time, "node.exe"), ppid: _ParentProc(parent_create_time, "python.exe")}

    taskkills = []

    class _FakeResult:
        returncode = 0

    def fake_run(cmd, **kwargs):
        taskkills.append(cmd)
        return _FakeResult()

    with patch.object(dp, "_scan_windows_node_processes", return_value=scanned), patch(
        "sys.platform", "win32"
    ), patch.object(dp, "_process_ppid", return_value=ppid), patch.object(
        psutil, "pid_exists", side_effect=lambda p: p in procs
    ), patch.object(
        psutil, "Process", side_effect=lambda pid: procs[pid]
    ), patch(
        "hermes_cli._subprocess_compat.windows_hide_flags", return_value=0
    ), patch(
        "subprocess.run", side_effect=fake_run
    ):
        result = dp._reap_orphaned_tui_nodes(sleep_fn=lambda _s: None)
    return result, taskkills


def test_reused_parent_pid_is_treated_as_dead_parent():
    """A dead parent's PID reused by a NEWER process must not shield an orphan:
    a real parent is always older than its child."""
    result, taskkills = _win_reap_with_parent_age(parent_create_time=5.0, child_create_time=1.0)
    assert 777 in result["matched"]
    assert 777 in result["killed"]
    assert taskkills  # taskkill /T /F was issued


def test_genuinely_live_older_parent_spares_node():
    """A parent older than the child is a real live launcher: the node is spared."""
    result, taskkills = _win_reap_with_parent_age(parent_create_time=0.5, child_create_time=1.0)
    assert result["matched"] == []
    assert taskkills == []


def test_is_node_exe_handles_spaced_absolute_path():
    r"""A Node binary at a path containing spaces (C:\Program Files\nodejs\)
    splits across cmdline tokens; the exe gate must still recognize it or the
    reaper silently never matches real TUI launches."""
    assert dp._is_node_exe(
        "C:\\Program Files\\nodejs\\node.exe --expose-gc C:\\x\\ui-tui\\dist\\entry.js"
    ) is True
    # A non-Node executable whose args merely mention a node path stays rejected.
    assert dp._is_node_exe("python C:\\tools\\run.js ui-tui/dist/entry.js") is False
    # Bare `node` as an argument (not path-qualified) after another exe is rejected.
    assert dp._is_node_exe("python node ui-tui/dist/entry.js") is False

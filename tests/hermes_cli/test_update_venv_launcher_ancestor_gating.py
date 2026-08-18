"""Test the ancestor-gating fix in _venv_launcher_ancestors (#87666).

The gateway is a two-process chain (venv launcher -> uv worker). When /update
is spawned BY the gateway, the launcher sits in the updater's own ancestor
chain and the old code skipped the whole chain, hiding the launcher. This
fix keeps gateway-runtime ancestors out of `skip` so the launcher is found.
"""
import sys
import types
from unittest.mock import patch

import hermes_cli.update_cmd as cli_main
from hermes_cli import main as cli_main_module


def _fake_psutil_with_ancestry(proc_tree, cmdlines):
    """Build a psutil stand-in.

    ``proc_tree`` maps pid -> parent pid (None for root).
    ``cmdlines`` maps pid -> argv list, for the skip-set gating.
    ``parents()`` on the CURRENT process returns the full ancestor chain
    (excluding self), which is what _venv_launcher_ancestors uses to build
    its skip set.
    """

    class FakeProc:
        def __init__(self, pid=None):
            # psutil.Process() with no args returns the CURRENT process.
            if pid is None:
                pid = cli_main.os.getpid()
            self.pid = pid

        def cmdline(self):
            if self.pid not in cmdlines:
                raise psutil_error("no such process")
            return cmdlines[self.pid]

        def parent(self):
            ppid = proc_tree.get(self.pid)
            if ppid is None:
                return None
            return FakeProc(ppid)

        def parents(self):
            # current process ancestry: walk up from self
            chain = []
            cur = self.pid
            seen = set()
            while cur in proc_tree and proc_tree[cur] is not None:
                cur = proc_tree[cur]
                if cur in seen:
                    break
                seen.add(cur)
                chain.append(FakeProc(cur))
            return chain

        def exe(self):
            # A pid is a venv launcher iff its cmdline starts with a venv
            # Scripts\\python.exe (mirrors _detect_venv_python_processes).
            # NOTE: the returned path is the REAL project venv, which may
            # differ from the hardcoded cmdline path (e.g. "C:\\Program
            # Files\\Hermes\\...") — that is deliberate: exe() only feeds the
            # startswith(venv_prefix) check, while the cmdline separately
            # exercises the quoting path independent of CI's checkout dir.
            raw = cmdlines.get(self.pid)
            if raw and str(raw[0]).lower().endswith(r"venv\scripts\python.exe"):
                return str((cli_main._m().PROJECT_ROOT / "venv" / "Scripts" / "python.exe")).lower()
            return r"C:\Users\x\uv\python.exe"

    class psutil_error(Exception):
        pass

    mod = types.SimpleNamespace(Process=FakeProc, NoSuchProcess=psutil_error)
    return mod


# The updater's ancestry: updater(300) -> worker(200, uv) -> launcher(100, venv) -> wscript(1)
# The launcher (100) is the venv holder we must find. It's in the updater's
# ancestor chain, so the OLD code skipped it (bug); the fix keeps it visible.
ANCESTRY = {300: 200, 200: 100, 100: 1, 1: None}

GATEWAY_WORKER_CMD = [
    r"C:\Users\x\AppData\Roaming\uv\python\cpython-3.11\python.exe",
    "-m", "hermes_cli.main", "gateway", "run", "--replace",
]
GATEWAY_LAUNCHER_CMD = [
    r"C:\hermes\venv\Scripts\python.exe",
    "-m", "hermes_cli.main", "gateway", "run",
]
WSCRIPT_CMD = [r"C:\Windows\System32\wscript.exe", "Hermes_Gateway.vbs"]


@patch.object(cli_main_module, "_is_windows", return_value=True)
def test_gateway_launcher_in_updater_ancestry_is_found(_winp, monkeypatch):
    """The gateway launcher in the updater's own ancestry must be returned."""
    cmdlines = {300: WSCRIPT_CMD, 200: GATEWAY_WORKER_CMD, 100: GATEWAY_LAUNCHER_CMD, 1: []}
    fake = _fake_psutil_with_ancestry(ANCESTRY, cmdlines)
    monkeypatch.setitem(sys.modules, "psutil", fake)

    # updater = pid 300 (the process whose parents() we simulate)
    monkeypatch.setattr(cli_main.os, "getpid", lambda: 300)

    found = cli_main._venv_launcher_ancestors([200])  # worker
    # Not just "100 in found": the fix must return exactly the launcher and
    # nothing else — a stray ancestor (e.g. the wscript) would trip the
    # venv-holder guard downstream.
    assert found == [100], f"launcher 100 should be found, got {found}"


@patch.object(cli_main_module, "_is_windows", return_value=True)
def test_non_gateway_ancestors_still_skipped(_winp, monkeypatch):
    """A non-gateway ancestor (e.g. plain shell) must still be skipped."""
    # updater(300) -> shell(250) -> wscript(1), and the worker also hangs off
    # the shell so ppid 250 actually reaches the skip check.
    # 250's cmdline starts with the venv python (so exe() reports it under
    # the venv prefix — it *would* be returned if gating dropped it) but is
    # NOT a gateway runtime, so it must be skipped.
    tree = {300: 250, 250: 1, 200: 250, 1: None}
    cmdlines = {
        300: [
            r"C:\Program Files\Hermes\venv\Scripts\python.exe",
            "-m", "hermes_cli.main", "update",
        ],
        250: [
            r"C:\Program Files\Hermes\venv\Scripts\python.exe",
            "-m", "hermes_cli.main", "update", "--gateway",
        ],
        200: GATEWAY_WORKER_CMD,
        1: [],
    }
    fake = _fake_psutil_with_ancestry(tree, cmdlines)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(cli_main.os, "getpid", lambda: 300)

    found = cli_main._venv_launcher_ancestors([200])
    assert found == [], "a non-gateway venv ancestor must still be skipped"


@patch.object(cli_main_module, "_is_windows", return_value=True)
def test_gateway_launcher_with_spaces_in_path_still_found(_winp, monkeypatch):
    """list2cmdline quoting: paths with spaces must not break matching."""
    cmdlines = {
        300: WSCRIPT_CMD,
        200: [
            r"C:\Users\x\AppData\Roaming\uv\python\cpython-3.11\python.exe",
            "-m", "hermes_cli.main", "gateway", "run", "--replace",
        ],
        100: [
            # Hardcoded: a path with spaces regardless of where CI checks
            # out the repo (deriving from PROJECT_ROOT would make this test
            # a byte-identical copy of the first one on space-free hosts).
            r"C:\Program Files\Hermes\venv\Scripts\python.exe",
            "-m", "hermes_cli.main", "gateway", "run",
        ],
        1: [],
    }
    fake = _fake_psutil_with_ancestry(ANCESTRY, cmdlines)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(cli_main.os, "getpid", lambda: 300)

    # Pin os.name to "nt": the serialization branch is `list2cmdline` only on
    # Windows, and this test must exercise it even on POSIX CI hosts.
    monkeypatch.setattr(cli_main.os, "name", "nt")

    # Spy on list2cmdline so a failure says which half broke: the quoting
    # (serialization) or the downstream matcher.
    serialized: list[str] = []
    orig_list2cmdline = cli_main.subprocess.list2cmdline

    def _spy(raw):
        text = orig_list2cmdline(raw)
        if "Program Files" in text:
            serialized.append(text)
        return text

    monkeypatch.setattr(cli_main.subprocess, "list2cmdline", _spy)

    found = cli_main._venv_launcher_ancestors([200])
    assert found == [100], f"launcher 100 (space in path) should be found, got {found}"
    assert serialized, "list2cmdline was never called with the spaced launcher path"
    # any() rather than serialized[0]: don't depend on ancestor walk order.
    assert any(
        '"C:\\Program Files\\Hermes\\venv\\Scripts\\python.exe"' in text
        for text in serialized
    ), f"spaced launcher path should stay quoted, got: {serialized}"


@patch.object(cli_main_module, "_is_windows", return_value=True)
def test_gateway_restart_launcher_in_updater_ancestry_is_found(_winp, monkeypatch):
    """A launcher whose argv reads ``gateway restart`` is still a venv holder.

    On hosts without a service manager the restart fallback runs
    ``run_gateway()`` in-process while argv still reads ``gateway restart``,
    so the matcher must treat it as a runtime ancestor too (PR #87666 keeps
    such ancestors out of the skip set).
    """
    cmdlines = {
        300: WSCRIPT_CMD,
        200: [
            r"C:\Users\x\AppData\Roaming\uv\python\cpython-3.11\python.exe",
            "-m", "hermes_cli.main", "gateway", "restart",
        ],
        100: [
            r"C:\Program Files\Hermes\venv\Scripts\python.exe",
            "-m", "hermes_cli.main", "gateway", "restart",
        ],
        1: [],
    }
    fake = _fake_psutil_with_ancestry(ANCESTRY, cmdlines)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(cli_main.os, "getpid", lambda: 300)

    found = cli_main._venv_launcher_ancestors([200])
    # ``restart`` is a management subcommand on service-managed hosts, but the
    # no-supervisor fallback makes it a runtime — so it is found, not skipped.
    assert found == [100], f"restart launcher 100 should be found, got {found}"


@patch.object(cli_main_module, "_is_windows", return_value=True)
def test_matcher_import_failure_falls_back_to_skip_all(_winp, monkeypatch, caplog):
    """If the gateway-status matcher cannot be imported, fall back to the old
    skip-everything behaviour and log why (never crash, never silently hang
    the update on the venv-holder guard)."""
    # Full ancestry WITH a venv launcher (100): without the fallback walk the
    # launcher would be found, so this asserts the fallback really skips.
    cmdlines = {
        300: WSCRIPT_CMD,
        200: GATEWAY_WORKER_CMD,
        100: GATEWAY_LAUNCHER_CMD,
        1: [],
    }
    fake = _fake_psutil_with_ancestry(ANCESTRY, cmdlines)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(cli_main.os, "getpid", lambda: 300)

    # Replace gateway.status with a module that lacks the matcher, so the
    # `from gateway.status import looks_like_gateway_runtime_command_line`
    # inside _venv_launcher_ancestors raises ImportError.
    import types as _types

    monkeypatch.setitem(
        sys.modules,
        "gateway.status",
        _types.SimpleNamespace(__name__="gateway.status"),
    )

    with caplog.at_level("DEBUG", logger="hermes_cli.update_cmd"):
        found = cli_main._venv_launcher_ancestors([200])
    assert found == [], "fallback skips everything, so no launcher is found"
    assert any(
        "ancestor gating unavailable" in r.message for r in caplog.records
    ), "fallback must be logged so the silent dead-end is debuggable"


@patch.object(cli_main_module, "_is_windows", return_value=True)
def test_ancestry_walk_failure_does_not_crash(_winp, monkeypatch):
    """If psutil.Process().parents() raises, the function must not propagate:
    degrade to an empty ancestry and still return a sane (empty) result."""
    class _ExplodingProcess:
        def __init__(self, pid=None):
            pass

        def parents(self):
            raise RuntimeError("simulated psutil walk failure")

        def parent(self):
            raise RuntimeError("simulated psutil walk failure")

    fake = types.SimpleNamespace(
        Process=_ExplodingProcess, NoSuchProcess=RuntimeError
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(cli_main.os, "getpid", lambda: 300)

    found = cli_main._venv_launcher_ancestors([200])
    assert found == [], "walk failure must degrade to no matches, not crash"

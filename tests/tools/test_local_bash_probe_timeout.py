"""Regression tests for the ``_bash_starts`` timeout path (PR #83413).

On Windows, the external-program probe could wedge a process forever:
the probe inherited the parent's stdin (a JSON-RPC pipe under ACP/gateway
embedding), and on ``TimeoutExpired`` CPython's ``run()`` killed only the
direct child.  With ``HERMES_GIT_BASH_PATH`` pointing at Git-for-Windows
``bin\\bash.exe`` (a shim that spawns ``usr\\bin\\bash.exe``) the kill
orphaned the real bash, which held the pipe write ends open — and the
follow-up ``communicate()`` blocked indefinitely.

These tests exercise the replacement plumbing with mocked subprocess
primitives: the probe must escalate to a tree-kill (``taskkill /T /F``
on Windows), bound the post-kill pipe drain, cache the failure, and
return ``False`` — never hang.
"""
import subprocess
from unittest.mock import MagicMock

import pytest

from tools.environments import local as local_mod

FAKE_BASH = r"C:\fake\git\bin\bash.exe"


@pytest.fixture(autouse=True)
def _clean_probe_caches():
    saved_starts = dict(local_mod._bash_starts_cache)
    saved_details = dict(local_mod._bash_probe_details_cache)
    local_mod._bash_starts_cache.clear()
    local_mod._bash_probe_details_cache.clear()
    yield
    local_mod._bash_starts_cache.clear()
    local_mod._bash_starts_cache.update(saved_starts)
    local_mod._bash_probe_details_cache.clear()
    local_mod._bash_probe_details_cache.update(saved_details)


def _timeout_proc(second_communicate=("", "")):
    """A fake Popen whose first communicate() raises TimeoutExpired."""
    proc = MagicMock()
    proc.pid = 4242
    effects = [subprocess.TimeoutExpired(cmd="bash", timeout=15)]
    effects.append(
        second_communicate
        if not isinstance(second_communicate, Exception)
        else second_communicate
    )
    proc.communicate.side_effect = effects
    return proc


def test_timeout_on_windows_tree_kills_and_returns_false(monkeypatch):
    monkeypatch.setattr(local_mod, "_IS_WINDOWS", True)
    proc = _timeout_proc()
    run_calls = []

    monkeypatch.setattr(
        local_mod.subprocess, "Popen", MagicMock(return_value=proc)
    )
    monkeypatch.setattr(
        local_mod.subprocess,
        "run",
        lambda args, **kw: run_calls.append(list(args)) or MagicMock(returncode=0),
    )

    ok = local_mod._bash_starts(FAKE_BASH)

    assert ok is False
    assert any(
        call[:3] == ["taskkill", "/T", "/F"] and str(proc.pid) in call
        for call in run_calls
    ), f"expected a taskkill /T /F on pid {proc.pid}, got {run_calls}"
    # The failure is cached so later candidates aren't re-probed into the
    # same wedge, and the details cache explains the verdict.
    assert local_mod._bash_starts_cache[FAKE_BASH] is False
    assert "timed out" in local_mod._bash_probe_details_cache[FAKE_BASH]


def test_timeout_on_posix_kills_process_directly(monkeypatch):
    monkeypatch.setattr(local_mod, "_IS_WINDOWS", False)
    proc = _timeout_proc()
    run_calls = []

    monkeypatch.setattr(
        local_mod.subprocess, "Popen", MagicMock(return_value=proc)
    )
    monkeypatch.setattr(
        local_mod.subprocess,
        "run",
        lambda args, **kw: run_calls.append(list(args)) or MagicMock(returncode=0),
    )

    ok = local_mod._bash_starts("/usr/bin/bash")

    assert ok is False
    proc.kill.assert_called_once()
    assert not run_calls, "taskkill must not be used off Windows"


def test_orphaned_fork_child_cannot_wedge_the_drain(monkeypatch):
    """Even if the post-kill communicate() times out again (an orphaned MSYS
    fork child still holds the pipes and is unreachable via taskkill /T on
    the shim's dead tree), the probe abandons the pipes and returns."""
    monkeypatch.setattr(local_mod, "_IS_WINDOWS", True)
    proc = _timeout_proc(
        second_communicate=subprocess.TimeoutExpired(cmd="bash", timeout=5)
    )

    monkeypatch.setattr(
        local_mod.subprocess, "Popen", MagicMock(return_value=proc)
    )
    monkeypatch.setattr(
        local_mod.subprocess, "run", lambda args, **kw: MagicMock(returncode=0)
    )

    ok = local_mod._bash_starts(FAKE_BASH)

    assert ok is False
    assert local_mod._bash_starts_cache[FAKE_BASH] is False


def test_healthy_probe_uses_devnull_stdin_and_caches_true(monkeypatch):
    monkeypatch.setattr(local_mod, "_IS_WINDOWS", True)
    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = 0
    proc.communicate.return_value = ("", "")
    popen = MagicMock(return_value=proc)

    monkeypatch.setattr(local_mod.subprocess, "Popen", popen)

    ok = local_mod._bash_starts(FAKE_BASH)

    assert ok is True
    assert local_mod._bash_starts_cache[FAKE_BASH] is True
    _args, kwargs = popen.call_args
    assert kwargs.get("stdin") == subprocess.DEVNULL

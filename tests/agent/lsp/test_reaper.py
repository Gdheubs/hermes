"""Tests for Adaptation D: client leases, monotonic idle timing, and the
cross-process LSP status snapshot.

Two bugs motivate the client lease/refcount:

- A client mid-operation (``open_file`` + ``wait_for_diagnostics`` can take
  several seconds) could be reaped out from under an in-flight
  ``get_diagnostics_sync``/``snapshot_baseline`` call, since the idle-reaper
  sweep and the operation coroutine both run on the same background loop
  and can interleave at any ``await`` point.
- ``_last_used`` was recorded with ``time.time()`` (wall clock), which can
  jump on an NTP step or manual clock change; ``time.monotonic()`` is the
  correct source for a duration-based cutoff.

Also covers the new cross-process status snapshot: ``hermes lsp status``
runs as a *separate* process from whichever one is actually running the
service, so it must read a published snapshot rather than instantiate its
own disconnected singleton.
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import time
from contextlib import redirect_stdout

import pytest

from agent.lsp.client import LSPClient
from agent.lsp.manager import LSPService


class _FakeClient:
    """Stand-in for LSPClient — just enough surface for the reaper/lease paths."""

    def __init__(self, server_id: str, workspace_root: str) -> None:
        self.server_id = server_id
        self.workspace_root = workspace_root
        self.shutdown_calls = 0

    @property
    def is_running(self) -> bool:
        return True

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


def _idle_service(**overrides) -> LSPService:
    kwargs = dict(
        enabled=True,
        wait_mode="document",
        wait_timeout=1.0,
        install_strategy="manual",
        # Long enough that the background reaper loop never sweeps on its
        # own during the test — sweeps are triggered manually below.
        idle_timeout=60.0,
    )
    kwargs.update(overrides)
    return LSPService(**kwargs)


def _seed_idle_client(svc: LSPService, key):
    client = _FakeClient(*key)
    with svc._state_lock:
        svc._clients[key] = client
        svc._last_used[key] = time.monotonic() - 10_000.0  # far past any cutoff
    return client


# ---------------------------------------------------------------------------
# Lease/refcount — a leased client must survive a sweep
# ---------------------------------------------------------------------------


def test_leased_client_survives_reap_sweep():
    svc = _idle_service()
    key = ("fake", "/tmp/ws-a")
    client = _seed_idle_client(svc, key)
    try:
        svc._acquire_client(key)
        svc._loop.run(svc._reap_idle_once(), timeout=5.0)
        assert key in svc._clients, "a leased client must not be reaped mid-operation"
        assert client.shutdown_calls == 0
    finally:
        svc.shutdown()


def test_released_client_is_reaped_on_next_sweep():
    svc = _idle_service()
    key = ("fake", "/tmp/ws-b")
    client = _seed_idle_client(svc, key)
    try:
        svc._acquire_client(key)
        svc._loop.run(svc._reap_idle_once(), timeout=5.0)
        assert key in svc._clients  # still leased

        svc._release_client(key)
        svc._loop.run(svc._reap_idle_once(), timeout=5.0)
        assert key not in svc._clients, "releasing the lease must let the sweep reap it"
        assert client.shutdown_calls == 1
    finally:
        svc.shutdown()


def test_nested_acquire_requires_matching_releases():
    """Two overlapping operations on the same client must both release
    their lease before the client becomes reapable."""
    svc = _idle_service()
    key = ("fake", "/tmp/ws-c")
    client = _seed_idle_client(svc, key)
    try:
        svc._acquire_client(key)
        svc._acquire_client(key)
        svc._release_client(key)

        svc._loop.run(svc._reap_idle_once(), timeout=5.0)
        assert key in svc._clients, "one outstanding lease must still block the sweep"

        svc._release_client(key)
        svc._loop.run(svc._reap_idle_once(), timeout=5.0)
        assert key not in svc._clients
        assert client.shutdown_calls == 1
    finally:
        svc.shutdown()


def test_release_without_acquire_does_not_go_negative():
    """An unmatched release must not leave the refcount negative — that
    would require multiple future acquires to counteract a single lease."""
    svc = _idle_service()
    key = ("fake", "/tmp/ws-d")
    _seed_idle_client(svc, key)
    try:
        svc._release_client(key)  # no matching acquire
        svc._acquire_client(key)
        svc._release_client(key)

        svc._loop.run(svc._reap_idle_once(), timeout=5.0)
        assert key not in svc._clients, "refcount must not go negative and block reaping forever"
    finally:
        svc.shutdown()


# ---------------------------------------------------------------------------
# Idle timing must use a monotonic clock
# ---------------------------------------------------------------------------


def test_last_used_is_recorded_via_monotonic_clock(monkeypatch):
    import agent.lsp.manager as manager_mod

    fake_monotonic = 12345.0
    monkeypatch.setattr(manager_mod.time, "monotonic", lambda: fake_monotonic)
    monkeypatch.setattr(manager_mod.time, "time", lambda: 999999999.0)

    svc = _idle_service()
    try:
        key = ("fake", "/tmp/ws-e")
        client = _FakeClient(*key)
        with svc._state_lock:
            svc._clients[key] = client
        svc._touch(client)
        assert svc._last_used[key] == fake_monotonic, (
            "idle timestamps must come from time.monotonic(), not time.time()"
        )
    finally:
        svc.shutdown()


def test_reap_cutoff_uses_monotonic_clock_not_wall_clock(monkeypatch):
    """The idle-reap cutoff must be derived from ``time.monotonic()``.

    Spies on both clock functions during a sweep — the wall clock must
    never be consulted, since it can jump (NTP step, DST, manual change)
    and either falsely reap a fresh client or keep a truly idle one alive.
    """
    import agent.lsp.manager as manager_mod

    real_monotonic = manager_mod.time.monotonic
    calls = {"monotonic": 0, "time": 0}

    def spy_monotonic():
        calls["monotonic"] += 1
        return real_monotonic()

    def spy_time():
        calls["time"] += 1
        return 0.0

    svc = _idle_service(idle_timeout=5.0)
    try:
        monkeypatch.setattr(manager_mod.time, "monotonic", spy_monotonic)
        monkeypatch.setattr(manager_mod.time, "time", spy_time)
        svc._loop.run(svc._reap_idle_once(), timeout=5.0)
        assert calls["monotonic"] >= 1, "the idle-reap cutoff must be computed from time.monotonic()"
        assert calls["time"] == 0, "the idle-reap cutoff must not read the wall clock"
    finally:
        svc.shutdown()


# ---------------------------------------------------------------------------
# Cross-process status snapshot
# ---------------------------------------------------------------------------


def test_status_snapshot_published_on_broken_mark(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from agent.lsp.status import read_lsp_status

    svc = _idle_service()
    try:
        key = ("fake", str(tmp_path))
        svc._broken.add(key)
        svc._publish_status()

        snapshot = read_lsp_status()
        assert snapshot is not None
        assert list(key) in snapshot["broken"]
    finally:
        svc.shutdown()


def test_status_cli_reads_snapshot_without_instantiating_singleton(tmp_path, monkeypatch):
    """``hermes lsp status`` must read the published snapshot, not spin up
    its own disconnected LSPService — which would show no clients even
    though a different process has real ones running."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from agent.lsp import cli as lsp_cli
    from agent.lsp.status import write_lsp_status

    write_lsp_status({
        "enabled": True,
        "wait_mode": "document",
        "wait_timeout": 5.0,
        "install_strategy": "auto",
        "clients": [
            {
                "server_id": "pyright",
                "workspace_root": str(tmp_path),
                "state": "running",
                "running": True,
            }
        ],
        "broken": [],
        "disabled_servers": [],
    })

    called = {"n": 0}

    def _boom():
        called["n"] += 1
        raise AssertionError("hermes lsp status must not instantiate a local LSPService singleton")

    monkeypatch.setattr("agent.lsp.get_service", _boom)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = lsp_cli._cmd_status(emit_json=False)

    assert rc == 0
    assert called["n"] == 0
    assert "active clients:  1" in buf.getvalue()


def test_publish_service_status_does_not_create_singleton(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import agent.lsp as lsp
    from agent.lsp.status import read_lsp_status

    monkeypatch.setattr(lsp, "_service", None)
    lsp.publish_service_status()

    snapshot = read_lsp_status()
    assert lsp._service is None
    assert snapshot is not None
    assert snapshot["enabled"] is False
    assert snapshot["clients"] == []
    assert snapshot["broken"] == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group semantics")
@pytest.mark.asyncio
async def test_cleanup_terminates_posix_process_group_descendant(tmp_path):
    """Cleanup must terminate a server wrapper and the child it spawned."""
    script = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "print(p.pid, flush=True); time.sleep(60)"
    )
    parent = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert parent.stdout is not None
    child_pid = int((await parent.stdout.readline()).decode().strip())

    client = LSPClient(
        server_id="test",
        workspace_root=str(tmp_path),
        command=[sys.executable],
    )
    client._proc = parent
    await client._cleanup_process()

    assert parent.returncode is not None
    for _ in range(50):
        try:
            state = open(f"/proc/{child_pid}/stat", encoding="utf-8").read().split()[2]
        except FileNotFoundError:
            break
        if state == "Z":
            break
        await asyncio.sleep(0.02)
    else:
        os.kill(child_pid, 0)
        pytest.fail("language-server descendant survived process-group cleanup")

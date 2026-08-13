from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def test_browser_terminal_shell_spec_prefers_explicit_override(monkeypatch, tmp_path: Path):
    from hermes_cli import web_server

    shell = tmp_path / "bash"
    shell.write_text("#!/bin/sh\n", encoding="utf-8")
    shell.chmod(0o755)
    argv, name = web_server._browser_terminal_shell_spec(
        {"HERMES_DESKTOP_SHELL": str(shell), "PATH": str(tmp_path)}
    )
    assert argv == [str(shell), "-il"]
    assert name == "bash"


def test_browser_terminal_process_cwd_reads_proc_on_linux_android(monkeypatch):
    from hermes_cli import web_server

    class Bridge:
        pid = os.getpid()

    monkeypatch.setattr(web_server.sys, "platform", "android")
    monkeypatch.setattr(web_server.os, "readlink", lambda path: os.getcwd())
    assert web_server._browser_terminal_process_cwd(Bridge()) == os.getcwd()


def test_browser_terminal_websocket_sends_ready_then_pty_bytes(monkeypatch, tmp_path: Path, _isolate_hermes_home):
    from starlette.testclient import TestClient

    from hermes_cli import web_server

    class FakeBridge:
        pid = os.getpid()

        def __init__(self):
            self.chunks = [b"termux-shell-ready", None]
            self.closed = False
            self.writes: list[bytes] = []
            self.resizes: list[tuple[int, int]] = []

        def read(self, _timeout: float):
            return self.chunks.pop(0) if self.chunks else None

        def write(self, data: bytes):
            self.writes.append(bytes(data))

        def resize(self, *, cols: int, rows: int):
            self.resizes.append((cols, rows))

        def close(self):
            self.closed = True

    bridge = FakeBridge()
    captured: dict[str, object] = {}

    def fake_spawn(argv, *, cwd=None, env=None, cols=80, rows=24):
        captured.update(argv=list(argv), cwd=cwd, env=env, cols=cols, rows=rows)
        return bridge

    monkeypatch.setattr(web_server.PtyBridge, "spawn", fake_spawn)
    monkeypatch.setattr(web_server, "_PTY_BRIDGE_AVAILABLE", True)
    monkeypatch.setattr(
        web_server,
        "_browser_terminal_spawn_spec",
        lambda *, cwd, profile: (["/bin/bash", "-il"], str(tmp_path), {"TERM": "xterm-256color"}, "bash"),
    )
    web_server.app.state.auth_required = False
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.browser_terminal_sessions = {}

    client = TestClient(web_server.app, base_url="http://127.0.0.1")
    url = (
        f"/api/terminal?token={web_server._SESSION_TOKEN}&id=term-test"
        f"&cwd={tmp_path}&cols=47&rows=19"
    )
    with client.websocket_connect(url, headers={"host": "127.0.0.1", "origin": "http://127.0.0.1"}) as socket:
        assert socket.receive_json() == {
            "type": "ready",
            "id": "term-test",
            "cwd": str(tmp_path),
            "shell": "bash",
        }
        assert socket.receive_bytes() == b"termux-shell-ready"

    assert captured["argv"] == ["/bin/bash", "-il"]
    assert captured["cwd"] == str(tmp_path)
    assert captured["cols"] == 47
    assert captured["rows"] == 19
    assert bridge.closed is True
    assert "term-test" not in web_server._browser_terminal_sessions()


def test_browser_terminal_websocket_rejects_bad_session_id(monkeypatch, _isolate_hermes_home):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from hermes_cli import web_server

    web_server.app.state.auth_required = False
    web_server.app.state.bound_host = "127.0.0.1"
    client = TestClient(web_server.app, base_url="http://127.0.0.1")
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            f"/api/terminal?token={web_server._SESSION_TOKEN}&id=bad/id",
            headers={"host": "127.0.0.1", "origin": "http://127.0.0.1"},
        ):
            pass
    assert exc.value.code == 4400


def test_browser_terminal_websocket_is_refused_on_non_loopback_bind(monkeypatch, _isolate_hermes_home):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from hermes_cli import web_server

    monkeypatch.setattr(web_server.app.state, "bound_host", "0.0.0.0", raising=False)
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app, base_url="http://127.0.0.1")
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            f"/api/terminal?token={web_server._SESSION_TOKEN}&id=term-public",
            headers={"host": "127.0.0.1", "origin": "http://127.0.0.1"},
        ):
            pass
    assert exc.value.code == 4403


def test_browser_terminal_websocket_requires_token_and_same_origin(_isolate_hermes_home):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from hermes_cli import web_server

    web_server.app.state.auth_required = False
    web_server.app.state.bound_host = "127.0.0.1"
    client = TestClient(web_server.app, base_url="http://127.0.0.1")

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "/api/terminal?id=no-token",
            headers={"host": "127.0.0.1", "origin": "http://127.0.0.1"},
        ):
            pass
    assert exc.value.code == 4401

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            f"/api/terminal?token={web_server._SESSION_TOKEN}&id=no-origin",
            headers={"host": "127.0.0.1"},
        ):
            pass
    assert exc.value.code == 4403

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            f"/api/terminal?token={web_server._SESSION_TOKEN}&id=cross-origin",
            headers={"host": "127.0.0.1", "origin": "http://127.0.0.1:9999"},
        ):
            pass
    assert exc.value.code == 4403


def test_browser_terminal_cwd_requires_session_token(_isolate_hermes_home):
    from starlette.testclient import TestClient
    from hermes_cli import web_server

    web_server.app.state.auth_required = False
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.browser_terminal_sessions = {}
    client = TestClient(web_server.app, base_url="http://127.0.0.1")

    assert client.get("/api/terminal/cwd?id=missing").status_code == 401
    response = client.get(
        "/api/terminal/cwd?id=missing",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )
    assert response.status_code == 200
    assert response.json() == {"cwd": None}


def test_browser_terminal_session_token_rotates_per_server_start(monkeypatch):
    from hermes_cli import web_server

    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
    monkeypatch.setattr(web_server.secrets, "token_urlsafe", lambda _n: "fresh-browser-token")
    web_server._refresh_session_token_for_server_start()
    assert web_server._SESSION_TOKEN == "fresh-browser-token"

    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "fixed-browser-token")
    web_server._refresh_session_token_for_server_start()
    assert web_server._SESSION_TOKEN == "fixed-browser-token"

from __future__ import annotations

import threading
import sys
import types

import pytest

from tui_gateway import server


class RecordingTransport:
    def __init__(self) -> None:
        self.count = 0
        self._closed = False

    def write(self, _obj: dict) -> bool:
        self.count += 1
        return True

    def close(self) -> None:
        pass


class FalseTransport(RecordingTransport):
    def write(self, _obj: dict) -> bool:
        self.count += 1
        return False


class RaisingTransport(RecordingTransport):
    def write(self, _obj: dict) -> bool:
        self.count += 1
        raise RuntimeError("closed")


@pytest.fixture(autouse=True)
def isolated_sessions():
    known = dict(server._sessions)
    known_compute_owners = dict(server._compute_host_event_owners)
    server._sessions.clear()
    server._compute_host_event_owners.clear()
    try:
        yield
    finally:
        server._sessions.clear()
        server._sessions.update(known)
        server._compute_host_event_owners.clear()
        server._compute_host_event_owners.update(known_compute_owners)


def frame(sid: str) -> dict:
    return server._event_frame("message.delta", sid, {"text": "opaque"})


def test_compression_approval_callback_cannot_follow_reused_sid(monkeypatch):
    captured: dict[str, object] = {}
    old_transport = RecordingTransport()
    replacement_transport = RecordingTransport()
    agent = types.SimpleNamespace(session_id="continued")
    old_session = {
        "agent": agent,
        "session_key": "original",
        "transport": old_transport,
    }
    server._sessions["same"] = old_session
    monkeypatch.setattr(server, "_transfer_active_session_slot", lambda *a, **k: True)
    approval = types.SimpleNamespace(
        unregister_gateway_notify=lambda _key: None,
        register_gateway_notify=lambda _key, cb: captured.__setitem__("callback", cb),
        is_session_yolo_enabled=lambda _key: False,
        enable_session_yolo=lambda _key: None,
        disable_session_yolo=lambda _key: None,
    )
    monkeypatch.setitem(sys.modules, "tools.approval", approval)

    server._sync_session_key_after_compress(
        "same", old_session, clear_pending_title=False, restart_slash_worker=False
    )
    server._sessions["same"] = {"transport": replacement_transport}
    callback = captured["callback"]
    assert callable(callback)
    callback({"opaque": True})

    assert old_transport.count == 0
    assert replacement_transport.count == 0


def test_private_missing_sid_never_falls_back_to_current_or_stdio(monkeypatch):
    current = RecordingTransport()
    stdio = RecordingTransport()
    monkeypatch.setattr(server, "current_transport", lambda: current)
    monkeypatch.setattr(server, "_stdio_transport", stdio)

    assert server.write_json(frame("absent")) is False
    assert (current.count, stdio.count) == (0, 0)

    monkeypatch.setattr(server, "current_transport", lambda: None)
    assert server.write_json(frame("absent")) is False
    assert (current.count, stdio.count) == (0, 0)


def test_private_finalized_and_dead_sessions_emit_zero_frames(monkeypatch):
    fallback = RecordingTransport()
    finalized = RecordingTransport()
    server._sessions["final"] = {"transport": finalized, "_finalized": True}
    server._sessions["dead"] = {"transport": server._detached_ws_transport}
    monkeypatch.setattr(server, "current_transport", lambda: fallback)
    monkeypatch.setattr(server, "_stdio_transport", fallback)

    assert server.write_json(frame("final")) is False
    assert server.write_json(frame("dead")) is False
    assert (finalized.count, fallback.count) == (0, 0)


@pytest.mark.parametrize("transport_type", [FalseTransport, RaisingTransport])
def test_private_write_failure_never_retries_fallback(monkeypatch, transport_type):
    owned = transport_type()
    fallback = RecordingTransport()
    server._sessions["owned"] = {"transport": owned}
    monkeypatch.setattr(server, "current_transport", lambda: fallback)
    monkeypatch.setattr(server, "_stdio_transport", fallback)

    assert server.write_json(frame("owned")) is False
    assert (owned.count, fallback.count) == (1, 0)


def test_private_closed_transport_is_rejected_without_write(monkeypatch):
    closed = RecordingTransport()
    closed._closed = True
    fallback = RecordingTransport()
    server._sessions["closed"] = {"transport": closed}
    monkeypatch.setattr(server, "current_transport", lambda: fallback)

    assert server.write_json(frame("closed")) is False
    assert (closed.count, fallback.count) == (0, 0)


def test_stale_generation_callback_cannot_route_to_sid_replacement():
    old_transport = RecordingTransport()
    new_transport = RecordingTransport()
    old = {"transport": old_transport}
    server._sessions["same"] = old

    def stale_callback() -> bool:
        return server._run_session_owned(
            old, server._emit, "message.delta", "same", {"text": "opaque"}
        )

    replacement = {"transport": new_transport}
    server._sessions["same"] = replacement

    assert stale_callback() is False
    assert (old_transport.count, new_transport.count) == (0, 0)


def test_live_registered_stdio_session_still_emits(monkeypatch):
    stdio = RecordingTransport()
    monkeypatch.setattr(server, "_stdio_transport", stdio)
    server._sessions["stdio"] = {"transport": stdio}

    assert server.write_json(frame("stdio")) is True
    assert stdio.count == 1


@pytest.mark.parametrize(
    ("callback_name", "args"),
    [
        ("thinking_callback", ("opaque",)),
        ("reasoning_callback", ("opaque",)),
        ("tool_start_callback", ("tool-id", "terminal", {})),
        ("clarify_callback", ("opaque", [])),
    ],
)
def test_ordinary_agent_callbacks_are_generation_fenced(
    monkeypatch, callback_name, args
):
    old_transport = RecordingTransport()
    new_transport = RecordingTransport()
    old = {"transport": old_transport, "tool_started_at": {}}
    server._sessions["same"] = old
    monkeypatch.setattr(
        server,
        "_on_tool_start",
        lambda sid, *_args: server._emit("tool.start", sid, {}),
    )
    monkeypatch.setattr(
        server,
        "_block",
        lambda event, sid, *_args, **_kwargs: server._emit(event, sid, {}),
    )
    callbacks = server._agent_cbs("same")
    server._sessions["same"] = {"transport": new_transport}

    callbacks[callback_name](*args)

    assert (old_transport.count, new_transport.count) == (0, 0)


def test_review_callback_style_expected_record_is_generation_fenced():
    old = {"transport": RecordingTransport()}
    replacement_transport = RecordingTransport()
    server._sessions["same"] = {"transport": replacement_transport}

    assert (
        server._emit(
            "review.summary", "same", {"text": "opaque"}, expected_session=old
        )
        is False
    )
    assert replacement_transport.count == 0


def test_compute_host_owners_are_independent_and_generation_fenced():
    ta = RecordingTransport()
    tb = RecordingTransport()
    old_a = {"transport": ta}
    owner_b = {"transport": tb}
    server._sessions.update({"a": old_a, "b": owner_b})
    server._compute_host_event_owners.update({"a": old_a, "b": owner_b})

    assert server._write_compute_host_rpc(frame("a")) is True
    assert server._write_compute_host_rpc(frame("b")) is True
    assert (ta.count, tb.count) == (1, 1)

    replacement = {"transport": RecordingTransport()}
    server._sessions["a"] = replacement
    assert server._write_compute_host_rpc(frame("a")) is False
    assert replacement["transport"].count == 0


def test_old_disconnect_teardown_cannot_detach_rebound_transport(monkeypatch):
    old_transport = RecordingTransport()
    rebound_transport = RecordingTransport()
    entered = threading.Event()
    release = threading.Event()

    class BlockingRecord(dict):
        blocked = False

        def get(self, key, default=None):
            if key == "close_on_disconnect" and not self.blocked:
                self.blocked = True
                entered.set()
                assert release.wait(timeout=2)
            return super().get(key, default)

    record = BlockingRecord(
        transport=old_transport,
        close_on_disconnect=False,
    )
    server._sessions["race"] = record
    monkeypatch.setattr(server, "_schedule_ws_orphan_reap", lambda _sid: None)
    result: list[tuple[int, int]] = []
    worker = threading.Thread(
        target=lambda: result.append(
            server._close_sessions_for_transport(old_transport)
        )
    )
    worker.start()
    assert entered.wait(timeout=2)
    with server._session_resume_lock:
        record["transport"] = server._detached_ws_transport
        assert server._rebind_live_session_transport(
            "race", record, rebound_transport
        )
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result == [(0, 0)]
    assert record["transport"] is rebound_transport


def test_old_close_on_disconnect_teardown_cannot_close_rebound_transport(monkeypatch):
    old_transport = RecordingTransport()
    rebound_transport = RecordingTransport()
    entered = threading.Event()
    release = threading.Event()

    class BlockingRecord(dict):
        blocked = False

        def get(self, key, default=None):
            if key == "close_on_disconnect" and not self.blocked:
                self.blocked = True
                entered.set()
                assert release.wait(timeout=2)
            return super().get(key, default)

    record = BlockingRecord(transport=old_transport, close_on_disconnect=True)
    server._sessions["race"] = record
    teardowns: list[dict] = []
    monkeypatch.setattr(
        server,
        "_teardown_session",
        lambda session, **_kwargs: teardowns.append(session),
    )
    result: list[tuple[int, int]] = []
    worker = threading.Thread(
        target=lambda: result.append(
            server._close_sessions_for_transport(old_transport)
        )
    )
    worker.start()
    assert entered.wait(timeout=2)
    with server._session_resume_lock:
        record["transport"] = server._detached_ws_transport
        assert server._rebind_live_session_transport(
            "race", record, rebound_transport
        )
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result == [(0, 0)]
    assert server._sessions["race"] is record
    assert record["transport"] is rebound_transport
    assert teardowns == []

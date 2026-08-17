"""WebSocketRelayTransport against a real in-process WebSocket server.

Exercises the production transport over an actual ``websockets`` server (no
mock socket): handshake (hello -> descriptor), inbound frame -> handler,
outbound request/response correlation, and follow_up routing. Proves the wire
framing (newline-delimited JSON) and the request/response future plumbing work
end to end on a live socket.

Skipped cleanly if the optional ``websockets`` dependency is absent.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
import pytest_asyncio

from gateway.relay.ws_transport import WebSocketRelayTransport, WEBSOCKETS_AVAILABLE

pytestmark = pytest.mark.skipif(not WEBSOCKETS_AVAILABLE, reason="websockets not installed")

if WEBSOCKETS_AVAILABLE:
    import websockets


DESCRIPTOR = {
    "contract_version": 1,
    "platform": "discord",
    "label": "Discord",
    "max_message_length": 2000,
    "supports_draft_streaming": False,
    "supports_edit": True,
    "supports_threads": True,
    "markdown_dialect": "discord",
    "len_unit": "chars",
}


class _StubConnectorServer:
    """Minimal connector: answers hello with a descriptor, echoes outbound."""

    def __init__(self):
        self.received: list[dict] = []
        self._server = None
        self.url = ""
        # Push channel: tests set this to a frame dict to deliver inbound.
        self._to_push: list[dict] = []

    async def start(self):
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        sock = next(iter(self._server.sockets))
        port = sock.getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, ws):
        async for raw in ws:
            for line in str(raw).split("\n"):
                if not line.strip():
                    continue
                frame = json.loads(line)
                self.received.append(frame)
                await self._on_frame(ws, frame)

    async def _on_frame(self, ws, frame):
        ftype = frame.get("type")
        if ftype == "hello":
            await ws.send(json.dumps({"type": "descriptor", "descriptor": DESCRIPTOR}) + "\n")
            # Deliver any queued inbound frames right after handshake.
            for f in self._to_push:
                await ws.send(json.dumps(f) + "\n")
        elif ftype == "outbound":
            action = frame.get("action", {})
            # Echo a successful result correlated by requestId.
            result = {"success": True, "message_id": f"srv-{action.get('op')}"}
            await ws.send(
                json.dumps({"type": "outbound_result", "requestId": frame["requestId"], "result": result})
                + "\n"
            )


@pytest_asyncio.fixture
async def server():
    srv = _StubConnectorServer()
    await srv.start()
    yield srv
    await srv.stop()


@pytest.mark.asyncio
async def test_handshake_negotiates_descriptor(server):
    t = WebSocketRelayTransport(server.url, "discord", "appShared")
    await t.connect()
    try:
        desc = await t.handshake()
        assert desc.platform == "discord"
        assert desc.max_message_length == 2000
        # The hello carried the platform + botId.
        hello = next(f for f in server.received if f["type"] == "hello")
        assert hello["platform"] == "discord"
        assert hello["botId"] == "appShared"
    finally:
        await t.disconnect()


@pytest.mark.asyncio
async def test_inbound_frame_reaches_handler(server):
    server._to_push = [
        {
            "type": "inbound",
            "event": {
                "text": "hello from connector",
                "message_type": "text",
                "source": {"platform": "discord", "chat_id": "chan1", "chat_type": "group", "scope_id": "guildA"},
            },
            "bufferId": "buf-1",
        }
    ]
    received = []
    t = WebSocketRelayTransport(server.url, "discord", "appShared")
    t.set_inbound_handler(lambda ev: received.append(ev) or asyncio.sleep(0))
    await t.connect()
    try:
        await t.handshake()
        # Give the reader a tick to deliver the pushed inbound frame.
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].text == "hello from connector"
        assert received[0].source.scope_id == "guildA"
    finally:
        await t.disconnect()


# ── outbound answers a RESULT when the socket is dead (never a raise/stall) ──


async def _serve(handler):
    """Start a throwaway ws server on an ephemeral port; return (server, url)."""
    srv = await websockets.serve(handler, "127.0.0.1", 0)
    port = next(iter(srv.sockets)).getsockname()[1]
    return srv, f"ws://127.0.0.1:{port}"


async def _await_reader_end(t, timeout_s: float = 3.0) -> None:
    """Block until the transport's read loop has ended (peer closed the socket).

    ``t._reader`` is the reader ``Task``, so wait on it rather than spin-polling
    ``done()``: this wakes the instant the task settles instead of on the next
    10ms tick, which is what makes it deterministic under CI load. ``asyncio.wait``
    specifically, because it neither retrieves the task's result nor cancels it
    on timeout — the caller's ``disconnect()`` still owns the reader's lifecycle.
    """
    assert t._reader is not None, "connect() should have started the read loop"
    _done, pending = await asyncio.wait({t._reader}, timeout=timeout_s)
    if pending:
        raise AssertionError("read loop did not end after the peer closed the socket")


@pytest.mark.asyncio
async def test_send_outbound_returns_result_after_peer_close():
    """A send issued after the peer dropped the socket must RETURN a failure result.

    ``RelayTransport.send_outbound`` is documented to return a result dict, and
    ``RelayAdapter.send``/``send_for_platform``/``edit_message`` consume it with
    no ``try`` — so a raise escapes into the send lane. Before this fix the
    reader left ``self._ws`` pointing at the closed socket, so the
    not-connected guard never fired and ``_send`` raised ``ConnectionClosed``.
    """

    async def handler(ws):
        async for raw in ws:
            for line in str(raw).split("\n"):
                if not line.strip():
                    continue
                if json.loads(line).get("type") == "hello":
                    await ws.send(
                        json.dumps({"type": "descriptor", "descriptor": DESCRIPTOR}) + "\n"
                    )
                    # Let the descriptor flush, then drop the socket.
                    await asyncio.sleep(0.05)
                    await ws.close()
                    return

    srv, url = await _serve(handler)
    t = WebSocketRelayTransport(url, "discord", "appShared", outbound_timeout_s=4.0)
    try:
        await t.connect()
        await t.handshake()
        await _await_reader_end(t)
        result = await t.send_outbound({"op": "send", "chat_id": "c1", "content": "hi"})
        assert isinstance(result, dict)
        assert result.get("success") is False
        assert result.get("error")
    finally:
        await t.disconnect()
        srv.close()
        await srv.wait_closed()


@pytest.mark.asyncio
async def test_outbound_answers_a_result_when_the_send_itself_raises(caplog):
    """The socket dies BETWEEN the not-connected guard and the send.

    That window cannot be closed by any ``self._ws is None`` check, so ``_send``
    still raises there and the catch-all is what keeps the documented result-dict
    contract. Two things it has to do: answer with the same shape as the
    not-connected case, and record the traceback — the returned string carries
    the exception's text but not its origin, so without a traceback a genuine
    defect in the frame-building above it is indistinguishable from an ordinary
    dead socket.

    The one test in this file that substitutes the socket: the window is by
    definition a race, so it cannot be scheduled deterministically against a
    live server.
    """

    class _SocketThatDiedMidSend:
        async def send(self, _payload):
            raise ConnectionResetError("peer closed between the guard and the send")

    t = WebSocketRelayTransport("ws://127.0.0.1:1", "discord", "appShared", outbound_timeout_s=4.0)
    t._ws = _SocketThatDiedMidSend()

    caplog.set_level(logging.DEBUG, logger="gateway.relay.ws_transport")
    result = await t.send_outbound({"op": "send", "chat_id": "c1", "content": "hi"})

    assert result == {
        "success": False,
        "error": "relay send failed: peer closed between the guard and the send",
    }
    # The waiter is discarded rather than left to accumulate per request id.
    assert t._pending == {}

    traced = [r for r in caplog.records if r.exc_info]
    assert traced, "the catch-all must record the traceback, not just the message"
    assert traced[0].exc_info[0] is ConnectionResetError


@pytest.mark.asyncio
async def test_inflight_outbound_fails_fast_on_peer_close():
    """An outbound already awaiting a result must settle when the socket dies.

    Before this fix nothing settled ``_pending`` outside ``disconnect()``, so the
    waiter rode the FULL outbound budget (30s in production) and then reported a
    bogus "timed out" for a socket that had died immediately.
    """

    async def handler(ws):
        async for raw in ws:
            for line in str(raw).split("\n"):
                if not line.strip():
                    continue
                frame = json.loads(line)
                if frame.get("type") == "hello":
                    await ws.send(
                        json.dumps({"type": "descriptor", "descriptor": DESCRIPTOR}) + "\n"
                    )
                elif frame.get("type") == "outbound":
                    # Never answer the request — just drop the socket under it.
                    await asyncio.sleep(0.05)
                    await ws.close()
                    return

    srv, url = await _serve(handler)
    t = WebSocketRelayTransport(url, "discord", "appShared", outbound_timeout_s=4.0)
    try:
        await t.connect()
        await t.handshake()
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await t.send_outbound({"op": "send", "chat_id": "c1", "content": "hi"})
        elapsed = loop.time() - started
        assert result.get("success") is False
        assert result.get("error") == "relay connection lost"
        # Fails fast on the close, rather than burning the whole 4s budget.
        assert elapsed < 2.0, f"outbound rode the timeout budget ({elapsed:.2f}s)"
    finally:
        await t.disconnect()
        srv.close()
        await srv.wait_closed()


@pytest.mark.asyncio
async def test_disconnect_settles_pending_outbound_with_result():
    """disconnect() under an in-flight outbound answers the waiter with a result.

    Same contract as above on the drain/shutdown path: the delivery router
    branches on ``success``/``error``, so a ``RuntimeError`` raised into
    ``RelayAdapter.send`` is not a failure it can route.
    """
    hold_open = asyncio.Event()

    async def handler(ws):
        async for raw in ws:
            for line in str(raw).split("\n"):
                if not line.strip():
                    continue
                frame = json.loads(line)
                if frame.get("type") == "hello":
                    await ws.send(
                        json.dumps({"type": "descriptor", "descriptor": DESCRIPTOR}) + "\n"
                    )
                elif frame.get("type") == "outbound":
                    # Leave the request unanswered so it is still pending.
                    await hold_open.wait()
                    return

    srv, url = await _serve(handler)
    t = WebSocketRelayTransport(url, "discord", "appShared", outbound_timeout_s=10.0)
    try:
        await t.connect()
        await t.handshake()
        call = asyncio.create_task(
            t.send_outbound({"op": "send", "chat_id": "c1", "content": "hi"})
        )
        pending = False
        for _ in range(300):
            if t._pending:
                pending = True
                break
            await asyncio.sleep(0.01)
        assert pending, "expected an in-flight outbound before disconnect"
        await t.disconnect()
        result = await asyncio.wait_for(call, timeout=2.0)
        assert result.get("success") is False
        assert result.get("error") == "relay transport closed"
    finally:
        hold_open.set()
        await t.disconnect()
        srv.close()
        await srv.wait_closed()


# ── Phase 7 Unit 7d-B: terminal 4401 (opt-out revocation) ────────────────────


class _Revoking4401Server:
    """Connector stub that, on hello, optionally sends a descriptor and then
    closes the socket with application code 4401 (unauthorized) — the shape of a
    connector that has revoked this gateway's per-gateway secret (opt-out)."""

    def __init__(self, *, send_descriptor_first: bool):
        self._server = None
        self.url = ""
        self._send_descriptor_first = send_descriptor_first

    async def start(self):
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        port = next(iter(self._server.sockets)).getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, ws):
        async for raw in ws:
            for line in str(raw).split("\n"):
                if not line.strip():
                    continue
                frame = json.loads(line)
                if frame.get("type") == "hello":
                    if self._send_descriptor_first:
                        await ws.send(
                            json.dumps({"type": "descriptor", "descriptor": DESCRIPTOR}) + "\n"
                        )
                        # Let the descriptor flush + be processed before the close.
                        await asyncio.sleep(0.05)
                    # Close with 4401 (the connector's "unauthorized" close).
                    await ws.close(code=4401, reason="unauthorized")
                    return


@pytest.mark.asyncio
async def test_4401_after_handshake_is_terminal_no_reconnect():
    """A 4401 close AFTER a successful handshake = a revoked credential (opt-out):
    the transport latches auth_revoked and does NOT spin the reconnect supervisor."""
    srv = _Revoking4401Server(send_descriptor_first=True)
    await srv.start()
    try:
        t = WebSocketRelayTransport(
            srv.url, "discord", "appShared",
            gateway_id="gw-x", upgrade_secret="secret-x",
            reconnect=True, reconnect_backoff_s=0.05,
        )
        await t.connect()
        await t.handshake()  # records _handshake_succeeded
        # Wait for the server's 4401 close to propagate through the read loop.
        for _ in range(100):
            if t.auth_revoked:
                break
            await asyncio.sleep(0.02)
        assert t.auth_revoked is True
        # Terminal: no reconnect supervisor was spawned.
        assert t._supervisor is None
        # Give a reconnect (if it were going to happen) time to NOT happen.
        await asyncio.sleep(0.2)
        assert t._supervisor is None
    finally:
        await t.disconnect()
        await srv.stop()


# ── one raising frame must not take the rest of its chunk with it ────────────


@pytest.mark.asyncio
async def test_raising_frame_does_not_discard_the_rest_of_its_chunk(caplog):
    """A frame whose handler raises must not drop the frames batched behind it.

    Relay frames are newline-delimited and several of them can ride a SINGLE
    WebSocket message, so the read loop splits one chunk into N frames and
    dispatches them in a row. Dispatching them unguarded made the first raising
    frame end the ``async for`` outright: the socket was dropped, every frame
    already parsed behind the failing one was discarded, and ``buf`` had already
    been reassigned so the trailing partial frame went with them.

    The cost lands on a caller that had nothing to do with the bad frame. An
    ``outbound_result`` batched behind a poisoned ``inbound`` never reaches
    ``_pending``, so the future ``send_outbound`` is awaiting gets settled by the
    connection-lost path instead of by its own result — one bad inbound frame
    fails an unrelated outbound send.
    """

    async def handler(ws):
        async for raw in ws:
            for line in str(raw).split("\n"):
                if not line.strip():
                    continue
                frame = json.loads(line)
                if frame.get("type") == "hello":
                    await ws.send(
                        json.dumps({"type": "descriptor", "descriptor": DESCRIPTOR}) + "\n"
                    )
                elif frame.get("type") == "outbound":
                    # ONE WebSocket message carrying TWO frames: a poisoned
                    # inbound, then the result the caller is blocked on.
                    poisoned = {
                        "type": "inbound",
                        "event": {
                            "text": "boom",
                            "message_type": "text",
                            "source": {
                                "platform": "discord",
                                "chat_id": "c1",
                                "chat_type": "group",
                                "scope_id": "guildA",
                            },
                        },
                    }
                    answer = {
                        "type": "outbound_result",
                        "requestId": frame["requestId"],
                        "result": {"success": True, "message_id": "srv-send"},
                    }
                    await ws.send(json.dumps(poisoned) + "\n" + json.dumps(answer) + "\n")

    async def raising_inbound(_event):
        raise RuntimeError("inbound handler blew up")

    srv, url = await _serve(handler)
    t = WebSocketRelayTransport(url, "discord", "appShared", outbound_timeout_s=4.0)
    t.set_inbound_handler(raising_inbound)
    caplog.set_level(logging.WARNING, logger="gateway.relay.ws_transport")
    try:
        await t.connect()
        await t.handshake()
        result = await t.send_outbound({"op": "send", "chat_id": "c1", "content": "hi"})
        # The result shared its chunk with the frame that raised, so this is the
        # assertion that matters: it was still dispatched.
        assert result == {"success": True, "message_id": "srv-send"}
        # And the loop skipped one frame rather than ending: the socket is still
        # live, so the reconnect supervisor was never needed.
        assert t._ws is not None
        assert t._reader is not None and not t._reader.done()
        assert t._supervisor is None
        # The skip is loud — a handler that raises is still a defect to chase.
        assert any(
            r.exc_info and r.exc_info[0] is RuntimeError for r in caplog.records
        ), "the skipped frame's traceback must be recorded"
    finally:
        await t.disconnect()
        srv.close()
        await srv.wait_closed()


@pytest.mark.asyncio
async def test_non_object_frame_is_skipped_like_an_undecodable_one(caplog):
    """A frame that is valid JSON but not an OBJECT is skipped, not raised on.

    ``_handle_frame`` opens by guarding the decode precisely so a bad frame from
    the connector is skipped rather than fatal. ``json.loads`` succeeds on a bare
    array/string/number/null, though, so those walked past that guard into
    ``frame.get("type")`` and raised ``AttributeError`` out of the one function
    whose stated contract is that a bad frame cannot do that.
    """
    t = WebSocketRelayTransport("ws://127.0.0.1:1", "discord", "appShared")
    caplog.set_level(logging.WARNING, logger="gateway.relay.ws_transport")

    # Valid JSON, no object: nothing this protocol has a reading for.
    for line in ("[]", '["descriptor"]', '"descriptor"', "3", "null"):
        await t._handle_frame(line)

    assert caplog.text.count("skipping malformed frame") == 5
    # Nothing was mistaken for a real frame.
    assert t._descriptor is None
    assert t._descriptors_by_platform == {}



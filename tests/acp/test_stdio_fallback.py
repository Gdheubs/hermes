"""Regression tests for #84515: ``hermes acp`` crashing with
``ValueError: Pipe transport is only for pipes, sockets and character devices``
when stdout is not a pipe.

The acp SDK's POSIX stdio transport calls ``loop.connect_write_pipe`` /
``loop.connect_read_pipe`` on the real stdio, which raises ValueError when
stdout is a regular file (redirected) or an in-memory wrapper.  The Hermes
entry layer detects the non-pipe stdio and substitutes pump-backed streams
instead of letting the SDK raise.

Platform note: detection is exercised with the platform passed as data
(``"linux"`` / ``"win32"``) so the decision logic is tested on every host;
``main()`` routing is tested by forcing the fallback branch.  On Windows the
SDK never hits this bug (it uses a thread feeder + custom stdout transport),
so the fallback is a POSIX-only path.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import threading

import acp
import pytest

from acp_adapter import entry


class _FakeStd:
    """Stand-in for sys.stdin/sys.stdout that is deliberately NOT pipe-like:
    no ``fileno()`` and an in-memory buffer — the redirected/wrapped-stdout
    case that makes the SDK's connect_write_pipe raise.

    Exposes the attribute surface CPython's ``sys.stdin``/``sys.stdout``
    setters validate (read/readline for stdin, write/flush for stdout) plus
    ``.buffer`` so the pump's feeder/transport can use it like a real stream.
    """

    def __init__(self, data: bytes = b"") -> None:
        self.buffer = io.BytesIO(data)

    def read(self, *args):  # noqa: ANN002
        return self.buffer.read(*args)

    def readline(self, *args):  # noqa: ANN002
        return self.buffer.readline(*args)

    def write(self, data):  # noqa: ANN001
        if isinstance(data, str):
            data = data.encode()
        return self.buffer.write(data)

    def flush(self) -> None:
        self.buffer.flush()


class _E2EStdin:
    """Fake stdin for the full-protocol test: serves the request line, then
    blocks (like a live pipe waiting for the client's next message) until the
    test releases EOF — avoiding a shutdown race with the SDK's background
    task supervisor."""

    def __init__(self, first_line: bytes) -> None:
        self._first = first_line
        self._served = False
        self._release = threading.Event()

    def read(self, *args):  # noqa: ANN002
        return self.readline(*args)

    def readline(self, *args):  # noqa: ANN002
        if not self._served:
            self._served = True
            return self._first
        self._release.wait(timeout=15)
        return b""

    def release_eof(self) -> None:
        self._release.set()


# -- detection --------------------------------------------------------------


def test_is_pipe_like_regular_file_is_false(tmp_path) -> None:
    with open(tmp_path / "out.txt", "wb") as f:
        assert entry._is_pipe_like(f) is False


def test_is_pipe_like_in_memory_wrapper_is_false() -> None:
    assert entry._is_pipe_like(io.StringIO()) is False


def test_is_pipe_like_pipe_is_true() -> None:
    r, w = os.pipe()
    try:
        with os.fdopen(r, "rb") as fr, os.fdopen(w, "wb") as fw:
            assert entry._is_pipe_like(fr) is True
            assert entry._is_pipe_like(fw) is True
    finally:
        # os.fdopen takes ownership; nothing left to close here.
        pass


def test_needs_stdio_fallback_win32_never_triggers(monkeypatch, tmp_path) -> None:
    """On Windows the SDK never connects to the real stdio, so no fallback
    is needed even when stdout is a regular file."""
    in_path = tmp_path / "in.txt"
    in_path.write_bytes(b"")
    with open(tmp_path / "out.txt", "wb") as out, open(in_path, "rb") as inp:
        monkeypatch.setattr(entry.sys, "stdout", out)
        monkeypatch.setattr(entry.sys, "stdin", inp)
        assert entry._needs_stdio_fallback(platform="win32") is False


def test_needs_stdio_fallback_posix_redirected_stdout_triggers(monkeypatch, tmp_path) -> None:
    """The reported bug: stdout redirected to a regular file must trigger
    the fallback on POSIX even when stdin is a clean pipe."""
    r, w = os.pipe()
    try:
        with os.fdopen(r, "rb") as stdin, open(tmp_path / "out.txt", "wb") as stdout:
            monkeypatch.setattr(entry.sys, "stdin", stdin)
            monkeypatch.setattr(entry.sys, "stdout", stdout)
            assert entry._needs_stdio_fallback(platform="linux") is True
    finally:
        os.close(w)


def test_needs_stdio_fallback_posix_all_pipes_no_fallback(monkeypatch) -> None:
    """Normal ACP hosting (clean stdio pipes) keeps the SDK default path."""
    r1, w1 = os.pipe()
    r2, w2 = os.pipe()
    try:
        with os.fdopen(r1, "rb") as stdin, os.fdopen(w2, "wb") as stdout:
            monkeypatch.setattr(entry.sys, "stdin", stdin)
            monkeypatch.setattr(entry.sys, "stdout", stdout)
            assert entry._needs_stdio_fallback(platform="linux") is False
    finally:
        os.close(w1)
        os.close(r2)


# -- fallback streams -------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_streams_pump_real_stdio(monkeypatch) -> None:
    """The pump must forward stdin -> reader and writer -> stdout without any
    ValueError — i.e. a graceful fallback when stdio is not pipe-backed."""
    request = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
    response = b'{"jsonrpc":"2.0","id":1,"result":{"protocol_version":1}}\n'

    in_fake = _FakeStd(request)
    out_fake = _FakeStd()
    monkeypatch.setattr(entry.sys, "stdin", in_fake)
    monkeypatch.setattr(entry.sys, "stdout", out_fake)

    reader, writer = entry._build_fallback_stdio_streams(asyncio.get_running_loop())

    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    assert json.loads(line)["method"] == "initialize"

    writer.write(response)
    await asyncio.wait_for(writer.drain(), timeout=5.0)
    assert out_fake.buffer.getvalue() == response

    writer.close()

    # The stdin feeder must have hit EOF and signalled the reader.
    rest = await asyncio.wait_for(reader.read(), timeout=5.0)
    assert rest == b""
    assert reader.at_eof()


# -- main() routing ---------------------------------------------------------


def test_main_uses_stdio_fallback_when_stdout_not_pipe(monkeypatch, tmp_path) -> None:
    """#84515 regression: with stdout redirected to a regular file,
    ``entry.main()`` must not crash with the SDK's ValueError; it must
    substitute pump-backed streams and hand them to ``acp.run_agent``."""
    calls = {}

    async def fake_run_agent(agent, **kwargs):  # noqa: ANN001
        calls["input_stream"] = kwargs.get("input_stream")
        calls["output_stream"] = kwargs.get("output_stream")
        calls["use_unstable_protocol"] = kwargs.get("use_unstable_protocol")
        # Let the stdin feeder thread reach EOF before the loop closes.
        if calls["output_stream"] is not None:
            try:
                await asyncio.wait_for(calls["output_stream"].read(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
        if calls["input_stream"] is not None:
            calls["input_stream"].close()

    # stdout is a regular file — the SDK's connect_write_pipe would raise.
    # Text-mode wrappers match the real ``hermes acp > out.txt`` redirect,
    # where sys.stdout is a TextIOWrapper exposing ``.buffer``.
    in_path = tmp_path / "acp_in.txt"
    in_path.write_text("", encoding="utf-8")
    out_file = open(tmp_path / "acp_out.txt", "w", encoding="utf-8")
    in_file = open(in_path, "r", encoding="utf-8")
    monkeypatch.setattr(entry.sys, "stdout", out_file)
    monkeypatch.setattr(entry.sys, "stdin", in_file)
    monkeypatch.setattr(entry, "_setup_logging", lambda: None)
    monkeypatch.setattr(entry, "_load_env", lambda: None)
    monkeypatch.setenv("HERMES_ACP_SKIP_CONFIGURED_MCP", "1")
    # Detection is covered by the unit tests above; force the fallback branch
    # so the routing itself is exercised on every host (on Windows the SDK
    # never hits the bug, so detection naturally returns False).
    monkeypatch.setattr(entry, "_needs_stdio_fallback", lambda: True)
    monkeypatch.setattr(acp, "run_agent", fake_run_agent)

    entry.main([])

    assert calls["use_unstable_protocol"] is True
    assert calls["output_stream"] is not None, (
        "fallback reader stream must be handed to run_agent"
    )
    assert calls["input_stream"] is not None, (
        "fallback writer stream must be handed to run_agent"
    )
    assert isinstance(calls["output_stream"], asyncio.StreamReader)
    assert isinstance(calls["input_stream"], asyncio.StreamWriter)
    out_file.close()
    in_file.close()


# -- full-protocol round trip -----------------------------------------------


class _MinimalAgent:
    """Minimal acp.Agent — enough for ``initialize`` to round-trip."""

    async def initialize(self, **kwargs):  # noqa: ANN003
        from acp.schema import AgentCapabilities, InitializeResponse

        return InitializeResponse(
            protocol_version=1,
            agent_capabilities=AgentCapabilities(),
        )

    async def new_session(self, cwd, mcp_servers=None, **kwargs):  # noqa: ANN001, ANN003
        from acp.schema import NewSessionResponse

        return NewSessionResponse(session_id="test")

    async def prompt(self, session_id, prompt, **kwargs):  # noqa: ANN001, ANN003
        from acp.schema import PromptResponse

        return PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id, **kwargs):  # noqa: ANN001, ANN003
        pass

    async def authenticate(self, **kwargs):  # noqa: ANN003
        pass

    def on_connect(self, conn):  # noqa: ANN001
        pass


@pytest.mark.asyncio
async def test_run_agent_serves_initialize_over_fallback_streams(monkeypatch) -> None:
    """Full-protocol regression: a real ``acp.run_agent`` over the pump
    streams must serve ``initialize`` to a redirected (non-pipe) stdout —
    the exact scenario that crashed with ValueError before the fix."""
    import acp

    request = (
        b'{"jsonrpc":"2.0","id":1,"method":"initialize",'
        b'"params":{"protocolVersion":1}}\n'
    )
    stdin = _E2EStdin(request)
    stdout = _FakeStd()  # in-memory stdout: not pipe-like, like a redirect
    monkeypatch.setattr(entry.sys, "stdin", stdin)
    monkeypatch.setattr(entry.sys, "stdout", stdout)

    reader, writer = entry._build_fallback_stdio_streams(asyncio.get_running_loop())
    agent_task = asyncio.create_task(
        acp.run_agent(
            _MinimalAgent(),
            input_stream=writer,
            output_stream=reader,
            use_unstable_protocol=True,
        )
    )

    try:
        # Wait for the initialize response to reach the (redirected) stdout.
        deadline = asyncio.get_running_loop().time() + 10.0
        response = b""
        while asyncio.get_running_loop().time() < deadline:
            if stdout.buffer.getvalue():
                response = stdout.buffer.getvalue()
                break
            await asyncio.sleep(0.05)
        assert response, "no ACP response reached the redirected stdout"

        payload = json.loads(response.decode().strip())
        assert payload["id"] == 1
        assert payload["result"]["protocolVersion"] == 1, payload

        # Now let stdin hit EOF so the agent shuts down cleanly.
        stdin.release_eof()
        await asyncio.wait_for(agent_task, timeout=10.0)
    finally:
        if not agent_task.done():
            agent_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await agent_task

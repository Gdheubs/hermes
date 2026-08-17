"""Tests for the gateway's child-session live mirror.

A delegated child runs synchronously inside the parent's turn; its activity
reaches the gateway only as relayed ``subagent.*`` events on the PARENT sid
(tagged with ``child_session_id``). When a UI resumes the child's own session
(desktop open-in-new-window), ``_mirror_subagent_to_child`` translates those
relayed events into native stream events on the CHILD's live sid so the window
shows a real midstream turn instead of sitting silent until persistence.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class _RecordingTransport:
    def __init__(self):
        self.frames: list[dict] = []

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        pass


@pytest.fixture()
def server():
    # Mocks are scoped to the initial import only (see
    # tests/tui_gateway/test_protocol.py for the rationale).
    with patch.dict(
        "sys.modules",
        {
            "hermes_constants": MagicMock(
                get_hermes_home=MagicMock(return_value="/tmp/hermes_test_child_mirror")
            ),
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
            "hermes_state": MagicMock(),
        },
    ):
        import importlib

        mod = importlib.import_module("tui_gateway.server")

    yield mod
    mod._sessions.clear()
    mod._pending.clear()
    mod._answers.clear()
    mod._child_mirrors.clear()
    mod._active_child_runs.clear()


@pytest.fixture()
def emits(server, monkeypatch):
    captured: list = []
    original = server._emit_session_owned

    def record(event, sid, payload=None, **kwargs):
        written = original(event, sid, payload, **kwargs)
        if written and event is not None:
            captured.append((event, sid, payload))
        return written

    monkeypatch.setattr(server, "_emit_session_owned", record)
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda sid: True)
    return captured


def _relay(server, event_type, **payload):
    """Drive _on_tool_progress the way the delegate relay does."""
    server._sessions.setdefault(
        "parent-sid",
        {"profile_home": None, "transport": _RecordingTransport()},
    )
    server._on_tool_progress(
        "parent-sid",
        event_type,
        payload.pop("tool_name", None),
        payload.pop("preview", None),
        None,
        goal="research X",
        task_count=1,
        task_index=0,
        **payload,
    )


def _watch(server, child_key="child-1", sid="live-1"):
    transport = _RecordingTransport()
    server._sessions[sid] = {
        "session_key": child_key,
        "agent": None,
        "transport": transport,
    }
    return transport


def test_no_live_child_session_no_mirror(server, emits):
    _relay(server, "subagent.tool", tool_name="terminal", child_session_id="child-1")

    # Only the parent-sid relay event — nothing mirrored, no state retained.
    assert [(e, s) for e, s, _ in emits] == [("subagent.tool", "parent-sid")]
    assert server._child_mirrors == {}


def test_missing_parent_drops_subagent_event_before_fallback_transport(server, monkeypatch):
    """An absent parent cannot leak child output through process fallback IO."""
    fallback = _RecordingTransport()
    monkeypatch.setattr(server, "_stdio_transport", fallback)
    monkeypatch.setattr(server, "current_transport", lambda: fallback)
    server._sessions.pop("missing-parent", None)

    server._on_tool_progress(
        "missing-parent",
        "subagent.tool",
        name="terminal",
        preview="private child output",
        child_session_id="child-1",
    )
    server._on_tool_progress(
        "missing-parent",
        "subagent.complete",
        child_session_id="child-1",
        status="completed",
        summary="private completion",
    )

    assert len(fallback.frames) == 0
    assert server._child_mirrors == {}
    assert server._active_child_runs == {}


def test_parent_without_explicit_transport_drops_entire_relay(server, monkeypatch):
    fallback = _RecordingTransport()
    child = _watch(server)
    server._sessions["parent-sid"] = {"profile_home": None}
    monkeypatch.setattr(server, "_stdio_transport", fallback)
    monkeypatch.setattr(server, "current_transport", lambda: fallback)

    _relay(server, "subagent.text", preview="sensitive", child_session_id="child-1")

    assert len(child.frames) == 0
    assert len(fallback.frames) == 0
    assert server._child_mirrors == {}
    assert server._active_child_runs == {}


def test_detached_parent_text_cannot_authorize_child_mirror(server, monkeypatch):
    """A parked disconnected parent is resumable state, not live authority."""
    fallback = _RecordingTransport()
    child = _watch(server)
    server._sessions["parent-sid"] = {
        "profile_home": None,
        "transport": server._detached_ws_transport,
    }
    monkeypatch.setattr(server, "_stdio_transport", fallback)
    monkeypatch.setattr(server, "current_transport", lambda: fallback)

    _relay(server, "subagent.text", preview="sensitive", child_session_id="child-1")

    assert len(child.frames) == 0
    assert len(fallback.frames) == 0
    assert server._child_mirrors == {}
    assert server._active_child_runs == {}


def test_closed_parent_text_cannot_authorize_child_mirror(server, monkeypatch):
    """A closed WSTransport-equivalent cannot authorize during detach races."""
    fallback = _RecordingTransport()
    child = _watch(server)
    closed_parent = _RecordingTransport()
    setattr(closed_parent, "_closed", True)
    server._sessions["parent-sid"] = {
        "profile_home": None,
        "transport": closed_parent,
    }
    monkeypatch.setattr(server, "_stdio_transport", fallback)
    monkeypatch.setattr(server, "current_transport", lambda: fallback)

    _relay(server, "subagent.text", preview="sensitive", child_session_id="child-1")

    assert len(closed_parent.frames) == 0
    assert len(child.frames) == 0
    assert len(fallback.frames) == 0
    assert server._child_mirrors == {}
    assert server._active_child_runs == {}


def test_parent_closing_during_write_cannot_authorize_child(server, monkeypatch):
    """A successful-looking write that closes its transport fails revalidation."""
    fallback = _RecordingTransport()
    child = _watch(server)

    class ClosingTransport(_RecordingTransport):
        def write(self, obj):
            written = super().write(obj)
            setattr(self, "_closed", True)
            return written

    parent = ClosingTransport()
    server._sessions["parent-sid"] = {
        "profile_home": None,
        "transport": parent,
    }
    monkeypatch.setattr(server, "_stdio_transport", fallback)
    monkeypatch.setattr(server, "current_transport", lambda: fallback)

    _relay(server, "subagent.tool", tool_name="terminal", child_session_id="child-1")

    assert [frame["params"]["type"] for frame in parent.frames] == ["subagent.tool"]
    assert len(child.frames) == 0
    assert len(fallback.frames) == 0
    assert server._child_mirrors == {}
    assert server._active_child_runs == {}


def test_child_without_explicit_transport_drops_only_synthetic_state(server, monkeypatch):
    fallback = _RecordingTransport()
    parent = _RecordingTransport()
    server._sessions.update(
        {
            "parent-sid": {"profile_home": None, "transport": parent},
            "live-1": {"session_key": "child-1", "agent": None},
        }
    )
    monkeypatch.setattr(server, "_stdio_transport", fallback)
    monkeypatch.setattr(server, "current_transport", lambda: fallback)

    _relay(server, "subagent.tool", tool_name="terminal", child_session_id="child-1")

    assert [frame["params"]["type"] for frame in parent.frames] == ["subagent.tool"]
    assert len(fallback.frames) == 0
    assert server._child_mirrors == {}
    assert server._child_run_active("child-1", profile_home=None)


def test_finalized_registered_parent_drops_entire_relay(server, monkeypatch):
    fallback = _RecordingTransport()
    parent = _RecordingTransport()
    child = _watch(server)
    server._sessions["final-parent"] = {
        "_finalized": True,
        "profile_home": None,
        "transport": parent,
    }
    monkeypatch.setattr(server, "_stdio_transport", fallback)
    monkeypatch.setattr(server, "current_transport", lambda: fallback)

    server._on_tool_progress(
        "final-parent",
        "subagent.text",
        preview="sensitive",
        child_session_id="child-1",
    )

    assert len(parent.frames) == 0
    assert len(child.frames) == 0
    assert len(fallback.frames) == 0
    assert server._child_mirrors == {}
    assert server._active_child_runs == {}


@pytest.mark.parametrize("finalize", [False, True], ids=["removed", "finalized"])
@pytest.mark.parametrize("event_type", ["subagent.tool", "subagent.text"])
def test_parent_lifecycle_race_cannot_fallback_or_authorize_child(
    server, monkeypatch, finalize, event_type
):
    """Barrier parent teardown between initial lookup and scoped authorization."""
    fallback = _RecordingTransport()
    current = _RecordingTransport()
    parent = _RecordingTransport()
    child = _watch(server)
    parent_session = {
        "profile_home": None,
        "transport": parent,
    }
    server._sessions["racing-parent"] = parent_session
    monkeypatch.setattr(server, "_stdio_transport", fallback)
    monkeypatch.setattr(server, "current_transport", lambda: current)
    original = server._emit_session_owned
    crossed = False

    def teardown_before_scoped_emit(event, sid, payload=None, **kwargs):
        nonlocal crossed
        if sid == "racing-parent" and not crossed:
            crossed = True
            if finalize:
                parent_session["_finalized"] = True
            else:
                server._sessions.pop(sid, None)
        return original(event, sid, payload, **kwargs)

    monkeypatch.setattr(server, "_emit_session_owned", teardown_before_scoped_emit)
    server._on_tool_progress(
        "racing-parent",
        event_type,
        name="terminal",
        preview="sensitive",
        child_session_id="child-1",
    )

    assert crossed
    assert len(parent.frames) == 0
    assert len(child.frames) == 0
    assert len(current.frames) == 0
    assert len(fallback.frames) == 0
    assert server._child_mirrors == {}
    assert server._active_child_runs == {}


@pytest.mark.parametrize("finalize", [False, True], ids=["removed", "finalized"])
def test_parent_teardown_during_direct_write_cannot_authorize_child(
    server, monkeypatch, finalize
):
    """A write-triggered teardown is revalidated before mirror/liveness work."""
    fallback = _RecordingTransport()
    current = _RecordingTransport()
    child = _watch(server)
    parent_session: dict = {"profile_home": None}

    class TeardownTransport(_RecordingTransport):
        def write(self, obj):
            if finalize:
                parent_session["_finalized"] = True
            else:
                server._sessions.pop("racing-parent", None)
            return super().write(obj)

    parent = TeardownTransport()
    parent_session["transport"] = parent
    server._sessions["racing-parent"] = parent_session
    monkeypatch.setattr(server, "_stdio_transport", fallback)
    monkeypatch.setattr(server, "current_transport", lambda: current)

    server._on_tool_progress(
        "racing-parent",
        "subagent.tool",
        name="terminal",
        preview="sensitive",
        child_session_id="child-1",
    )

    assert [frame["params"]["type"] for frame in parent.frames] == ["subagent.tool"]
    assert len(child.frames) == 0
    assert len(current.frames) == 0
    assert len(fallback.frames) == 0
    assert server._child_mirrors == {}
    assert server._active_child_runs == {}


@pytest.mark.parametrize("finalize", [False, True], ids=["removed", "finalized"])
def test_child_lifecycle_race_before_first_emit_drops_mirror_without_fallback(
    server, monkeypatch, finalize
):
    """Barrier child teardown after lookup but before message.start."""
    fallback = _RecordingTransport()
    current = _RecordingTransport()
    parent = _RecordingTransport()
    child = _watch(server)
    child_session = server._sessions["live-1"]
    server._sessions["parent-sid"] = {
        "profile_home": None,
        "transport": parent,
    }
    monkeypatch.setattr(server, "_stdio_transport", fallback)
    monkeypatch.setattr(server, "current_transport", lambda: current)
    original_find = server._find_live_session_by_key

    def teardown_after_lookup(child_key, **kwargs):
        live = original_find(child_key, **kwargs)
        assert live is not None
        if finalize:
            child_session["_finalized"] = True
        else:
            server._sessions.pop(live[0], None)
        return live

    monkeypatch.setattr(server, "_find_live_session_by_key", teardown_after_lookup)
    _relay(server, "subagent.tool", tool_name="terminal", child_session_id="child-1")

    assert [frame["params"]["type"] for frame in parent.frames] == ["subagent.tool"]
    assert len(child.frames) == 0
    assert len(current.frames) == 0
    assert len(fallback.frames) == 0
    assert server._child_mirrors == {}
    assert server._child_run_active("child-1", profile_home=None)


@pytest.mark.parametrize("finalize", [False, True], ids=["removed", "finalized"])
def test_child_lifecycle_race_between_mirror_emits_drops_state_without_fallback(
    server, monkeypatch, finalize
):
    """Barrier child teardown after message.start but before the next frame."""
    fallback = _RecordingTransport()
    current = _RecordingTransport()
    parent = _RecordingTransport()
    child = _watch(server)
    child_session = server._sessions["live-1"]
    server._sessions["parent-sid"] = {
        "profile_home": None,
        "transport": parent,
    }
    monkeypatch.setattr(server, "_stdio_transport", fallback)
    monkeypatch.setattr(server, "current_transport", lambda: current)
    original = server._emit_session_owned
    child_calls = 0

    def teardown_before_second_child_emit(event, sid, payload=None, **kwargs):
        nonlocal child_calls
        if sid == "live-1":
            child_calls += 1
            if child_calls == 2:
                if finalize:
                    child_session["_finalized"] = True
                else:
                    server._sessions.pop(sid, None)
        return original(event, sid, payload, **kwargs)

    monkeypatch.setattr(server, "_emit_session_owned", teardown_before_second_child_emit)
    _relay(server, "subagent.tool", tool_name="terminal", child_session_id="child-1")

    assert child_calls == 2
    assert [frame["params"]["type"] for frame in parent.frames] == ["subagent.tool"]
    assert [frame["params"]["type"] for frame in child.frames] == ["message.start"]
    assert len(current.frames) == 0
    assert len(fallback.frames) == 0
    assert server._child_mirrors == {}
    assert server._child_run_active("child-1", profile_home=None)


def test_live_child_session_gets_native_stream(server, emits):
    # A window resumed the child session: live sid differs from the stored key.
    _watch(server)

    _relay(server, "subagent.tool", tool_name="terminal", preview="ls", child_session_id="child-1")
    _relay(server, "subagent.thinking", preview="hmm", child_session_id="child-1")
    _relay(server, "subagent.tool", tool_name="read_file", child_session_id="child-1")
    _relay(
        server,
        "subagent.complete",
        child_session_id="child-1",
        status="completed",
        summary="done deal",
    )

    child = [(e, p) for e, s, p in emits if s == "live-1"]

    # Synthetic turn: start → tool → reasoning → tool rotation → close + summary.
    assert [e for e, _ in child] == [
        "message.start",
        "tool.start",
        "reasoning.delta",
        "tool.complete",
        "tool.start",
        "tool.complete",
        "message.complete",
    ]
    first_tool = child[1][1]
    assert first_tool["name"] == "terminal"
    assert first_tool["tool_id"].startswith("submirror:child-1:")
    assert child[2][1] == {"text": "hmm"}
    # The rotated-out tool closes with the same id it opened with.
    assert child[3][1]["tool_id"] == first_tool["tool_id"]
    assert child[6][1] == {"text": "done deal"}

    # Parent relay is untouched alongside the mirror.
    assert [e for e, s, _ in emits if s == "parent-sid"] == [
        "subagent.tool",
        "subagent.thinking",
        "subagent.tool",
        "subagent.complete",
    ]
    # Completion clears mirror state.
    assert server._child_mirrors == {}


def test_window_closed_midrun_drops_state_then_fresh_turn_on_reopen(server, emits):
    _watch(server)
    _relay(server, "subagent.tool", tool_name="terminal", child_session_id="child-1")
    assert (None, "child-1") in server._child_mirrors

    # Window closes → live session gone → state dropped on the next event.
    server._sessions.clear()
    _relay(server, "subagent.tool", tool_name="read_file", child_session_id="child-1")
    assert server._child_mirrors == {}

    # Reopen under a new live sid → a fresh synthetic turn starts.
    emits.clear()
    _watch(server, sid="live-2")
    _relay(server, "subagent.tool", tool_name="web_search", child_session_id="child-1")
    assert [(e, s) for e, s, _ in emits if s == "live-2"] == [
        ("message.start", "live-2"),
        ("tool.start", "live-2"),
    ]


def test_upgraded_child_session_not_mirrored(server, emits):
    """A watch window upgraded to a full session (agent built) owns a real
    native stream — mirroring on top would interleave two turns on one sid."""
    server._sessions["live-1"] = {
        "session_key": "child-1",
        "agent": object(),
        "transport": _RecordingTransport(),
    }

    _relay(server, "subagent.tool", tool_name="terminal", child_session_id="child-1")

    assert [(e, s) for e, s, _ in emits] == [("subagent.tool", "parent-sid")]
    assert server._child_mirrors == {}
    # Liveness registry still updates — it serves resume, not the mirror.
    assert (None, "child-1") in server._active_child_runs


def test_stale_child_run_not_reported_active(server, emits):
    """A leaked registry entry (lost completion event) must age out instead of
    pinning running=true on every future lazy resume of that child."""
    server._active_child_runs[(None, "child-1")] = 0.0  # epoch — ancient

    assert server._child_run_active("child-1", profile_home=None) is False

    _relay(server, "subagent.tool", tool_name="terminal", child_session_id="child-1")
    assert server._child_run_active("child-1", profile_home=None) is True


def test_prompt_submit_rejected_while_child_run_active(server, emits):
    """Typing into a watch window mid-run must not build a second agent racing
    the in-flight child on the same stored session — busy error instead."""
    import threading

    server._sessions["live-1"] = {
        "agent": None,
        "history_lock": threading.Lock(),
        "lazy": True,
        "running": False,
        "session_key": "child-1",
        "transport": _RecordingTransport(),
    }
    _relay(server, "subagent.tool", tool_name="terminal", child_session_id="child-1")

    result = server._methods["prompt.submit"]("rid-1", {"session_id": "live-1", "text": "hi"})
    assert result["error"]["code"] == 4009

    # Run completes → the same submit upgrades into a real conversation
    # (passes the guard; fails later only because this test stubs no agent).
    _relay(server, "subagent.complete", child_session_id="child-1", status="completed", summary="ok")
    assert server._child_run_active("child-1", profile_home=None) is False


def test_active_child_runs_registry_tracks_liveness(server, emits):
    """Every relayed event marks the child as in flight (even with no window
    open), and completion clears it — lazy watch resumes read this registry to
    report running=true while the child is silent inside a long tool call."""
    _relay(server, "subagent.start", preview="go", child_session_id="child-1")
    assert (None, "child-1") in server._active_child_runs

    _relay(server, "subagent.tool", tool_name="terminal", child_session_id="child-1")
    assert (None, "child-1") in server._active_child_runs

    _relay(server, "subagent.complete", child_session_id="child-1", status="completed", summary="ok")
    assert (None, "child-1") not in server._active_child_runs


def test_duplicate_child_ids_are_mirrored_and_tracked_per_profile(server, tmp_path):
    """Profile B relay frames and completion cannot touch profile A state."""
    profile_a = tmp_path / "profiles" / "a"
    profile_b = tmp_path / "profiles" / "b"
    profile_a.mkdir(parents=True)
    profile_b.mkdir(parents=True)
    watch_a = _RecordingTransport()
    watch_b = _RecordingTransport()

    server._sessions.update(
        {
            "parent-a": {
                "profile_home": str(profile_a),
                "transport": _RecordingTransport(),
            },
            "parent-b": {
                "profile_home": str(profile_b),
                "transport": _RecordingTransport(),
            },
            "watch-a": {
                "session_key": "duplicate-child",
                "profile_home": str(profile_a),
                "agent": None,
                "transport": watch_a,
            },
            "watch-b": {
                "session_key": "duplicate-child",
                "profile_home": str(profile_b),
                "agent": None,
                "transport": watch_b,
            },
        }
    )

    server._on_tool_progress(
        "parent-b",
        "subagent.text",
        preview="B only",
        child_session_id="duplicate-child",
    )

    assert len(watch_a.frames) == 0
    assert [frame["params"]["type"] for frame in watch_b.frames] == [
        "message.start",
        "message.delta",
    ]
    assert watch_b.frames[-1]["params"]["payload"] == {"text": "B only"}
    key_a = server._child_runtime_key(profile_a, "duplicate-child")
    key_b = server._child_runtime_key(profile_b, "duplicate-child")
    assert key_a != key_b
    assert key_b in server._child_mirrors
    assert key_a not in server._child_mirrors
    assert server._child_run_active("duplicate-child", profile_home=profile_b)
    assert not server._child_run_active("duplicate-child", profile_home=profile_a)

    server._on_tool_progress(
        "parent-a",
        "subagent.tool",
        name="terminal",
        child_session_id="duplicate-child",
    )
    assert key_a in server._child_mirrors
    assert key_b in server._child_mirrors
    assert server._child_run_active("duplicate-child", profile_home=profile_a)
    assert server._child_run_active("duplicate-child", profile_home=profile_b)

    server._on_tool_progress(
        "parent-b",
        "subagent.complete",
        child_session_id="duplicate-child",
        status="completed",
        summary="B done",
    )
    assert key_b not in server._child_mirrors
    assert key_a in server._child_mirrors
    assert not server._child_run_active("duplicate-child", profile_home=profile_b)
    assert server._child_run_active("duplicate-child", profile_home=profile_a)


def test_start_mirrors_as_immediate_header_line(server, emits):
    _watch(server)

    # subagent.start emits a one-time header (the goal) so a freshly opened
    # window shows context immediately. subagent.progress (batched tool-name
    # rollups) no longer pollutes the message body — tools mirror natively via
    # tool.start and the reply streams via subagent.text.
    _relay(server, "subagent.start", preview="starting child branch", child_session_id="child-1")
    _relay(server, "subagent.progress", preview="step 1/3", child_session_id="child-1")

    child = [(e, p) for e, s, p in emits if s == "live-1"]
    assert child == [
        ("message.start", None),
        ("message.delta", {"text": "starting child branch\n"}),
    ]


def test_text_mirrors_as_message_delta(server, emits):
    """The child's streamed reply (subagent.text) becomes a native
    message.delta on the live child sid — the watch window streams it as the
    agent 'talking', the piece that was previously missing entirely."""
    _watch(server)

    _relay(server, "subagent.text", preview="Here is ", child_session_id="child-1")
    _relay(server, "subagent.text", preview="the answer.", child_session_id="child-1")

    child = [(e, p) for e, s, p in emits if s == "live-1"]
    assert child == [
        ("message.start", None),
        ("message.delta", {"text": "Here is "}),
        ("message.delta", {"text": "the answer."}),
    ]



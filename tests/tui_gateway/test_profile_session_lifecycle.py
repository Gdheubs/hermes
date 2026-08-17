"""Profile lifecycle isolation regressions for session create/resume."""

from __future__ import annotations

import threading
from typing import Any, cast

from tui_gateway import server
from tui_gateway.transport import Transport, bind_transport, reset_transport


class _Transport:
    def write(self, _obj: dict[str, Any]) -> bool:
        return True

    def close(self) -> None:
        pass


def _clear_added_sessions(known: set[str]) -> None:
    with server._sessions_lock:
        for sid in [sid for sid in server._sessions if sid not in known]:
            server._sessions.pop(sid, None)


def test_create_and_resume_fail_closed_after_profile_is_deleted(monkeypatch, tmp_path):
    """RPC-time profile resolution fails closed after a profile is deleted."""
    profile_home = tmp_path / "profiles" / "coder_2"
    profile_home.mkdir(parents=True)
    profile_home.rmdir()
    launch_db_calls: list[str] = []
    monkeypatch.setattr(
        server,
        "_profile_home",
        lambda profile: profile_home
        if profile == "coder_2" and profile_home.exists()
        else None,
    )
    monkeypatch.setattr(server, "_current_profile_name", lambda: "coder_1")
    monkeypatch.setattr(
        server, "_get_db", lambda: launch_db_calls.append("launch") or object()
    )

    known = set(server._sessions)
    create = server._methods["session.create"]("create", {"profile": "coder_2"})
    resume = server._methods["session.resume"](
        "resume", {"profile": "coder_2", "session_id": "same-id"}
    )

    assert create["error"]["code"] == 5006
    assert resume["error"]["code"] == 5000
    assert set(server._sessions) == known
    assert launch_db_calls == []


def test_absent_explicit_profile_never_falls_back_to_launch_db(monkeypatch):
    launch_db = object()
    calls = []
    monkeypatch.setattr(server, "_profile_home", lambda _profile: None)
    monkeypatch.setattr(server, "_current_profile_name", lambda: "coder_1")
    monkeypatch.setattr(
        server, "_get_db", lambda: calls.append("launch") or launch_db
    )

    assert server._db_for_profile("coder_2") == (None, False)
    assert calls == []
    assert server._db_for_profile("coder_1") == (launch_db, False)
    assert server._db_for_profile() == (launch_db, False)
    assert calls == ["launch", "launch"]


def test_omitted_and_explicit_launch_profile_create_remain_valid(monkeypatch, tmp_path):
    class LaunchDB:
        def get_session(self, _target):
            return None

        def get_session_by_title(self, _target):
            return None

    launch_db_calls: list[str] = []
    monkeypatch.setattr(server, "_profile_home", lambda _profile: None)
    monkeypatch.setattr(server, "_current_profile_name", lambda: "coder_1")
    monkeypatch.setattr(
        server,
        "_get_db",
        lambda: launch_db_calls.append("launch") or LaunchDB(),
    )
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda *a, **k: None)
    monkeypatch.setattr(server, "_completion_cwd", lambda _params=None: str(tmp_path))

    known = set(server._sessions)
    try:
        omitted = server._methods["session.create"]("omitted", {})
        explicit = server._methods["session.create"](
            "explicit", {"profile": "coder_1"}
        )
        assert "result" in omitted
        assert "result" in explicit
        assert omitted["result"]["info"]["profile_name"] == "coder_1"
        assert explicit["result"]["info"]["profile_name"] == "coder_1"
        omitted_resume = server._methods["session.resume"](
            "omitted-resume", {"session_id": "missing"}
        )
        explicit_resume = server._methods["session.resume"](
            "explicit-resume", {"profile": "coder_1", "session_id": "missing"}
        )
        assert omitted_resume["error"]["code"] == 4007
        assert explicit_resume["error"]["code"] == 4007
        assert launch_db_calls == ["launch", "launch"]
    finally:
        _clear_added_sessions(known)


def test_lazy_resume_race_uses_profile_scoped_child_liveness(monkeypatch, tmp_path):
    """Profile B liveness cannot authorize profile A's missing durable row."""
    profile_a = tmp_path / "profiles" / "coder_a"
    profile_b = tmp_path / "profiles" / "coder_b"
    profile_a.mkdir(parents=True)
    profile_b.mkdir(parents=True)

    class MissingRowDB:
        def __init__(self, db_path=None):
            self.db_path = db_path

        def close(self):
            pass

        def get_session(self, _target):
            return None

        def get_session_by_title(self, _target):
            return None

        def reopen_session(self, _target):
            pass

        def get_messages_as_conversation(self, _target, **_kwargs):
            return []

    monkeypatch.setattr(
        server,
        "_profile_home",
        lambda profile: {"coder_a": profile_a, "coder_b": profile_b}.get(profile),
    )
    monkeypatch.setattr("hermes_state.SessionDB", MissingRowDB)
    monkeypatch.setattr(server, "_profile_configured_cwd", lambda _home: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    known = set(server._sessions)
    key_b = server._child_runtime_key(profile_b, "same-child")
    server._active_child_runs[key_b] = server.time.time()
    try:
        denied_a = server._methods["session.resume"](
            "a",
            {"profile": "coder_a", "session_id": "same-child", "lazy": True},
        )
        allowed_b = server._methods["session.resume"](
            "b",
            {"profile": "coder_b", "session_id": "same-child", "lazy": True},
        )

        assert denied_a["error"]["code"] == 4007
        assert allowed_b["result"]["running"] is True
        assert allowed_b["result"]["status"] == "streaming"
    finally:
        server._active_child_runs.pop(key_b, None)
        _clear_added_sessions(known)


def test_concurrent_same_key_claims_are_isolated_by_profile(monkeypatch, tmp_path):
    """Different profiles may own the same durable id without transport theft."""
    profile_a = tmp_path / "profiles" / "coder_1"
    profile_b = tmp_path / "profiles" / "coder_2"
    profile_a.mkdir(parents=True)
    profile_b.mkdir(parents=True)
    transport_a = _Transport()
    transport_b = _Transport()
    records = {
        "runtime-a": {
            "session_key": "same-id",
            "profile_home": str(profile_a),
            "transport": transport_a,
        },
        "runtime-b": {
            "session_key": "same-id",
            "profile_home": str(profile_b),
            "transport": transport_b,
        },
    }
    barrier = threading.Barrier(3)
    outcomes: dict[str, object] = {}
    known = dict(server._sessions)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)

    def claim(sid: str) -> None:
        barrier.wait()
        outcomes[sid] = server._claim_or_reuse_live(
            sid, "same-id", records[sid], None
        )

    threads = [
        threading.Thread(target=claim, args=(sid,)) for sid in records
    ]
    try:
        server._sessions.clear()
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()

        assert outcomes == {"runtime-a": None, "runtime-b": None}
        assert server._find_live_session_by_key(
            "same-id", profile_home=profile_a
        ) == ("runtime-a", records["runtime-a"])
        assert server._find_live_session_by_key(
            "same-id", profile_home=profile_b
        ) == ("runtime-b", records["runtime-b"])
        assert server._sessions["runtime-a"]["transport"] is transport_a
        assert server._sessions["runtime-b"]["transport"] is transport_b
    finally:
        for thread in threads:
            thread.join(timeout=2)
        server._sessions.clear()
        server._sessions.update(known)


def test_resume_reuses_only_matching_profile_transport(monkeypatch, tmp_path):
    """Fast resume cannot attach profile B to profile A's live runtime."""
    profile_a = tmp_path / "profiles" / "coder_1"
    profile_b = tmp_path / "profiles" / "coder_2"
    profile_a.mkdir(parents=True)
    profile_b.mkdir(parents=True)
    transport_a = _Transport()
    transport_b = _Transport()

    class ProfileDB:
        def __init__(self, db_path=None):
            self.db_path = db_path

        def close(self):
            pass

        def get_session(self, target):
            return {"id": target, "cwd": str(tmp_path), "message_count": 0}

        def get_session_by_title(self, _target):
            return None

        def resolve_resume_session_id(self, target):
            return target

        def reopen_session(self, _target):
            pass

        def get_resume_conversations(self, _target):
            return ([], [])

        def get_ancestor_display_prefix(self, _target):
            return []

    monkeypatch.setattr(
        server,
        "_profile_home",
        lambda profile: {"coder_1": profile_a, "coder_2": profile_b}.get(profile),
    )
    monkeypatch.setattr("hermes_state.SessionDB", ProfileDB)
    monkeypatch.setattr(server, "_profile_configured_cwd", lambda _home: str(tmp_path))
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda *a, **k: None)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a, **k: None)
    monkeypatch.setattr(server, "_stored_session_runtime_overrides", lambda _row: {})
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)

    existing = {
        "session_key": "same-id",
        "profile_home": str(profile_a),
        "transport": transport_a,
        "history": [],
        "created_at": 1.0,
        "last_active": 1.0,
        "running": False,
        "agent": None,
    }
    known = dict(server._sessions)
    token = bind_transport(cast(Transport, transport_b))
    try:
        server._sessions.clear()
        server._sessions["runtime-a"] = existing
        response = server._methods["session.resume"](
            "resume",
            {"profile": "coder_2", "session_id": "same-id"},
        )
        assert "result" in response, response
        runtime_b = response["result"]["session_id"]
        assert runtime_b != "runtime-a"
        assert server._sessions["runtime-a"]["transport"] is transport_a
        assert server._sessions[runtime_b]["profile_home"] == str(profile_b)
        assert server._sessions[runtime_b]["transport"] is transport_b
    finally:
        reset_transport(token)
        server._sessions.clear()
        server._sessions.update(known)

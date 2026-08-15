import json

import pytest

import tools.react_to_message_tool as reaction_tool


class _FakeSessionDB:
    def __init__(self, *, latest_row_id=7, reactions=None, write_error=None):
        self.latest_row_id = latest_row_id
        self.reactions = reactions if reactions is not None else [{"emoji": "👍"}]
        self.write_error = write_error
        self.close_calls = 0

    def latest_message_row_id(self, session_key, *, role, offset):
        assert session_key == "session-1"
        assert role == "user"
        assert offset == 0
        return self.latest_row_id

    def set_message_reaction(self, session_key, row_id, emoji, *, author):
        if self.write_error is not None:
            raise self.write_error
        assert (session_key, row_id, emoji, author) == (
            "session-1",
            7,
            "👍",
            "agent",
        )
        return self.reactions

    def close(self):
        self.close_calls += 1


@pytest.fixture
def session_env(monkeypatch):
    monkeypatch.setattr(
        reaction_tool,
        "get_session_env",
        lambda key, default="": "session-1" if key == "HERMES_SESSION_KEY" else default,
    )
    monkeypatch.setattr(reaction_tool.desktop_ui, "emit", lambda *args, **kwargs: None)


@pytest.mark.parametrize(
    ("db", "expected_success"),
    [
        (_FakeSessionDB(), True),
        (_FakeSessionDB(latest_row_id=None), False),
        (_FakeSessionDB(write_error=RuntimeError("database busy")), False),
    ],
)
def test_reaction_tool_closes_its_session_db_on_every_return(
    monkeypatch, session_env, db, expected_success
):
    monkeypatch.setattr(reaction_tool, "_open_session_db", lambda: db)

    result = json.loads(reaction_tool.react_to_message_tool("👍"))

    assert result.get("success", False) is expected_success
    assert db.close_calls == 1


def test_reaction_tool_ignores_close_failure(monkeypatch, session_env):
    db = _FakeSessionDB()

    def fail_close():
        db.close_calls += 1
        raise RuntimeError("close failed")

    db.close = fail_close
    monkeypatch.setattr(reaction_tool, "_open_session_db", lambda: db)

    result = json.loads(reaction_tool.react_to_message_tool("👍"))

    assert result["success"] is True
    assert db.close_calls == 1

"""Real-path lifecycle for agent-visible reasoning provenance.

Everything else about this feature is covered against fakes; this file drives
a real ``AIAgent`` and a real ``SessionDB`` under a hermetic ``HERMES_HOME``
so the pieces are checked as one lifecycle: the prompt is built with truthful
provenance, persisted, resumed byte-for-byte on the next turn, and — when the
session's effective effort no longer matches — rebuilt once and re-persisted.

No network: nothing here calls a provider. The prompt build and the session-DB
roundtrip are the whole surface under test.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.conversation_loop import _restore_or_build_system_prompt
from agent.system_prompt import read_prompt_reasoning_effort

SESSION_ID = "reasoning-provenance-lifecycle"
HISTORY = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.delenv("TERMINAL_CWD", raising=False)


def _make_agent(session_db, reasoning_config):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="gpt-5.6-sol",
            provider="openrouter",
            quiet_mode=True,
            session_db=session_db,
            session_id=SESSION_ID,
            skip_context_files=True,
            skip_memory=True,
            reasoning_config=reasoning_config,
        )
    agent._ensure_db_session()
    return agent


def _turn(db, reasoning_config, history=HISTORY):
    """One turn's system-prompt restore-or-build against the real DB."""
    agent = _make_agent(db, reasoning_config)
    _restore_or_build_system_prompt(agent, None, history)
    return agent


@pytest.fixture()
def session_db():
    from hermes_state import SessionDB

    with tempfile.TemporaryDirectory() as tmpdir:
        db = SessionDB(db_path=Path(tmpdir) / "state.db")
        try:
            yield db
        finally:
            db.close()


ULTRA = {"enabled": True, "effort": "ultra"}


def test_full_lifecycle_build_persist_resume_and_recover(session_db):
    """The bug this fixes, end to end.

    A real session running at ``ultra`` used to give the model a prompt whose
    metadata stopped at Model/Provider/Platform, so after a resume the agent
    could not tell what tier it was on without querying SQLite.
    """
    # ── Turn 1: fresh build carries the truth and is persisted ──
    first = _turn(session_db, ULTRA, history=[])
    built = first._cached_system_prompt
    assert read_prompt_reasoning_effort(built) == "ultra"
    assert session_db.get_session(SESSION_ID)["system_prompt"] == built

    # ── Turn 2: same runtime, fresh agent (the gateway/desktop shape) →
    # the stored bytes come back untouched, so the prefix cache still hits.
    second = _turn(session_db, ULTRA)
    assert second._cached_system_prompt == built
    assert session_db.get_session(SESSION_ID)["system_prompt"] == built

    # ── Turn 3: the session's effective effort changed → the stale claim is
    # rejected exactly once, rebuilt truthfully, and re-persisted.
    third = _turn(session_db, {"enabled": True, "effort": "low"})
    assert third._cached_system_prompt != built
    assert read_prompt_reasoning_effort(third._cached_system_prompt) == "low"
    assert (
        session_db.get_session(SESSION_ID)["system_prompt"]
        == third._cached_system_prompt
    )

    # ── Turn 4: the recovery was one-time — the new bytes now match. ──
    fourth = _turn(session_db, {"enabled": True, "effort": "low"})
    assert fourth._cached_system_prompt == third._cached_system_prompt


def test_legacy_persisted_prompt_gains_provenance_without_crashing(session_db):
    """A row written before this metadata existed must resume safely."""
    legacy = (
        "You are Hermes Agent.\n\n"
        "Conversation started: Tuesday, June 16, 2026\n"
        f"Session ID: {SESSION_ID}\n"
        "Model: gpt-5.6-sol\n"
        "Provider: openrouter"
    )
    _make_agent(session_db, ULTRA)
    session_db.update_system_prompt(SESSION_ID, legacy)

    agent = _turn(session_db, ULTRA)
    assert agent._cached_system_prompt != legacy
    assert read_prompt_reasoning_effort(agent._cached_system_prompt) == "ultra"
    assert session_db.get_session(SESSION_ID)["system_prompt"] == (
        agent._cached_system_prompt
    )


def test_disabled_reasoning_survives_the_roundtrip_as_disabled(session_db):
    """Explicitly-off thinking must not resume as a provider default."""
    first = _turn(session_db, {"enabled": False}, history=[])
    assert read_prompt_reasoning_effort(first._cached_system_prompt) == "disabled"

    second = _turn(session_db, {"enabled": False})
    assert second._cached_system_prompt == first._cached_system_prompt

    # ...and is not interchangeable with "no explicit effort".
    third = _turn(session_db, None)
    assert read_prompt_reasoning_effort(third._cached_system_prompt) == (
        "provider-default"
    )

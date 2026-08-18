"""Gateway /loop command tests — dispatch, routing capture, mid-run guard."""

import logging
import time
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import loops


class _FakeSessionEntry:
    session_id = "sid-gateway-loop"


class _FakeSessionStore:
    def __init__(self):
        self.entry = _FakeSessionEntry()

    def get_or_create_session(self, source, *, touch_activity=True):
        return self.entry

    def _generate_session_key(self, source):
        return "agent:main:discord:channel:loop-test"


@pytest.fixture
def loop_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    loops._DB_CACHE.clear()
    yield home
    loops._DB_CACHE.clear()


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {}
    runner._queued_events = {}
    return runner


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chat-loop",
            chat_type="channel",
            thread_id="thread-9",
            user_id="user-loop",
        ),
        message_id="msg-loop",
    )


@pytest.mark.asyncio
async def test_gateway_loop_create_captures_route(loop_env):
    runner = _make_runner()
    response = await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m check the deploy"))
    assert "Loop set" in response
    assert "every 5m" in response

    state = loops.load_loop("sid-gateway-loop")
    assert state is not None
    assert state.prompt == "check the deploy"
    assert state.route["platform"] == "discord"
    assert state.route["chat_id"] == "chat-loop"
    assert state.route["thread_id"] == "thread-9"


@pytest.mark.asyncio
async def test_gateway_loop_status_pause_stop(loop_env):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))

    status = await GatewayRunner._handle_loop_command(runner, _make_event("/loop status"))
    assert "poll CI" in status

    paused = await GatewayRunner._handle_loop_command(runner, _make_event("/loop pause"))
    assert "paused" in paused.lower()

    stopped = await GatewayRunner._handle_loop_command(runner, _make_event("/loop stop"))
    assert "stopped" in stopped.lower()


@pytest.mark.asyncio
async def test_gateway_loop_goal_note_when_goal_active(loop_env):
    from hermes_cli.goals import GoalManager

    GoalManager(session_id="sid-gateway-loop").set("finish the migration")
    runner = _make_runner()
    response = await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))
    assert "active /goal" in response


@pytest.mark.asyncio
async def test_post_turn_loop_completion_completes_inflight_tick(loop_env):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))

    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    mgr.state.next_due_at = time.time() - 1
    assert mgr.fire_tick() is not None

    entry = _FakeSessionEntry()
    await GatewayRunner._post_turn_loop_completion(
        runner,
        session_entry=entry,
        source=None,
        final_response="CI is done.\nLOOP_COMPLETE",
    )
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.status == "done"


@pytest.mark.asyncio
async def test_post_turn_loop_completion_noop_without_inflight_tick(loop_env):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))
    entry = _FakeSessionEntry()
    # No tick fired — the ordinary user turn must not consume loop state.
    await GatewayRunner._post_turn_loop_completion(
        runner,
        session_entry=entry,
        source=None,
        final_response="regular reply LOOP_COMPLETE",
    )
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.status == "active"
    assert reloaded.ticks_fired == 0


def test_streamed_already_sent_none_recovers_text_for_hooks():
    """Streamed turns return None. Hooks must still see the delivered reply."""
    event = _make_event("wakeup")
    event._streamed_final_response = "CI is green.\nLOOP_COMPLETE"
    assert GatewayRunner._final_text_for_post_turn_hooks(None, event) == (
        "CI is green.\nLOOP_COMPLETE"
    )
    assert GatewayRunner._final_text_for_post_turn_hooks(None, _make_event("x")) == ""
    assert (
        GatewayRunner._final_text_for_post_turn_hooks(
            {"final_response": "from dict"}, event
        )
        == "from dict"
    )


@pytest.mark.asyncio
async def test_streamed_already_sent_completes_loop_tick(loop_env):
    """A streamed wakeup must not leave awaiting_response stuck."""
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))

    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    mgr.state.next_due_at = time.time() - 1
    assert mgr.fire_tick() is not None
    assert mgr.state.awaiting_response is True
    assert mgr.is_due() is False

    event = _make_event("wakeup")
    event._streamed_final_response = "CI is done.\nLOOP_COMPLETE"
    # Same inputs the already_sent branch leaves for _handle_message.
    final_text = GatewayRunner._final_text_for_post_turn_hooks(None, event)
    assert final_text.strip()

    await GatewayRunner._post_turn_loop_completion(
        runner,
        session_entry=_FakeSessionEntry(),
        source=None,
        final_response=final_text,
    )
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.awaiting_response is False
    assert reloaded.status == "done"


@pytest.mark.asyncio
async def test_empty_agent_result_releases_inflight_loop_tick(loop_env):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))

    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    mgr.state.next_due_at = time.time() - 1
    assert mgr.fire_tick() is not None
    assert mgr.state.awaiting_response is True

    runner._post_turn_goal_continuation = AsyncMock()
    await GatewayRunner._run_post_turn_hooks(
        runner,
        agent_result={"final_response": ""},
        source=_make_event("wakeup").source,
        is_internal=True,
    )

    runner._post_turn_goal_continuation.assert_not_awaited()
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.awaiting_response is False
    assert reloaded.status == "active"
    assert reloaded.next_due_at > time.time()


@pytest.mark.asyncio
async def test_goal_hook_failure_does_not_block_loop_completion(loop_env, caplog):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))

    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    mgr.state.next_due_at = time.time() - 1
    assert mgr.fire_tick() is not None

    runner._post_turn_goal_continuation = AsyncMock(side_effect=RuntimeError("judge failed"))
    with caplog.at_level(logging.DEBUG, logger="gateway.run"):
        await GatewayRunner._run_post_turn_hooks(
            runner,
            agent_result={"final_response": "still working"},
            source=_make_event("wakeup").source,
            is_internal=True,
        )

    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.awaiting_response is False
    assert "goal continuation hook failed: judge failed" in caplog.text


@pytest.mark.asyncio
async def test_post_turn_session_resolution_failure_is_logged(loop_env, caplog):
    runner = _make_runner()
    runner.session_store.get_or_create_session = Mock(side_effect=RuntimeError("store unavailable"))

    with caplog.at_level(logging.DEBUG, logger="gateway.run"):
        await GatewayRunner._run_post_turn_hooks(
            runner,
            agent_result={"final_response": ""},
            source=_make_event("wakeup").source,
            is_internal=True,
        )

    assert "post-turn session resolution failed: store unavailable" in caplog.text


def _install_scoped_loop_db(home) -> None:
    """Pre-populate loops._DB_CACHE with an explicitly-pathed SessionDB for
    `home`.

    tests/conftest.py's autouse _hermetic_environment fixture re-points
    hermes_state.DEFAULT_DB_PATH to ONE fixed file for the whole test (a
    hygiene guard against a bare SessionDB() ever touching a developer's
    real ~/.hermes). _get_session_db() calls SessionDB() with no explicit
    db_path, so — left alone — every simulated profile home in a
    multi-profile test would collapse onto that same pinned file instead of
    getting its own. Installing an explicitly-pathed instance under the
    cache key _get_session_db() will look up (str(get_hermes_home()) while
    `home` is the active override) bypasses that shortcut and restores the
    real per-home separation this test needs to exercise.
    """
    from hermes_state import SessionDB

    key = str(home)
    if key not in loops._DB_CACHE:
        loops._DB_CACHE[key] = SessionDB(db_path=home / "state.db")


def _seed_due_loop(home, session_id: str, chat_id: str) -> None:
    """Persist a due, routed loop directly to `home`'s own state.db."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    _install_scoped_loop_db(home)
    token = set_hermes_home_override(str(home))
    try:
        state = loops.LoopState(
            prompt="poll CI",
            interval_seconds=300,
            next_due_at=time.time() - 1,
            route={
                "platform": "discord",
                "chat_id": chat_id,
                "chat_type": "dm",
                "thread_id": "",
                "user_id": "",
                "user_name": "",
            },
        )
        loops.save_loop(session_id, state)
    finally:
        reset_hermes_home_override(token)


async def _run_one_wakeup_tick(monkeypatch, runner):
    """Drive _loop_wakeup_watcher for exactly one tick (mirrors the same
    technique tests/gateway/test_kanban_notifier.py uses for its watcher)."""
    import asyncio

    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await GatewayRunner._loop_wakeup_watcher(runner, interval=1)


@pytest.mark.asyncio
async def test_loop_wakeup_watcher_fires_secondary_profile_loops(tmp_path, monkeypatch):
    """Regression: _loop_wakeup_watcher is a single gateway-wide asyncio
    task. get_hermes_home() would otherwise stay pinned to whichever
    profile's context was ambient when the task was created, and adapter
    resolution only ever consulted the default profile's self.adapters —
    so a loop set from a secondary profile's chat would never fire, and
    even if it were scanned, would risk firing through the WRONG (default
    profile's) bot. Seeds one due loop per profile, in that profile's own
    state.db, each routed to a different chat, and asserts each fires
    through its OWN profile's adapter."""
    home_default = tmp_path / "default_home"
    home_work = tmp_path / "work_home"
    home_default.mkdir()
    home_work.mkdir()
    _seed_due_loop(home_default, "sid-default", "chat-default")
    _seed_due_loop(home_work, "sid-work", "chat-work")

    adapter_default = AsyncMock()
    adapter_work = AsyncMock()

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters = {Platform.DISCORD: adapter_default}
    runner._profile_adapters = {"work": {Platform.DISCORD: adapter_work}}
    runner._running_agents = {}
    runner._cached_session_sources = {}
    runner._session_sources_max = 512

    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex, profile_allowlist=None: [("default", home_default), ("work", home_work)],
    )
    monkeypatch.setattr(GatewayRunner, "_active_profile_name", lambda self: "default")

    await _run_one_wakeup_tick(monkeypatch, runner)

    assert adapter_default.handle_message.await_count == 1, "default profile's due loop never fired"
    assert adapter_work.handle_message.await_count == 1, "secondary profile's due loop never fired"
    assert adapter_default.handle_message.call_args.args[0].source.chat_id == "chat-default"
    assert adapter_work.handle_message.call_args.args[0].source.chat_id == "chat-work"

    # Each profile's own loop must be marked fired — not left due forever
    # (which would also produce a false "it fired" from a retried scan).
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    for name, home in (("sid-default", home_default), ("sid-work", home_work)):
        token = set_hermes_home_override(str(home))
        try:
            reloaded = loops.load_loop(name)
            assert reloaded.ticks_fired == 1
        finally:
            reset_hermes_home_override(token)


@pytest.mark.asyncio
async def test_loop_wakeup_watcher_single_profile_unchanged(tmp_path, monkeypatch, loop_env):
    """Non-multiplex gateways (the overwhelming majority) must see
    byte-for-byte the same single-profile behavior as before this fix, and
    the fix must resolve the served-profile set with THIS gateway's own
    multiplex_profiles flag — not unconditionally multiplex=True (which
    would sweep in every profile directory found on disk, including ones
    this gateway was never configured to serve)."""
    _seed_due_loop(loop_env, "sid-solo", "chat-solo")

    adapter = AsyncMock()
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.config = GatewayConfig(multiplex_profiles=False)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {}
    runner._running_agents = {}
    runner._cached_session_sources = {}
    runner._session_sources_max = 512

    seen_multiplex_flags = []

    def fake_profiles_to_serve(multiplex, profile_allowlist=None):
        seen_multiplex_flags.append(multiplex)
        return [("default", loop_env)]

    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve", fake_profiles_to_serve)

    await _run_one_wakeup_tick(monkeypatch, runner)

    assert seen_multiplex_flags == [False]
    assert adapter.handle_message.await_count == 1
    assert adapter.handle_message.call_args.args[0].source.chat_id == "chat-solo"

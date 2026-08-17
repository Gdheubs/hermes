"""Regression coverage for EZ-525's single-surface Slack agent UX."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.run import GatewayRunner, TurnRunner
from gateway.session import SessionSource
from plugins.platforms.slack.adapter import SlackAdapter
from plugins.platforms.slack.block_kit import render_todo_progress, todo_progress_eligible


def _snapshot(second_status="in_progress", third_status="pending"):
    return {
        "todos": [
            {"content": "inspect /Users/aditya/.env TOKEN=secret", "status": "completed"},
            {"content": "implement", "status": second_status},
            {"content": "verify", "status": third_status},
        ]
    }


def _slack_adapter():
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake", extra={}))
    adapter._app = MagicMock()
    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ts": "111.222"})
    client.chat_update = AsyncMock(return_value={"ts": "111.222"})
    adapter._get_client = MagicMock(return_value=client)
    adapter.stop_typing = AsyncMock()
    adapter._running = True
    return adapter, client


def _source(user_id="U1", chat_type="group"):
    return SessionSource(
        platform=Platform.SLACK,
        scope_id="T1",
        chat_id="C1",
        chat_type=chat_type,
        user_id=user_id,
        thread_id="111.1",
    )


def _event(text="change direction", user_id="U1"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(user_id),
        message_id="111.2",
    )


def test_todo_renderer_is_private_bounded_and_interactive():
    snapshot = _snapshot()
    snapshot["todos"][0]["content"] += (
        " bearer sk-live-example123456 C:\\Users\\aditya\\.env \\\\server\\share\\secret"
    )

    assert todo_progress_eligible(snapshot)
    rendered = render_todo_progress(
        snapshot,
        active=True,
        generation=3,
        revision=1,
        action_token="opaque-token",
    )

    assert rendered is not None
    _, blocks = rendered
    assert [block["type"] for block in blocks] == ["section", "context", "actions"]
    blob = str(blocks)
    assert "hermes_todo_stop" in blob
    assert "hermes_todo_restart" in blob
    assert "/Users/" not in blob
    assert "C:\\\\Users" not in blob
    assert "\\\\server" not in blob
    assert "secret" not in blob
    assert "sk-live" not in blob


def test_todo_card_only_opens_for_active_multistep_work():
    assert not todo_progress_eligible(
        {"todos": [{"content": "one", "status": "pending"}]}
    )
    assert not todo_progress_eligible(
        {
            "todos": [
                {"content": "one", "status": "completed"},
                {"content": "two", "status": "completed"},
                {"content": "three", "status": "cancelled"},
            ]
        }
    )


@pytest.mark.asyncio
async def test_projection_updates_one_message_and_final_delivery_removes_controls():
    adapter, client = _slack_adapter()
    session_key = "slack:T1:C1:111.1"
    metadata = {"team_id": "T1", "thread_id": "111.1"}

    assert await adapter.project_todo_progress(
        "C1",
        _snapshot(),
        metadata=metadata,
        session_key=session_key,
        generation=12,
        user_id="U1",
    )
    assert await adapter.project_todo_progress(
        "C1",
        _snapshot("completed", "completed"),
        metadata=metadata,
        session_key=session_key,
        generation=12,
        user_id="U1",
    )

    active = asyncio.Event()
    setattr(active, "_hermes_run_generation", 12)
    adapter._active_sessions[session_key] = active
    runner = MagicMock()
    runner._session_key_for_source = MagicMock(return_value=session_key)
    setattr(adapter, "gateway_runner", runner)
    await adapter.on_processing_complete(_event(), ProcessingOutcome.SUCCESS)

    client.chat_postMessage.assert_awaited_once()
    assert client.chat_update.await_count == 2
    assert all(
        block["type"] != "actions"
        for block in client.chat_update.await_args.kwargs["blocks"]
    )
    assert next(iter(adapter._todo_progress_states.values())).closed is True


async def _project_action_card(active_generation=7):
    adapter, client = _slack_adapter()
    session_key = "slack:T1:C1:111.1"
    active = asyncio.Event()
    setattr(active, "_hermes_run_generation", active_generation)
    adapter._active_sessions[session_key] = active
    adapter._is_interactive_user_authorized = MagicMock(return_value=True)
    runner = MagicMock()
    runner.handle_slack_todo_action = AsyncMock(return_value=True)
    setattr(adapter, "gateway_runner", runner)
    await adapter.project_todo_progress(
        "C1",
        _snapshot(),
        metadata={"team_id": "T1", "thread_id": "111.1"},
        session_key=session_key,
        generation=7,
        user_id="U1",
    )
    state = next(iter(adapter._todo_progress_states.values()))
    body = {
        "team": {"id": "T1"},
        "channel": {"id": "C1"},
        "message": {"ts": "111.222", "thread_ts": "111.1"},
        "user": {"id": "U1"},
    }
    return adapter, client, runner, state, body


@pytest.mark.asyncio
async def test_control_is_generation_owned_authorized_and_one_shot():
    adapter, client, runner, state, body = await _project_action_card()
    action = {"action_id": "hermes_todo_stop", "value": state.token}

    await adapter._handle_todo_action(AsyncMock(), body, action)
    await adapter._handle_todo_action(AsyncMock(), body, action)

    runner.handle_slack_todo_action.assert_awaited_once()
    assert state.closed is True
    assert state.token not in adapter._todo_progress_tokens
    assert not await adapter.finalize_todo_progress(
        state.session_key, state.generation, "success"
    )
    client.chat_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_or_different_user_control_cannot_mutate_card():
    adapter, client, runner, state, body = await _project_action_card(active_generation=8)
    await adapter._handle_todo_action(
        AsyncMock(),
        body,
        {"action_id": "hermes_todo_restart", "value": state.token},
    )
    runner.handle_slack_todo_action.assert_not_awaited()
    client.chat_update.assert_not_awaited()

    adapter, client, runner, state, body = await _project_action_card()
    body["user"]["id"] = "U2"
    await adapter._handle_todo_action(
        AsyncMock(),
        body,
        {"action_id": "hermes_todo_stop", "value": state.token},
    )
    runner.handle_slack_todo_action.assert_not_awaited()
    client.chat_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_completed_todo_results_project_to_slack():
    projector = SimpleNamespace(project_todo_progress=AsyncMock(return_value=True))
    ctx = SimpleNamespace(
        _live_status_adapter=None,
        _live_status_mode="off",
        _run_still_current=lambda: True,
        _status_adapter=projector,
        _status_thread_metadata={"team_id": "T1", "thread_id": "111.1"},
        source=_source(),
        session_key="slack:T1:C1:111.1",
        run_generation=9,
        _loop_for_step=asyncio.get_running_loop(),
        log_queue=None,
        progress_queue=None,
    )
    result = json.dumps(_snapshot())
    turn_runner = object.__new__(TurnRunner)
    turn_runner._ctx = ctx

    turn_runner.progress_callback("tool.started", "todo", "", {}, result=result)
    turn_runner.progress_callback("tool.completed", "todo", "", {}, result=result)
    await asyncio.sleep(0.01)

    projector.project_todo_progress.assert_awaited_once()
    kwargs = projector.project_todo_progress.await_args.kwargs
    assert kwargs["session_key"] == "slack:T1:C1:111.1"
    assert kwargs["generation"] == 9
    assert kwargs["user_id"] == "U1"


def _steering_runner(agent):
    runner = object.__new__(GatewayRunner)
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter.platform = Platform.SLACK
    adapter._text_debounce = {}
    adapter._busy_text_debounce_seconds = 0.6
    runner.adapters = {Platform.SLACK: adapter}
    runner._busy_input_mode = "interrupt"
    runner._busy_text_mode = "interrupt"
    runner._draining = False
    runner._is_user_authorized = lambda source: True
    state = SimpleNamespace(turn=SimpleNamespace(agent=agent), busy_ack_ts=0.0)
    runner._peek_session_state = MagicMock(return_value=state)
    runner._agent_has_active_subagents = MagicMock(return_value=False)
    runner._session_has_compression_in_flight = AsyncMock(return_value=False)
    runner._prepare_busy_steer_text = AsyncMock(return_value="change direction")
    runner._queue_or_replace_pending_event = MagicMock()
    runner._adapter_for_source = lambda source: adapter
    return runner, adapter


@pytest.mark.asyncio
async def test_slack_active_thread_reply_steers_silently():
    agent = MagicMock()
    agent.steer.return_value = True
    agent.active_children = set()
    runner, adapter = _steering_runner(agent)
    event = _event()
    session_key = "slack:T1:C1:111.1"

    assert await runner._handle_active_session_busy_message(event, session_key)

    agent.steer.assert_called_once()
    runner._queue_or_replace_pending_event.assert_not_called()
    adapter._send_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_slack_steer_queues_fifo_without_ack():
    agent = MagicMock()
    agent.steer.return_value = False
    agent.active_children = set()
    runner, adapter = _steering_runner(agent)
    event = _event()
    session_key = "slack:T1:C1:111.1"

    assert await runner._handle_active_session_busy_message(event, session_key)

    runner._queue_or_replace_pending_event.assert_called_once_with(session_key, event)
    adapter._send_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_workspace_does_not_force_slack_thread_steer():
    agent = MagicMock()
    agent.steer.return_value = True
    agent.active_children = set()
    runner, adapter = _steering_runner(agent)
    runner._busy_input_mode = "queue"
    runner._busy_text_mode = "queue"
    event = _event()
    session_key = "slack:T1:C1:111.1"

    assert await runner._handle_active_session_busy_message(event, session_key) is False

    agent.steer.assert_not_called()
    adapter._send_with_retry.assert_not_awaited()


def _control_runner(active_generation=5):
    runner = object.__new__(GatewayRunner)
    session_key = "slack:T1:C1:111.1"
    active = asyncio.Event()
    setattr(active, "_hermes_run_generation", active_generation)
    active_task = asyncio.get_running_loop().create_future()
    adapter = MagicMock()
    adapter._active_sessions = {session_key: active}
    adapter._session_tasks = {session_key: active_task}
    adapter._background_tasks = set()
    adapter.handle_message = AsyncMock()
    runner._is_user_authorized = lambda source: True
    runner._session_key_for_source = lambda source: session_key
    runner._adapter_for_source = lambda source: adapter
    runner._busy_stop_command = AsyncMock()
    runner._peek_session_state = MagicMock(
        return_value=SimpleNamespace(
            persistent=SimpleNamespace(run_generation=active_generation + 1)
        )
    )
    runner._is_session_run_current = MagicMock(return_value=True)
    async_store = MagicMock()
    async_store.get_or_create_session = AsyncMock(
        return_value=SimpleNamespace(session_id="S1")
    )
    runner.session_store = MagicMock()
    async_store._store = runner.session_store
    runner._async_session_store = async_store
    return runner, adapter, session_key, active_task


def _control_payload(session_key, generation=5):
    return {
        "team_id": "T1",
        "channel_id": "C1",
        "chat_type": "group",
        "thread_ts": "111.1",
        "message_ts": "111.222",
        "user_id": "U1",
        "session_key": session_key,
        "generation": generation,
    }


@pytest.mark.asyncio
async def test_stop_control_uses_canonical_busy_stop_path():
    runner, adapter, session_key, _active_task = _control_runner()

    assert await runner.handle_slack_todo_action(
        "stop", _control_payload(session_key)
    )

    runner._busy_stop_command.assert_awaited_once()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_control_stops_then_retries_current_turn():
    runner, adapter, session_key, active_task = _control_runner()

    assert await runner.handle_slack_todo_action(
        "restart", _control_payload(session_key)
    )

    runner._busy_stop_command.assert_awaited_once()
    adapter.handle_message.assert_not_awaited()

    adapter._active_sessions.pop(session_key)
    active_task.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    adapter.handle_message.assert_awaited_once()
    retry_event = adapter.handle_message.await_args.args[0]
    assert retry_event.text == "/retry"
    assert retry_event.message_type is MessageType.COMMAND
    runner._is_session_run_current.assert_called_once_with(session_key, 6)


@pytest.mark.asyncio
async def test_restart_drops_if_a_newer_generation_wins_the_race():
    runner, adapter, session_key, active_task = _control_runner()

    assert await runner.handle_slack_todo_action(
        "restart", _control_payload(session_key)
    )
    runner._is_session_run_current.return_value = False
    adapter._active_sessions.pop(session_key)
    active_task.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_drops_if_the_conversation_session_changed():
    runner, adapter, session_key, active_task = _control_runner()
    runner._async_session_store.get_or_create_session.side_effect = [
        SimpleNamespace(session_id="S1"),
        SimpleNamespace(session_id="S2"),
    ]

    assert await runner.handle_slack_todo_action(
        "restart", _control_payload(session_key)
    )
    adapter._active_sessions.pop(session_key)
    active_task.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_flat_slack_control_uses_threadless_session_source():
    runner, adapter, session_key, _active_task = _control_runner()
    payload = _control_payload(session_key)
    payload["thread_ts"] = ""

    assert await runner.handle_slack_todo_action("stop", payload)

    event = runner._busy_stop_command.await_args.args[0]
    assert event.source.thread_id is None


@pytest.mark.asyncio
async def test_gateway_rejects_stale_control_generation():
    runner, adapter, session_key, _active_task = _control_runner(active_generation=6)

    assert not await runner.handle_slack_todo_action(
        "stop", _control_payload(session_key, generation=5)
    )

    runner._busy_stop_command.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_rejects_non_integer_control_generation():
    runner, adapter, session_key, _active_task = _control_runner()
    payload = _control_payload(session_key)
    payload["generation"] = "not-a-generation"

    assert not await runner.handle_slack_todo_action("stop", payload)

    runner._busy_stop_command.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_complete_does_not_overwrite_stopped_card():
    adapter, client, _runner, state, body = await _project_action_card()
    await adapter._handle_todo_action(
        AsyncMock(),
        body,
        {"action_id": "hermes_todo_stop", "value": state.token},
    )
    client.chat_update.reset_mock()

    newer = asyncio.Event()
    setattr(newer, "_hermes_run_generation", 13)
    adapter._active_sessions[state.session_key] = newer
    await adapter.on_processing_complete(_event(), ProcessingOutcome.SUCCESS)

    client.chat_update.assert_not_awaited()
    assert state.closed is True
    assert next(iter(adapter._todo_progress_states.values())).generation == 7


@pytest.mark.asyncio
async def test_complete_without_active_session_still_closes_card():
    adapter, client = _slack_adapter()
    session_key = "slack:T1:C1:111.1"
    assert await adapter.project_todo_progress(
        "C1",
        _snapshot(),
        metadata={"team_id": "T1", "thread_id": "111.1"},
        session_key=session_key,
        generation=4,
        user_id="U1",
    )
    runner = MagicMock()
    runner._session_key_for_source = MagicMock(return_value=session_key)
    setattr(adapter, "gateway_runner", runner)

    await adapter.on_processing_complete(_event(), ProcessingOutcome.SUCCESS)

    assert next(iter(adapter._todo_progress_states.values())).closed is True
    assert all(
        block["type"] != "actions"
        for block in client.chat_update.await_args.kwargs["blocks"]
    )

"""Tests for /background gateway slash command.

Tests the _handle_background_command handler (run a prompt in a separate
background session) across gateway messenger platforms.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text="/background", platform=Platform.TELEGRAM,
                user_id="12345", chat_id="67890"):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    """Create a bare GatewayRunner with minimal mocks."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._background_tasks = set()

    mock_store = MagicMock()
    runner.session_store = mock_store

    from gateway.hooks import HookRegistry
    runner.hooks = HookRegistry()

    return runner


# ---------------------------------------------------------------------------
# _handle_background_command
# ---------------------------------------------------------------------------


class TestHandleBackgroundCommand:
    """Tests for GatewayRunner._handle_background_command."""

    @pytest.mark.asyncio
    async def test_no_prompt_shows_usage(self):
        """Running /background with no prompt shows usage."""
        runner = _make_runner()
        event = _make_event(text="/background")
        result = await runner._handle_background_command(event)
        assert "Usage:" in result
        assert "/background" in result

    @pytest.mark.asyncio
    async def test_bg_alias_no_prompt_shows_usage(self):
        """Running /bg with no prompt shows usage."""
        runner = _make_runner()
        event = _make_event(text="/bg")
        result = await runner._handle_background_command(event)
        assert "Usage:" in result

    @pytest.mark.asyncio
    async def test_empty_prompt_shows_usage(self):
        """Running /background with only whitespace shows usage."""
        runner = _make_runner()
        event = _make_event(text="/background   ")
        result = await runner._handle_background_command(event)
        assert "Usage:" in result

    @pytest.mark.asyncio
    async def test_passes_parent_and_complete_reply_context(self):
        runner = _make_runner()
        parent = MagicMock(session_id="parent-session")
        runner.session_store.get_or_create_session.return_value = parent
        runner._run_background_task = AsyncMock(return_value=None)
        event = _make_event(text="/background inspect this")
        decision_after_old_limit = "APPROVE_THE_REPORT_AFTER_CHARACTER_500"
        event.reply_to_text = "q" * 700 + decision_after_old_limit
        event.reply_to_is_own_message = True

        result = await runner._handle_background_command(event)
        await asyncio.sleep(0)

        assert "Background" in result
        kwargs = runner._run_background_task.await_args.kwargs
        assert kwargs["parent_session_id"] == "parent-session"
        assert kwargs["parent_session_key"] == runner._session_key_for_source(event.source)
        assert kwargs["reply_to_text"] == event.reply_to_text
        assert decision_after_old_limit in kwargs["reply_to_text"]
        assert kwargs["reply_to_is_own_message"] is True
        assert kwargs["origin"]["execution_kind"] == "user_explicit_background"
        assert kwargs["origin"]["user_initiated"] is True
        assert kwargs["origin"]["command"] == "/background"


# ---------------------------------------------------------------------------
# _run_background_task
# ---------------------------------------------------------------------------


class TestRunBackgroundTask:
    """Tests for GatewayRunner._run_background_task (the actual execution)."""


    @pytest.mark.asyncio
    async def test_no_credentials_sends_error(self):
        """When provider credentials are missing, an error is sent."""
        runner = _make_runner()
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        runner.adapters[Platform.TELEGRAM] = mock_adapter

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )

        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": None}):
            await runner._run_background_task("test prompt", source, "bg_test")

        # Should have sent an error message
        mock_adapter.send.assert_called_once()
        call_args = mock_adapter.send.call_args
        assert "failed" in call_args[1].get("content", call_args[0][1] if len(call_args[0]) > 1 else "").lower()

    @pytest.mark.asyncio
    async def test_successful_task_sends_result(self):
        """When the agent completes successfully, the result is sent."""
        runner = _make_runner()
        mock_adapter = MagicMock()
        mock_adapter.send = AsyncMock()
        mock_adapter.extract_media = MagicMock(return_value=([], "Hello from background!"))
        mock_adapter.extract_images = MagicMock(return_value=([], "Hello from background!"))
        runner.adapters[Platform.TELEGRAM] = mock_adapter

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )

        mock_result = {"final_response": "Hello from background!", "messages": []}

        checkpoint_config = {
            "checkpoints": {
                "enabled": True,
                "max_snapshots": 8,
                "max_total_size_mb": 222,
                "max_file_size_mb": 3,
            }
        }
        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}), \
             patch("gateway.run._load_gateway_config", return_value=checkpoint_config), \
             patch("run_agent.AIAgent") as MockAgent:
            mock_agent_instance = MagicMock()
            mock_agent_instance.shutdown_memory_provider = MagicMock()
            mock_agent_instance.close = MagicMock()
            mock_agent_instance.run_conversation.return_value = mock_result
            MockAgent.return_value = mock_agent_instance

            decision_after_old_limit = "APPROVE_THE_REPORT_AFTER_CHARACTER_500"
            complete_reply = "q" * 700 + decision_after_old_limit
            await runner._run_background_task(
                "say hello",
                source,
                "bg_test",
                parent_session_id="parent-session",
                parent_session_key="telegram:67890",
                reply_to_text=complete_reply,
                reply_to_is_own_message=True,
                origin={
                    "execution_kind": "user_explicit_background",
                    "user_initiated": True,
                    "command": "/background",
                },
            )

        # Should have sent the result
        mock_adapter.send.assert_called_once()
        call_args = mock_adapter.send.call_args
        content = call_args[1].get("content", call_args[0][1] if len(call_args[0]) > 1 else "")
        assert "Background task complete" in content
        assert "Hello from background!" in content
        agent_kwargs = MockAgent.call_args.kwargs
        assert agent_kwargs["checkpoints_enabled"] is True
        assert agent_kwargs["checkpoint_max_snapshots"] == 8
        assert agent_kwargs["checkpoint_max_total_size_mb"] == 222
        assert agent_kwargs["checkpoint_max_file_size_mb"] == 3
        assert agent_kwargs["parent_session_id"] == "parent-session"
        assert agent_kwargs["gateway_session_key"] == "telegram:67890"
        run_kwargs = mock_agent_instance.run_conversation.call_args.kwargs
        assert run_kwargs["user_message"] == (
            f'[Replying to your previous message: "{complete_reply}"]\n\nsay hello'
        )
        assert decision_after_old_limit in run_kwargs["user_message"]
        record_kwargs = mock_agent_instance.record_gateway_session_peer.call_args.kwargs
        origin = record_kwargs["origin"]
        assert origin["execution_kind"] == "user_explicit_background"
        assert origin["user_initiated"] is True
        assert origin["command"] == "/background"
        mock_agent_instance.shutdown_memory_provider.assert_called_once()
        mock_agent_instance.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_generic_caller_supplies_its_own_provenance(self):
        runner = _make_runner()
        mock_adapter = MagicMock()
        mock_adapter.send = AsyncMock()
        mock_adapter.extract_media = MagicMock(return_value=([], "done"))
        mock_adapter.extract_images = MagicMock(return_value=([], "done"))
        runner.adapters[Platform.TELEGRAM] = mock_adapter
        source = _make_event().source
        caller_origin = {"execution_kind": "scheduled_background"}

        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}), \
             patch("gateway.run._load_gateway_config", return_value={}), \
             patch("run_agent.AIAgent") as MockAgent:
            agent = MockAgent.return_value
            agent.run_conversation.return_value = {"final_response": "done", "messages": []}
            await runner._run_background_task(
                "scheduled work",
                source,
                "bg_other",
                parent_session_id="parent-session",
                parent_session_key="telegram:67890",
                origin=caller_origin,
            )

        assert agent.record_gateway_session_peer.call_args.kwargs == {
            "origin": caller_origin,
        }
        assert "command" not in caller_origin

    @pytest.mark.asyncio
    async def test_peer_record_failure_still_cleans_up_agent(self):
        runner = _make_runner()
        mock_adapter = MagicMock()
        mock_adapter.send = AsyncMock()
        runner.adapters[Platform.TELEGRAM] = mock_adapter
        source = _make_event().source

        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}), \
             patch("gateway.run._load_gateway_config", return_value={}), \
             patch("run_agent.AIAgent") as MockAgent:
            agent = MockAgent.return_value
            agent.record_gateway_session_peer.side_effect = RuntimeError(
                "Session DB unavailable; gateway peer was not recorded"
            )

            await runner._run_background_task(
                "scheduled work",
                source,
                "bg_failure",
                parent_session_id="parent-session",
                parent_session_key="telegram:67890",
                origin={"execution_kind": "scheduled_background"},
            )

        agent.run_conversation.assert_not_called()
        agent.shutdown_memory_provider.assert_called_once()
        agent.close.assert_called_once()
        assert "Session DB unavailable" in mock_adapter.send.call_args.kwargs["content"]


class TestAIAgentGatewayPeerContract:
    def test_records_peer_through_public_contract(self):
        from run_agent import AIAgent

        agent = object.__new__(AIAgent)
        agent.session_id = "bg_test"
        agent.platform = "telegram"
        agent._session_db = MagicMock()
        agent._ensure_db_session = MagicMock()
        agent._user_id = "user"
        agent._gateway_session_key = "telegram:chat"
        agent._chat_id = "chat"
        agent._chat_type = "group"
        agent._thread_id = "topic"
        agent._chat_name = "Test chat"
        origin = {"execution_kind": "user_explicit_background"}

        agent.record_gateway_session_peer(origin=origin)

        agent._ensure_db_session.assert_called_once_with()
        record_kwargs = agent._session_db.record_gateway_session_peer.call_args.kwargs
        assert record_kwargs == {
            "source": "telegram",
            "user_id": "user",
            "session_key": "telegram:chat",
            "chat_id": "chat",
            "chat_type": "group",
            "thread_id": "topic",
            "display_name": "Test chat",
            "origin_json": json.dumps(origin),
        }
        record_args = agent._session_db.record_gateway_session_peer.call_args.args
        assert record_args == ("bg_test",)

    def test_missing_session_db_fails_explicitly(self):
        from run_agent import AIAgent

        agent = object.__new__(AIAgent)
        agent.session_id = "bg_test"
        agent._session_db = None
        agent._ensure_db_session = MagicMock()

        with pytest.raises(RuntimeError, match="Session DB unavailable"):
            agent.record_gateway_session_peer(origin={})

    def test_missing_gateway_session_key_fails_explicitly(self):
        from run_agent import AIAgent

        agent = object.__new__(AIAgent)
        agent.session_id = "bg_test"
        agent._session_db = MagicMock()
        agent._ensure_db_session = MagicMock()
        agent._gateway_session_key = ""

        with pytest.raises(RuntimeError, match="Gateway session key missing"):
            agent.record_gateway_session_peer(origin={})

        agent._session_db.record_gateway_session_peer.assert_not_called()


# ---------------------------------------------------------------------------
# /background in help and known_commands
# ---------------------------------------------------------------------------


class TestBackgroundInHelp:
    """Verify /background appears in help text and known commands."""

    @pytest.mark.asyncio
    async def test_background_in_help_output(self):
        """The /help output includes /background."""
        runner = _make_runner()
        event = _make_event(text="/help")
        result = await runner._handle_help_command(event)
        assert "/background" in result


# ---------------------------------------------------------------------------
# CLI /background command definition
# ---------------------------------------------------------------------------


class TestBackgroundInCLICommands:
    """Verify /background is registered in the CLI command system."""


    def test_background_autocompletes(self):
        """The /background command appears in autocomplete results."""
        pytest.importorskip("prompt_toolkit")
        from hermes_cli.commands import SlashCommandCompleter
        from prompt_toolkit.document import Document

        completer = SlashCommandCompleter()
        doc = Document("backgro")  # Partial match
        completions = list(completer.get_completions(doc, None))
        # Text doesn't start with / so no completions
        assert len(completions) == 0

        doc = Document("/backgro")  # With slash prefix
        completions = list(completer.get_completions(doc, None))
        cmd_displays = [str(c.display) for c in completions]
        assert any("/background" in d for d in cmd_displays)

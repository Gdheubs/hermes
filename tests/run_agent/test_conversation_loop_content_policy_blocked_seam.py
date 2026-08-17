"""Seam tests for the conversation_loop R1 extraction.

R1 moved ``_content_policy_blocked_result`` byte-verbatim out of
``agent/conversation_loop.py`` (lines 1043-1065 at pin
ee4bb75b532e932a1055d9a710802a7435163b6a) into the new module
``agent/content_policy_blocked_result.py``, leaving a real module-level
binding in ``agent/conversation_loop.py`` so the module-path contract
survives (consumers: run_agent.py, cli.py, turn_finalizer.py,
context_compressor.py, acp_adapter/server.py, gateway/run.py,
tui_gateway/server.py).

These tests pin:

1. Object identity — ``agent.conversation_loop._content_policy_blocked_result``
   IS the same function object as
   ``agent.content_policy_blocked_result._content_policy_blocked_result``, so
   module-object patch consumers
   (``agent.conversation_loop._content_policy_blocked_result``) keep working.
2. Behavior through ``run_conversation`` — both the HTTP-200 refusal handler
   (``finish_reason == "content_filter"``) and the exception-path handler
   (``FailoverReason.content_policy_blocked``) must return the identical
   terminal turn shape (failed, non-completed, ``content_policy_blocked:``
   prefixed error).

The behavioral tests are extraction-agnostic: they pass identically whether
the builder lives in ``conversation_loop.py`` or the extracted module.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def test_content_policy_blocked_result_object_identity():
    """The forwarder must be the extracted module's own function object."""
    import agent.conversation_loop as conversation_loop
    import agent.content_policy_blocked_result as blocked_module

    name = "_content_policy_blocked_result"
    assert getattr(conversation_loop, name) is getattr(blocked_module, name), (
        "conversation_loop._content_policy_blocked_result must be the same "
        "function object as content_policy_blocked_result._content_policy_blocked_result "
        "so module-object patch consumers keep working."
    )
    # Sanity: callable with the pure-builder signature survives extraction.
    import inspect

    sig = inspect.signature(blocked_module._content_policy_blocked_result)
    for param in ("messages", "api_call_count", "final_response", "error_detail"):
        assert param in sig.parameters, param
    assert sig.return_annotation.__name__ == "Dict"


def _make_agent():
    """Minimal real AIAgent on the chat_completions transport (mirrors
    tests/run_agent/test_32646_fallback_429_after_timeout.py)."""
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://example.com/v1",
            provider="openai-compat",
            model="test-model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.api_mode = "chat_completions"
    agent.client = MagicMock()
    return agent


def _response(content: str, finish_reason: str = "stop"):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test-model", usage=None)


def _run_turn(agent, fake_api_call):
    """Drive one run_conversation turn with a scripted API call.

    ``_interruptible_api_call`` is the loop's non-streaming API entry point;
    patching it keeps the test off the network and fully deterministic.
    """
    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_dump_api_request_debug"),
        patch("agent.agent_runtime_helpers.time.sleep"),
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda m, p: m,
        ),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        return agent.run_conversation(
            "hello", conversation_history=[], task_id="conv-r1-seam"
        )


def test_http_200_refusal_handler_returns_blocked_result():
    """finish_reason=content_filter (HTTP-200 refusal) must funnel through the
    extracted builder: failed, non-completed, content_policy_blocked error."""
    agent = _make_agent()

    def fake_api_call(api_kwargs):
        return _response("I can't help with that request.", finish_reason="content_filter")

    result = _run_turn(agent, fake_api_call)

    assert result["completed"] is False
    assert result["failed"] is True
    assert result["api_calls"] == 1
    assert result["error"].startswith("content_policy_blocked: ")
    assert "I can't help with that request." in result["error"]
    assert "Model's explanation: I can't help with that request." in result["final_response"]
    assert isinstance(result["messages"], list)
    # Deterministic refusal: the loop must NOT retry (api_calls stays 1).
    assert result["api_calls"] == 1


def test_exception_path_handler_returns_blocked_result():
    """A provider safety-filter rejection raised as a non-retryable client
    error must funnel through the extracted builder with the same shape."""
    agent = _make_agent()

    def fake_api_call(api_kwargs):
        raise Exception(
            "This content was flagged for possible cybersecurity risk. "
            "If this seems wrong, try rephrasing your request."
        )

    result = _run_turn(agent, fake_api_call)

    assert result["completed"] is False
    assert result["failed"] is True
    assert result["error"].startswith("content_policy_blocked: ")
    assert "cybersecurity risk" in result["error"]
    assert "cybersecurity risk" in result["final_response"]
    assert isinstance(result["messages"], list)
    # Non-retryable: no retry burn on a deterministic refusal.
    assert result["api_calls"] == 1

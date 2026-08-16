"""End-to-end regressions for the deterministic routing gate in the core loop.

The ``routing_gate`` produced by a ``pre_llm_call`` plugin is a *hard*
prerequisite, not prompt advice. These exercise the real ``run_conversation``
loop against an in-process mock provider and assert the fail-closed contract:

- a ``block`` gate aborts before any parent-model API call;
- an ``allow`` gate with ``disable_tools``/``disable_fallback`` produces exactly
  one tool-free, no-retry call; and
- a missing post-API review provenance fails closed.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest

from run_agent import AIAgent


class _MockHandler(BaseHTTPRequestHandler):
    captured_requests: list = []
    response_queue: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        type(self).captured_requests.append(req)
        resp = type(self).response_queue.pop(0) if type(self).response_queue else _text_resp("review done")
        msg = resp["choices"][0]["message"]
        if req.get("stream") is True:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            content = msg.get("content") or ""
            chunks = [
                {"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
                {"id": "m", "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]},
                {"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


def _text_resp(text: str) -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
    }


@pytest.fixture()
def agent_env():
    _MockHandler.captured_requests = []
    _MockHandler.response_queue = []
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    agent = AIAgent(
        api_key="test-key",
        base_url=f"http://127.0.0.1:{port}/v1",
        provider="openai-compat",
        model="test-model",
        max_iterations=10,
        enabled_toolsets=[],
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        save_trajectories=False,
        platform="cli",
    )
    agent.valid_tool_names = {"terminal"}
    agent.tools = [
        {"type": "function", "function": {"name": "terminal", "description": "x", "parameters": {}}}
    ]
    agent._fallback_chain = [{"model": "fallback-model", "provider": "other"}]
    agent._fallback_index = 0

    try:
        yield agent, _MockHandler
    finally:
        srv.shutdown()


def _allow_gate(agent) -> dict:
    return {
        "action": "allow",
        "state_key": "turn-key",
        "required_model": agent.model,
        "required_provider": agent.provider,
        "max_primary_calls": 1,
        "disable_fallback": True,
        "disable_tools": True,
    }


def test_block_gate_aborts_before_any_api_call(agent_env):
    agent, handler = agent_env

    def fake_invoke(hook_name, **kwargs):
        if hook_name == "pre_llm_call":
            return [{
                "routing_required": True,
                "context": "",
                "routing_gate": {"action": "block", "reason": "worker failed", "disable_fallback": True},
            }]
        return []

    with patch("hermes_cli.lifecycle.invoke_hook", side_effect=fake_invoke):
        result = agent.run_conversation("fix the parser bug", conversation_history=[], task_id="t")

    assert result["failed"] is True
    assert "model_routing_blocked" in result["error"]
    assert "worker failed" in result["error"]
    chat_requests = [r for r in handler.captured_requests if isinstance(r, dict) and "messages" in r]
    assert chat_requests == []


def test_allow_gate_disables_tools_and_fallback_for_single_review(agent_env):
    agent, handler = agent_env

    def fake_invoke(hook_name, **kwargs):
        if hook_name == "pre_llm_call":
            return [{"routing_required": True, "context": "", "routing_gate": _allow_gate(agent)}]
        if hook_name == "pre_api_request":
            return [{"action": "allow", "reason": "gate satisfied"}]
        if hook_name == "post_api_request":
            return [{"action": "allow", "routing_review_recorded": True}]
        return []

    with patch("hermes_cli.lifecycle.invoke_hook", side_effect=fake_invoke):
        result = agent.run_conversation("implement the feature", conversation_history=[], task_id="t")

    assert result.get("failed") is not True
    assert agent._fallback_chain == []
    chat_requests = [r for r in handler.captured_requests if isinstance(r, dict) and "messages" in r]
    assert len(chat_requests) == 1
    tools = chat_requests[0].get("tools")
    assert tools == [] or tools is None


def test_missing_review_provenance_fails_closed(agent_env):
    agent, handler = agent_env

    def fake_invoke(hook_name, **kwargs):
        if hook_name == "pre_llm_call":
            return [{"routing_required": True, "context": "", "routing_gate": _allow_gate(agent)}]
        if hook_name == "pre_api_request":
            return [{"action": "allow", "reason": "gate satisfied"}]
        # post_api_request records nothing -> review provenance missing.
        return []

    with patch("hermes_cli.lifecycle.invoke_hook", side_effect=fake_invoke):
        result = agent.run_conversation("implement the feature", conversation_history=[], task_id="t")

    assert result["failed"] is True
    assert "model_routing_blocked" in result["error"]

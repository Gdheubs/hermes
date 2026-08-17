"""Session token fuse: cumulative-token hard stop in the conversation loop.

End-to-end behavior tests running ``AIAgent.run_conversation`` against an
in-process mock provider. The fuse (``agent.session_token_hard_stop``) must:

* stop a turn BEFORE any API call when the session is already over the cap
  (no summary-fallback call either — the fuse exists to stop spending);
* leave turns untouched when disabled (0, the default);
* warn once at 80% without blocking the turn.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


class _MockHandler(BaseHTTPRequestHandler):
    captured_requests: list = []
    response_queue: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or b"{}")
        type(self).captured_requests.append(req)
        if type(self).response_queue:
            resp = type(self).response_queue.pop(0)
        else:
            resp = _text_resp("done")
        msg = resp["choices"][0]["message"]
        if req.get("stream") is True:
            content = msg.get("content") or ""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [{"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}]
            if content:
                chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})
            chunks.append({"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            payload = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def log_message(self, *args):  # silence request logging
        pass


def _text_resp(text: str) -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


@pytest.fixture()
def agent_env():
    _MockHandler.captured_requests = []
    _MockHandler.response_queue = []
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    test_home = tempfile.mkdtemp(prefix="hermes_token_fuse_")
    os.makedirs(os.path.join(test_home, ".hermes"))
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")

    for mod in list(sys.modules):
        if mod == "run_agent" or mod.startswith("agent.") or mod.startswith("tools.") or mod.startswith("hermes_"):
            del sys.modules[mod]
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
        provider="openai-compat", model="test-model",
        max_iterations=10, enabled_toolsets=[],
        quiet_mode=True, skip_context_files=True, skip_memory=True,
        save_trajectories=False, platform="cli",
    )

    try:
        yield agent, _MockHandler
    finally:
        srv.shutdown()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home


def test_blown_fuse_stops_turn_with_zero_api_calls(agent_env):
    agent, handler = agent_env
    agent.session_token_hard_stop = 1_000
    agent.session_total_tokens = 1_500

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    chat_requests = [r for r in handler.captured_requests if "messages" in r]
    assert chat_requests == [], (
        "a blown fuse must stop the turn before ANY provider call, "
        "including the budget-exhausted summary fallback"
    )
    assert result["api_calls"] == 0
    assert str(result["turn_exit_reason"]).startswith("session_token_fuse(")


def test_disabled_fuse_leaves_turn_untouched(agent_env):
    agent, handler = agent_env
    assert agent.session_token_hard_stop == 0  # default: disabled
    agent.session_total_tokens = 10_000_000
    handler.response_queue = [_text_resp("all good")]

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    chat_requests = [r for r in handler.captured_requests if "messages" in r]
    assert len(chat_requests) == 1
    assert result["final_response"] == "all good"


def test_warn_threshold_emits_once_and_does_not_block(agent_env):
    agent, handler = agent_env
    agent.session_token_hard_stop = 1_000
    agent.session_total_tokens = 900  # >= 80%
    handler.response_queue = [_text_resp("still working")]

    statuses = []
    agent._emit_status = statuses.append

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    chat_requests = [r for r in handler.captured_requests if "messages" in r]
    assert len(chat_requests) == 1
    assert result["final_response"] == "still working"
    warn_msgs = [s for s in statuses if "fuse" in s]
    assert len(warn_msgs) == 1
    assert agent._session_token_fuse_warned is True

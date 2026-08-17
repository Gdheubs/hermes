"""
Happy-path integration for DeepSeek wire-time replay economy.

Runs a real ``AIAgent.run_conversation`` turn against an in-process mock
DeepSeek endpoint (OpenAI-compat chat completions) with history containing a
large tool result and a plain assistant turn carrying reasoning. Asserts the
wire copy sent to the provider has:
  - the large tool result compacted (head+tail marker),
  - ``reasoning_content`` stripped from the plain assistant turn,
  - ``reasoning_content`` preserved on the tool-call assistant turn
    (DeepSeek thinking-mode echo-back).

Mirrors the in-process mock pattern of test_empty_tool_name_loop_dampening.
"""

from __future__ import annotations

import pytest

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Repo root = three levels up from tests/agent/<file>.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _MockHandler(BaseHTTPRequestHandler):
    captured_requests: list = []
    response_queue: list = []

    def _serve_responses_sse(self):
        """Serve the native Codex/Responses SSE wire (gpt-5.x, DeepSeek /responses)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        events = [
            {"type": "response.created", "response": {"id": "r1", "object": "response", "status": "in_progress"}},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"id": "m1", "type": "message", "role": "assistant", "phase": "default", "content": []}},
            {"type": "response.output_text.delta", "item_id": "m1", "output_index": 0, "content_index": 0, "delta": "done"},
            {"type": "response.output_item.done", "output_index": 0,
             "item": {"id": "m1", "type": "message", "role": "assistant",
                      "content": [{"type": "output_text", "text": "done"}]}},
            {"type": "response.completed",
             "response": {"id": "r1", "object": "response", "status": "completed",
                          "output": [{"id": "m1", "type": "message", "role": "assistant",
                                      "content": [{"type": "output_text", "text": "done"}]}]}},
        ]
        for e in events:
            self.wfile.write(f"event: {e['type']}\n".encode())
            self.wfile.write(("data: " + json.dumps(e) + "\n\n").encode())
        self.wfile.flush()

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        type(self).captured_requests.append(req)
        if self.path.rstrip("/").endswith("/responses"):
            self._serve_responses_sse()
            return
        is_stream = req.get("stream") is True
        resp = type(self).response_queue.pop(0) if type(self).response_queue else _text_resp("DONE")
        msg = resp["choices"][0]["message"]
        if is_stream:
            content = msg.get("content") or ""
            tcs = msg.get("tool_calls")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [{"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}]
            if content:
                chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})
            if tcs:
                for ti, tc in enumerate(tcs):
                    chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"tool_calls": [{
                        "index": ti, "id": tc["id"], "type": "function",
                        "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}]}, "finish_reason": None}]})
            chunks.append({"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tcs else "stop"}]})
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

    def log_message(self, *a, **kw):  # noqa: N802 — silence default stderr logging
        pass


def _text_resp(text: str) -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


@pytest.mark.integration
@pytest.mark.wire_it
def test_deepseek_wire_compacts_tool_result_and_strips_plain_reasoning():
    import pytest

    _MockHandler.captured_requests = []
    _MockHandler.response_queue = [_text_resp("done")]
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    test_home = tempfile.mkdtemp(prefix="hermes_e2e_dsreplay_")
    os.makedirs(os.path.join(test_home, ".hermes"))
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")

    try:
        for mod in list(sys.modules):
            if mod == "run_agent" or mod.startswith("agent.") or mod.startswith("tools.") or mod.startswith("hermes_"):
                del sys.modules[mod]
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
            provider="deepseek", model="deepseek-v4-flash",
            max_iterations=5, enabled_toolsets=[],
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            save_trajectories=False, platform="cli",
        )
        agent.valid_tool_names = {"read_file"}

        history = [
            {"role": "user", "content": "read the file"},
            {"role": "assistant", "content": "", "reasoning_content": "toolchain",
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "read_file", "arguments": '{"path": "x"}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "L" * 13000},
            {"role": "assistant", "content": "preview", "reasoning_content": "R" * 500},
        ]
        agent.run_conversation("go on", conversation_history=history, task_id="t")

        wire = _MockHandler.captured_requests[-1]
        msgs = wire["messages"]
        tool_msg = next(m for m in msgs if m.get("role") == "tool")
        plain_assistant = next(m for m in msgs if m.get("role") == "assistant" and m.get("content") == "preview")
        tool_turn = next(m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls"))

        assert tool_msg["content"] != "L" * 13000
        assert "--- head ---" in tool_msg["content"] and "--- tail ---" in tool_msg["content"]
        assert "reasoning_content" not in plain_assistant
        assert tool_turn.get("reasoning_content") == "toolchain"
        # Echo-only preflight defers compression to the post-compaction size:
        # the recorded request pressure must sit below the raw session estimate.
        from agent.model_metadata import estimate_messages_tokens_rough

        assert agent.context_compressor._pending_request_rough_tokens < estimate_messages_tokens_rough(history)
        # Send-copy only: the passed-in history keeps the raw result.
        assert history[2]["content"] == "L" * 13000, "stored history must keep the raw result"
    finally:
        srv.shutdown()
        # Same teardown hygiene as test_empty_tool_name_loop_dampening: the
        # AIAgent init routes file logging through hermes_logging's process-global
        # queue listener under the temp HERMES_HOME; stop the listener + close
        # its handlers before rmtree so a later log write in the same process
        # can't hit the deleted path (cross-file flake).
        from hermes_logging import _reset_queued_handlers
        _reset_queued_handlers()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home


@pytest.mark.integration
@pytest.mark.wire_it
def test_openai_wire_preflight_tracks_compacted_wire():
    """Ungated preflight on a non-echo wire: the recorded request pressure
    sits below the raw session estimate (the raw overstatement used to fire
    compression early); the wire still carries the compacted marker."""
    _MockHandler.captured_requests = []
    _MockHandler.response_queue = [_text_resp("done")]
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    test_home = tempfile.mkdtemp(prefix="hermes_e2e_openai_")
    os.makedirs(os.path.join(test_home, ".hermes"))
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")

    try:
        from run_agent import AIAgent

        # gpt-4o's native wire is OpenAI chat-completions (newer gpt-5.x
        # models route to Codex Responses); the mock serves chat/completions,
        # so this uses the model's real wire with no forcing.
        agent = AIAgent(
            api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
            provider="openai", model="gpt-4o",
            max_iterations=5, enabled_toolsets=[],
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            save_trajectories=False, platform="cli",
        )
        agent.valid_tool_names = {"read_file"}

        history = [
            {"role": "user", "content": "read the file"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "read_file", "arguments": '{"path": "x"}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "L" * 130_000},
        ]
        agent.run_conversation("go on", conversation_history=history, task_id="t")

        from agent.model_metadata import estimate_messages_tokens_rough

        # The recorded pressure (estimate + tool defs) must sit far below the
        # raw session size: the ungated preflight tracks the compacted wire,
        # not the ~40x raw overstatement of an oversized tool result.
        raw = estimate_messages_tokens_rough(history)
        pressure = agent.context_compressor._pending_request_rough_tokens
        assert pressure < raw, f"openai preflight={pressure} must track the compacted wire, not raw={raw}"
        wire = _MockHandler.captured_requests[-1]
        tool_msg = next(m for m in wire["messages"] if m.get("role") == "tool")
        assert "--- head ---" in tool_msg["content"]
    finally:
        srv.shutdown()
        from hermes_logging import _reset_queued_handlers

        _reset_queued_handlers()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home


@pytest.mark.integration
@pytest.mark.wire_it
def test_gpt56_native_responses_wire_preflight_tracks_compacted_wire():
    """gpt-5.6's native Codex/Responses SSE wire: compaction runs
    pre-conversion and the ungated preflight tracks the compacted wire."""
    _MockHandler.captured_requests = []
    _MockHandler.response_queue = []
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    test_home = tempfile.mkdtemp(prefix="hermes_e2e_responses_")
    os.makedirs(os.path.join(test_home, ".hermes"))
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")

    try:
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
            provider="openai", model="gpt-5.6",
            max_iterations=5, enabled_toolsets=[],
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            save_trajectories=False, platform="cli",
        )
        assert agent.api_mode == "codex_responses"  # native wire, no forcing
        agent.valid_tool_names = {"read_file"}

        history = [
            {"role": "user", "content": "read the file"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "read_file", "arguments": '{"path": "x"}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "L" * 130_000},
        ]
        agent.run_conversation("go on", conversation_history=history, task_id="t")

        from agent.model_metadata import estimate_messages_tokens_rough

        raw = estimate_messages_tokens_rough(history)
        pressure = agent.context_compressor._pending_request_rough_tokens
        assert pressure < raw, f"responses preflight={pressure} must track the compacted wire, not raw={raw}"
        wire = _MockHandler.captured_requests[-1]
        tool_out = next(m for m in wire["input"] if m.get("type") == "function_call_output")
        assert "--- head ---" in tool_out["output"]
    finally:
        srv.shutdown()
        from hermes_logging import _reset_queued_handlers

        _reset_queued_handlers()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home

"""Classified turn-error messages for TUI/desktop frames (#64182 item 3)."""

from __future__ import annotations

import types

from tui_gateway.server import _fail_inflight_turn, _summarize_turn_error_message


def test_summarize_enriches_exception_with_agent_route():
    class _Boom(Exception):
        status_code = 429

    agent = types.SimpleNamespace(
        provider="xai",
        model="grok-4.5",
        base_url="https://api.x.ai/v1",
        _summarize_api_error=lambda e: "rate limited",
    )
    msg = _summarize_turn_error_message(_Boom("request failed"), agent)
    assert "rate limited" in msg
    assert "HTTP 429" in msg
    assert "provider=xai" in msg
    assert "model=grok-4.5" in msg


def test_summarize_result_dict_keeps_failure_reason():
    agent = types.SimpleNamespace(
        provider="openrouter",
        model="foo",
        base_url="https://openrouter.ai/api/v1",
    )
    msg = _summarize_turn_error_message(
        {
            "error": "credits exhausted",
            "failure_reason": "billing",
            "failed": True,
        },
        agent,
    )
    assert "credits exhausted" in msg
    assert "reason=billing" in msg
    assert "provider=openrouter" in msg


def test_fail_inflight_uses_summarized_message():
    session = {
        "agent": types.SimpleNamespace(
            provider="ollama",
            model="qwen",
            base_url="http://127.0.0.1:11434",
            _summarize_api_error=lambda e: "connection refused",
        ),
        "inflight_turn": {"user": "hi", "assistant": "", "started_at": 0},
    }
    _fail_inflight_turn(session, ConnectionError("request failed"))
    err = session["inflight_turn"]["error"]
    assert "connection refused" in err
    assert "provider=ollama" in err


def test_summarize_renders_fallback_chain_not_list_repr():
    agent = types.SimpleNamespace(
        provider="nous",
        model="deepseek-v4-flash",
        _fallback_chain=[
            {"provider": "xai", "model": "grok-4.5"},
            {"provider": "openrouter", "model": "foo"},
        ],
    )
    msg = _summarize_turn_error_message(RuntimeError("request failed"), agent)
    assert "fallback=grok-4.5 (xai) → foo (openrouter)" in msg
    assert "[{" not in msg
    assert "'provider'" not in msg

"""Unit tests for agent.turn_finalizer._count_tool_errors.

The count feeds ``tool_error_count`` into the Layer 0 outcome eval and must
reflect ONLY the current turn — ``messages`` is the full session history, and
earlier turns' tool errors must not be counted (nor re-parsed).
"""

from __future__ import annotations

from agent.turn_finalizer import _count_tool_errors


def _tool(content: str) -> dict:
    return {"role": "tool", "content": content}


def test_counts_tool_errors_from_current_turn_only():
    messages = [
        {"role": "user", "content": "first turn"},
        _tool('{"error": "old failure"}'),
        _tool('{"error": "another old failure"}'),
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second turn"},
        _tool('{"error": "this turn failure"}'),
        _tool('{"ok": true}'),
    ]
    assert _count_tool_errors(messages) == 1


def test_turn_boundary_is_the_last_user_message():
    """Tool rows after the last user message belong to the current turn even
    when no assistant row separates them; earlier turns are excluded."""
    messages = [
        {"role": "user", "content": "first turn"},
        _tool('{"error": "old"}'),
        {"role": "user", "content": "second turn"},
        _tool('{"error": "current 1"}'),
        _tool('{"error": "current 2"}'),
    ]
    assert _count_tool_errors(messages) == 2


def test_no_user_message_counts_all_tool_errors():
    messages = [_tool('{"error": "a"}'), _tool('{"error": "b"}')]
    assert _count_tool_errors(messages) == 2


def test_non_json_and_empty_tool_content_ignored():
    messages = [
        {"role": "user", "content": "go"},
        _tool("plain text, not json"),
        _tool(""),
        _tool('{"error": true}'),
    ]
    assert _count_tool_errors(messages) == 1


def test_missing_error_key_not_counted():
    messages = [
        {"role": "user", "content": "go"},
        _tool('{"result": "fine"}'),
        _tool('{"error": null}'),
    ]
    assert _count_tool_errors(messages) == 0


def test_empty_messages_returns_zero():
    assert _count_tool_errors([]) == 0

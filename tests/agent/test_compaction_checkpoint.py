"""Tests for the deterministic compaction checkpoint mode."""

from unittest.mock import patch

from agent.context_compressor import ContextCompressor


def _compressor(**kw):
    return ContextCompressor(
        model="test-model", threshold_percent=0.75, protect_first_n=1,
        protect_last_n=2, quiet_mode=True, config_context_length=200000,
        provider="test", **kw
    )


def _tool_heavy_messages():
    return [
        {"role": "user", "content": "refactor the parser"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "L" * 5000},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "type": "function",
                             "function": {"name": "run_tests", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "name": "run_tests", "content": "R" * 5000},
        {"role": "assistant", "content": "done", "tool_calls": []},
    ]


def test_span_tool_ratio():
    cc = _compressor()
    assert cc._span_tool_ratio(_tool_heavy_messages()) >= 0.7
    conv = [{"role": "assistant", "content": "a" * 400},
            {"role": "assistant", "content": "b" * 400}]
    assert cc._span_tool_ratio(conv) < 0.7


def test_checkpoint_content():
    cc = _compressor()
    msgs = _tool_heavy_messages()
    cp = cc._build_compaction_checkpoint(msgs, msgs, 0, memory_context="keep the parser note")
    assert "GOAL" in cp and "NEXT ACTION" in cp and "RECENT TOOL EVIDENCE" in cp
    assert "MEMORY CONTEXT" in cp and "parser note" in cp
    assert "session" in cp  # the recovery note (raw middle stays searchable)


def test_compress_uses_checkpoint_when_enabled_and_tool_heavy():
    cc = _compressor(checkpoint_mode=True)
    msgs = _tool_heavy_messages()
    with patch("agent.context_compressor.call_llm", side_effect=AssertionError("no LLM")) as llm:
        result = cc.compress(list(msgs))
    assert not llm.called
    joined = " ".join(str(m.get("content")) for m in result)
    assert "NEXT ACTION" in joined
    assert cc._previous_summary and "NEXT ACTION" in cc._previous_summary


def test_compress_off_by_default_keeps_llm_path():
    cc = _compressor()
    msgs = _tool_heavy_messages()
    with patch("agent.context_compressor.call_llm", side_effect=RuntimeError("no provider")):
        result = cc.compress(list(msgs))  # LLM path fails -> static fallback, no crash
    assert len(result) <= len(msgs)


def test_checkpoint_message_is_detected_as_a_summary():
    """End-to-end: the child-session / memory / frontend consumers detect the
    checkpoint message as a compaction summary (merged-into-tail + metadata)."""
    from agent.context_compressor import is_compaction_summary_message

    cc = _compressor(checkpoint_mode=True)
    result = cc.compress(list(_tool_heavy_messages()))
    assert any(is_compaction_summary_message(m) for m in result)


def test_multi_signal_coding_score():
    """The hardened heuristic: ANY strong signal decides (tool tokens, coding
    tool names, prompt hints) while a plain question scores 0."""
    cc = _compressor()
    small_coding = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "write_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    assert cc._coding_score(small_coding, small_coding) >= 0.7  # S2: coding names, tiny payloads
    code_fence = [{"role": "user", "content": "```python\nx=1\n```\nwhy?"}]
    assert cc._coding_score(code_fence, code_fence) == 1.0      # S3: code fence, no tools
    big_unknown = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "custom_analyzer", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 8000},
    ]
    assert cc._coding_score(big_unknown, big_unknown) >= 0.7    # S1: big results, unknown name
    plain = [{"role": "user", "content": "what is 2+2?"}, {"role": "assistant", "content": "4"}]
    assert cc._coding_score(plain, plain) < 0.7                 # all signals low


def test_checkpoint_uses_tool_heavy_not_coding_hint():
    """A conversational middle quoting code is NOT tool-heavy: the checkpoint
    must keep the LLM summary (the coding hint alone must not fire it)."""
    cc = _compressor(checkpoint_mode=True)
    msgs = [{"role": "user", "content": "```python\nx=1\n```\nwhy?"},
            {"role": "assistant", "content": "a" * 400},
            {"role": "user", "content": "and?"},
            {"role": "assistant", "content": "b" * 400}]
    assert cc._coding_score(msgs, msgs) >= 0.7          # coding hint fires the score
    assert cc._tool_heavy_score(msgs) < 0.7             # but not the tool-heavy signal


def test_checkpoint_mode_conversational_keeps_llm_path():
    """Black-box: checkpoint_mode ON + a conversational (non-tool-heavy)
    middle must NOT produce the checkpoint — the LLM summary path stays."""
    from unittest.mock import patch

    cc = _compressor(checkpoint_mode=True)
    msgs = [{"role": "system", "content": "system"}]
    for i in range(6):
        msgs.append({"role": "user", "content": f"q{i}"})
        msgs.append({"role": "assistant", "content": f"a{i} " + "z" * 400})
    with patch("agent.context_compressor.call_llm", side_effect=RuntimeError("no provider")):
        result = cc.compress(list(msgs))
    joined = " ".join(str(m.get("content") or "") for m in result)
    assert "NEXT ACTION" not in joined and "GOAL" not in joined

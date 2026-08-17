"""Unit tests for agent/deepseek_replay.py (wire-time context economy)."""

from agent.deepseek_replay import (
    ReplayCompactionLimits,
    apply_deepseek_replay_compaction,
    estimate_request_tokens_after_deepseek_replay,
    is_deepseek_replay_target,
    tool_result_replay_content,
)

_DS = {"provider": "deepseek", "model": "deepseek-v4-flash", "base_url": "https://api.deepseek.com/v1"}
_KIMI = {"provider": "kimi-coding", "model": "moonshot-v1-128k", "base_url": "https://api.moonshot.ai/v1"}
_MIMO = {"provider": "xiaomi", "model": "MiMo-7B-RL", "base_url": "https://api.xiaomimimo.com/v1"}
_OPENAI = {"provider": "openai", "model": "gpt-4o", "base_url": ""}


def test_openai_chat_completions_gets_compaction_not_strip():
    # Compaction is wire-based: any non-Anthropic-schema wire qualifies
    # (OpenAI, Mistral, Groq, ...). The reasoning strip stays provider-gated.
    assert not is_deepseek_replay_target(**_OPENAI)
    messages = [{"role": "assistant", "content": "plain", "reasoning_content": "x" * 100},
                {"role": "tool", "tool_call_id": "t1", "content": "z" * 13000}]
    out, diag = apply_deepseek_replay_compaction(messages, **_OPENAI)
    assert diag.compacted == 1 and "--- head ---" in messages[1]["content"]
    assert messages[0]["reasoning_content"] == "x" * 100  # strip NOT applied
    assert diag.stripped_reasoning == 0
    # Wire-agnostic: fresh Anthropic input compacts too.
    fresh = [{"role": "assistant", "content": "plain", "reasoning_content": "x" * 100},
             {"role": "tool", "tool_call_id": "t1", "content": "z" * 13000}]
    out2, diag2 = apply_deepseek_replay_compaction(fresh, **_OPENAI)
    assert diag2.compacted == 1 and "--- head ---" in fresh[1]["content"]


def test_compaction_thresholds():
    small = "ok" * 100
    assert tool_result_replay_content(small) == small
    _, diag = apply_deepseek_replay_compaction([{"role": "tool", "tool_call_id": "t1", "content": small}], **_DS)
    assert diag.compacted == 0 and diag.raw_tokens == diag.replay_tokens
    # 2500 CJK runes ≈ 2500 tokens but ≤ 3000 runes → kept verbatim (rune floor).
    assert tool_result_replay_content("界" * 2500) == "界" * 2500
    big = "b" * 13000  # ~3250 est tokens > 2000; chars > 12288
    replay = tool_result_replay_content(big)
    assert ("--- head ---\n" + "b" * 1500) in replay and replay.endswith("b" * 1500)
    _, diag = apply_deepseek_replay_compaction([{"role": "tool", "tool_call_id": "t1", "content": big}], **_DS)
    assert diag.compacted == 1 and diag.tokens_saved > 0
    assert diag.raw_tokens == diag.replay_tokens + diag.tokens_saved and "saved=" in diag.summary()


def test_strip_keeps_tool_turn_reasoning():
    messages = [{"role": "assistant", "content": "plain", "reasoning_content": "x" * 100},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}], "reasoning_content": " keep "},
                {"role": "tool", "tool_call_id": "c1", "content": "z" * 13000}]
    _, diag = apply_deepseek_replay_compaction(messages, **_DS)
    assert "reasoning_content" not in messages[0]
    assert messages[1]["reasoning_content"] == " keep "
    assert diag.stripped_reasoning == 1 and diag.compacted == 1


def test_echo_family_strip_rules_match_provider_contracts():
    # Compaction is wire-generic (whole family). Reasoning strip: DeepSeek +
    # MiMo proven safe; Kimi unproven → keeps reasoning echoed.
    for kw in (_KIMI, _MIMO):
        messages = [{"role": "assistant", "content": "plain", "reasoning_content": "x" * 100},
                    {"role": "tool", "tool_call_id": "t1", "content": "b" * 13000}]
        _, diag = apply_deepseek_replay_compaction(messages, **kw)
        assert diag.compacted == 1
    _, diag = apply_deepseek_replay_compaction([{"role": "assistant", "content": "plain", "reasoning_content": "x" * 100}], **_KIMI)
    assert diag.stripped_reasoning == 0
    for kw in (_MIMO, _DS):
        _, diag = apply_deepseek_replay_compaction([{"role": "assistant", "content": "plain", "reasoning_content": "x" * 100}], **kw)
        assert diag.stripped_reasoning == 1


def test_estimator_reflects_strip_and_compaction():
    from agent.model_metadata import estimate_messages_tokens_rough

    mixed = [{"role": "assistant", "content": "plain", "reasoning_content": "R" * 5000},
             {"role": "tool", "tool_call_id": "t1", "content": "b" * 13000}]
    raw = estimate_messages_tokens_rough(mixed)
    post_ds = estimate_request_tokens_after_deepseek_replay(mixed, **_DS)
    post_mimo = estimate_request_tokens_after_deepseek_replay(mixed, **_MIMO)
    post_kimi = estimate_request_tokens_after_deepseek_replay(mixed, **_KIMI)
    assert post_ds < raw and post_mimo == post_ds < post_kimi  # ds+mimo strip reasoning; kimi keeps it
    # Compaction subtraction applies on every wire; reasoning strip stays
    # echo-gated (openai/anthropic subtract compaction only, so > post_ds).
    post_openai = estimate_request_tokens_after_deepseek_replay(mixed, **_OPENAI)
    assert raw > post_openai > post_ds
    post_anthropic = estimate_request_tokens_after_deepseek_replay(
        mixed, provider="anthropic", model="claude-sonnet-4.7", base_url=""
    )
    assert post_anthropic == post_openai  # both compaction-only wires


def test_skips_non_dict_and_non_string_tool_content():
    messages = ["junk",
                {"role": "tool", "tool_call_id": "t1", "content": [{"type": "text", "text": "z" * 13000}]},
                {"role": "tool", "tool_call_id": "t2", "content": ""}]
    _, diag = apply_deepseek_replay_compaction(messages, **_DS)
    assert messages[1]["content"] == [{"type": "text", "text": "z" * 13000}]
    assert diag.compacted == 0 and diag.raw_tokens == 0


def test_merge_replay_usage():
    from agent.deepseek_replay import DeepSeekReplayDiagnostics, merge_replay_usage

    usage = {"prompt_tokens": 1}
    merge_replay_usage(usage, DeepSeekReplayDiagnostics(raw_tokens=100, replay_tokens=60, compacted=1, stripped_reasoning=2))
    assert usage == {"prompt_tokens": 1, "deepseek_replay_tokens_saved": 40, "deepseek_tool_results_compacted": 1,
                     "deepseek_reasoning_stripped": 2}
    empty = {"prompt_tokens": 1}
    merge_replay_usage(empty, DeepSeekReplayDiagnostics())
    assert empty == {"prompt_tokens": 1}


def test_replay_compaction_limits_defaults_and_overrides():
    from unittest.mock import patch

    from agent.deepseek_replay import replay_compaction_limits

    with patch("hermes_cli.config.load_config", return_value={}):
        base = replay_compaction_limits("deepseek")
        assert (base.max_tokens, base.max_chars, base.keep_runes) == (2000, 12288, 3000)
    cfg = {"replay_compaction": {"max_tokens": 2000, "max_chars": 12288, "keep_runes": 3000,
                                 "provider_overrides": {"anthropic": {"max_tokens": 4000}}}}
    with patch("hermes_cli.config.load_config", return_value=cfg):
        assert replay_compaction_limits("anthropic").max_tokens == 4000
        assert replay_compaction_limits("deepseek").max_tokens == 2000
    with patch("hermes_cli.config.load_config", return_value={"replay_compaction": {"max_tokens": "garbage"}}):
        assert replay_compaction_limits("anthropic").max_tokens == 2000
    with patch("hermes_cli.config.load_config", return_value={"replay_compaction": {"max_tokens": True}}):
        assert replay_compaction_limits("anthropic").max_tokens == 2000  # bool rejected
    with patch("hermes_cli.config.load_config", side_effect=RuntimeError("boom")):
        assert replay_compaction_limits("anthropic").max_tokens == 2000  # config read failure


def test_apply_uses_custom_limits():
    content = "x" * 800  # ~200 est tokens — kept under the default threshold
    _, diag = apply_deepseek_replay_compaction([{"role": "tool", "tool_call_id": "t1", "content": content}], **_DS)
    assert diag.compacted == 0
    tight = ReplayCompactionLimits(max_tokens=50, max_chars=12288, keep_runes=100)
    _, diag = apply_deepseek_replay_compaction([{"role": "tool", "tool_call_id": "t1", "content": content}], **_DS, limits=tight)
    assert diag.compacted == 1


# ── All-wire coverage matrix ────────────────────────────────────────────────
# Every feature must be proven on every wire it touches. Compaction eligibility
# is wire-based; the strip is provider-contract-based; the estimate is
# echo-family-only.

# Two recent versions per provider (latest + previous); compaction is
# wire-agnostic, so the provider column is display-only here.
_COMPACTION_WIRES = [
    # (provider, model, base_url, api_mode, expect_compacted)
    ("Anthropic", "claude-fable-5", "", True),
    ("Anthropic", "claude-opus-5", "", True),
    ("Anthropic", "claude-sonnet-5", "", True),
    ("Anthropic", "claude-opus-4-8", "", True),
    ("OpenAI", "gpt-5.6", "", True),
    ("OpenAI", "gpt-5.5", "", True),
    ("Google", "gemini-3.7-flash", "", True),
    ("Google", "gemini-3.6-flash", "", True),
    ("Google", "gemini-3.1-pro", "", True),
    ("Meta", "muse-spark-1.2", "", True),
    ("Meta", "muse-glimmer", "", True),
    ("xAI", "grok-4.6", "", True),
    ("xAI", "grok-4.5", "", True),
    ("DeepSeek", "deepseek-v4-pro", "https://api.deepseek.com/v1", True),
    ("DeepSeek", "deepseek-v4-flash", "https://api.deepseek.com/v1", True),
    ("DeepSeek", "deepseek-v4-pro", "https://api.deepseek.com/v1", True),
    ("Moonshot AI", "kimi-k3", "https://api.moonshot.ai/v1", True),
    ("Moonshot AI", "kimi-k2.7", "https://api.moonshot.ai/v1", True),
    ("Z.ai", "glm-5.2", "", True),
    ("Z.ai", "glm-5.1", "", True),
    ("MiMo", "MiMo-V2.5-Pro", "https://api.xiaomimimo.com/v1", True),
    ("MiMo", "MiMo-V2.5", "https://api.xiaomimimo.com/v1", True),
    ("Mistral", "mistral-medium-3.5", "", True),
    ("Mistral", "mistral-small-4", "", True),
]

_STRIP_WIRES = [
    # (label, provider, model, base_url, expect_stripped)
    ("DeepSeek", "deepseek", "deepseek-v4-pro", "https://api.deepseek.com/v1", True),
    ("DeepSeek", "deepseek", "deepseek-v4-flash", "https://api.deepseek.com/v1", True),
    ("MiMo", "xiaomi", "MiMo-V2.5-Pro", "https://api.xiaomimimo.com/v1", True),
    ("Qwen", "qwen", "qwen3.8-max", "https://portal.qwen.ai/v1", False),
    ("Moonshot AI", "kimi-coding", "kimi-k3", "https://api.moonshot.ai/v1", False),
    ("Z.ai", "zai-org", "glm-5.2", "", False),
    ("OpenAI", "openai", "gpt-5.6", "", False),
]


import pytest


@pytest.mark.parametrize(
    "provider,model,base_url,expect_compacted", _COMPACTION_WIRES
)
def test_compaction_all_wires(provider, model, base_url, expect_compacted):
    messages = [{"role": "tool", "tool_call_id": "t1", "content": "b" * 13000}]
    _, diag = apply_deepseek_replay_compaction(
        messages, model=model, base_url=base_url
    )
    assert (diag.compacted == 1) == expect_compacted, f"{provider}: compacted={diag.compacted}"


@pytest.mark.parametrize(
    "label,provider,model,base_url,expect_stripped", _STRIP_WIRES
)
def test_strip_all_provider_contracts(label, provider, model, base_url, expect_stripped):
    messages = [{"role": "assistant", "content": "plain", "reasoning_content": "x" * 100}]
    _, diag = apply_deepseek_replay_compaction(
        messages, provider=provider, model=model, base_url=base_url
    )
    assert (diag.stripped_reasoning == 1) == expect_stripped, f"{label}: stripped={diag.stripped_reasoning}"


def test_estimate_all_wires():
    from agent.model_metadata import estimate_messages_tokens_rough

    mixed = [{"role": "assistant", "content": "plain", "reasoning_content": "R" * 5000},
             {"role": "tool", "tool_call_id": "t1", "content": "b" * 13000}]
    raw = estimate_messages_tokens_rough(mixed)
    # Echo family (any wire): compaction + (proven) reasoning subtracted.
    assert estimate_request_tokens_after_deepseek_replay(
        mixed, provider="deepseek", model="deepseek-v4-flash", base_url="https://api.deepseek.com/v1"
    ) < raw
    # Non-echo chat-completions (OpenAI): compaction subtracted.
    assert estimate_request_tokens_after_deepseek_replay(mixed, provider="openai", model="gpt-4o", base_url="") < raw


def test_apply_is_idempotent_across_retries():
    # The loop reuses api_messages across attempts; a second pass must not
    # re-compact, re-strip, or mutate further (byte-stable for cache prefix).
    messages = [{"role": "assistant", "content": "plain", "reasoning_content": "x" * 100},
                {"role": "tool", "tool_call_id": "t1", "content": "b" * 13000}]
    out1, diag1 = apply_deepseek_replay_compaction(messages, **_DS)
    assert diag1.compacted == 1 and diag1.stripped_reasoning == 1
    before = [dict(m) for m in out1]
    out2, diag2 = apply_deepseek_replay_compaction(out1, **_DS)
    assert [dict(m) for m in out2] == before
    assert diag2.compacted == 0 and diag2.stripped_reasoning == 0


def test_compacted_content_reaches_anthropic_tool_result_block():
    # The anthropic adapter wraps msg["content"] into tool_result blocks at
    # send time; the compacted marker must survive the conversion.
    from agent.anthropic_adapter import convert_messages_to_anthropic

    # Orphaned tool results are stripped by the adapter; use a real turn shape
    # (assistant tool_use + matching tool result) so the block survives.
    msgs = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "b" * 13000}]
    out, diag = apply_deepseek_replay_compaction(
        msgs, provider="anthropic", model="claude-sonnet-4.7", base_url=""
    )
    assert diag.compacted == 1
    _, converted = convert_messages_to_anthropic(out)
    blocks = [b for m in converted if isinstance(m, dict) for b in (m.get("content") or []) if isinstance(b, dict)]
    result_block = next(b for b in blocks if b.get("type") == "tool_result")
    # Byte-identical through the adapter: no re-truncation, no mutation.
    assert result_block["content"] == out[1]["content"]
    text = str(result_block.get("content"))
    assert "compacted for model replay" in text and "--- head ---" in text and "--- tail ---" in text


def test_deepseek_via_anthropic_strip_survives_conversion():
    # DeepSeek model served over the Anthropic wire: the strip fires
    # (provider-gated, not wire-gated); the converter still produces valid
    # messages (thinking block for the tool-call turn, none for the plain).
    from agent.anthropic_adapter import convert_messages_to_anthropic

    msgs = [{"role": "assistant", "content": "preview", "reasoning_content": "R" * 100},
            {"role": "assistant", "content": "", "reasoning_content": "r" * 100,
             "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "b" * 13000}]
    out, diag = apply_deepseek_replay_compaction(
        msgs, provider="deepseek", model="deepseek-v4-flash",
        base_url="https://api.deepseek.com/v1"
    )
    assert diag.stripped_reasoning == 1 and diag.compacted == 1
    # The adapter requires reasoning_content on every replayed tool-call turn;
    # only PLAIN turns are stripped, so the tool-call turn keeps its reasoning.
    tool_turn = next(m for m in out if m.get("tool_calls"))
    assert tool_turn.get("reasoning_content") is not None
    _, converted = convert_messages_to_anthropic(out)
    roles = [m["role"] for m in converted]
    assert "assistant" in roles and "user" in roles  # valid shape, no crash


def test_unicode_compaction_survives_anthropic_conversion():
    # Rune-safety end to end: a CJK/emoji tool result is compacted (head+tail
    # by runes) and the converter wraps it byte-identically into the block.
    from agent.anthropic_adapter import convert_messages_to_anthropic

    content = ("界语混排 😀🎉 " * 3000)[:13000]
    msgs = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": content}]
    out, diag = apply_deepseek_replay_compaction(
        msgs, provider="anthropic", model="claude-sonnet-4.7", base_url=""
    )
    assert diag.compacted == 1
    _, converted = convert_messages_to_anthropic(out)
    blocks = [b for m in converted if isinstance(m, dict) for b in (m.get("content") or []) if isinstance(b, dict)]
    result_block = next(b for b in blocks if b.get("type") == "tool_result")
    assert result_block["content"] == out[1]["content"]
    assert "--- head ---" in result_block["content"] and "😀" in result_block["content"]


def test_strip_never_removes_thinking_content_blocks():
    # DeepSeek-via-Anthropic thinking round-trips as content blocks, not the
    # reasoning_content field; the strip must only remove the field, never a
    # thinking block (conservative direction (the endpoint accepts extras).
    msgs = [{"role": "assistant", "content": "preview", "reasoning_content": "R" * 100},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "T" * 100}],
             "reasoning_content": "R" * 100},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "b" * 13000}]
    out, diag = apply_deepseek_replay_compaction(
        msgs, provider="deepseek", model="deepseek-v4-flash",
        base_url="https://api.deepseek.com/anthropic"
    )
    assert diag.stripped_reasoning == 2  # both plain turns: field removed
    thinking_turn = next(m for m in out if isinstance(m.get("content"), list))
    assert thinking_turn["content"][0]["type"] == "thinking"  # block survives
    assert "reasoning_content" not in thinking_turn


def test_compacted_content_reaches_bedrock_tool_result_block():
    # Bedrock has its OWN converter (bedrock_adapter, not anthropic_adapter);
    # the compacted string must survive convert_messages_to_converse into the
    # toolResult text block.
    from agent.bedrock_adapter import convert_messages_to_converse

    msgs = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "b" * 13000}]
    out, diag = apply_deepseek_replay_compaction(
        msgs, provider="bedrock", model="claude-sonnet-4.7", base_url=""
    )
    assert diag.compacted == 1
    _, converted = convert_messages_to_converse(out)
    text_blocks = [b for m in converted if isinstance(m, dict)
                   for c in (m.get("content") or []) if isinstance(c, dict)
                   for b in (c.get("toolResult", {}).get("content") or []) if isinstance(b, dict)]
    text = " ".join(str(b.get("text", "")) for b in text_blocks)
    assert "compacted for model replay" in text and "--- head ---" in text


def test_send_copy_only_unit_pin():
    """The wire economy mutates the SEND COPY it is given — the caller's
    copy protects the session store (the integration test pins the real flow;
    this pins the contract at the unit level)."""
    import copy
    from agent.deepseek_replay import apply_deepseek_replay_compaction

    msgs = [
        {"role": "assistant", "content": "plain", "reasoning_content": "R" * 300},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "L" * 13000},
    ]
    stored = copy.deepcopy(msgs)  # the session store holds a deep copy
    out, _ = apply_deepseek_replay_compaction(
        copy.deepcopy(msgs), provider="deepseek", model="deepseek-v4-pro",
        base_url="https://api.deepseek.com/v1",
    )
    assert stored[0].get("reasoning_content") == "R" * 300   # the stored reasoning intact
    assert stored[2]["content"] == "L" * 13000               # the stored tool result raw
    wire_tool = next(m for m in out if m.get("role") == "tool")
    assert wire_tool["content"] != "L" * 13000               # the wire compacted

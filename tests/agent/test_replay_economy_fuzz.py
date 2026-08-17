"""Property-based fuzz for the replay economy (Hypothesis).

Deterministic per run (CI-reproducible); each run explores the strategy space
within the example budget, and failures shrink to minimal counterexamples
(rotate --hypothesis-seed in CI to sample new regions). Invariants pin the
compaction shape (exactly keep_runes runes), apply's bounded non-filtering
behavior, estimator-vs-wire tracking, and retry idempotency.
"""

from hypothesis import given, settings, strategies as st

from agent.deepseek_replay import (
    ReplayCompactionLimits,
    apply_deepseek_replay_compaction,
    merge_replay_usage,
    tool_result_replay_content,
)

# CI budget: bounded but meaningful.
_MAX_EXAMPLES = 200


@settings(max_examples=_MAX_EXAMPLES, deadline=None)
@given(
    content=st.text(max_size=100_000),
    max_tokens=st.integers(min_value=1, max_value=2_000_000),
    max_chars=st.integers(min_value=1, max_value=2_000_000),
    keep_runes=st.integers(min_value=1, max_value=10_000),
)
def test_compaction_retains_exactly_keep_runes(content, max_tokens, max_chars, keep_runes):
    limits = ReplayCompactionLimits(max_tokens, max_chars, keep_runes)
    out = tool_result_replay_content(content, limits=limits)
    assert isinstance(out, str)
    if out == content:
        return  # kept verbatim (small or under both thresholds)
    head_runes = keep_runes // 2
    tail_runes = keep_runes - head_runes
    head = out.split("--- head ---\n", 1)[1].split("\n\n--- omitted ---", 1)[0]
    tail = out.split("--- tail ---\n", 1)[1]
    assert len(head) == head_runes and len(tail) == tail_runes
    assert "original_estimated_tokens=" in out


@settings(max_examples=_MAX_EXAMPLES, deadline=None)
@given(
    n_tools=st.integers(min_value=0, max_value=5),
    n_plain=st.integers(min_value=0, max_value=5),
    content=st.text(max_size=20_000),
    reasoning=st.text(max_size=5_000),
)
def test_apply_bounded_and_non_filtering(n_tools, n_plain, content, reasoning):
    msgs = []
    for i in range(n_tools):
        msgs.append({"role": "assistant", "content": "", "reasoning_content": reasoning,
                     "tool_calls": [{"id": f"c{i}", "function": {"name": "f", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": content})
    for i in range(n_plain):
        msgs.append({"role": "assistant", "content": "a", "reasoning_content": reasoning})
    out, diag = apply_deepseek_replay_compaction(
        msgs, provider="deepseek", model="deepseek-v4-flash", base_url="https://api.deepseek.com/v1"
    )
    assert len(out) == len(msgs)  # never filters
    assert diag.compacted <= n_tools and diag.stripped_reasoning <= n_plain
    assert diag.raw_tokens >= diag.replay_tokens


def _random_session(n_tools, n_plain, content, reasoning):
    msgs = []
    for i in range(n_tools):
        msgs.append({"role": "assistant", "content": "", "reasoning_content": reasoning,
                     "tool_calls": [{"id": f"c{i}", "function": {"name": "f", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": content})
    for i in range(n_plain):
        msgs.append({"role": "assistant", "content": "a", "reasoning_content": reasoning})
    return msgs


@settings(max_examples=_MAX_EXAMPLES, deadline=None)
@given(
    n_tools=st.integers(min_value=0, max_value=5),
    n_plain=st.integers(min_value=0, max_value=5),
    content=st.text(max_size=20_000),
    reasoning=st.text(max_size=5_000),
)
def test_estimator_tracks_post_apply_wire(n_tools, n_plain, content, reasoning):
    # est must track the actual wire for ANY session shape (not just the
    # canary's fixed one): dropping the reasoning subtraction alone would push
    # est to ~2x wire on plain-turn-heavy sessions.
    from agent.deepseek_replay import estimate_request_tokens_after_deepseek_replay
    from agent.model_metadata import estimate_messages_tokens_rough

    est = estimate_request_tokens_after_deepseek_replay(
        _random_session(n_tools, n_plain, content, reasoning),
        provider="deepseek", model="deepseek-v4-flash", base_url="https://api.deepseek.com/v1",
    )
    out, _ = apply_deepseek_replay_compaction(
        _random_session(n_tools, n_plain, content, reasoning),
        provider="deepseek", model="deepseek-v4-flash", base_url="https://api.deepseek.com/v1",
    )
    wire = estimate_messages_tokens_rough(out)
    assert wire * 0.8 <= est <= wire * 1.5, f"est={est} wire={wire}"


@settings(max_examples=_MAX_EXAMPLES, deadline=None)
@given(
    n_tools=st.integers(min_value=0, max_value=5),
    n_plain=st.integers(min_value=0, max_value=5),
    content=st.text(max_size=20_000),
    reasoning=st.text(max_size=5_000),
)
def test_apply_idempotent_under_fuzz(n_tools, n_plain, content, reasoning):
    msgs = _random_session(n_tools, n_plain, content, reasoning)
    out1, _ = apply_deepseek_replay_compaction(
        msgs, provider="deepseek", model="deepseek-v4-flash", base_url="https://api.deepseek.com/v1"
    )
    before = [dict(m) for m in out1]
    out2, diag2 = apply_deepseek_replay_compaction(
        out1, provider="deepseek", model="deepseek-v4-flash", base_url="https://api.deepseek.com/v1"
    )
    assert [dict(m) for m in out2] == before
    assert diag2.compacted == 0 and diag2.stripped_reasoning == 0


@settings(max_examples=_MAX_EXAMPLES, deadline=None)
@given(
    n_tools=st.integers(min_value=0, max_value=3),
    content=st.text(max_size=20_000),
)
def test_merge_usage_never_clobbers_and_apply_is_byte_stable(n_tools, content):
    # merge_replay_usage must only add its namespaced keys, never touch the
    # rest of the usage dict; apply output must be byte-identical when run
    # twice on fresh identical sessions (deterministic, no hash-seed drift).
    session = _random_session(n_tools, 0, content, "")
    out1, diag1 = apply_deepseek_replay_compaction(
        session, provider="deepseek", model="deepseek-v4-flash", base_url="https://api.deepseek.com/v1"
    )
    out2, diag2 = apply_deepseek_replay_compaction(
        _random_session(n_tools, 0, content, ""),
        provider="deepseek", model="deepseek-v4-flash", base_url="https://api.deepseek.com/v1",
    )
    assert [dict(m) for m in out1] == [dict(m) for m in out2]  # byte-stable
    assert diag1 == diag2
    usage = {"prompt_tokens": 5, "total_tokens": 5}
    merge_replay_usage(usage, diag1)
    assert usage["prompt_tokens"] == 5 and usage["total_tokens"] == 5  # never clobbered
    if diag1.tokens_saved <= 0 and diag1.stripped_reasoning <= 0:
        assert set(usage) == {"prompt_tokens", "total_tokens"}  # no keys when zero


@settings(max_examples=_MAX_EXAMPLES, deadline=None)
@given(api_content=st.text(max_size=2000))
def test_apply_never_touches_api_content_sidecars(api_content):
    # User-correction sidecars are substituted at send time; the economy must
    # leave them byte-identical (never rewrite the persist-what-you-send form).
    msgs = [{"role": "user", "content": "scaffold", "api_content": api_content},
            {"role": "tool", "tool_call_id": "t1", "content": "b" * 13000}]
    out, _ = apply_deepseek_replay_compaction(
        msgs, provider="anthropic", model="claude-sonnet-4.7", base_url=""
    )
    assert out[0]["api_content"] == api_content and out[0]["content"] == "scaffold"


@settings(max_examples=_MAX_EXAMPLES, deadline=None)
@given(
    n_tools=st.integers(min_value=0, max_value=4),
    n_plain=st.integers(min_value=0, max_value=4),
    content=st.text(max_size=20_000),
    reasoning=st.text(max_size=5_000),
)
def test_deepseek_via_anthropic_wire_is_safe_and_idempotent(n_tools, n_plain, content, reasoning):
    # DeepSeek model over the Anthropic wire: strip fires (provider-gated),
    # compaction fires, second pass is a no-op (idempotent across retries).
    def _run(msgs):
        return apply_deepseek_replay_compaction(
            msgs, provider="deepseek", model="deepseek-v4-flash",
            base_url="https://api.deepseek.com/v1",
        )
    msgs = _random_session(n_tools, n_plain, content, reasoning)
    out1, diag1 = _run(msgs)
    before = [dict(m) for m in out1]
    out2, diag2 = _run(out1)
    assert [dict(m) for m in out2] == before
    assert diag2.compacted == 0 and diag2.stripped_reasoning == 0
    assert diag1.compacted <= n_tools and diag1.stripped_reasoning <= n_plain

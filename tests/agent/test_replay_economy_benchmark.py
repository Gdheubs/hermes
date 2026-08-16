"""CI canary: token-win floors for the wire-time economy.

Fast (pure in-memory). Pins the win factors so a future PR that weakens the
economy (thresholds, gates, strip, estimator, stats) fails the suite instead
of silently growing token usage. Session shared with
scripts/bench_replay_economy.py (single source of truth).
Limitation: a uniformly-zero estimate_tokens_rough would pass every floor
(raw = wire = 0); that is a measurement bug, not a savings regression — the
char-threshold still compacts, so real tokens are still saved.
"""

from scripts.bench_replay_economy import _deepseek_session

from agent.deepseek_replay import (
    DeepSeekReplayDiagnostics,
    apply_deepseek_replay_compaction,
    estimate_request_tokens_after_deepseek_replay,
    merge_replay_usage,
)
from agent.model_metadata import estimate_messages_tokens_rough

_DS = {"provider": "deepseek", "model": "deepseek-v4-flash", "base_url": "https://api.deepseek.com/v1"}
_OPENAI = {"provider": "openai", "model": "gpt-4o", "base_url": ""}


def test_economy_win_factors_hold_on_tool_heavy_session():
    msgs = _deepseek_session()
    raw = estimate_messages_tokens_rough(msgs)
    out, diag = apply_deepseek_replay_compaction(msgs, **_DS)
    wire = estimate_messages_tokens_rough(out)
    # Compaction + strip must at least halve the replayed input.
    assert wire * 2 <= raw, f"wire={wire} raw={raw}: economy regressed"
    assert diag.compacted >= 8, f"compacted={diag.compacted}: oversized results not compacted"
    assert diag.stripped_reasoning >= 6, f"stripped={diag.stripped_reasoning}: plain reasoning replayed"


def test_estimator_and_stats_report_the_savings():
    # est must track the actual post-apply wire (verified est/wire ~1.0): a
    # PR dropping the reasoning subtraction alone would push est to ~2x wire.
    est = estimate_request_tokens_after_deepseek_replay(_deepseek_session(), **_DS)
    out, diag = apply_deepseek_replay_compaction(_deepseek_session(), **_DS)
    wire = estimate_messages_tokens_rough(out)
    assert wire * 0.8 <= est <= wire * 1.5, f"est={est} wire={wire}: estimator drifted"
    usage = {}
    merge_replay_usage(usage, diag)
    assert usage.get("deepseek_replay_tokens_saved", 0) > 0, "stats missing the savings"


def test_wire_gates_still_hold():
    # OpenAI chat-completions keeps compaction (the win that must not regress).
    _, diag = apply_deepseek_replay_compaction(_deepseek_session(), **_OPENAI)
    assert diag.compacted >= 8, f"openai compacted={diag.compacted}: compaction lost for OpenAI"
    # Anthropic-schema wires get compaction too (string compaction runs
    # pre-conversion into tool_result blocks); the strip never applies there.
    _, diag = apply_deepseek_replay_compaction(_deepseek_session(), **_OPENAI)
    assert diag.compacted >= 8, "anthropic wire lost compaction"
    assert diag.stripped_reasoning == 0, "strip must not fire on non-echo providers"
    # DeepSeekReplayDiagnostics shape is stable (used by merge/display).
    assert DeepSeekReplayDiagnostics().tokens_saved == 0

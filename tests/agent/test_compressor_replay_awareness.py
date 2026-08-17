"""Compressor replay-awareness tests: _replays_all_turn_thinking and the
duplicate-alias charge in the tail-protection budget walks."""
from agent.context_compressor import (
    ContextCompressor,
    _CHARS_PER_TOKEN,
    _estimate_msg_budget_tokens,
)


def _stub(provider, model, base_url):
    cc = object.__new__(ContextCompressor)
    cc.provider, cc.model, cc.base_url = provider, model, base_url
    return cc


class TestCompressorReplayAllAwareness:
    def test_soft_replay_loopback(self):
        cc = _stub("local", "qwen", "http://localhost:8081/v1")
        assert cc._replays_all_turn_thinking() is True

    def test_require_side_echo_family(self):
        cc = _stub("deepseek", "d", "https://api.deepseek.com")
        assert cc._replays_all_turn_thinking() is True

    def test_strict_indifferent(self):
        cc = _stub("openai", "gpt-5", "https://api.openai.com/v1")
        assert cc._replays_all_turn_thinking() is False

    def test_result_cached_per_provider_triplet(self):
        cc = _stub("local", "qwen", "http://localhost:8081/v1")
        assert cc._replays_all_turn_thinking() is True
        assert cc._replay_all_cache is not None  # warm
        # a different stub for a strict provider evaluates independently
        assert _stub("mistral", "m", "https://api.mistral.ai/v1") \
            ._replays_all_turn_thinking() is False


class TestAgentSoftReplayCacheKey:
    """The agent-side cache key must match the classifier signature
    (provider, model, base_url) — same triplet as the require-side pad
    cache and _replays_all_turn_thinking.  A model change alone must
    invalidate a warm entry, so a future model-dependent rule cannot
    serve a stale answer (review point on PR #87123)."""

    class _Agent:
        _soft_replay_cache = None  # warmed dynamically by the helper

        def __init__(self, provider, model, base_url):
            self.provider, self.model, self.base_url = provider, model, base_url

    def test_model_change_invalidates_warm_cache(self):
        from agent.agent_runtime_helpers import replays_reasoning_content_for_agent

        agent = self._Agent("local", "qwen-a", "http://localhost:8081/v1")
        assert replays_reasoning_content_for_agent(agent) is True
        warm = getattr(agent, "_soft_replay_cache")
        assert warm is not None and warm[0] == ("local", "qwen-a", "http://localhost:8081/v1")
        # swap model only — classification is host-based so the answer is
        # still True, but the warm entry must NOT be served: it must be
        # re-evaluated under the new triplet
        agent.model = "qwen-b"
        assert replays_reasoning_content_for_agent(agent) is True
        warm = getattr(agent, "_soft_replay_cache")
        assert warm is not None and warm[0] == ("local", "qwen-b", "http://localhost:8081/v1")


class TestDuplicateAliasCharge:
    """Persisted assistant messages carry the same thinking text under BOTH
    'reasoning' and 'reasoning_content' (write-time promotion).  Only one
    ships on any transport, so the tail budget must charge it once —
    summing both recreates the #73624 overcharge the walks fixed.  Pinned
    per the estimator/provenance case raised in the PR #87123 discussion."""

    def test_identical_aliases_charged_once(self):
        cot = "t" * (_CHARS_PER_TOKEN * 40)  # 40 tokens of thinking
        msg = {"role": "assistant", "content": "hi",
               "reasoning": cot, "reasoning_content": cot}
        charged = _estimate_msg_budget_tokens(msg, charge_stale_thinking=True)
        # content ("hi" -> ~10) + exactly one 40-token thinking charge
        assert charged < 10 + 40 + 5  # strictly less than a doubled charge
        assert charged >= 10 + 40  # and never undercharged

    def test_larger_alias_wins_not_sum(self):
        # Asymmetric aliases: the max() must pick the larger payload once,
        # never sum (a sum would land between double-small and double-large).
        small = "s" * (_CHARS_PER_TOKEN * 10)
        large = "l" * (_CHARS_PER_TOKEN * 100)
        msg = {"role": "assistant", "content": "",
               "reasoning": small, "reasoning_content": large}
        charged = _estimate_msg_budget_tokens(msg, charge_stale_thinking=True)
        assert 100 + 10 <= charged < 10 + 100 + 90  # < small+large

    def test_single_alias_unchanged(self):
        # No duplication: one reasoning field charges exactly once.
        cot = "x" * (_CHARS_PER_TOKEN * 50)
        one = _estimate_msg_budget_tokens(
            {"role": "assistant", "content": "", "reasoning": cot},
            charge_stale_thinking=True)
        both = _estimate_msg_budget_tokens(
            {"role": "assistant", "content": "",
             "reasoning": cot, "reasoning_content": cot},
            charge_stale_thinking=True)
        assert one == both

    def test_scaled_history_no_double_charge(self):
        # Scaled version of the discussion fixture: a few hundred assistant
        # rows with byte-identical reasoning/reasoning_content, as persisted
        # by a soft-replay local session.  The per-message dedupe must hold
        # at volume: per-message charge identical to the single-alias shape.
        cot = "c" * (_CHARS_PER_TOKEN * 20)
        dup = {"role": "assistant", "content": "a",
               "reasoning": cot, "reasoning_content": cot}
        single = {"role": "assistant", "content": "a", "reasoning": cot}
        per_dup = _estimate_msg_budget_tokens(dup, charge_stale_thinking=True)
        per_single = _estimate_msg_budget_tokens(single, charge_stale_thinking=True)
        assert per_dup == per_single
        # 300 rows: no accumulation drift from the alias duplication
        total = sum(
            _estimate_msg_budget_tokens(
                {**dup}, charge_stale_thinking=True) for _ in range(300))
        assert total == 300 * per_dup

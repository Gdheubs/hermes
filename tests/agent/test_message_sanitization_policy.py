"""Tests for the single-owner call_id + reasoning_content policies.

Audit F4 consolidation: agent/message_sanitization.py now owns the
deterministic call_id synthesis, call_id coalescing/dedup, and the
reasoning_content strip-vs-repad provider-direction policy. These tests pin
the owner functions' behavior (including byte-exact hash outputs — they feed
prompt-cache keys) and verify the legacy entry points still delegate here.
"""

from types import SimpleNamespace

import pytest

from agent.message_sanitization import (
    apply_reasoning_content_policy,
    coalesce_tool_call_id,
    deterministic_call_id,
    matches_reasoning_echo_family,
    needs_reasoning_echo,
    reapply_reasoning_echo,
    reasoning_echo_family,
    replays_reasoning_content,
    uniquify_tool_call_ids,
)


# ---------------------------------------------------------------------------
# deterministic_call_id — byte-exact (prompt-cache keys)
# ---------------------------------------------------------------------------

class TestDeterministicCallId:
    def test_known_hash_outputs_are_stable(self):
        # Golden values: sha256(f"{fn}:{args}:{index}")[:12] prefixed call_.
        # Any change here invalidates users' prompt caches — do NOT update
        # these expectations without a migration plan.
        assert deterministic_call_id("terminal", '{"command":"ls"}', 0) == \
            "call_40ccaef54d02"
        assert deterministic_call_id("terminal", '{"command":"ls"}', 1) == \
            "call_567cb168d22d"
        assert deterministic_call_id("", "", 0) == "call_feda901d71ea"

    def test_deterministic_across_calls(self):
        a = deterministic_call_id("web_search", '{"q":"x"}', 3)
        b = deterministic_call_id("web_search", '{"q":"x"}', 3)
        assert a == b
        assert a.startswith("call_")
        assert len(a) == len("call_") + 12

    def test_index_disambiguates(self):
        assert deterministic_call_id("t", "{}", 0) != deterministic_call_id("t", "{}", 1)

    def test_surrogates_do_not_crash(self):
        out = deterministic_call_id("t", "bad \ud800 arg", 0)
        assert out.startswith("call_")

    def test_codex_adapter_wrapper_delegates(self):
        from agent.codex_responses_adapter import _deterministic_call_id
        assert _deterministic_call_id("terminal", '{"command":"ls"}', 0) == \
            deterministic_call_id("terminal", '{"command":"ls"}', 0)

    def test_run_agent_static_delegates(self):
        from run_agent import AIAgent
        assert AIAgent._deterministic_call_id("terminal", '{"command":"ls"}', 0) == \
            deterministic_call_id("terminal", '{"command":"ls"}', 0)


# ---------------------------------------------------------------------------
# coalesce_tool_call_id
# ---------------------------------------------------------------------------

class TestCoalesceToolCallId:
    def test_dict_call_id_wins_over_id(self):
        assert coalesce_tool_call_id({"call_id": "c", "id": "i"}) == "c"

    def test_dict_falls_back_to_id_and_strips(self):
        assert coalesce_tool_call_id({"id": " i "}) == "i"
        assert coalesce_tool_call_id({"call_id": "", "id": "i2"}) == "i2"

    def test_dict_empty(self):
        assert coalesce_tool_call_id({}) == ""

    def test_object_forms(self):
        assert coalesce_tool_call_id(SimpleNamespace(call_id="c", id="i")) == "c"
        assert coalesce_tool_call_id(SimpleNamespace(call_id=None, id=" i ")) == "i"
        assert coalesce_tool_call_id(SimpleNamespace(call_id=None, id=None)) == ""

    def test_run_agent_static_delegates(self):
        from run_agent import AIAgent
        tc = {"call_id": "c9", "id": "i9"}
        assert AIAgent._get_tool_call_id_static(tc) == coalesce_tool_call_id(tc)


# ---------------------------------------------------------------------------
# uniquify_tool_call_ids
# ---------------------------------------------------------------------------

class TestUniquifyToolCallIds:
    def test_no_duplicates_untouched(self):
        tcs = [
            {"id": "a", "function": {"name": "f", "arguments": "{}"}},
            {"id": "b", "function": {"name": "g", "arguments": "{}"}},
        ]
        out = uniquify_tool_call_ids(tcs)
        assert out is tcs
        assert [tc["id"] for tc in out] == ["a", "b"]

    def test_duplicate_gets_deterministic_suffix(self):
        tcs = [
            {"id": "x", "call_id": "x", "function": {"name": "f", "arguments": "{}"}},
            {"id": "x", "call_id": "x", "function": {"name": "g", "arguments": "{}"}},
            {"id": "x", "function": {"name": "h", "arguments": "{}"}},
        ]
        uniquify_tool_call_ids(tcs)
        assert tcs[0]["id"] == "x"
        assert tcs[1]["id"] == "x_d2"
        assert tcs[1]["call_id"] == "x_d2"
        assert tcs[2]["id"] == "x_d3"

    def test_composite_id_collides_on_call_half_and_preserves_item_half(self):
        tcs = [
            {"id": "call_y|fc_1", "function": {"name": "f", "arguments": "{}"}},
            {"id": "call_y|fc_2", "function": {"name": "g", "arguments": "{}"}},
        ]
        uniquify_tool_call_ids(tcs)
        assert tcs[0]["id"] == "call_y|fc_1"
        assert tcs[1]["id"] == "call_y_d2|fc_2"

    def test_suffix_collision_advances_counter(self):
        tcs = [
            {"id": "z", "function": {"name": "a", "arguments": "{}"}},
            {"id": "z_d2", "function": {"name": "b", "arguments": "{}"}},
            {"id": "z", "function": {"name": "c", "arguments": "{}"}},
        ]
        uniquify_tool_call_ids(tcs)
        assert tcs[2]["id"] == "z_d3"

    def test_blank_and_non_string_ids_skipped(self):
        tcs = [
            {"id": "", "function": {"name": "a", "arguments": "{}"}},
            {"id": None, "function": {"name": "b", "arguments": "{}"}},
            SimpleNamespace(id=42, call_id=None, function=None),
        ]
        uniquify_tool_call_ids(tcs)
        assert tcs[0]["id"] == ""
        assert tcs[1]["id"] is None

    def test_namespace_objects_mutated(self):
        tcs = [
            SimpleNamespace(id="n", call_id="n",
                            function=SimpleNamespace(name="a", arguments="{}")),
            SimpleNamespace(id="n", call_id="n",
                            function=SimpleNamespace(name="b", arguments="{}")),
        ]
        uniquify_tool_call_ids(tcs)
        assert tcs[1].id == "n_d2"
        assert tcs[1].call_id == "n_d2"

    def test_empty_and_none_inputs(self):
        assert uniquify_tool_call_ids([]) == []
        assert uniquify_tool_call_ids(None) is None


# ---------------------------------------------------------------------------
# reasoning_echo_family — the provider-direction table
# ---------------------------------------------------------------------------

class TestReasoningEchoFamily:
    @pytest.mark.parametrize("provider,model,base_url,family", [
        ("kimi-coding", None, "https://x", "kimi"),
        ("kimi-coding-cn", None, "https://x", "kimi"),
        ("custom", None, "https://api.kimi.com/v1", "kimi"),
        ("custom", None, "https://api.moonshot.ai/v1", "kimi"),
        ("custom", None, "https://api.moonshot.cn/v1", "kimi"),
        ("deepseek", "whatever", "https://x", "deepseek"),
        ("DeepSeek", "whatever", "https://x", "deepseek"),
        ("openrouter", "deepseek/deepseek-v3", "https://openrouter.ai", "deepseek"),
        ("custom", None, "https://api.deepseek.com", "deepseek"),
        ("xiaomi", None, "https://x", "mimo"),
        ("custom", "MiMo-7B", "https://x", "mimo"),
        ("custom", None, "https://api.xiaomimimo.com/v1", "mimo"),
        ("openai", "gpt-5", "https://api.openai.com/v1", None),
        ("mistral", "mistral-large", "https://api.mistral.ai/v1", None),
        (None, None, None, None),
    ])
    def test_table(self, provider, model, base_url, family):
        assert reasoning_echo_family(provider, model, base_url) == family
        assert needs_reasoning_echo(provider, model, base_url) is (family is not None)

    def test_kimi_provider_match_is_exact_not_lowered(self):
        # Original predicate compared the raw provider string against the
        # kimi-coding set; keep that semantic.
        assert matches_reasoning_echo_family("kimi", "KIMI-CODING", None, "https://x") is False

    def test_membership_is_per_family(self):
        # A deepseek model pointed at a kimi host matches both families
        # independently (the per-family predicates on AIAgent rely on this).
        assert matches_reasoning_echo_family(
            "kimi", "custom", "deepseek-chat", "https://api.kimi.com") is True
        assert matches_reasoning_echo_family(
            "deepseek", "custom", "deepseek-chat", "https://api.kimi.com") is True

    def test_unknown_family_raises(self):
        with pytest.raises(KeyError):
            matches_reasoning_echo_family("nope", "p", "m", "https://x")


# ---------------------------------------------------------------------------
# apply_reasoning_content_policy
# ---------------------------------------------------------------------------

class TestApplyReasoningContentPolicy:
    def test_non_assistant_untouched(self):
        api = {"role": "user", "content": "u", "reasoning_content": "keep"}
        apply_reasoning_content_policy(
            {"role": "user", "content": "u", "reasoning_content": "keep"}, api, True)
        assert api["reasoning_content"] == "keep"

    def test_require_side_preserves_existing(self):
        api = {"role": "assistant", "content": "x"}
        apply_reasoning_content_policy(
            {"role": "assistant", "content": "x", "reasoning_content": "thoughts"},
            api, True)
        assert api["reasoning_content"] == "thoughts"

    def test_require_side_upgrades_empty_string_to_space(self):
        api = {"role": "assistant", "content": "x", "reasoning_content": ""}
        apply_reasoning_content_policy(
            {"role": "assistant", "content": "x", "reasoning_content": ""}, api, True)
        assert api["reasoning_content"] == " "

    def test_strict_side_strips_existing(self):
        api = {"role": "assistant", "content": "x", "reasoning_content": " "}
        apply_reasoning_content_policy(
            {"role": "assistant", "content": "x", "reasoning_content": " "}, api, False)
        assert "reasoning_content" not in api

    def test_cross_provider_poisoned_history_pads_with_space(self):
        src = {"role": "assistant", "content": "x", "reasoning": "other-provider CoT",
               "tool_calls": [{"id": "c", "function": {"name": "t", "arguments": "{}"}}]}
        api = {"role": "assistant", "content": "x"}
        apply_reasoning_content_policy(src, api, True)
        assert api["reasoning_content"] == " "  # pad, never the foreign CoT

    def test_reasoning_promoted_only_on_require_side(self):
        src = {"role": "assistant", "content": "x", "reasoning": "healthy"}
        api = {"role": "assistant", "content": "x"}
        apply_reasoning_content_policy(src, api, True)
        assert api["reasoning_content"] == "healthy"
        api2 = {"role": "assistant", "content": "x", "reasoning_content": "stale"}
        apply_reasoning_content_policy(src, api2, False)
        assert "reasoning_content" not in api2

    def test_require_side_pads_bare_assistant_turn(self):
        api = {"role": "assistant", "content": "x"}
        apply_reasoning_content_policy({"role": "assistant", "content": "x"}, api, True)
        assert api["reasoning_content"] == " "

    def test_non_string_reasoning_content_removed(self):
        api = {"role": "assistant", "content": "x", "reasoning_content": None}
        apply_reasoning_content_policy(
            {"role": "assistant", "content": "x", "reasoning_content": None}, api, False)
        assert "reasoning_content" not in api


# ---------------------------------------------------------------------------
# reapply_reasoning_echo
# ---------------------------------------------------------------------------

class TestReapplyReasoningEcho:
    MSGS = [
        {"role": "assistant", "content": "a1", "reasoning_content": " "},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u"},
        {"role": "tool", "content": "t", "tool_call_id": "c"},
    ]

    def test_require_side_pads_missing_only(self):
        import copy
        msgs = copy.deepcopy(self.MSGS)
        assert reapply_reasoning_echo(msgs, True) == 1
        assert msgs[0]["reasoning_content"] == " "  # untouched
        assert msgs[1]["reasoning_content"] == " "  # padded
        assert "reasoning_content" not in msgs[2]

    def test_strict_side_strips_all(self):
        import copy
        msgs = copy.deepcopy(self.MSGS)
        assert reapply_reasoning_echo(msgs, False) == 1
        assert all("reasoning_content" not in m for m in msgs)

    def test_idempotent(self):
        import copy
        msgs = copy.deepcopy(self.MSGS)
        reapply_reasoning_echo(msgs, True)
        assert reapply_reasoning_echo(msgs, True) == 0
        reapply_reasoning_echo(msgs, False)
        assert reapply_reasoning_echo(msgs, False) == 0


# ---------------------------------------------------------------------------
# soft replay family — loopback endpoints that accept-and-attend
# reasoning_content without requiring it (local llama.cpp et al.)
# ---------------------------------------------------------------------------

class TestSoftReplayClassification:
    @pytest.mark.parametrize("base_url", [
        "http://localhost:8081/v1",
        "http://127.0.0.1:8080/v1",
        "http://127.8.9.10:8080/v1",           # any 127.0.0.0/8
        "http://[::1]:8080/v1",
        "http://[::ffff:127.0.0.1]:8080/v1",   # IPv4-mapped loopback
        "http://0.0.0.0:8080/v1",              # unspecified bind form
        "localhost:8081",
    ])
    def test_loopback_hosts_match(self, base_url):
        assert replays_reasoning_content("local", "some-model", base_url) is True

    @pytest.mark.parametrize("base_url", [
        "https://api.openai.com/v1",
        "https://api.mistral.ai/v1",
        "https://qwen.example.com/v1",        # model-name-ish host: no match
        "http://localhost.evil.example/v1",   # loopback as subdomain: no match
        "https://my-localhost.demo/v1",
        "http://192.168.1.5:8080/v1",         # LAN address: not loopback
        None,
    ])
    def test_non_loopback_no_match(self, base_url):
        assert replays_reasoning_content("openai", "qwen3.8-27b", base_url) is False

    def test_disjoint_from_require_side(self):
        # A require-side host never matches the soft table and vice versa.
        assert replays_reasoning_content(
            "custom", None, "https://api.deepseek.com") is False
        assert reasoning_echo_family(
            "local", "qwen3.8", "http://localhost:8081/v1") is None


class TestApplyPolicySoftReplay:
    def test_soft_preserves_existing_reasoning(self):
        api = {"role": "assistant", "content": "x"}
        apply_reasoning_content_policy(
            {"role": "assistant", "content": "x", "reasoning_content": "thoughts"},
            api, needs_thinking_pad=False, soft_replay=True)
        assert api["reasoning_content"] == "thoughts"

    def test_soft_never_fabricates_pad_on_bare_turn(self):
        # THE soft-family contract: no field -> no field.  A lone assistant
        # turn with no reasoning must NOT gain a " " pad (branch 4 stays
        # require-side-only).
        api = {"role": "assistant", "content": "x"}
        apply_reasoning_content_policy(
            {"role": "assistant", "content": "x"}, api,
            needs_thinking_pad=False, soft_replay=True)
        assert "reasoning_content" not in api

    def test_soft_drops_empty_string(self):
        # "" is upgraded to " " only on require-side; soft omits.
        api = {"role": "assistant", "content": "x", "reasoning_content": ""}
        apply_reasoning_content_policy(
            {"role": "assistant", "content": "x", "reasoning_content": ""}, api,
            needs_thinking_pad=False, soft_replay=True)
        assert "reasoning_content" not in api

    def test_soft_does_not_pad_foreign_cot(self):
        # Branch 2 (foreign CoT -> " " pad) is require-side-only.
        src = {"role": "assistant", "content": "x", "reasoning": "other CoT",
               "tool_calls": [{"id": "c", "function": {"name": "t",
                                                       "arguments": "{}"}}]}
        api = {"role": "assistant", "content": "x"}
        apply_reasoning_content_policy(
            src, api, needs_thinking_pad=False, soft_replay=True)
        assert "reasoning_content" not in api

    def test_soft_promotes_internal_reasoning_field(self):
        # Same-provider history where thinking lives under 'reasoning'
        # (write-time promotion missed it): promote, don't pad.
        src = {"role": "assistant", "content": "x", "reasoning": "my thoughts"}
        api = {"role": "assistant", "content": "x"}
        apply_reasoning_content_policy(
            src, api, needs_thinking_pad=False, soft_replay=True)
        assert api["reasoning_content"] == "my thoughts"

    def test_strict_default_unchanged(self):
        # needs_thinking_pad=False, soft_replay=False -> historical strip.
        api = {"role": "assistant", "content": "x", "reasoning_content": "t"}
        apply_reasoning_content_policy(
            {"role": "assistant", "content": "x", "reasoning_content": "t"},
            api, needs_thinking_pad=False)
        assert "reasoning_content" not in api


class TestReapplyReasoningEchoSoft:
    MSGS = [
        {"role": "assistant", "content": "a1", "reasoning_content": "genuine CoT"},
        {"role": "assistant", "content": "a2", "reasoning_content": " "},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u"},
    ]

    def test_soft_keeps_genuine_drops_pads(self):
        import copy
        msgs = copy.deepcopy(self.MSGS)
        assert reapply_reasoning_echo(msgs, False, soft_replay=True) == 1
        assert msgs[0]["reasoning_content"] == "genuine CoT"
        assert "reasoning_content" not in msgs[1]  # pad dropped
        assert "reasoning_content" not in msgs[2]  # nothing added

    def test_soft_idempotent(self):
        import copy
        msgs = copy.deepcopy(self.MSGS)
        reapply_reasoning_echo(msgs, False, soft_replay=True)
        assert reapply_reasoning_echo(msgs, False, soft_replay=True) == 0


class TestSoftToStrictFallbackNoLeak:
    """The local→strict provider fallback boundary (PR #87123 discussion):
    history persisted under a soft-replay loopback primary carries
    reasoning_content on every assistant turn.  When the request falls
    back to a strict provider (Mistral/Cerebras/Groq reject the field
    with 400/422), reapply_reasoning_echo must strip every trace —
    genuine preserved reasoning included — so nothing leaks to the wire."""

    # A soft-replay session's history: genuine CoT preserved verbatim.
    SOFT_MSGS = [
        {"role": "assistant", "content": "a1",
         "reasoning_content": "genuine chain of thought"},
        {"role": "assistant", "content": "a2", "reasoning_content": " "},
        {"role": "assistant", "content": "a3"},  # bare turn, nothing to strip
        {"role": "user", "content": "u"},
    ]

    def test_fallback_strips_genuine_reasoning_too(self):
        import copy
        msgs = copy.deepcopy(self.SOFT_MSGS)
        changed = reapply_reasoning_echo(msgs, needs_thinking_pad=False)
        assert changed == 2
        assert all("reasoning_content" not in m for m in msgs)

    def test_fallback_idempotent(self):
        import copy
        msgs = copy.deepcopy(self.SOFT_MSGS)
        reapply_reasoning_echo(msgs, False)
        assert reapply_reasoning_echo(msgs, False) == 0

    def test_apply_policy_strict_strips_soft_shaped_history(self):
        # The rebuild path (apply_reasoning_content_policy) on a message
        # shaped by a soft primary: strict target must not carry the field.
        api = {"role": "assistant", "content": "x",
               "reasoning_content": "genuine CoT"}
        apply_reasoning_content_policy(
            {"role": "assistant", "content": "x",
             "reasoning_content": "genuine CoT"},
            api, needs_thinking_pad=False, soft_replay=False)
        assert "reasoning_content" not in api

"""Pure tool-call guardrail primitive tests."""

import json

from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolCallSignature,
    canonical_tool_args,
    classify_tool_failure,
)


def test_tool_call_signature_hashes_canonical_nested_unicode_args_without_exposing_raw_args():
    args_a = {
        "z": [{"β": "☤", "a": 1}],
        "a": {"y": 2, "x": "secret-token-value"},
    }
    args_b = {
        "a": {"x": "secret-token-value", "y": 2},
        "z": [{"a": 1, "β": "☤"}],
    }

    assert canonical_tool_args(args_a) == canonical_tool_args(args_b)
    sig_a = ToolCallSignature.from_call("web_search", args_a)
    sig_b = ToolCallSignature.from_call("web_search", args_b)

    assert sig_a == sig_b
    assert len(sig_a.args_hash) == 64
    metadata = sig_a.to_metadata()
    assert metadata == {"tool_name": "web_search", "args_hash": sig_a.args_hash}
    assert "secret-token-value" not in json.dumps(metadata)
    assert "☤" not in json.dumps(metadata)




def test_config_parses_nested_warn_and_hard_stop_thresholds():
    cfg = ToolCallGuardrailConfig.from_mapping(
        {
            "warnings_enabled": False,
            "hard_stop_enabled": True,
            "warn_after": {
                "exact_failure": 3,
                "same_tool_failure": 4,
                "idempotent_no_progress": 5,
            },
            "hard_stop_after": {
                "exact_failure": 6,
                "same_tool_failure": 7,
                "idempotent_no_progress": 8,
            },
        }
    )

    assert cfg.warnings_enabled is False
    assert cfg.hard_stop_enabled is True
    assert cfg.exact_failure_warn_after == 3
    assert cfg.same_tool_failure_warn_after == 4
    assert cfg.no_progress_warn_after == 5
    assert cfg.exact_failure_block_after == 6
    assert cfg.same_tool_failure_halt_after == 7
    assert cfg.no_progress_block_after == 8


def test_default_repeated_identical_failed_call_warns_without_blocking():
    controller = ToolCallGuardrailController()
    args = {"query": "same"}

    decisions = []
    for _ in range(5):
        assert controller.before_call("web_search", args).action == "allow"
        decisions.append(
            controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
        )

    assert decisions[0].action == "allow"
    assert [d.action for d in decisions[1:]] == ["warn", "warn", "warn", "warn"]
    assert {d.code for d in decisions[1:]} == {"repeated_exact_failure_warning"}
    assert controller.before_call("web_search", args).action == "allow"
    assert controller.halt_decision is None


def test_hard_stop_enabled_blocks_repeated_exact_failure_before_next_execution():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=2,
            exact_failure_block_after=2,
            same_tool_failure_halt_after=99,
        )
    )
    args = {"query": "same"}

    assert controller.before_call("web_search", args).action == "allow"
    first = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert first.action == "allow"

    assert controller.before_call("web_search", args).action == "allow"
    second = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert second.action == "warn"
    assert second.code == "repeated_exact_failure_warning"

    blocked = controller.before_call("web_search", args)
    assert blocked.action == "block"
    assert blocked.code == "repeated_exact_failure_block"
    assert blocked.count == 2














def test_successful_identical_signatures_block_even_when_result_hash_changes():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            no_progress_warn_after=2,
            no_progress_block_after=3,
        )
    )
    args = {"todos": [{"id": "loop-fix", "content": "same", "status": "in_progress"}]}

    assert controller.before_call("todo", args).action == "allow"
    assert controller.after_call(
        "todo",
        args,
        '{"todos":[{"id":"loop-fix","content":"same","status":"in_progress"}]}',
        failed=False,
    ).action == "allow"

    assert controller.before_call("todo", args).action == "allow"
    second = controller.after_call(
        "todo",
        args,
        "[Duplicate tool output — same content as a more recent call]",
        failed=False,
    )
    assert second.action == "warn"
    assert second.code == "no_progress_warning"
    assert second.count == 2

    assert controller.before_call("todo", args).action == "allow"
    third = controller.after_call(
        "todo",
        args,
        '{"status":"unchanged","content_returned":false}',
        failed=False,
    )
    assert third.action == "warn"
    assert third.count == 3

    blocked = controller.before_call("todo", args)
    assert blocked.action == "block"
    assert blocked.code == "no_progress_block"
    assert blocked.count == 3






# ── Per-turn runaway-loop caps (Claude Code v2.1.212, Week 29) ──────────────

from agent.tool_guardrails import LoopCapConfig  # noqa: E402






def test_loop_cap_zero_disables_and_junk_falls_back():
    # 0 is a legitimate "unlimited" value; negatives / junk fall back to default.
    assert LoopCapConfig.from_mapping({"max_web_searches": 0}).max_web_searches == 0
    assert LoopCapConfig.from_mapping({"max_web_searches": -5}).max_web_searches == 50
    assert LoopCapConfig.from_mapping({"max_subagents": "nope"}).max_subagents == 50


def test_web_search_cap_blocks_after_limit_regardless_of_hard_stop():
    # Loop caps fire even with hard_stop_enabled=False (the per-turn loop
    # detector's flag). Each distinct query avoids the loop detector so we know
    # the block came from the loop cap, not exact-failure repetition.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=False,
            loop_caps=LoopCapConfig(max_web_searches=3),
        )
    )
    for i in range(3):
        assert controller.before_call("web_search", {"query": f"q{i}"}).action == "allow"
    decision = controller.before_call("web_search", {"query": "q4"})
    assert decision.action == "block"
    assert decision.code == "loop_web_search_cap"
    assert decision.should_halt is True












def test_no_progress_streak_survives_turn_boundary_until_real_work_lands():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            no_progress_warn_after=2,
            no_progress_block_after=3,
        )
    )
    args = {"todos": [{"id": "a", "content": "same", "status": "in_progress"}]}

    for _ in range(3):
        assert controller.before_call("todo", args).action == "allow"
        controller.after_call("todo", args, "same-list", failed=False)
        # Context compaction / a new user message starts a fresh turn.
        controller.reset_for_turn()

    blocked = controller.before_call("todo", args)
    assert blocked.action == "block"
    assert blocked.code == "no_progress_block"

    # A real mutating write moves the world and clears the stale streak.
    controller.after_call(
        "write_file",
        {"path": "/tmp/x", "content": "x"},
        '{"path": "/tmp/x", "bytes_written": 1}',
        failed=False,
    )
    assert controller.before_call("todo", args).action == "allow"


# ── Arg jitter + duplicate-stub hashing ────────────────────────────────────
#
# Signature counting (bcb9e0374) closes the result-hash hole, but the signature
# is still derived from canonical args -- so jittering an irrelevant field
# mints a fresh signature and restarts the streak at 1. Observed live on
# ``todo``: alternating ``merge`` while re-asserting a byte-identical list.


_DUPLICATE_STUB = "[Duplicate tool output — same content as a more recent call]"


def test_cosmetic_todo_jitter_does_not_mint_a_fresh_signature():
    items = [
        {"id": "1", "content": "convert skill", "status": "completed"},
        {"id": "2", "content": "open PR", "status": "pending"},
    ]
    a = {"todos": items, "merge": True}
    b = {"todos": list(reversed(items)), "merge": False}

    assert canonical_tool_args(a) == canonical_tool_args(b)
    assert ToolCallSignature.from_call("todo", a) == ToolCallSignature.from_call("todo", b)


def test_jittered_todo_payloads_still_reach_the_no_progress_block():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            no_progress_warn_after=2,
            no_progress_block_after=3,
        )
    )
    items = [{"id": "1", "content": "convert skill", "status": "pending"}]

    for i in range(3):
        # Flip an irrelevant field every call; the asserted end state is identical.
        args = {"todos": items, "merge": bool(i % 2)}
        assert controller.before_call("todo", args).action == "allow"
        controller.after_call("todo", args, "same-list", failed=False)

    blocked = controller.before_call("todo", {"todos": items, "merge": True})
    assert blocked.action == "block"
    assert blocked.code == "no_progress_block"


def test_non_declarative_args_are_untouched_by_normalization():
    # Normalization must not collapse genuinely different calls.
    a = {"path": "/tmp/a", "content": "x"}
    b = {"path": "/tmp/b", "content": "x"}
    assert canonical_tool_args(a) != canonical_tool_args(b)
    # A todos payload that is not a list is passed through unchanged.
    assert canonical_tool_args({"todos": "not-a-list"}) == canonical_tool_args({"todos": "not-a-list"})


def test_duplicate_stub_hashes_to_a_stable_sentinel():
    from agent.tool_guardrails import _result_hash

    assert _result_hash(_DUPLICATE_STUB) == _result_hash(f"  {_DUPLICATE_STUB} trailing text")
    assert _result_hash(_DUPLICATE_STUB) != _result_hash('{"todos":[]}')

"""Regression coverage for the Ares Context Governor host binding.

The configured external engine must be discoverable.  A silent fallback to
Hermes' built-in LLM ContextCompressor changes the Ares deterministic-first,
hash-preserving contract and is therefore a conformance failure.
"""

import json
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.context_engine import ContextEngine
from hermes_state import SessionDB
from plugins.context_engine import load_context_engine
from plugins.context_engine._context_governor import ContextGovernorEngine
from tools.todo_tool import TODO_INJECTION_HEADER


def _bind_fixture(engine):
    """Fixture-only held-descriptor authority; no path/key material exists."""
    binding = SimpleNamespace(
        command_args=lambda: [
            "--governed-key-fd",
            "71",
            "--governed-snapshot-fd",
            "72",
        ],
        close=lambda: None,
    )
    engine._key_binding = binding
    engine._certified_store_args = binding.command_args


def test_ares_governor_is_discoverable_as_a_context_engine():
    """Configured Ares ownership must not silently resolve to built-in LLM compression."""
    engine = load_context_engine("ri-context-governor")

    assert engine is not None
    assert isinstance(engine, ContextEngine)
    assert engine.name == "ri-context-governor"
    # Discovery is intentionally separate from activation. A default local
    # install without a certified binary/key pair must not be selected.
    assert engine.is_available() is False


def test_ares_governor_rejects_an_arbitrary_configured_key_path():
    with (
        TemporaryDirectory() as store_dir,
        patch(
            "hermes_cli.config.load_config",
            return_value={
                "context": {
                    "governor": {"receipt_hmac_key_path": "/tmp/not-canonical.key"}
                }
            },
        ),
    ):
        engine = ContextGovernorEngine(
            binary="/tmp/context-governor", store_dir=store_dir
        )
    with pytest.raises(Exception, match="ConfigurationPathOutsideCanonicalState"):
        engine.probe_activation()


def _valid_llm_summary(body: str = "checkpoint") -> str:
    return (
        "=== ACTIVE TASK ===\nfinal\n\n"
        "=== ACCEPTANCE GATES ===\nNone\n\n"
        "=== EXACT FALLBACK REFS ===\nNone\n\n"
        "=== SUMMARY LOSSES ===\nNone\n\n"
        f"=== PRIOR CONTEXT SUMMARY ===\n{body}"
    )


def _checkpoint_engine(
    *,
    target_tokens: int,
    llm_output: str,
    checkpoint_strategy: str = "ineffective_only",
):
    """Return a deterministic-core fixture with a saturated 95/100 receipt."""
    config = {
        "context": {
            "governor": {
                "summary_mode": "llm",
                "checkpoint_strategy": checkpoint_strategy,
            }
        }
    }
    with patch("hermes_cli.config.load_config", return_value=config):
        engine = ContextGovernorEngine(binary="/tmp/context-governor")
    _bind_fixture(engine)
    engine._target_tokens = lambda _current: target_tokens
    engine._store_response = MagicMock()
    llm = MagicMock(return_value=llm_output)
    engine._call_summary_llm = llm

    def run_json(args, payload):
        if args[:3] == ["compact-v2", "--dir", str(engine.store_dir)]:
            return {
                "receipt": {
                    "schema": "ContextCompactionReceiptV2",
                    "receipt_id": "ctxr_checkpoint",
                    "original_transcript_blake3": "a" * 64,
                    "compacted_transcript_blake3": "b" * 64,
                    "original_approx_tokens": 100,
                    "compacted_approx_tokens": 95,
                    "token_savings_estimate": 5,
                    "generation": 1,
                    "covered_original_sources": [],
                },
                "allocation_plan": {
                    "summarized_item_ids": ["ctxi_old"],
                    "items": [{"item_id": "ctxi_old", "start_index": 0}],
                },
                "compacted_messages": [
                    {
                        "role": "assistant",
                        "name": "context_governor",
                        "content": "deterministic extractive summary",
                    },
                    {"role": "user", "content": "final"},
                ],
            }
        if args == ["render-prompt-v2"]:
            return {"system": "system", "user": "prompt"}
        if args == ["boundary-audit"]:
            return {"safe_to_reinject": True}
        if args == ["finalize-v2"]:
            response = payload
            tokens = sum(
                max(1, len(str(message.get("content", ""))) // 4)
                for message in response["compacted_messages"]
            )
            response["receipt"]["compacted_approx_tokens"] = tokens
            response["receipt"]["token_savings_estimate"] = 100 - tokens
            return response
        raise AssertionError(f"unexpected command: {args}")

    engine._run_json = run_json
    return engine, llm


def test_below_threshold_does_not_schedule_governor_compaction():
    engine = ContextGovernorEngine(binary="/tmp/context-governor")
    engine.update_model("fixture", context_length=1_000)

    assert engine.should_compress(499) is False


def test_deterministic_result_within_target_never_calls_llm_checkpoint():
    """A fixed point alone is insufficient when the deterministic budget already fits."""
    engine, llm = _checkpoint_engine(target_tokens=95, llm_output=_valid_llm_summary())

    compacted = engine.compress(
        [{"role": "assistant", "content": "old"}, {"role": "user", "content": "final"}],
        current_tokens=100,
    )

    assert llm.call_count == 0
    assert compacted[0]["content"] == "deterministic extractive summary"


def test_deterministic_saturation_above_target_invokes_one_llm_checkpoint():
    engine, llm = _checkpoint_engine(target_tokens=90, llm_output=_valid_llm_summary())

    compacted = engine.compress(
        [{"role": "assistant", "content": "old"}, {"role": "user", "content": "final"}],
        current_tokens=100,
    )

    assert llm.call_count == 1
    assert compacted[0]["content"] == _valid_llm_summary()


def test_oversized_llm_checkpoint_reverts_to_deterministic_projection():
    engine, llm = _checkpoint_engine(
        target_tokens=90,
        llm_output=_valid_llm_summary("oversized " * 2_000),
    )

    compacted = engine.compress(
        [{"role": "assistant", "content": "old"}, {"role": "user", "content": "final"}],
        current_tokens=100,
    )

    assert llm.call_count == 1
    assert compacted[0]["content"] == "deterministic extractive summary"
    assert engine.last_warning and "exceeded target" in engine.last_warning


@pytest.mark.parametrize("llm_output", ["", "not the required summary schema"])
def test_empty_or_malformed_llm_checkpoint_keeps_deterministic_projection(llm_output):
    engine, llm = _checkpoint_engine(target_tokens=90, llm_output=llm_output)

    compacted = engine.compress(
        [{"role": "assistant", "content": "old"}, {"role": "user", "content": "final"}],
        current_tokens=100,
    )

    assert llm.call_count == 1
    assert compacted[0]["content"] == "deterministic extractive summary"
    assert (
        engine.last_compaction_metrics["integrity_result"] == "store_admission_verified"
    )


def test_summary_provider_timeout_keeps_deterministic_projection_and_receipt():
    engine, llm = _checkpoint_engine(target_tokens=90, llm_output=_valid_llm_summary())
    llm.side_effect = TimeoutError("synthetic summary deadline")

    compacted = engine.compress(
        [{"role": "assistant", "content": "old"}, {"role": "user", "content": "final"}],
        current_tokens=100,
    )

    assert llm.call_count == 1
    assert compacted[0]["content"] == "deterministic extractive summary"
    metrics = engine.last_compaction_metrics
    assert metrics["summary_id"] == "ctxr_checkpoint"
    assert metrics["llm_call_reason"].endswith(":fallback_extract")
    assert metrics["integrity_result"] == "store_admission_verified"


def test_governor_config_reaches_rust_policy_owner():
    config = {
        "context": {
            "governor": {
                "unsafe_summary_policy": "fail_closed",
                "checkpoint_strategy": "after_n:3",
                "max_checkpoints": 4,
                "token_budget": 1234,
                "protect_first_n": 2,
                "protect_last_n": 5,
            }
        }
    }
    with patch("hermes_cli.config.load_config", return_value=config):
        engine = ContextGovernorEngine(binary="/tmp/context-governor")

    assert engine._policy["unsafe_summary_policy"] == "fail_closed"
    assert engine._checkpoint_strategy_json() == {"after_n": 3}
    assert engine._max_checkpoints() == 4
    assert engine._target_tokens(99_999) == 1234
    assert engine.protect_first_n == 2
    assert engine.protect_last_n == 5


def test_default_provenance_budget_covers_a_tool_heavy_recursive_suffix():
    """A normal 256-message suffix must fit before checkpoint evaluation."""
    with patch("hermes_cli.config.load_config", return_value={}):
        engine = ContextGovernorEngine(binary="/tmp/context-governor")

    conservative_reference_bytes = 1024

    assert engine._policy["max_provenance_bytes"] >= (
        256 * conservative_reference_bytes
    )


def test_after_n_checkpoint_policy_calls_llm_only_on_intended_boundary():
    engine, llm = _checkpoint_engine(
        target_tokens=90,
        llm_output=_valid_llm_summary(),
        checkpoint_strategy="after_n:2",
    )
    messages = [
        {"role": "assistant", "content": "old"},
        {"role": "user", "content": "final"},
    ]

    first = engine.compress(messages, current_tokens=100)
    second = engine.compress(messages, current_tokens=100)

    assert first[0]["content"] == "deterministic extractive summary"
    assert second[0]["content"] == _valid_llm_summary()
    assert llm.call_count == 1
    assert engine.compression_count == 2
    assert engine._llm_checkpoint_count == 1


def test_compaction_metrics_expose_deterministic_llm_and_receipt_boundaries():
    engine, _llm = _checkpoint_engine(
        target_tokens=90,
        llm_output=_valid_llm_summary(),
    )

    engine.compress(
        [{"role": "assistant", "content": "old"}, {"role": "user", "content": "final"}],
        current_tokens=100,
    )

    metrics = engine.get_status()["last_compaction_metrics"]
    assert metrics["passes"] == 2
    assert metrics["llm_call"] is True
    assert metrics["llm_call_reason"].endswith(":applied")
    assert metrics["llm_latency_ms"] is not None
    assert metrics["summary_id"] == "ctxr_checkpoint"
    assert metrics["deterministic_reduction_tokens"] is not None
    assert metrics["after_tokens"] is not None
    assert metrics["integrity_result"] == "store_admission_verified"


def test_fixed_point_without_new_summary_items_still_calls_secondary_llm():
    """A fixed point enhances the governed projection, not only newly omitted turns."""
    engine, llm = _checkpoint_engine(
        target_tokens=90,
        llm_output=_valid_llm_summary("fixed-point checkpoint"),
    )
    response = {
        "receipt": {
            "original_approx_tokens": 100,
            "covered_original_sources": [],
        },
        "allocation_plan": {"summarized_item_ids": [], "items": []},
    }
    engine._run_json = MagicMock(
        side_effect=[
            {"system": "system", "user": "prompt"},
            {"safe_to_reinject": True},
        ]
    )
    engine.last_compaction_metrics = {"llm_call": False}

    compacted = engine._enhance_with_llm_summary(
        [
            {
                "role": "assistant",
                "name": "context_governor",
                "content": "deterministic extractive summary",
            },
            {"role": "user", "content": "final"},
        ],
        [{"role": "user", "content": "final"}],
        response,
        None,
    )

    assert llm.call_count == 1
    assert engine.last_compaction_metrics["llm_call"] is True
    assert compacted[0]["content"] == _valid_llm_summary("fixed-point checkpoint")


def test_missing_summary_projection_does_not_claim_an_llm_call():
    engine, llm = _checkpoint_engine(
        target_tokens=90,
        llm_output=_valid_llm_summary(),
    )
    engine.last_compaction_metrics = {
        "llm_call": False,
        "llm_call_reason": "checkpoint_ready",
    }

    compacted = engine._enhance_with_llm_summary(
        [{"role": "user", "content": "final"}],
        [{"role": "user", "content": "final"}],
        {"receipt": {}, "allocation_plan": {}},
        None,
    )

    assert compacted == [{"role": "user", "content": "final"}]
    assert llm.call_count == 0
    assert engine.last_compaction_metrics == {
        "llm_call": False,
        "llm_call_reason": "summary_projection_unavailable",
    }


def test_host_todo_snapshot_does_not_block_recursive_llm_checkpoint():
    """Host-only todo state must not invalidate the receipt-backed parent prefix."""
    config = {
        "context": {
            "governor": {
                "summary_mode": "llm",
                "checkpoint_strategy": "after_n:2",
            }
        }
    }
    with patch("hermes_cli.config.load_config", return_value=config):
        engine = ContextGovernorEngine(binary="/tmp/context-governor")
    _bind_fixture(engine)
    engine._target_tokens = lambda current_tokens: 90
    engine._store_response = MagicMock(return_value={"verified": True})
    llm = MagicMock(return_value=_valid_llm_summary("recursive checkpoint"))
    engine._call_summary_llm = llm
    parent_projection = None
    generation = 0

    def run_json(args, payload):
        nonlocal generation, parent_projection
        if args[:3] == ["compact-v2", "--dir", str(engine.store_dir)]:
            incoming = payload["messages"]
            if parent_projection is not None and incoming[: len(parent_projection)] != parent_projection:
                raise RuntimeError(
                    "parent compacted transcript is not the exact child-input prefix"
                )
            generation += 1
            return {
                "receipt": {
                    "schema": "ContextCompactionReceiptV2",
                    "receipt_id": f"ctxr_checkpoint_{generation}",
                    "original_transcript_blake3": "a" * 64,
                    "compacted_transcript_blake3": "b" * 64,
                    "original_approx_tokens": 100,
                    "compacted_approx_tokens": 95,
                    "token_savings_estimate": 5,
                    "generation": generation,
                    "covered_original_sources": [],
                },
                "allocation_plan": {
                    "summarized_item_ids": ["ctxi_old"],
                    "items": [{"item_id": "ctxi_old", "start_index": 0}],
                },
                "compacted_messages": [
                    {
                        "role": "assistant",
                        "name": "context_governor",
                        "content": "deterministic extractive summary",
                    },
                    {"role": "user", "content": "final"},
                ],
            }
        if args == ["render-prompt-v2"]:
            return {"system": "system", "user": "prompt"}
        if args == ["boundary-audit"]:
            return {"safe_to_reinject": True}
        if args == ["finalize-v2"]:
            response = payload
            parent_projection = response["compacted_messages"]
            tokens = sum(
                max(1, len(str(message.get("content", ""))) // 4)
                for message in parent_projection
            )
            response["receipt"]["compacted_approx_tokens"] = tokens
            response["receipt"]["token_savings_estimate"] = 100 - tokens
            return response
        raise AssertionError(f"unexpected command: {args}")

    engine._run_json = run_json
    first = engine.compress(
        [{"role": "assistant", "content": "old"}, {"role": "user", "content": "final"}],
        current_tokens=100,
    )
    first[-1]["content"] += f"\n\n{TODO_INJECTION_HEADER}\n- [>] reproduce"

    second = engine.compress(first, current_tokens=100)

    assert llm.call_count == 1
    assert second[0]["content"] == _valid_llm_summary("recursive checkpoint")
    assert engine.last_error is None


def test_governor_projection_roundtrips_through_session_store(tmp_path):
    """Receipt projection fields must survive Hermes' durable in-place rewrite."""
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "governor-roundtrip"
    db.create_session(session_id, source="cli")
    engine = ContextGovernorEngine(binary="/tmp/context-governor")
    governor_messages = [
        {
            "role": "tool",
            "id": "call_123",
            "name": "skill_view",
            "content": "tool result",
            "metadata": {"tool_call_id": "call_123"},
        },
        {
            "role": "assistant",
            "id": "summary_random_id",
            "name": "context_governor",
            "content": "deterministic extractive summary",
        },
    ]
    host_messages = [
        engine._message_from_governor(message) for message in governor_messages
    ]

    db.archive_and_compact(session_id, host_messages)
    reloaded = db.get_messages_as_conversation(session_id)
    roundtripped = [
        engine._message_to_governor(message, index)
        for index, message in enumerate(reloaded)
    ]

    assert roundtripped == [
        governor_messages[0],
        {
            "role": "assistant",
            "name": "context_governor",
            "content": "deterministic extractive summary",
        },
    ]


def test_legacy_receipt_prefix_rehydrates_only_lost_durable_fields(tmp_path):
    """A pre-fix archive can resume with names absent but no other drift."""
    session_id = "legacy-prefix"
    engine = ContextGovernorEngine(binary="/tmp/context-governor", store_dir=tmp_path)
    engine.session_id = session_id
    receipt_prefix = [
        {
            "role": "tool",
            "id": "call_123",
            "name": "skill_view",
            "content": "tool result",
            "metadata": {"tool_call_id": "call_123"},
        },
        {
            "role": "assistant",
            "id": "summary_123",
            "name": "context_governor",
            "content": "deterministic extractive summary",
        },
    ]
    (tmp_path / "ctxr_legacy.json").write_text(
        json.dumps(
            {
                "receipt": {
                    "session_id": session_id,
                    "generation": 1,
                    "created_utc": "2026-08-16T00:00:00Z",
                },
                "compacted_messages": receipt_prefix,
            }
        ),
        encoding="utf-8",
    )

    # Simulate the historical SessionDB projection: tool-call id survives,
    # while provider-facing names and the assistant summary id do not.
    resumed = [
        {
            "role": "tool",
            "content": "tool result",
            "tool_call_id": "call_123",
        },
        {"role": "assistant", "content": "deterministic extractive summary"},
        {"role": "user", "content": "new work"},
    ]
    incoming = [
        engine._message_to_governor(message, index)
        for index, message in enumerate(resumed)
    ]

    assert engine._rehydrate_legacy_parent_prefix(incoming) == receipt_prefix + [
        {"role": "user", "content": "new work"}
    ]

    # Same legacy field loss is not sufficient if content has drifted.
    incoming[0]["content"] = "different result"
    assert engine._rehydrate_legacy_parent_prefix(incoming) == incoming

"""Behavioral conformance tests for the durable execution read contract."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli.execution_contract import (
    CONTRACT_VERSION,
    ContractConflictError,
    ContractCursorGoneError,
    ContractDataError,
    ContractForbiddenError,
    ContractValidationError,
    ExecutionContractStore,
    UnsupportedContractVersionError,
    canonical_digest,
    contract_capabilities,
    contract_schema,
)


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parents[1] / "fixtures" / "execution_contract"


def _digest(label: str) -> str:
    return canonical_digest({"fixture": label})


def _store(tmp_path, *, profile="default", runtime="runtime-a"):
    return ExecutionContractStore(
        database_path=tmp_path / profile / "execution_contract.sqlite3",
        profile_name=profile,
        runtime_instance_id=runtime,
    )


def _bound_execution(store: ExecutionContractStore, *, suffix="one"):
    return store.create_execution(
        lifecycle="queued",
        source_run_id=f"run-{suffix}",
        work_ref=f"work:{suffix}",
        proposal_ref=f"proposal:{suffix}",
        effect_id=f"effect:{suffix}",
        now=NOW,
    )


def _resolved_decision(store: ExecutionContractStore, execution: dict, *, suffix="one"):
    decision = store.create_decision(
        execution_id=execution["execution_id"],
        effect_id=execution["effect_id"],
        proposal_ref=execution["proposal_ref"],
        request_digest=_digest(f"request-{suffix}"),
        candidate_digest=_digest(f"candidate-{suffix}"),
        policy_digest=_digest(f"policy-{suffix}"),
        allowed_choices=["once", "deny"],
        expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )
    return store.resolve_decision(
        decision["decision_id"],
        choice="once",
        resolution_evidence_digest=_digest(f"resolution-{suffix}"),
        now=NOW + timedelta(seconds=1),
    )


def _record_evidence(
    store: ExecutionContractStore,
    execution: dict,
    *,
    outcome="succeeded",
    decision_id=None,
    suffix="one",
    reconciliation_ref=None,
):
    return store.record_effect_evidence(
        execution_id=execution["execution_id"],
        effect_id=execution["effect_id"],
        outcome=outcome,
        subject_digest=_digest(f"subject-{suffix}"),
        evidence_digest=_digest(f"evidence-{suffix}"),
        result_digest=_digest(f"result-{suffix}"),
        decision_id=decision_id,
        reconciliation_ref=reconciliation_ref,
        now=NOW + timedelta(seconds=2),
    )


def test_absent_read_is_side_effect_free_then_create_reopen_and_migrate(tmp_path):
    store = _store(tmp_path)
    assert not store.database_path.exists()
    assert not store.profile_anchor_path.exists()

    empty = store.list_executions()

    assert empty["items"] == []
    assert empty["page"] == {
        "cursor": 0,
        "high_water": 0,
        "snapshot_high_water": 0,
        "minimum_available": 0,
        "has_more": False,
        "completeness": "complete",
    }
    assert not store.database_path.exists()
    assert not store.profile_anchor_path.exists()

    execution = store.create_execution(
        lifecycle="accepted",
        source_run_id="run-create",
        work_ref="work:create",
        proposal_ref="proposal:create",
        now=NOW,
    )

    assert store.database_path.exists()
    assert store.profile_anchor_path.exists()
    assert os.stat(store.database_path).st_mode & 0o077 == 0
    assert os.stat(store.profile_anchor_path).st_mode & 0o777 == 0o600
    anchor_payload = json.loads(store.profile_anchor_path.read_text(encoding="utf-8"))
    assert anchor_payload["version"] == 1
    assert len(anchor_payload["instance_id"]) == 64
    assert str(store.profile_home) not in json.dumps(store.authority.public())
    assert hashlib.sha256(str(store.profile_home).encode()).hexdigest() not in json.dumps(
        store.authority.public()
    )
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    reopened = _store(tmp_path)
    assert reopened.get_execution(execution["execution_id"])["revision"] == 1
    assert reopened.get_execution(execution["execution_id"])["freshness"] == "live"


def test_packaged_schema_and_synthetic_fixtures_are_closed(tmp_path):
    schema = contract_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    fixture_defs = {
        "capabilities.json": ("capabilities", None),
        "execution-list.json": ("executionList", "execution"),
        "decision-list.json": ("decisionList", "decision"),
        "event-list.json": ("eventList", "event"),
        "receipt-list.json": ("receiptList", "receipt"),
    }
    for filename, (definition_name, item_definition_name) in fixture_defs.items():
        payload = json.loads((FIXTURES / filename).read_text(encoding="utf-8"))
        definition = schema["$defs"][definition_name]
        assert definition["additionalProperties"] is False
        assert set(payload) == set(definition["required"])
        if item_definition_name:
            collection = definition_name.removesuffix("List").lower() + "s"
            item_definition = schema["$defs"][item_definition_name]
            assert item_definition["additionalProperties"] is False
            assert set(payload[collection][0]) == set(item_definition["required"])

    synthetic_home = tmp_path / "synthetic-profile"
    synthetic_store = ExecutionContractStore(
        profile_home=synthetic_home,
        profile_name="synthetic",
    )
    synthetic_store.initialize()
    capabilities = contract_capabilities(synthetic_home, "synthetic")
    fixture_capabilities = json.loads(
        (FIXTURES / "capabilities.json").read_text(encoding="utf-8")
    )
    assert capabilities["authority"]["profile_name"] == "synthetic"
    assert capabilities["authority"]["profile_id"].startswith(
        "hermes-profile-instance:"
    )
    assert {
        key: value for key, value in capabilities.items() if key != "authority"
    } == {
        key: value for key, value in fixture_capabilities.items() if key != "authority"
    }
    assert schema["$defs"]["errorDetail"]["additionalProperties"] is False


def test_unknown_store_version_fails_closed(tmp_path):
    store = _store(tmp_path)
    store.initialize()
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("PRAGMA user_version=99")

    with pytest.raises(UnsupportedContractVersionError):
        store.initialize()
    with pytest.raises(UnsupportedContractVersionError):
        store.list_executions()


@pytest.mark.parametrize(
    ("outcome", "lifecycle"),
    [
        ("succeeded", "terminal_succeeded"),
        ("failed", "terminal_failed"),
        ("cancelled", "terminal_cancelled"),
        ("partial", "terminal_partial"),
        ("ambiguous", "terminal_ambiguous"),
    ],
)
def test_all_evidence_backed_receipt_outcomes_publish_atomically(
    tmp_path,
    outcome,
    lifecycle,
):
    store = _store(tmp_path)
    execution = _bound_execution(store, suffix=outcome)
    store.transition_execution(execution["execution_id"], "running", now=NOW)
    evidence = _record_evidence(store, execution, outcome=outcome, suffix=outcome)

    terminal = store.transition_execution(
        execution["execution_id"],
        lifecycle,
        now=NOW + timedelta(seconds=3),
    )

    assert evidence["outcome"] == outcome
    assert terminal["lifecycle"] == lifecycle
    assert terminal["receipt_state"] == "published"
    receipt = store.get_receipt(terminal["receipt_id"])
    assert receipt["execution_id"] == execution["execution_id"]
    assert receipt["effect_id"] == execution["effect_id"]
    assert receipt["outcome"] == outcome
    assert receipt["revision"] == 1
    events = store.list_events(execution_id=execution["execution_id"])["items"]
    assert events[-2]["event_type"] == "execution.transitioned"
    assert events[-1]["event_type"] == "receipt.published"
    assert events[-1]["receipt_id"] == receipt["receipt_id"]


def test_nonterminal_lifecycle_progression_and_initial_state_are_closed(tmp_path):
    store = _store(tmp_path)
    execution = store.create_execution(lifecycle="accepted", now=NOW)
    queued = store.transition_execution(
        execution["execution_id"],
        "queued",
        now=NOW + timedelta(seconds=1),
    )
    running = store.transition_execution(
        execution["execution_id"],
        "running",
        now=NOW + timedelta(seconds=2),
    )
    cancelling = store.transition_execution(
        execution["execution_id"],
        "cancellation_requested",
        now=NOW + timedelta(seconds=3),
    )
    terminal = store.transition_execution(
        execution["execution_id"],
        "terminal_cancelled",
        now=NOW + timedelta(seconds=4),
    )
    assert [
        queued["lifecycle"],
        running["lifecycle"],
        cancelling["lifecycle"],
        terminal["lifecycle"],
    ] == ["queued", "running", "cancellation_requested", "terminal_cancelled"]
    with pytest.raises(ContractValidationError, match="initial lifecycle"):
        store.create_execution(lifecycle="awaiting_decision", now=NOW)


def test_unproven_effect_is_terminal_ambiguous_without_receipt(tmp_path):
    store = _store(tmp_path)
    execution = _bound_execution(store)
    store.transition_execution(execution["execution_id"], "running", now=NOW)

    terminal = store.transition_execution(
        execution["execution_id"],
        "terminal_succeeded",
        reason_code="generic_chat_completed",
        now=NOW + timedelta(seconds=1),
    )

    assert terminal["lifecycle"] == "terminal_ambiguous"
    assert terminal["receipt_state"] == "unproven"
    assert terminal["receipt_id"] is None
    assert store.list_receipts()["items"] == []


def test_execution_without_external_effect_can_terminate_without_receipt(tmp_path):
    store = _store(tmp_path)
    execution = store.create_execution(
        lifecycle="running",
        source_run_id="run-chat-only",
        work_ref="work:chat",
        proposal_ref="proposal:chat",
        now=NOW,
    )

    terminal = store.transition_execution(
        execution["execution_id"],
        "terminal_succeeded",
        now=NOW + timedelta(seconds=1),
    )

    assert terminal["lifecycle"] == "terminal_succeeded"
    assert terminal["receipt_state"] == "not_applicable"
    assert terminal["receipt_id"] is None


def test_pending_resolved_expired_and_superseded_decisions(tmp_path):
    store = _store(tmp_path)

    resolved_execution = _bound_execution(store, suffix="resolved")
    resolved = _resolved_decision(store, resolved_execution, suffix="resolved")
    assert resolved["state"] == "resolved"
    assert resolved["choice"] == "once"
    assert resolved["resolution_evidence_digest"] == _digest("resolution-resolved")

    expiring_execution = _bound_execution(store, suffix="expired")
    expiring = store.create_decision(
        execution_id=expiring_execution["execution_id"],
        effect_id=expiring_execution["effect_id"],
        proposal_ref=expiring_execution["proposal_ref"],
        request_digest=_digest("request-expired"),
        allowed_choices=["once", "deny"],
        expires_at=NOW + timedelta(seconds=1),
        now=NOW,
    )
    assert store.expire_decisions(now=NOW + timedelta(seconds=2)) == [
        expiring["decision_id"]
    ]
    assert store.get_decision(expiring["decision_id"])["state"] == "expired"
    assert store.get_execution(expiring_execution["execution_id"])["lifecycle"] == (
        "terminal_ambiguous"
    )

    superseded_execution = _bound_execution(store, suffix="superseded")
    superseded = store.create_decision(
        execution_id=superseded_execution["execution_id"],
        effect_id=superseded_execution["effect_id"],
        proposal_ref=superseded_execution["proposal_ref"],
        request_digest=_digest("request-superseded"),
        allowed_choices=["once", "deny"],
        expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )
    superseded = store.supersede_decision(
        superseded["decision_id"],
        reason_code="proposal_replaced",
        now=NOW + timedelta(seconds=1),
    )
    assert superseded["state"] == "superseded"
    assert store.list_decisions(state="pending")["items"] == []


def test_one_pending_decision_and_terminalization_closes_it(tmp_path):
    store = _store(tmp_path)
    execution = _bound_execution(store)
    decision = store.create_decision(
        execution_id=execution["execution_id"],
        effect_id=execution["effect_id"],
        proposal_ref=execution["proposal_ref"],
        request_digest=_digest("request-first"),
        allowed_choices=["once", "deny"],
        expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )
    with pytest.raises(ContractConflictError, match="already has a pending"):
        store.create_decision(
            execution_id=execution["execution_id"],
            effect_id=execution["effect_id"],
            proposal_ref=execution["proposal_ref"],
            request_digest=_digest("request-second"),
            allowed_choices=["once", "deny"],
            expires_at=NOW + timedelta(minutes=5),
            now=NOW,
        )

    terminal = store.transition_execution(
        execution["execution_id"],
        "terminal_failed",
        now=NOW + timedelta(seconds=1),
    )
    assert terminal["lifecycle"] == "terminal_ambiguous"
    assert store.get_decision(decision["decision_id"])["state"] == "superseded"
    assert store.expire_decisions(now=NOW + timedelta(minutes=10)) == []


def test_decision_and_effect_bindings_fail_closed(tmp_path):
    store = _store(tmp_path)
    first = _bound_execution(store, suffix="first")
    second = _bound_execution(store, suffix="second")

    with pytest.raises(ContractConflictError, match="effect binding"):
        store.create_decision(
            execution_id=first["execution_id"],
            effect_id=second["effect_id"],
            proposal_ref=first["proposal_ref"],
            request_digest=_digest("wrong-effect"),
            allowed_choices=["once", "deny"],
            expires_at=NOW + timedelta(minutes=5),
            now=NOW,
        )

    decision = _resolved_decision(store, first, suffix="first")
    with pytest.raises(ContractConflictError, match="exact decision"):
        _record_evidence(store, first, decision_id=None)
    with pytest.raises(ContractConflictError, match="not bound"):
        _record_evidence(
            store,
            first,
            decision_id="dec_"
            + store.authority.profile_key
            + "_"
            + "f" * 32,
        )
    with pytest.raises(ContractConflictError, match="effect evidence binding"):
        store.record_effect_evidence(
            execution_id=first["execution_id"],
            effect_id="effect:wrong",
            outcome="succeeded",
            subject_digest=_digest("subject"),
            evidence_digest=_digest("evidence"),
            result_digest=_digest("result"),
            decision_id=decision["decision_id"],
            now=NOW,
        )


def test_duplicate_evidence_is_idempotent_and_conflict_is_rejected(tmp_path):
    store = _store(tmp_path)
    execution = _bound_execution(store)
    first = _record_evidence(store, execution)
    duplicate = _record_evidence(store, execution)
    assert duplicate == first

    with pytest.raises(ContractConflictError, match="conflicting effect evidence"):
        _record_evidence(store, execution, outcome="partial")


def test_optimistic_revision_and_out_of_order_lifecycle_fail_closed(tmp_path):
    store = _store(tmp_path)
    execution = store.create_execution(lifecycle="queued", now=NOW)

    running = store.transition_execution(
        execution["execution_id"],
        "running",
        expected_revision=1,
        now=NOW + timedelta(seconds=1),
    )
    with pytest.raises(ContractConflictError, match="revision"):
        store.transition_execution(
            execution["execution_id"],
            "cancellation_requested",
            expected_revision=1,
            now=NOW + timedelta(seconds=2),
        )
    terminal = store.transition_execution(
        execution["execution_id"],
        "terminal_failed",
        expected_revision=running["revision"],
        now=NOW + timedelta(seconds=2),
    )
    with pytest.raises(ContractConflictError, match="invalid execution transition"):
        store.transition_execution(
            terminal["execution_id"],
            "running",
            now=NOW + timedelta(seconds=3),
        )


def test_transaction_rolls_back_state_when_event_append_fails(tmp_path, monkeypatch):
    store = _store(tmp_path)
    execution = store.create_execution(lifecycle="queued", now=NOW)

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("synthetic crash boundary")

    monkeypatch.setattr(store, "_append_event_in_txn", fail_event)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        store.transition_execution(
            execution["execution_id"],
            "running",
            now=NOW + timedelta(seconds=1),
        )

    reopened = _store(tmp_path)
    current = reopened.get_execution(execution["execution_id"])
    assert current["lifecycle"] == "queued"
    assert current["revision"] == 1
    assert len(reopened.list_events()["items"]) == 1


def test_terminal_receipt_and_event_roll_back_together(tmp_path, monkeypatch):
    store = _store(tmp_path)
    execution = _bound_execution(store)
    store.transition_execution(execution["execution_id"], "running", now=NOW)
    _record_evidence(store, execution)
    original_append = store._append_event_in_txn

    def fail_terminal_event(*args, **kwargs):
        if kwargs.get("event_type") == "execution.transitioned":
            raise RuntimeError("synthetic crash after receipt insert")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(store, "_append_event_in_txn", fail_terminal_event)
    with pytest.raises(RuntimeError, match="after receipt insert"):
        store.transition_execution(
            execution["execution_id"],
            "terminal_succeeded",
            now=NOW + timedelta(seconds=3),
        )

    reopened = _store(tmp_path)
    current = reopened.get_execution(execution["execution_id"])
    assert current["lifecycle"] == "running"
    assert current["receipt_state"] == "pending_evidence"
    assert reopened.list_receipts()["items"] == []


def test_restart_marks_orphaned_nonterminal_execution_ambiguous(tmp_path):
    first_runtime = _store(tmp_path, runtime="runtime-a")
    execution = first_runtime.create_execution(lifecycle="running", now=NOW)

    second_runtime = _store(tmp_path, runtime="runtime-b")
    before = second_runtime.get_execution(execution["execution_id"])
    assert before["freshness"] == "stale"

    recovered = second_runtime.recover_orphaned_executions(
        recovery_ref="restart:test",
        now=NOW + timedelta(seconds=1),
    )

    assert recovered == [execution["execution_id"]]
    after = second_runtime.get_execution(execution["execution_id"])
    assert after["lifecycle"] == "terminal_ambiguous"
    assert after["freshness"] == "terminal"
    assert after["recovery_ref"] == "restart:test"


def test_concurrent_append_has_monotonic_unique_sequences_and_pagination(tmp_path):
    path = tmp_path / "shared" / "execution_contract.sqlite3"

    def create(index: int) -> str:
        store = ExecutionContractStore(
            database_path=path,
            profile_name="default",
            runtime_instance_id="runtime-a",
        )
        return store.create_execution(
            lifecycle="queued",
            source_run_id=f"run-{index}",
            now=NOW + timedelta(microseconds=index),
        )["execution_id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(create, range(24)))

    assert len(set(ids)) == 24
    store = ExecutionContractStore(
        database_path=path,
        profile_name="default",
        runtime_instance_id="runtime-a",
    )
    first = store.list_events(limit=7)
    second = store.list_events(after=first["page"]["cursor"], limit=200)
    sequences = [event["sequence"] for event in first["items"] + second["items"]]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences)) == 24
    assert first["page"]["has_more"] is True
    assert second["page"]["high_water"] == sequences[-1]
    assert second["page"]["has_more"] is False


def test_pruned_cursor_returns_explicit_gap(tmp_path):
    store = _store(tmp_path)
    old = NOW - timedelta(days=40)
    execution = store.create_execution(lifecycle="running", now=old)
    store.transition_execution(
        execution["execution_id"],
        "terminal_failed",
        now=old + timedelta(seconds=1),
    )
    assert store.prune_events(now=NOW) == 2

    with pytest.raises(ContractCursorGoneError) as exc:
        store.list_events(after=0)
    assert exc.value.minimum_available == 3
    assert exc.value.high_water == 2


def test_retention_does_not_prune_across_a_live_execution_gap(tmp_path):
    store = _store(tmp_path)
    old = NOW - timedelta(days=40)
    live = store.create_execution(lifecycle="running", now=old)
    terminal = store.create_execution(lifecycle="running", now=old)
    store.transition_execution(
        terminal["execution_id"],
        "terminal_failed",
        now=old + timedelta(seconds=1),
    )

    assert store.prune_events(now=NOW) == 0
    events = store.list_events(after=0)["items"]
    assert events[0]["execution_id"] == live["execution_id"]
    assert len(events) == 3


@pytest.mark.parametrize("missing_sequence", [1, 2, 4])
def test_prune_rejects_global_gap_without_mutating_rows_or_watermark(
    tmp_path,
    missing_sequence,
):
    store = _store(tmp_path)
    old = NOW - timedelta(days=40)
    executions = []
    for index in range(2):
        execution = store.create_execution(
            lifecycle="running",
            source_run_id=f"prune-gap-{index}",
            now=old + timedelta(microseconds=index),
        )
        executions.append(execution)
        store.transition_execution(
            execution["execution_id"],
            "terminal_failed",
            now=old + timedelta(seconds=1, microseconds=index),
        )

    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "DELETE FROM execution_events WHERE sequence=?",
            (missing_sequence,),
        )
        connection.commit()
        before_rows = connection.execute(
            "SELECT sequence, event_id FROM execution_events ORDER BY sequence"
        ).fetchall()
        before_watermark = connection.execute(
            "SELECT value FROM execution_contract_metadata "
            "WHERE key='events_pruned_through'"
        ).fetchone()[0]

    with pytest.raises(ContractDataError, match="event sequence gap"):
        store.prune_events(now=NOW)

    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT sequence, event_id FROM execution_events ORDER BY sequence"
        ).fetchall() == before_rows
        assert connection.execute(
            "SELECT value FROM execution_contract_metadata "
            "WHERE key='events_pruned_through'"
        ).fetchone()[0] == before_watermark == "0"

    with pytest.raises(ContractDataError, match="event sequence gap"):
        store.list_events()
    with pytest.raises(ContractDataError, match="event sequence gap"):
        store.list_events(execution_id=executions[0]["execution_id"])


@pytest.mark.parametrize("missing_sequence", [4, 5, 6])
def test_prune_rejects_gap_in_retained_suffix_before_deleting_terminal_prefix(
    tmp_path,
    missing_sequence,
):
    store = _store(tmp_path)
    old = NOW - timedelta(days=40)
    terminal = store.create_execution(
        lifecycle="running",
        source_run_id="prunable-terminal-prefix",
        now=old,
    )
    store.transition_execution(
        terminal["execution_id"],
        "terminal_failed",
        now=old + timedelta(seconds=1),
    )
    live = store.create_execution(
        lifecycle="running",
        source_run_id="live-prune-boundary",
        now=old + timedelta(seconds=2),
    )
    retained = store.create_execution(
        lifecycle="accepted",
        source_run_id="retained-terminal-suffix",
        now=NOW,
    )
    store.transition_execution(
        retained["execution_id"],
        "running",
        now=NOW + timedelta(seconds=1),
    )
    store.transition_execution(
        retained["execution_id"],
        "terminal_failed",
        now=NOW + timedelta(seconds=2),
    )

    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT sequence FROM execution_events ORDER BY sequence"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,)]
        connection.execute(
            "DELETE FROM execution_events WHERE sequence=?",
            (missing_sequence,),
        )
        connection.commit()
        before_rows = connection.execute(
            "SELECT sequence, event_id, execution_id FROM execution_events "
            "ORDER BY sequence"
        ).fetchall()
        before_watermark = connection.execute(
            "SELECT value FROM execution_contract_metadata "
            "WHERE key='events_pruned_through'"
        ).fetchone()[0]

    with pytest.raises(ContractDataError, match="event sequence gap"):
        store.prune_events(now=NOW)

    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT sequence, event_id, execution_id FROM execution_events "
            "ORDER BY sequence"
        ).fetchall() == before_rows
        assert connection.execute(
            "SELECT value FROM execution_contract_metadata "
            "WHERE key='events_pruned_through'"
        ).fetchone()[0] == before_watermark == "0"

    with pytest.raises(ContractDataError, match="event sequence gap"):
        store.list_events()
    with pytest.raises(ContractDataError, match="event sequence gap"):
        store.list_events(execution_id=terminal["execution_id"])
    assert live["lifecycle"] == "running"


def test_prune_boundary_excludes_append_waiting_on_same_write_lock(tmp_path):
    seed = _store(tmp_path, runtime="prune-race")
    old = NOW - timedelta(days=40)
    execution = seed.create_execution(lifecycle="running", now=old)
    seed.transition_execution(
        execution["execution_id"],
        "terminal_failed",
        now=old + timedelta(seconds=1),
    )
    pruner = _store(tmp_path, runtime="prune-race")
    writer = _store(tmp_path, runtime="prune-race")
    prune_locked = threading.Event()
    release_prune = threading.Event()
    writer_attempted = threading.Event()

    def hold_prune(operation, _execution_id):
        if operation == "prune":
            prune_locked.set()
            assert release_prune.wait(timeout=5)

    pruner._after_write_lock_acquired = hold_prune

    def append_event():
        writer_attempted.set()
        return writer.create_execution(
            lifecycle="queued",
            source_run_id="after-prune-boundary",
            now=NOW,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        prune_future = pool.submit(pruner.prune_events, now=NOW)
        assert prune_locked.wait(timeout=5)
        append_future = pool.submit(append_event)
        assert writer_attempted.wait(timeout=5)
        assert not append_future.done()
        release_prune.set()
        assert prune_future.result(timeout=10) == 2
        appended = append_future.result(timeout=10)

    page = seed.list_events(after=2)
    assert [event["execution_id"] for event in page["items"]] == [
        appended["execution_id"]
    ]
    assert page["page"]["pruned_through"] == 2
    assert page["page"]["snapshot_high_water"] == 3
    assert page["page"]["completeness"] == "complete"


def test_profile_crossing_and_malformed_identifiers_are_distinct(tmp_path):
    first = _store(tmp_path, profile="first")
    second = _store(tmp_path, profile="second")
    execution = first.create_execution(lifecycle="queued", now=NOW)
    second.initialize()

    with pytest.raises(ContractForbiddenError):
        second.get_execution(execution["execution_id"])
    with pytest.raises(ContractValidationError):
        first.get_execution("run_not-an-execution-id")


def test_concurrent_profile_initialization_creates_one_stable_anchor(tmp_path):
    profile_home = tmp_path / "concurrent-profile"

    def initialize(_index: int):
        store = ExecutionContractStore(
            profile_home=profile_home,
            runtime_instance_id="runtime-a",
        )
        store.initialize()
        return store.authority

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        authorities = list(pool.map(initialize, range(16)))

    assert len(set(authorities)) == 1
    anchor = profile_home / ".execution-contract-profile-instance.json"
    assert anchor.exists()
    assert stat.S_IMODE(anchor.stat().st_mode) == 0o600
    payload = json.loads(anchor.read_text(encoding="utf-8"))
    assert set(payload) == {"version", "instance_id"}


def test_full_profile_home_move_and_restore_preserve_authority(tmp_path):
    original_home = tmp_path / "profile-original"
    moved_home = tmp_path / "profile-moved"
    restored_home = tmp_path / "profile-restored"
    backup_home = tmp_path / "profile-backup"
    original = ExecutionContractStore(
        profile_home=original_home,
        runtime_instance_id="runtime-a",
    )
    execution = original.create_execution(lifecycle="queued", now=NOW)
    original_authority = original.authority

    original_home.rename(moved_home)
    moved = ExecutionContractStore(
        profile_home=moved_home,
        runtime_instance_id="runtime-a",
    )
    assert moved.get_execution(execution["execution_id"])["execution_id"] == (
        execution["execution_id"]
    )
    assert moved.authority.profile_id == original_authority.profile_id
    assert moved.authority.authority_id == original_authority.authority_id

    moved_home.rename(restored_home)
    restored = ExecutionContractStore(
        profile_home=restored_home,
        runtime_instance_id="runtime-a",
    )
    assert restored.authority.profile_id == original_authority.profile_id
    assert restored.get_execution(execution["execution_id"])["execution_id"] == (
        execution["execution_id"]
    )
    with sqlite3.connect(restored.database_path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copytree(restored_home, backup_home)
    backup = ExecutionContractStore(
        profile_home=backup_home,
        runtime_instance_id="runtime-a",
    )
    assert backup.authority.profile_id == original_authority.profile_id
    assert backup.get_execution(execution["execution_id"])["execution_id"] == (
        execution["execution_id"]
    )


def test_database_only_copy_fails_against_destination_anchor(tmp_path):
    source_home = tmp_path / "source-profile"
    destination_home = tmp_path / "destination-profile"
    source = ExecutionContractStore(profile_home=source_home)
    source.create_execution(lifecycle="queued", now=NOW)
    with sqlite3.connect(source.database_path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    destination_identity = ExecutionContractStore(
        profile_home=destination_home,
        database_path=destination_home / "identity-seed.sqlite3",
    )
    destination_identity.initialize()
    shutil.copy2(source.database_path, destination_home / "execution_contract.sqlite3")
    copied = ExecutionContractStore(profile_home=destination_home)

    with pytest.raises(ContractDataError, match="authority metadata"):
        copied.list_executions()
    with pytest.raises(ContractDataError, match="authority metadata"):
        copied.initialize()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "unsafe-mode", "symlink"])
def test_missing_corrupt_or_unsafe_profile_anchor_fails_closed(tmp_path, damage):
    store = _store(tmp_path)
    store.create_execution(lifecycle="queued", now=NOW)
    anchor = store.profile_anchor_path
    if damage == "missing":
        anchor.unlink()
    elif damage == "corrupt":
        anchor.write_text('{"version":1,"instance_id":"bad"}\n', encoding="utf-8")
        anchor.chmod(0o600)
    elif damage == "unsafe-mode":
        anchor.chmod(0o644)
    else:
        anchor.unlink()
        anchor.symlink_to(store.database_path)

    reopened = _store(tmp_path)
    with pytest.raises(ContractDataError, match="profile instance anchor"):
        reopened.list_executions()
    with pytest.raises(ContractDataError, match="profile instance anchor"):
        reopened.initialize()


def test_malformed_persisted_state_and_cursor_ahead_fail_closed(tmp_path):
    store = _store(tmp_path)
    execution = store.create_execution(lifecycle="queued", now=NOW)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE executions SET lifecycle='future_state' WHERE execution_id=?",
            (execution["execution_id"],),
        )
        connection.commit()

    with pytest.raises(ContractDataError, match="unknown state"):
        store.get_execution(execution["execution_id"])
    with pytest.raises(ContractConflictError, match="cursor"):
        store.list_events(after=999)


def test_closed_reference_and_digest_validation(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ContractValidationError):
        store.create_execution(lifecycle="queued", work_ref="bad\nref", now=NOW)
    with pytest.raises(ContractValidationError, match="exact work_ref"):
        store.create_execution(
            lifecycle="queued",
            effect_id="effect:unbound",
            now=NOW,
        )
    with pytest.raises(ContractValidationError, match="timezone-aware"):
        store.create_execution(
            lifecycle="queued",
            now=datetime(2026, 8, 15, 12, 0),
        )
    execution = _bound_execution(store)
    with pytest.raises(ContractValidationError, match="SHA-256"):
        store.record_effect_evidence(
            execution_id=execution["execution_id"],
            effect_id=execution["effect_id"],
            outcome="succeeded",
            subject_digest="not-a-digest",
            evidence_digest=_digest("evidence"),
            result_digest=_digest("result"),
            now=NOW,
        )
    assert CONTRACT_VERSION == execution["contract_version"]


def test_restart_recovery_transactionally_supersedes_pending_decision(tmp_path):
    first_runtime = _store(tmp_path, runtime="runtime-a")
    execution = _bound_execution(first_runtime, suffix="restart-pending")
    decision = first_runtime.create_decision(
        execution_id=execution["execution_id"],
        effect_id=execution["effect_id"],
        proposal_ref=execution["proposal_ref"],
        request_digest=_digest("restart-pending"),
        allowed_choices=["once", "deny"],
        expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )

    second_runtime = _store(tmp_path, runtime="runtime-b")
    assert second_runtime.recover_orphaned_executions(
        recovery_ref="restart:pending",
        now=NOW + timedelta(seconds=1),
    ) == [execution["execution_id"]]

    assert second_runtime.get_execution(execution["execution_id"])["lifecycle"] == (
        "terminal_ambiguous"
    )
    assert second_runtime.get_decision(decision["decision_id"])["state"] == (
        "superseded"
    )
    assert second_runtime.list_decisions(state="pending")["items"] == []


def test_decision_request_racing_cancellation_never_leaves_pending(tmp_path):
    seed = _store(tmp_path, runtime="runtime-race")
    execution = _bound_execution(seed, suffix="cancel-race")
    seed.transition_execution(execution["execution_id"], "running", now=NOW)
    decision_store = _store(tmp_path, runtime="runtime-race")
    cancel_store = _store(tmp_path, runtime="runtime-race")
    barrier = threading.Barrier(2)

    def request_decision():
        barrier.wait()
        try:
            return decision_store.create_decision(
                execution_id=execution["execution_id"],
                effect_id=execution["effect_id"],
                proposal_ref=execution["proposal_ref"],
                request_digest=_digest("cancel-race"),
                allowed_choices=["once", "deny"],
                expires_at=NOW + timedelta(minutes=5),
                now=NOW,
            )
        except ContractConflictError:
            return None

    def request_cancel():
        barrier.wait()
        return cancel_store.transition_execution(
            execution["execution_id"],
            "cancellation_requested",
            now=NOW + timedelta(seconds=1),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        decision_future = pool.submit(request_decision)
        cancel_future = pool.submit(request_cancel)
        decision_result = decision_future.result()
        cancel_future.result()

    current = seed.get_execution(execution["execution_id"])
    assert current["lifecycle"] == "cancellation_requested"
    assert seed.list_decisions(state="pending")["items"] == []
    if decision_result is not None:
        assert seed.get_decision(decision_result["decision_id"])["state"] == (
            "superseded"
        )


@pytest.mark.parametrize("operation", ["resolve", "supersede"])
def test_decision_close_cannot_revive_terminal_execution(tmp_path, operation):
    store = _store(tmp_path)
    execution = _bound_execution(store, suffix=f"terminal-{operation}")
    decision = store.create_decision(
        execution_id=execution["execution_id"],
        effect_id=execution["effect_id"],
        proposal_ref=execution["proposal_ref"],
        request_digest=_digest(f"terminal-{operation}"),
        allowed_choices=["once", "deny"],
        expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )
    terminal = "2026-08-15T12:00:01.000000Z"
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE executions SET lifecycle='terminal_ambiguous', "
            "receipt_state='unproven', terminal_at=?, updated_at=? "
            "WHERE execution_id=?",
            (terminal, terminal, execution["execution_id"]),
        )
        connection.commit()

    with pytest.raises(ContractConflictError, match="terminal_ambiguous"):
        if operation == "resolve":
            store.resolve_decision(
                decision["decision_id"],
                choice="once",
                resolution_evidence_digest=_digest("terminal-resolution"),
                now=NOW + timedelta(seconds=2),
            )
        else:
            store.supersede_decision(
                decision["decision_id"],
                reason_code="terminal",
                now=NOW + timedelta(seconds=2),
            )
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT lifecycle FROM executions WHERE execution_id=?",
            (execution["execution_id"],),
        ).fetchone()[0] == "terminal_ambiguous"
        assert connection.execute(
            "SELECT state FROM decisions WHERE decision_id=?",
            (decision["decision_id"],),
        ).fetchone()[0] == "pending"


def test_old_resolution_cannot_authorize_newer_pending_decision(tmp_path):
    store = _store(tmp_path)
    execution = _bound_execution(store, suffix="decision-generation")
    old = _resolved_decision(store, execution, suffix="old")
    newer = store.create_decision(
        execution_id=execution["execution_id"],
        effect_id=execution["effect_id"],
        proposal_ref=execution["proposal_ref"],
        request_digest=_digest("request-newer"),
        allowed_choices=["once", "deny"],
        expires_at=NOW + timedelta(minutes=5),
        now=NOW + timedelta(seconds=2),
    )

    with pytest.raises(ContractConflictError, match="newer pending"):
        _record_evidence(
            store,
            execution,
            decision_id=old["decision_id"],
            suffix="old-resolution",
        )
    assert store.get_decision(newer["decision_id"])["state"] == "pending"


def test_evidence_blocks_new_decisions_but_preserves_idempotent_resolved_retry(
    tmp_path,
):
    store = _store(tmp_path)
    execution = _bound_execution(store, suffix="evidence-decision-gate")
    resolved = _resolved_decision(store, execution, suffix="evidence-decision-gate")
    evidence = _record_evidence(
        store,
        execution,
        decision_id=resolved["decision_id"],
        suffix="evidence-decision-gate",
    )
    duplicate = store.create_decision(
        execution_id=execution["execution_id"],
        effect_id=execution["effect_id"],
        proposal_ref=execution["proposal_ref"],
        request_digest=_digest("request-evidence-decision-gate"),
        candidate_digest=_digest("candidate-evidence-decision-gate"),
        policy_digest=_digest("policy-evidence-decision-gate"),
        allowed_choices=["once", "deny"],
        expires_at=NOW + timedelta(minutes=5),
        now=NOW + timedelta(seconds=3),
    )
    assert duplicate["decision_id"] == resolved["decision_id"]
    assert duplicate["state"] == "resolved"
    assert store.record_effect_evidence(
        execution_id=execution["execution_id"],
        effect_id=execution["effect_id"],
        outcome="succeeded",
        subject_digest=_digest("subject-evidence-decision-gate"),
        evidence_digest=_digest("evidence-evidence-decision-gate"),
        result_digest=_digest("result-evidence-decision-gate"),
        decision_id=resolved["decision_id"],
        now=NOW + timedelta(seconds=4),
    )["evidence_id"] == evidence["evidence_id"]

    with pytest.raises(ContractConflictError, match="no new decision"):
        store.create_decision(
            execution_id=execution["execution_id"],
            effect_id=execution["effect_id"],
            proposal_ref=execution["proposal_ref"],
            request_digest=_digest("request-after-evidence"),
            allowed_choices=["once", "deny"],
            expires_at=NOW + timedelta(minutes=6),
            now=NOW + timedelta(seconds=3),
        )
    with pytest.raises(ContractConflictError, match="conflicting bindings"):
        store.create_decision(
            execution_id=execution["execution_id"],
            effect_id=execution["effect_id"],
            proposal_ref=execution["proposal_ref"],
            request_digest=_digest("request-evidence-decision-gate"),
            candidate_digest=_digest("candidate-conflict"),
            policy_digest=_digest("policy-evidence-decision-gate"),
            allowed_choices=["once", "deny"],
            expires_at=NOW + timedelta(minutes=5),
            now=NOW + timedelta(seconds=3),
        )


@pytest.mark.parametrize(
    ("case", "expected_state"),
    [
        ("pending", "pending"),
        ("expired", "expired"),
        ("resolved", "resolved"),
        ("superseded", "superseded"),
        ("evidence", "resolved"),
        ("receipt", "resolved"),
    ],
)
def test_exact_decision_retry_remains_idempotent_after_expiry_and_effects(
    tmp_path,
    case,
    expected_state,
):
    store = _store(tmp_path)
    execution = _bound_execution(store, suffix=f"retry-{case}")
    expires_at = NOW + timedelta(seconds=1)
    request_digest = _digest(f"retry-{case}")
    create = {
        "execution_id": execution["execution_id"],
        "effect_id": execution["effect_id"],
        "proposal_ref": execution["proposal_ref"],
        "request_digest": request_digest,
        "candidate_digest": _digest(f"candidate-retry-{case}"),
        "policy_digest": _digest(f"policy-retry-{case}"),
        "allowed_choices": ["once", "deny"],
        "expires_at": expires_at,
    }
    decision = store.create_decision(**create, now=NOW)

    if case == "expired":
        assert store.expire_decisions(now=NOW + timedelta(seconds=2)) == [
            decision["decision_id"]
        ]
    elif case in {"resolved", "evidence", "receipt"}:
        decision = store.resolve_decision(
            decision["decision_id"],
            choice="once",
            resolution_evidence_digest=_digest(f"resolution-retry-{case}"),
            now=NOW + timedelta(microseconds=500_000),
        )
        if case in {"evidence", "receipt"}:
            _record_evidence(
                store,
                execution,
                decision_id=decision["decision_id"],
                suffix=f"retry-{case}",
            )
        if case == "receipt":
            store.transition_execution(
                execution["execution_id"],
                "terminal_succeeded",
                now=NOW + timedelta(seconds=3),
            )
    elif case == "superseded":
        decision = store.supersede_decision(
            decision["decision_id"],
            reason_code="retry-test",
            now=NOW + timedelta(microseconds=500_000),
        )

    exact = store.create_decision(**create, now=NOW + timedelta(days=1))
    assert exact["decision_id"] == decision["decision_id"]
    assert exact["state"] == expected_state

    conflicting = dict(create)
    conflicting["allowed_choices"] = ["once", "deny", "always"]
    with pytest.raises(ContractConflictError, match="conflicting bindings"):
        store.create_decision(**conflicting, now=NOW + timedelta(days=1))


@pytest.mark.parametrize("first_operation", ["decision", "evidence"])
def test_decision_and_evidence_race_is_serialized_by_write_transaction(
    tmp_path,
    first_operation,
):
    seed = _store(tmp_path, runtime="race-runtime")
    execution = _bound_execution(seed, suffix=f"race-{first_operation}")
    decision_store = _store(tmp_path, runtime="race-runtime")
    evidence_store = _store(tmp_path, runtime="race-runtime")
    first_locked = threading.Event()
    release_first = threading.Event()
    second_attempted = threading.Event()

    def hold_first(operation, _execution_id):
        if operation == first_operation:
            first_locked.set()
            assert release_first.wait(timeout=5)

    if first_operation == "decision":
        decision_store._after_write_lock_acquired = hold_first
    else:
        evidence_store._after_write_lock_acquired = hold_first

    def request_decision():
        if first_operation != "decision":
            second_attempted.set()
        return decision_store.create_decision(
            execution_id=execution["execution_id"],
            effect_id=execution["effect_id"],
            proposal_ref=execution["proposal_ref"],
            request_digest=_digest(f"race-request-{first_operation}"),
            allowed_choices=["once", "deny"],
            expires_at=NOW + timedelta(minutes=5),
            now=NOW,
        )

    def record_evidence():
        if first_operation != "evidence":
            second_attempted.set()
        return _record_evidence(
            evidence_store,
            execution,
            suffix=f"race-{first_operation}",
        )

    first_call = request_decision if first_operation == "decision" else record_evidence
    second_call = record_evidence if first_operation == "decision" else request_decision
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first_call)
        assert first_locked.wait(timeout=5)
        second_future = pool.submit(second_call)
        assert second_attempted.wait(timeout=5)
        release_first.set()
        first_result = first_future.result(timeout=10)
        with pytest.raises(ContractConflictError):
            second_future.result(timeout=10)

    if first_operation == "decision":
        assert first_result["state"] == "pending"
        assert seed.list_decisions(state="pending")["items"] == [first_result]
        assert seed.list_events(execution_id=execution["execution_id"])["page"][
            "completeness"
        ] == "complete"
    else:
        assert first_result["outcome"] == "succeeded"
        assert seed.list_decisions()["items"] == []
        assert seed.get_execution(execution["execution_id"])["receipt_state"] == (
            "pending_evidence"
        )


def test_receipt_also_blocks_new_decisions(tmp_path):
    store = _store(tmp_path)
    execution = _bound_execution(store, suffix="receipt-decision-gate")
    store.transition_execution(execution["execution_id"], "running", now=NOW)
    _record_evidence(store, execution, suffix="receipt-decision-gate")
    store.transition_execution(
        execution["execution_id"],
        "terminal_succeeded",
        now=NOW + timedelta(seconds=3),
    )
    with pytest.raises(ContractConflictError, match="no new decision"):
        store.create_decision(
            execution_id=execution["execution_id"],
            effect_id=execution["effect_id"],
            proposal_ref=execution["proposal_ref"],
            request_digest=_digest("receipt-new-decision"),
            allowed_choices=["once", "deny"],
            expires_at=NOW + timedelta(minutes=5),
            now=NOW + timedelta(seconds=4),
        )


def test_collection_snapshot_excludes_concurrent_insert(monkeypatch, tmp_path):
    import hermes_state

    monkeypatch.setattr(
        hermes_state,
        "apply_wal_with_fallback",
        lambda connection, **_kwargs: connection.execute("PRAGMA journal_mode=WAL"),
    )
    store = _store(tmp_path)
    writer = _store(tmp_path)
    store.create_execution(lifecycle="queued", source_run_id="snapshot-1", now=NOW)
    store.create_execution(lifecycle="queued", source_run_id="snapshot-2", now=NOW)
    writer.initialize()
    inserted = []

    def append_after_pin(collection, _high_water):
        if collection == "executions" and not inserted:
            inserted.append(
                writer.create_execution(
                    lifecycle="queued",
                    source_run_id="snapshot-concurrent",
                    now=NOW,
                )
            )

    store._after_snapshot_pinned = append_after_pin
    first = store.list_executions(limit=1)
    assert first["page"]["snapshot_high_water"] == 2
    assert inserted
    store._after_snapshot_pinned = lambda *_args: None
    second = store.list_executions(
        after=first["page"]["cursor"],
        snapshot_high_water=first["page"]["snapshot_high_water"],
    )
    source_ids = [item["source_run_id"] for item in first["items"] + second["items"]]
    assert source_ids == ["snapshot-1", "snapshot-2"]
    assert "snapshot-concurrent" not in source_ids


def test_event_snapshot_excludes_append_after_high_water(monkeypatch, tmp_path):
    import hermes_state

    monkeypatch.setattr(
        hermes_state,
        "apply_wal_with_fallback",
        lambda connection, **_kwargs: connection.execute("PRAGMA journal_mode=WAL"),
    )
    store = _store(tmp_path)
    writer = _store(tmp_path)
    first_execution = store.create_execution(lifecycle="queued", now=NOW)
    second_execution = store.create_execution(lifecycle="queued", now=NOW)
    writer.initialize()
    appended = []

    def append_after_pin(collection, _high_water):
        if collection == "events" and not appended:
            appended.append(
                writer.transition_execution(
                    second_execution["execution_id"],
                    "running",
                    now=NOW + timedelta(seconds=1),
                )
            )

    store._after_snapshot_pinned = append_after_pin
    page = store.list_events(limit=200)
    assert page["page"]["snapshot_high_water"] == 2
    assert appended
    assert [event["execution_id"] for event in page["items"]] == [
        first_execution["execution_id"],
        second_execution["execution_id"],
    ]


@pytest.mark.parametrize("gap", ["start", "interior", "end"])
def test_global_event_sequence_gaps_fail_closed_before_complete(tmp_path, gap):
    store = _store(tmp_path)
    for index in range(4):
        store.create_execution(
            lifecycle="queued",
            source_run_id=f"gap-{gap}-{index}",
            now=NOW + timedelta(microseconds=index),
        )
    sequence = {"start": 1, "interior": 2, "end": 4}[gap]
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "DELETE FROM execution_events WHERE sequence=?",
            (sequence,),
        )
        connection.commit()

    with pytest.raises(ContractDataError, match="event sequence gap"):
        store.list_events()


def test_filtered_event_feed_still_validates_unfiltered_global_continuity(tmp_path):
    store = _store(tmp_path)
    first = store.create_execution(lifecycle="queued", now=NOW)
    store.create_execution(
        lifecycle="queued",
        now=NOW + timedelta(microseconds=1),
    )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DELETE FROM execution_events WHERE sequence=2")
        connection.commit()

    with pytest.raises(ContractDataError, match="event sequence gap"):
        store.list_events(execution_id=first["execution_id"])


def test_empty_and_fully_pruned_event_windows_are_legitimately_complete(tmp_path):
    empty = _store(tmp_path, profile="empty")
    assert empty.list_events()["page"] == {
        "cursor": 0,
        "high_water": 0,
        "snapshot_high_water": 0,
        "minimum_available": 1,
        "has_more": False,
        "completeness": "complete",
        "pruned_through": 0,
        "retention_seconds": 2592000,
    }

    store = _store(tmp_path, profile="pruned")
    old = NOW - timedelta(days=40)
    execution = store.create_execution(lifecycle="running", now=old)
    store.transition_execution(
        execution["execution_id"],
        "terminal_failed",
        now=old + timedelta(seconds=1),
    )
    assert store.prune_events(now=NOW) == 2
    page = store.list_events(after=2, snapshot_high_water=2)
    assert page["items"] == []
    assert page["page"]["completeness"] == "complete"
    assert page["page"]["pruned_through"] == 2
    assert page["page"]["snapshot_high_water"] == 2


def test_pruning_after_snapshot_pin_does_not_create_false_gap(
    monkeypatch,
    tmp_path,
):
    import hermes_state

    monkeypatch.setattr(
        hermes_state,
        "apply_wal_with_fallback",
        lambda connection, **_kwargs: connection.execute("PRAGMA journal_mode=WAL"),
    )
    reader = _store(tmp_path)
    writer = _store(tmp_path)
    old = NOW - timedelta(days=40)
    execution = reader.create_execution(lifecycle="running", now=old)
    reader.transition_execution(
        execution["execution_id"],
        "terminal_failed",
        now=old + timedelta(seconds=1),
    )
    writer.initialize()
    pruned = []

    def prune_after_pin(collection, _high_water):
        if collection == "events" and not pruned:
            pruned.append(writer.prune_events(now=NOW))

    reader._after_snapshot_pinned = prune_after_pin
    page = reader.list_events(after=0)
    assert pruned == [2]
    assert len(page["items"]) == 2
    assert page["page"]["completeness"] == "complete"
    reader._after_snapshot_pinned = lambda *_args: None
    with pytest.raises(ContractCursorGoneError):
        reader.list_events(after=0)


def test_late_ambiguous_evidence_publishes_receipt_transactionally(tmp_path):
    store = _store(tmp_path)
    execution = _bound_execution(store, suffix="late-ambiguous")
    terminal = store.transition_execution(
        execution["execution_id"],
        "terminal_failed",
        recovery_ref="recovery:late-ambiguous",
        now=NOW + timedelta(seconds=1),
    )
    assert terminal["lifecycle"] == "terminal_ambiguous"
    assert terminal["receipt_state"] == "unproven"

    with pytest.raises(ContractConflictError, match="reconciliation path"):
        _record_evidence(store, execution, outcome="ambiguous", suffix="late")

    receipt = store.reconcile_ambiguous_evidence(
        execution_id=execution["execution_id"],
        effect_id=execution["effect_id"],
        outcome="ambiguous",
        subject_digest=_digest("late-subject"),
        evidence_digest=_digest("late-evidence"),
        result_digest=_digest("late-result"),
        reconciliation_ref="reconcile:late-ambiguous",
        now=NOW + timedelta(seconds=2),
    )
    current = store.get_execution(execution["execution_id"])
    assert receipt["outcome"] == "ambiguous"
    assert current["lifecycle"] == "terminal_ambiguous"
    assert current["receipt_state"] == "published"
    assert current["receipt_id"] == receipt["receipt_id"]
    assert store.get_receipt(receipt["receipt_id"])["reconciliation_ref"] == (
        "reconcile:late-ambiguous"
    )
    assert receipt["recovery_ref"] == "recovery:late-ambiguous"
    identical = store.reconcile_ambiguous_evidence(
        execution_id=execution["execution_id"],
        effect_id=execution["effect_id"],
        outcome="ambiguous",
        subject_digest=_digest("late-subject"),
        evidence_digest=_digest("late-evidence"),
        result_digest=_digest("late-result"),
        reconciliation_ref="reconcile:late-ambiguous",
        now=NOW + timedelta(seconds=3),
    )
    assert identical["receipt_id"] == receipt["receipt_id"]
    with pytest.raises(ContractConflictError, match="not eligible"):
        store.reconcile_ambiguous_evidence(
            execution_id=execution["execution_id"],
            effect_id=execution["effect_id"],
            outcome="ambiguous",
            subject_digest=_digest("late-subject"),
            evidence_digest=_digest("late-evidence"),
            result_digest=_digest("late-result"),
            recovery_ref="recovery:conflicting-retry",
            reconciliation_ref="reconcile:late-ambiguous",
            now=NOW + timedelta(seconds=4),
        )


@pytest.mark.parametrize("offset_seconds", [-86400, 0, 86400])
def test_normal_receipt_terminal_timestamp_must_equal_execution_terminal(
    tmp_path,
    offset_seconds,
):
    store = _store(tmp_path)
    execution = _bound_execution(store, suffix=f"receipt-time-{offset_seconds}")
    _record_evidence(
        store,
        execution,
        suffix=f"receipt-time-{offset_seconds}",
        reconciliation_ref=(
            "reconcile:ordinary-terminal" if offset_seconds == 0 else None
        ),
    )
    terminal_time = NOW + timedelta(seconds=3)
    terminal = store.transition_execution(
        execution["execution_id"],
        "terminal_succeeded",
        now=terminal_time,
    )
    receipt_id = terminal["receipt_id"]
    if offset_seconds:
        changed = (terminal_time + timedelta(seconds=offset_seconds)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        with sqlite3.connect(store.database_path) as connection:
            connection.execute(
                "UPDATE receipts SET terminal_at=? WHERE receipt_id=?",
                (changed, receipt_id),
            )
            connection.commit()
        with pytest.raises(
            ContractDataError,
            match="must equal execution terminal timestamp",
        ):
            store.get_receipt(receipt_id)
    else:
        assert store.get_receipt(receipt_id)["terminal_at"] == terminal["terminal_at"]


@pytest.mark.parametrize("offset_seconds", [-1, 0, 1])
def test_reconciled_receipt_terminal_timestamp_must_equal_evidence_recorded_at(
    tmp_path,
    offset_seconds,
):
    store = _store(tmp_path)
    execution = _bound_execution(store, suffix=f"reconcile-time-{offset_seconds}")
    store.transition_execution(
        execution["execution_id"],
        "terminal_failed",
        recovery_ref=f"recovery:reconcile-time-{offset_seconds}",
        now=NOW + timedelta(seconds=1),
    )
    evidence_time = NOW + timedelta(seconds=2)
    receipt = store.reconcile_ambiguous_evidence(
        execution_id=execution["execution_id"],
        effect_id=execution["effect_id"],
        outcome="ambiguous",
        subject_digest=_digest(f"reconcile-subject-{offset_seconds}"),
        evidence_digest=_digest(f"reconcile-evidence-{offset_seconds}"),
        result_digest=_digest(f"reconcile-result-{offset_seconds}"),
        reconciliation_ref=f"reconcile:timestamp-{offset_seconds}",
        now=evidence_time,
    )
    if offset_seconds:
        changed = (evidence_time + timedelta(seconds=offset_seconds)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        with sqlite3.connect(store.database_path) as connection:
            connection.execute(
                "UPDATE receipts SET terminal_at=? WHERE receipt_id=?",
                (changed, receipt["receipt_id"]),
            )
            connection.commit()
        with pytest.raises(
            ContractDataError,
            match="must equal evidence recorded timestamp",
        ):
            store.get_receipt(receipt["receipt_id"])
    else:
        assert store.get_receipt(receipt["receipt_id"])["terminal_at"] == (
            evidence_time.isoformat(timespec="microseconds").replace("+00:00", "Z")
        )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("execution_id", "identifier"),
        ("execution_timestamp", "timestamp ordering"),
        ("execution_receipt_state", "receipt"),
        ("decision_digest", "request_digest"),
        ("decision_binding", "binding"),
        ("receipt_digest", "evidence_digest"),
    ],
)
def test_deep_persisted_corruption_fails_before_projection(tmp_path, case, message):
    store = _store(tmp_path)
    execution = _bound_execution(store, suffix=case)
    target = "execution"
    target_id = execution["execution_id"]

    if case.startswith("decision_"):
        decision = store.create_decision(
            execution_id=execution["execution_id"],
            effect_id=execution["effect_id"],
            proposal_ref=execution["proposal_ref"],
            request_digest=_digest(f"request-{case}"),
            allowed_choices=["once", "deny"],
            expires_at=NOW + timedelta(minutes=5),
            now=NOW,
        )
        target = "decision"
        target_id = decision["decision_id"]
    elif case == "receipt_digest":
        evidence = _record_evidence(store, execution, suffix=case)
        assert evidence["outcome"] == "succeeded"
        terminal = store.transition_execution(
            execution["execution_id"],
            "terminal_succeeded",
            now=NOW + timedelta(seconds=3),
        )
        target = "receipt"
        target_id = terminal["receipt_id"]

    with sqlite3.connect(store.database_path) as connection:
        if case == "execution_id":
            connection.execute(
                "UPDATE executions SET execution_id='bad-id' WHERE execution_id=?",
                (execution["execution_id"],),
            )
        elif case == "execution_timestamp":
            connection.execute(
                "UPDATE executions SET updated_at='2026-08-14T00:00:00.000000Z' "
                "WHERE execution_id=?",
                (execution["execution_id"],),
            )
        elif case == "execution_receipt_state":
            connection.execute(
                "UPDATE executions SET receipt_state='published', "
                "receipt_id=? WHERE execution_id=?",
                (
                    f"rcp_{store.authority.profile_key}_{'f' * 32}",
                    execution["execution_id"],
                ),
            )
        elif case == "decision_digest":
            connection.execute(
                "UPDATE decisions SET request_digest='bad' WHERE decision_id=?",
                (target_id,),
            )
        elif case == "decision_binding":
            connection.execute(
                "UPDATE decisions SET effect_id='effect:other' WHERE decision_id=?",
                (target_id,),
            )
        elif case == "receipt_digest":
            connection.execute(
                "UPDATE receipts SET evidence_digest='bad' WHERE receipt_id=?",
                (target_id,),
            )
        connection.commit()

    with pytest.raises(ContractDataError, match=message):
        if target == "execution":
            store.list_executions()
        elif target == "decision":
            store.get_decision(target_id)
        else:
            store.get_receipt(target_id)

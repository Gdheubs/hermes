import copy
import json

import pytest

from agent.background_review_evidence import (
    build_event_catalog,
    build_promotable_clusters,
    parse_failure_candidates,
    parse_reflection_decisions,
)


TRAJECTORY = "session-123"


def _messages():
    return [
        {"role": "user", "content": "Run the check; api_key=very-secret"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "bad-1",
                    "function": {
                        "name": "terminal",
                        "arguments": json.dumps(
                            {"command": "bad", "password": "also-secret"}
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "bad-1",
            "content": json.dumps({"success": False, "error": "bad flag"}),
        },
        {"role": "user", "content": "Use the literal-path flag instead."},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "good-1",
                    "function": {
                        "name": "terminal",
                        "arguments": json.dumps({"command": "fixed"}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "good-1",
            "content": json.dumps({"success": True, "message": "check passed"}),
        },
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "bad-2",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "bad-2",
            "content": json.dumps({"success": False, "error": "bad flag"}),
        },
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "good-2",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "good-2",
            "content": json.dumps({"success": True, "message": "check passed"}),
        },
    ]


def _catalog():
    return build_event_catalog(_messages())


def _candidate(
    *,
    signal_kind="user_correction",
    failure_events=None,
    correction_events=None,
    repair_events=None,
    verification_events=None,
    strategy="correct_arguments",
    pattern="filesystem:literal-path",
    trigger=None,
    persistence="persistent",
    status="verified",
    confidence=0.95,
    failure_counter=None,
    repair_counter=None,
    target_name="filesystem-workflows",
    rule="Use the literal-path flag for untrusted filesystem names.",
):
    if trigger is None:
        trigger = {
            "user_correction": "explicit_user_correction",
            "validated_technique": "verified_workflow",
            "failure": "tool_contract_mismatch",
        }[signal_kind]
    if failure_events is None:
        failure_events = ["evt-000003"]
    if correction_events is None:
        correction_events = (
            ["evt-000004"] if signal_kind == "user_correction" else []
        )
    if repair_events is None:
        repair_events = ["evt-000005"]
    if verification_events is None:
        verification_events = ["evt-000006"]
    return {
        "signal_kind": signal_kind,
        "domain_id": "filesystem",
        "failure": {
            "stage_id": "tool_execution",
            "mode_id": "invalid_arguments",
            "trigger_id": trigger,
            "persistence": persistence,
            "evidence_event_ids": failure_events,
            "correction_event_ids": correction_events,
            "counter_evidence_event_ids": failure_counter or [],
        },
        "repair": {
            "strategy_id": strategy,
            "pattern_id": pattern,
            "status": status,
            "evidence_event_ids": repair_events,
            "verification_event_ids": verification_events,
            "counter_evidence_event_ids": repair_counter or [],
        },
        "guidance": {
            "rule": rule,
            "applicability": "Filesystem operations with literal user paths.",
            "anti_pattern": "Do not infer that the tool itself is broken.",
        },
        "target": {"action": "patch_existing", "skill_name": target_name},
        "confidence": confidence,
    }


def _payload(*candidates, trajectory=TRAJECTORY):
    return json.dumps(
        {
            "schema_version": "failure-candidates.v1",
            "trajectory_id": trajectory,
            "candidates": list(candidates),
        }
    )


def _records(*candidates):
    return parse_failure_candidates(_payload(*candidates), _catalog(), TRAJECTORY)


def _decision(cluster, *, decision="accept", **overrides):
    value = {
        "cluster_id": cluster["cluster_id"],
        "decision": decision,
        "reason": "The later successful tool result independently verifies the fix.",
        "evidence_event_ids": [
            cluster["failure_anchor_event_ids"][0],
            cluster["repair_evidence_event_ids"][0],
            cluster["verification_event_ids"][0],
        ],
        "counter_evidence_event_ids": [],
        "repair_strategy_id": cluster["repair"]["strategy_id"],
        "repair_pattern_id": cluster["repair"]["pattern_id"],
        "target": cluster["target"],
    }
    value.update(overrides)
    return value


def _reflection_payload(*decisions):
    return json.dumps(
        {
            "schema_version": "failure-reflections.v1",
            "decisions": list(decisions),
        }
    )


def test_event_catalog_is_stable_bounded_and_does_not_copy_tool_arguments():
    messages = _messages()
    original = copy.deepcopy(messages)
    first = build_event_catalog(messages)
    second = build_event_catalog(messages)

    assert first == second
    assert messages == original
    assert [event["event_id"] for event in first] == [
        f"evt-{index:06d}" for index in range(1, len(first) + 1)
    ]
    assert [event["sequence"] for event in first] == list(range(len(first)))
    serialized = json.dumps(first)
    assert "very-secret" not in serialized
    assert "also-secret" not in serialized
    assert "[REDACTED]" in serialized
    assert "arguments" not in serialized
    assert first[1]["kind"] == "tool_call"
    assert first[2]["outcome"] == "failure"
    assert first[5]["outcome"] == "success"

    bounded = build_event_catalog(
        [{"role": "user", "content": f"message-{index}"} for index in range(600)]
    )
    assert len(bounded) == 192
    assert bounded[-1]["preview"] == "message-599"

    quoted = build_event_catalog(
        [
            {
                "role": "user",
                "content": '{"api_key":"json-secret"} password="env-secret"',
            }
        ]
    )
    assert "json-secret" not in quoted[0]["preview"]
    assert "env-secret" not in quoted[0]["preview"]
    assert quoted[0]["preview"].count("[REDACTED]") == 2


def test_event_catalog_understands_real_terminal_results_and_masked_success():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "failed", "function": {"name": "terminal", "arguments": "{}"}},
                {"id": "passed", "function": {"name": "terminal", "arguments": "{}"}},
                {"id": "masked", "function": {"name": "terminal", "arguments": "{}"}},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "failed",
            "content": json.dumps(
                {
                    "output": "failed",
                    "exit_code": 1,
                    "error": None,
                    "verification_evidence": {"status": "failed"},
                }
            ),
        },
        {
            "role": "tool",
            "tool_call_id": "passed",
            "content": json.dumps(
                {
                    "output": "ok",
                    "exit_code": 0,
                    "error": None,
                    "verification_evidence": {"status": "passed"},
                }
            ),
        },
        {
            "role": "tool",
            "tool_call_id": "masked",
            "content": json.dumps(
                {
                    "output": "error: build failed",
                    "exit_code": 0,
                    "error": None,
                    "hint": "Pipeline exit code may mask an upstream failure.",
                }
            ),
        },
    ]

    catalog = build_event_catalog(messages)

    assert catalog[3]["outcome"] == "failure"
    assert catalog[4]["outcome"] == "success"
    assert catalog[5]["outcome"] == "unknown"

    unverified_zero = build_event_catalog(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "plain",
                        "function": {
                            "name": "terminal",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "plain",
                "content": json.dumps(
                    {"output": "ok", "exit_code": 0, "error": None}
                ),
            },
        ]
    )
    assert unverified_zero[-1]["outcome"] == "unknown"

    exit_meanings = build_event_catalog(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "grep", "function": {"name": "terminal", "arguments": "{}"}},
                    {"id": "curl", "function": {"name": "terminal", "arguments": "{}"}},
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "grep",
                "content": json.dumps(
                    {
                        "output": "",
                        "exit_code": 1,
                        "error": None,
                        "exit_code_meaning": "No matches found (not an error)",
                    }
                ),
            },
            {
                "role": "tool",
                "tool_call_id": "curl",
                "content": json.dumps(
                    {
                        "output": "",
                        "exit_code": 6,
                        "error": None,
                        "exit_code_meaning": "Could not resolve host",
                    }
                ),
            },
        ]
    )
    assert exit_meanings[-2]["outcome"] == "unknown"
    assert exit_meanings[-1]["outcome"] == "failure"


def test_valid_candidate_gets_program_owned_deterministic_ids():
    first = _records(_candidate())[0]
    second = _records(_candidate())[0]

    assert first == second
    assert first["record_id"].startswith("fr1_")
    assert first["failure_cluster_key"].startswith("fc1_")
    assert first["memory_subject_id"].startswith("ms1_")
    assert first["eligible"] is True
    assert first["block_reasons"] == []
    assert first["normalized_subject"] == {
        "task_domain": "filesystem",
        "failure_scenario": {
            "stage": "tool_execution",
            "mode": "invalid_arguments",
            "trigger": "explicit_user_correction",
        },
        "repair_method": {
            "strategy": "correct_arguments",
            "pattern": "filesystem:literal-path",
        },
    }


def test_exact_json_fence_is_accepted_but_mixed_prose_is_rejected():
    body = _payload(_candidate())
    assert parse_failure_candidates(f"```json\n{body}\n```", _catalog(), TRAJECTORY)
    with pytest.raises(ValueError):
        parse_failure_candidates(f"candidate follows:\n{body}", _catalog(), TRAJECTORY)
    with pytest.raises(ValueError):
        parse_failure_candidates(f"{body}\nextra", _catalog(), TRAJECTORY)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda root: root.update({"unexpected": True}),
        lambda root: root.update({"trajectory_id": "wrong"}),
        lambda root: root["candidates"][0].update({"confidence": True}),
        lambda root: root["candidates"][0].update({"confidence": 1.1}),
        lambda root: root["candidates"][0]["failure"].update(
            {"mode_id": "made_up"}
        ),
        lambda root: root["candidates"][0]["failure"].update(
            {"mode_id": " invalid_arguments "}
        ),
        lambda root: root["candidates"][0]["failure"].update(
            {"evidence_event_ids": ["missing-event"]}
        ),
        lambda root: root["candidates"][0]["repair"].update(
            {"verification_event_ids": ["evt-000004"]}
        ),
        lambda root: root["candidates"][0]["repair"].update(
            {"verification_event_ids": ["evt-000007"]}
        ),
        lambda root: root["candidates"][0]["repair"].update(
            {"evidence_event_ids": ["evt-000003"]}
        ),
    ],
)
def test_invalid_candidate_payloads_fail_closed(mutate):
    root = json.loads(_payload(_candidate()))
    mutate(root)
    with pytest.raises(ValueError):
        parse_failure_candidates(json.dumps(root), _catalog(), TRAJECTORY)


def test_duplicate_keys_non_finite_and_candidate_limit_fail_closed():
    with pytest.raises(ValueError, match="duplicate"):
        parse_failure_candidates(
            '{"schema_version":"failure-candidates.v1",'
            '"schema_version":"failure-candidates.v1",'
            '"trajectory_id":"session-123","candidates":[]}',
            _catalog(),
            TRAJECTORY,
        )
    with pytest.raises(ValueError, match="non-finite"):
        parse_failure_candidates(
            _payload(_candidate()).replace("0.95", "NaN"), _catalog(), TRAJECTORY
        )
    with pytest.raises(ValueError, match="at most 8"):
        parse_failure_candidates(
            _payload(*[_candidate(confidence=0.8 + index / 100) for index in range(9)]),
            _catalog(),
            TRAJECTORY,
        )


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (_candidate(persistence="transient"), "transient_or_unknown"),
        (
            _candidate(status="proposed", verification_events=[]),
            "repair_unverified",
        ),
        (_candidate(confidence=0.79), "low_confidence"),
        (
            _candidate(failure_counter=["evt-000007"]),
            "counter_evidence",
        ),
    ],
)
def test_blocked_records_never_reach_promotion(candidate, reason):
    record = _records(candidate)[0]
    assert record["eligible"] is False
    assert reason in record["block_reasons"]
    assert build_promotable_clusters([record]) == []


def test_failure_key_ignores_repair_but_subject_id_splits_repairs():
    first = _records(_candidate(strategy="correct_arguments"))[0]
    second = _records(_candidate(strategy="add_validation"))[0]
    assert first["failure_cluster_key"] == second["failure_cluster_key"]
    assert first["memory_subject_id"] != second["memory_subject_id"]

    third = _records(_candidate(pattern="filesystem:validate-before-write"))[0]
    assert first["failure_cluster_key"] == third["failure_cluster_key"]
    assert first["memory_subject_id"] != third["memory_subject_id"]

    assert build_promotable_clusters([first, third]) == []


def test_plain_failure_needs_recurrence_but_correction_can_promote_once():
    plain = _records(
        _candidate(
            signal_kind="failure",
            failure_events=["evt-000003"],
        )
    )
    assert build_promotable_clusters(plain) == []

    repeated = _records(
        _candidate(signal_kind="failure"),
        _candidate(
            signal_kind="failure",
            failure_events=["evt-000008"],
            repair_events=["evt-000009"],
            verification_events=["evt-000010"],
        ),
    )
    clusters = build_promotable_clusters(repeated)
    assert len(clusters) == 1
    assert clusters[0]["support_count"] == 2

    correction = build_promotable_clusters(_records(_candidate()))
    assert len(correction) == 1
    assert correction[0]["cluster_id"].startswith("ms1_")


def test_evidence_order_cannot_forge_a_second_failure():
    first = _candidate(
        signal_kind="failure",
        failure_events=["evt-000003", "evt-000008"],
        repair_events=["evt-000009"],
        verification_events=["evt-000010"],
    )
    reordered = copy.deepcopy(first)
    reordered["failure"]["evidence_event_ids"] = list(
        reversed(reordered["failure"]["evidence_event_ids"])
    )

    with pytest.raises(ValueError, match="duplicate records"):
        _records(first, reordered)


def test_verified_repair_requires_failed_anchor_and_paired_success_result():
    ordinary_messages = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    fake = _candidate(
        signal_kind="failure",
        failure_events=["evt-000001"],
        repair_events=["evt-000002"],
        verification_events=["evt-000003"],
    )
    with pytest.raises(ValueError, match="failed tool results"):
        parse_failure_candidates(
            _payload(fake), build_event_catalog(ordinary_messages), TRAJECTORY
        )

    unpaired = _candidate(verification_events=["evt-000010"])
    with pytest.raises(ValueError, match="one successful result per repair tool call"):
        _records(unpaired)

    mixed = _candidate(verification_events=["evt-000006", "evt-000008"])
    with pytest.raises(ValueError, match="only successful tool results"):
        _records(mixed)

    unrelated_success = _candidate(
        verification_events=["evt-000006", "evt-000010"]
    )
    with pytest.raises(ValueError, match="one successful result per repair tool call"):
        _records(unrelated_success)


def test_signal_kind_and_guidance_cannot_borrow_support():
    without_correction = _candidate(correction_events=[])
    with pytest.raises(ValueError, match="must not be empty"):
        _records(without_correction)

    mismatched_trigger = _candidate(trigger="verified_workflow")
    with pytest.raises(ValueError, match="requires trigger"):
        _records(mismatched_trigger)

    polluted_technique = _candidate(
        signal_kind="validated_technique",
        correction_events=["evt-000004"],
    )
    with pytest.raises(ValueError, match="requires user_correction"):
        _records(polluted_technique)

    validated_once = _records(_candidate(signal_kind="validated_technique"))
    assert build_promotable_clusters(validated_once) == []

    variants = _records(
        _candidate(signal_kind="failure"),
        _candidate(
            signal_kind="failure",
            failure_events=["evt-000008"],
            repair_events=["evt-000009"],
            verification_events=["evt-000010"],
            rule="Retry with a larger timeout instead.",
        ),
    )
    assert build_promotable_clusters(variants) == []


def test_snapshot_local_event_ids_never_cluster_across_trajectories():
    first = _records(_candidate(signal_kind="failure"))[0]
    second_candidate = _candidate(
        signal_kind="failure",
        failure_events=["evt-000008"],
        repair_events=["evt-000009"],
        verification_events=["evt-000010"],
    )
    second = parse_failure_candidates(
        _payload(second_candidate, trajectory="another-session"),
        _catalog(),
        "another-session",
    )[0]

    assert build_promotable_clusters([first, second]) == []


def test_reflector_accepts_only_complete_evidence_bound_decisions():
    clusters = build_promotable_clusters(_records(_candidate()))
    accepted = parse_reflection_decisions(
        _reflection_payload(_decision(clusters[0])), clusters, _catalog()
    )
    assert len(accepted) == 1
    assert accepted[0]["reflection"]["decision"] == "accept"

    rejected = parse_reflection_decisions(
        _reflection_payload(
            _decision(
                clusters[0],
                decision="reject",
                evidence_event_ids=[],
                reason="The correction is too task-specific.",
            )
        ),
        clusters,
        _catalog(),
    )
    assert rejected == []


@pytest.mark.parametrize(
    "change",
    [
        {"counter_evidence_event_ids": ["evt-000004"]},
        {"evidence_event_ids": ["evt-000003"]},
        {"evidence_event_ids": ["evt-000003", "evt-000006"]},
        {"repair_strategy_id": "replan"},
        {"repair_pattern_id": "filesystem:different-repair"},
        {
            "target": {
                "action": "patch_existing",
                "skill_name": "different-skill",
            }
        },
    ],
)
def test_reflector_cannot_expand_or_contradict_promoted_cluster(change):
    clusters = build_promotable_clusters(_records(_candidate()))
    decision = _decision(clusters[0], **change)
    with pytest.raises(ValueError):
        parse_reflection_decisions(
            _reflection_payload(decision), clusters, _catalog()
        )


def test_reflector_must_cite_a_matching_repair_result_pair():
    records = _records(
        _candidate(signal_kind="failure"),
        _candidate(
            signal_kind="failure",
            failure_events=["evt-000008"],
            repair_events=["evt-000009"],
            verification_events=["evt-000010"],
        ),
    )
    cluster = build_promotable_clusters(records)[0]
    mismatched = _decision(
        cluster,
        evidence_event_ids=[
            "evt-000003",
            "evt-000005",
            "evt-000010",
        ],
    )

    with pytest.raises(ValueError, match="paired repair call"):
        parse_reflection_decisions(
            _reflection_payload(mismatched), [cluster], _catalog()
        )


def test_reflector_must_decide_every_cluster_exactly_once():
    clusters = build_promotable_clusters(_records(_candidate()))
    with pytest.raises(ValueError, match="omitted"):
        parse_reflection_decisions(_reflection_payload(), clusters, _catalog())
    duplicate = _decision(clusters[0])
    with pytest.raises(ValueError, match="duplicates"):
        parse_reflection_decisions(
            _reflection_payload(duplicate, duplicate), clusters, _catalog()
        )

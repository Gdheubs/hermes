import json

import pytest

import agent.background_review as background_review
from agent.background_review_evidence import (
    build_event_catalog,
    build_promotable_clusters,
    parse_failure_candidates,
)


TRAJECTORY = "review-session"
FULL_WHITELIST = {"memory", "skills_list", "skill_view", "skill_manage"}


def _snapshot():
    return [
        {"role": "user", "content": "Please update the file."},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "bad",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "bad",
            "content": json.dumps({"success": False, "error": "bad arguments"}),
        },
        {"role": "user", "content": "Use the literal-path form."},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "good",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "good",
            "content": json.dumps({"success": True, "message": "verified"}),
        },
    ]


def _candidate_payload(*, persistence="persistent"):
    return json.dumps(
        {
            "schema_version": "failure-candidates.v1",
            "trajectory_id": TRAJECTORY,
            "candidates": [
                {
                    "signal_kind": "user_correction",
                    "domain_id": "filesystem",
                    "failure": {
                        "stage_id": "tool_execution",
                        "mode_id": "invalid_arguments",
                        "trigger_id": "explicit_user_correction",
                        "persistence": persistence,
                        "evidence_event_ids": ["evt-000003"],
                        "correction_event_ids": ["evt-000004"],
                        "counter_evidence_event_ids": [],
                    },
                    "repair": {
                        "strategy_id": "correct_arguments",
                        "pattern_id": "filesystem:literal-path",
                        "status": "verified",
                        "evidence_event_ids": ["evt-000005"],
                        "verification_event_ids": ["evt-000006"],
                        "counter_evidence_event_ids": [],
                    },
                    "guidance": {
                        "rule": "Use literal-path operations for user paths.",
                        "applicability": "Filesystem operations on literal names.",
                        "anti_pattern": "Do not claim the tool is broken.",
                    },
                    "target": {
                        "action": "patch_existing",
                        "skill_name": "filesystem-workflows",
                    },
                    "confidence": 0.95,
                }
            ],
        }
    )


def _empty_payload():
    return json.dumps(
        {
            "schema_version": "failure-candidates.v1",
            "trajectory_id": TRAJECTORY,
            "candidates": [],
        }
    )


def _cluster():
    catalog = build_event_catalog(_snapshot())
    records = parse_failure_candidates(
        _candidate_payload(), catalog, TRAJECTORY
    )
    return build_promotable_clusters(records)[0]


def _reflection_payload(*, accept=True):
    cluster = _cluster()
    return json.dumps(
        {
            "schema_version": "failure-reflections.v1",
            "decisions": [
                {
                    "cluster_id": cluster["cluster_id"],
                    "decision": "accept" if accept else "reject",
                    "reason": (
                        "The successful result verifies the corrected call."
                        if accept
                        else "The procedure is too task-specific."
                    ),
                    "evidence_event_ids": (
                        [
                            cluster["failure_anchor_event_ids"][0],
                            cluster["repair_evidence_event_ids"][0],
                            cluster["verification_event_ids"][0],
                        ]
                        if accept
                        else []
                    ),
                    "counter_evidence_event_ids": [],
                    "repair_strategy_id": cluster["repair"]["strategy_id"],
                    "repair_pattern_id": cluster["repair"]["pattern_id"],
                    "target": cluster["target"],
                }
            ],
        }
    )


def _response(text, marker):
    return {
        "final_response": text,
        "messages": [{"role": "assistant", "content": marker}],
    }


class ScriptedReviewAgent:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self._session_messages = []
        # The helper must not mutate the schema-level tools collection.
        self.tools = ("parent-tool-schema",)

    def run_conversation(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        self._session_messages = list(result["messages"])
        return result


class WhitelistRecorder:
    def __init__(self):
        self.values = []
        self.clear_count = 0

    def set(self, value, **_kwargs):
        self.values.append(set(value))

    def clear(self):
        self.clear_count += 1


def _run(agent, recorder, *, review_memory=False):
    return background_review._run_staged_skill_review(
        review_agent=agent,
        messages_snapshot=_snapshot(),
        conversation_history=[{"role": "user", "content": "history"}],
        prompt="base review prompt",
        trajectory_id=TRAJECTORY,
        review_memory=review_memory,
        review_whitelist=set(FULL_WHITELIST),
        set_thread_tool_whitelist=recorder.set,
        clear_thread_tool_whitelist=recorder.clear,
    )


@pytest.mark.parametrize("generator_output", [_empty_payload(), "not json"])
def test_empty_or_invalid_candidates_stop_after_read_only_generator(generator_output):
    agent = ScriptedReviewAgent([_response(generator_output, "generator")])
    recorder = WhitelistRecorder()

    result = _run(agent, recorder)

    assert result == [{"role": "assistant", "content": "generator"}]
    assert len(agent.calls) == 1
    assert recorder.values == [{"skills_list", "skill_view"}]
    assert recorder.clear_count == 1
    assert "skill_manage" not in recorder.values[0]


def test_non_promotable_candidate_skips_reflector_and_curator():
    agent = ScriptedReviewAgent(
        [_response(_candidate_payload(persistence="transient"), "generator")]
    )
    recorder = WhitelistRecorder()

    _run(agent, recorder)

    assert len(agent.calls) == 1
    assert all("skill_manage" not in value for value in recorder.values)


@pytest.mark.parametrize("reflection_output", [_reflection_payload(accept=False), "bad"])
def test_rejected_or_invalid_reflection_never_opens_skill_writes(reflection_output):
    agent = ScriptedReviewAgent(
        [
            _response(_candidate_payload(), "generator"),
            _response(reflection_output, "reflector"),
        ]
    )
    recorder = WhitelistRecorder()

    result = _run(agent, recorder)

    assert result == [{"role": "assistant", "content": "reflector"}]
    assert len(agent.calls) == 2
    assert recorder.values == [
        {"skills_list", "skill_view"},
        {"skills_list", "skill_view"},
    ]
    assert all("skill_manage" not in value for value in recorder.values)


def test_accepted_cluster_reuses_one_fork_and_opens_writes_only_for_curator(
    monkeypatch,
):
    reset_calls = []
    scope_events = []
    monkeypatch.setattr(
        "tools.skill_manager_tool._reset_background_review_read_marks",
        lambda: reset_calls.append("reset"),
    )
    monkeypatch.setattr(
        "tools.skill_manager_tool._set_background_review_write_scope",
        lambda scope: scope_events.append(("set", set(scope))) or "scope-token",
    )
    monkeypatch.setattr(
        "tools.skill_manager_tool._reset_background_review_write_scope",
        lambda token: scope_events.append(("reset", token)),
    )
    generator_messages = [{"role": "assistant", "content": "generator"}]
    reflector_messages = [{"role": "assistant", "content": "reflector"}]
    curator_messages = [{"role": "assistant", "content": "curator"}]
    agent = ScriptedReviewAgent(
        [
            {
                "final_response": _candidate_payload(),
                "messages": generator_messages,
            },
            {
                "final_response": _reflection_payload(),
                "messages": reflector_messages,
            },
            {"final_response": "done", "messages": curator_messages},
        ]
    )
    original_tools = agent.tools
    recorder = WhitelistRecorder()

    result = _run(agent, recorder)

    assert result == curator_messages
    assert len(agent.calls) == 3
    assert agent.calls[1]["conversation_history"] == generator_messages
    assert agent.calls[2]["conversation_history"] == reflector_messages
    assert "PHASE 1" in agent.calls[0]["user_message"]
    assert "PHASE 2" in agent.calls[1]["user_message"]
    assert "PHASE 3" in agent.calls[2]["user_message"]
    assert recorder.values == [
        {"skills_list", "skill_view"},
        {"skills_list", "skill_view"},
        {"skills_list", "skill_view", "skill_manage"},
    ]
    assert recorder.clear_count == 3
    assert reset_calls == ["reset"]
    assert scope_events == [
        ("set", {("patch", "filesystem-workflows")}),
        ("reset", "scope-token"),
    ]
    assert agent.tools is original_tools


def test_combined_review_allows_memory_only_in_generator():
    agent = ScriptedReviewAgent(
        [
            _response(_candidate_payload(), "generator"),
            _response(_reflection_payload(), "reflector"),
            _response("done", "curator"),
        ]
    )
    recorder = WhitelistRecorder()

    _run(agent, recorder, review_memory=True)

    assert "memory" in recorder.values[0]
    assert "skill_manage" not in recorder.values[0]
    assert all("memory" not in value for value in recorder.values[1:])
    assert recorder.values[-1] == {"skills_list", "skill_view", "skill_manage"}


def test_spawn_passes_review_mode_flags_to_worker(monkeypatch):
    captured = {}

    def fake_worker(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(background_review, "_run_review_in_thread", fake_worker)
    agent = object()
    target, _ = background_review.spawn_background_review_thread(
        agent,
        [{"role": "user", "content": "hi"}],
        review_memory=True,
        review_skills=True,
        task_cfg={},
    )

    target()

    assert captured["kwargs"]["review_memory"] is True
    assert captured["kwargs"]["review_skills"] is True
    assert captured["kwargs"]["manual_refine"] is False


def test_spawn_keeps_explicit_refine_on_the_single_pass_path(monkeypatch):
    captured = {}

    def fake_worker(*args, **kwargs):
        captured["kwargs"] = kwargs

    monkeypatch.setattr(background_review, "_run_review_in_thread", fake_worker)
    target, _ = background_review.spawn_background_review_thread(
        object(),
        [{"role": "user", "content": "save this workflow"}],
        review_skills=True,
        focus="Save the successful workflow as a skill.",
        task_cfg={},
    )

    target()

    assert captured["kwargs"]["manual_refine"] is True

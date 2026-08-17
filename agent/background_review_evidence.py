"""Typed transcript-evidence gate for automatic background skill review.

This module is deliberately pure: it does not call an LLM, read configuration,
touch profile storage, or write skills.  The background-review harness supplies
one immutable conversation snapshot, the model proposes typed candidates, and
these helpers decide whether any candidate is structured and evidenced well
enough to be shown to the existing Curator.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Sequence


_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_CANDIDATES = 8
_MAX_DEPTH = 12
_MAX_EVIDENCE_IDS = 16
_MIN_CONFIDENCE = 0.8
_MAX_CATALOG_MESSAGES = 512
_MAX_CATALOG_EVENTS = 192

_DOMAIN_IDS = frozenset(
    {
        "software_engineering",
        "shell",
        "filesystem",
        "web_research",
        "browser_automation",
        "data_analysis",
        "document_work",
        "communication",
        "model_tooling",
        "memory_skill_management",
        "other",
        "unknown",
    }
)
_STAGE_IDS = frozenset(
    {
        "instruction_understanding",
        "planning",
        "tool_selection",
        "argument_construction",
        "tool_execution",
        "observation_interpretation",
        "reasoning",
        "artifact_generation",
        "verification",
        "delivery",
        "other",
        "unknown",
    }
)
_MODE_IDS = frozenset(
    {
        "omission",
        "wrong_action",
        "invalid_arguments",
        "execution_error",
        "timeout",
        "permission_denied",
        "unavailable_dependency",
        "stale_state",
        "incorrect_result",
        "incomplete_result",
        "unsupported_claim",
        "format_violation",
        "preference_mismatch",
        "regression",
        "other",
        "unknown",
    }
)
_TRIGGER_IDS = frozenset(
    {
        "ambiguous_instruction",
        "missing_context",
        "stale_context",
        "incorrect_assumption",
        "boundary_case",
        "tool_contract_mismatch",
        "unsupported_input_shape",
        "external_service",
        "resource_limit",
        "concurrency_race",
        "configuration_state",
        "malformed_model_output",
        "explicit_user_correction",
        "verified_workflow",
        "other",
        "unknown",
    }
)
_REPAIR_STRATEGY_IDS = frozenset(
    {
        "retry_same",
        "retry_backoff",
        "correct_arguments",
        "replan",
        "alternate_tool",
        "add_precondition",
        "add_validation",
        "narrow_scope",
        "fallback_path",
        "rollback",
        "request_clarification",
        "environment_fix",
        "adopt_user_preference",
        "minimal_patch",
        "other",
        "unknown",
    }
)
_SIGNAL_KINDS = frozenset(
    {"failure", "user_correction", "validated_technique"}
)
_PERSISTENCE_VALUES = frozenset({"persistent", "transient", "unknown"})
_REPAIR_STATUSES = frozenset({"proposed", "verified"})
_TARGET_ACTIONS = frozenset({"patch_existing", "create_umbrella"})
_DECISIONS = frozenset({"accept", "reject"})

_EXT_ID_RE = re.compile(
    r"^[a-z][a-z0-9_.-]{1,63}:[a-z0-9][a-z0-9_.\/-]{0,127}$"
)
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_JSON_FENCE_RE = re.compile(r"\A```json[ \t]*\r?\n([\s\S]*?)\r?\n```\Z")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|"
    r"client[_-]?secret|private[_-]?key|authorization|cookie)"
    r"([\"']?\s*[:=]\s*[\"']?)([^\s,;\"']+)"
)
_AUTH_HEADER_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+\-/=]+")
_URL_USERINFO_RE = re.compile(r"(?i)(https?://)[^/@\s]+@")
_URL_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|password|secret)=)"
    r"[^&#\s]+"
)
_BENIGN_EXIT_MEANING_MARKERS = (
    "not an error",
    "(expected",
    "often normal",
    "partial results may still be valid",
    "condition evaluated to false",
)


def _reject_duplicate_pairs(pairs: Sequence[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _json_depth(value: Any, depth: int = 0) -> int:
    if depth > _MAX_DEPTH:
        raise ValueError("JSON nesting exceeds limit")
    if isinstance(value, dict):
        for item in value.values():
            _json_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_depth(item, depth + 1)
    return depth


def _strict_json_object(raw: str) -> Dict[str, Any]:
    if not isinstance(raw, str):
        raise TypeError("model output must be text")
    if len(raw.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        raise ValueError("model output exceeds 64 KiB")
    text = raw.lstrip("\ufeff").strip()
    fenced = _JSON_FENCE_RE.fullmatch(text)
    if fenced is not None:
        text = fenced.group(1)
    elif text.startswith("```") or text.endswith("```"):
        raise ValueError("only one complete ```json fenced object is accepted")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_finite,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("top-level JSON value must be an object")
    _json_depth(value)
    return value


def _redact_text(value: str, max_chars: int = 180) -> str:
    text = value.replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    text = _AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)} [REDACTED]", text)
    text = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text
    )
    text = _URL_USERINFO_RE.sub(r"\1[REDACTED]@", text)
    text = _URL_SECRET_QUERY_RE.sub(r"\1[REDACTED]", text)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for item in content[:16]:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return " ".join(parts)


def _tool_result_summary(content: Any) -> tuple[str, str]:
    text = _message_text(content)
    outcome = "unknown"
    preview_source = text
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict):
        success = data.get("success")
        is_error = data.get("isError") is True
        error = data.get("error")
        exit_code = data.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            exit_code = None
        verification = data.get("verification_evidence")
        verification_status = (
            verification.get("status")
            if isinstance(verification, Mapping)
            and isinstance(verification.get("status"), str)
            else None
        )
        status = data.get("status") if isinstance(data.get("status"), str) else None
        masked_success = exit_code == 0 and bool(data.get("hint"))
        exit_meaning = data.get("exit_code_meaning")
        benign_nonzero = (
            isinstance(exit_meaning, str)
            and any(
                marker in exit_meaning.lower()
                for marker in _BENIGN_EXIT_MEANING_MARKERS
            )
        )

        if (
            success is False
            or is_error
            or verification_status == "failed"
            or (isinstance(error, str) and bool(error.strip()))
        ):
            outcome = "failure"
        elif exit_code is not None:
            if (
                exit_code == 0
                and not masked_success
                and (verification_status == "passed" or success is True)
            ):
                outcome = "success"
            elif exit_code != 0 and not benign_nonzero:
                outcome = "failure"
        elif verification_status == "passed" or success is True:
            outcome = "success"
        elif status in {"passed", "success", "succeeded", "completed", "ok"}:
            outcome = "success"
        elif status in {"failed", "failure", "error", "timeout", "cancelled"}:
            outcome = "failure"
        safe_parts: List[str] = []
        for key in ("message", "error", "status", "target", "exit_code"):
            item = data.get(key)
            if isinstance(item, (str, int, float, bool)):
                safe_parts.append(f"{key}={item}")
        if verification_status is not None:
            safe_parts.append(f"verification_status={verification_status}")
        if masked_success:
            safe_parts.append("masked_success_hint=true")
        preview_source = "; ".join(safe_parts) or f"result outcome={outcome}"
    return outcome, _redact_text(preview_source)


def build_event_catalog(messages: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Build stable event references without copying tool arguments.

    Event IDs depend only on snapshot order.  Previews are bounded and redact
    common credential shapes; tool calls expose their name but never arguments.
    """
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise TypeError("messages must be a sequence")
    events: List[Dict[str, Any]] = []
    tool_names: Dict[str, str] = {}

    def append_event(**fields: Any) -> None:
        sequence = len(events)
        events.append(
            {
                "event_id": f"evt-{sequence + 1:06d}",
                "sequence": sequence,
                **fields,
            }
        )

    # Reviews need recent repair evidence, not an unbounded second transcript.
    # IDs remain stable for the selected tail and the returned catalog is also
    # event-bounded below.
    for message in list(messages)[-_MAX_CATALOG_MESSAGES:]:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role") if isinstance(message.get("role"), str) else "unknown"
        content = _message_text(message.get("content"))
        if role == "user":
            append_event(
                kind="user_message",
                role=role,
                outcome="neutral",
                preview=_redact_text(content),
            )
        elif role == "assistant":
            if content:
                append_event(
                    kind="assistant_message",
                    role=role,
                    outcome="neutral",
                    preview=_redact_text(content),
                )
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for call in tool_calls[:32]:
                    if not isinstance(call, Mapping):
                        continue
                    fn = call.get("function")
                    fn = fn if isinstance(fn, Mapping) else {}
                    name = fn.get("name") if isinstance(fn.get("name"), str) else "unknown"
                    call_id = call.get("id") if isinstance(call.get("id"), str) else ""
                    if call_id:
                        tool_names[call_id] = name
                    append_event(
                        kind="tool_call",
                        role=role,
                        outcome="neutral",
                        tool_name=name,
                        tool_call_id=call_id or None,
                        preview=f"tool={name}",
                    )
        elif role == "tool":
            call_id = (
                message.get("tool_call_id")
                if isinstance(message.get("tool_call_id"), str)
                else ""
            )
            outcome, preview = _tool_result_summary(message.get("content"))
            append_event(
                kind="tool_result",
                role=role,
                outcome=outcome,
                tool_name=tool_names.get(call_id, "unknown"),
                tool_call_id=call_id or None,
                preview=preview,
            )
        elif content:
            append_event(
                kind="system_message" if role == "system" else "other_message",
                role=role,
                outcome="neutral",
                preview=_redact_text(content),
            )
    return events[-_MAX_CATALOG_EVENTS:]


def _expect_keys(
    value: Mapping[str, Any],
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
    path: str,
) -> None:
    required_set = set(required)
    allowed = required_set.union(optional)
    missing = required_set.difference(value)
    unknown = set(value).difference(allowed)
    if missing:
        raise ValueError(f"{path} missing fields: {sorted(missing)}")
    if unknown:
        raise ValueError(f"{path} has unknown fields: {sorted(unknown)}")


def _bounded_string(value: Any, *, path: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    if not value.strip() or len(value) > max_length:
        raise ValueError(f"{path} must contain 1..{max_length} characters")
    return value.strip()


def _enum(value: Any, allowed: frozenset[str], *, path: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError(f"{path} must be an exact taxonomy string")
    if value not in allowed:
        raise ValueError(f"{path} has unsupported value: {value}")
    return value


def _extension(
    owner: Mapping[str, Any],
    *,
    id_field: str,
    ext_field: str,
    path: str,
) -> str | None:
    identifier = owner[id_field]
    extension = owner.get(ext_field)
    if identifier == "other":
        if not isinstance(extension, str) or not _EXT_ID_RE.fullmatch(extension):
            raise ValueError(f"{path}.{ext_field} must be a namespaced id")
        return extension
    if ext_field in owner:
        raise ValueError(f"{path}.{ext_field} is only valid when {id_field}=other")
    return None


def _event_ids(value: Any, *, path: str, required: bool = False) -> List[str]:
    if not isinstance(value, list) or len(value) > _MAX_EVIDENCE_IDS:
        raise ValueError(f"{path} must be a list with at most {_MAX_EVIDENCE_IDS} ids")
    if required and not value:
        raise ValueError(f"{path} must not be empty")
    result: List[str] = []
    for index, item in enumerate(value):
        result.append(_bounded_string(item, path=f"{path}[{index}]", max_length=64))
    if len(result) != len(set(result)):
        raise ValueError(f"{path} contains duplicate ids")
    return result


def _canonical_hash(prefix: str, value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = base64.b32encode(hashlib.sha256(payload).digest()).decode("ascii")
    return f"{prefix}_{digest.rstrip('=').lower()}"


def _effective(identifier: str, extension: str | None) -> str:
    return identifier if identifier != "other" else f"other:{extension}"


def _catalog_index(catalog: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    if not isinstance(catalog, Sequence) or isinstance(catalog, (str, bytes)):
        raise TypeError("event catalog must be a sequence")
    result: Dict[str, Dict[str, Any]] = {}
    tool_calls: set[str] = set()
    tool_results: set[str] = set()
    previous_sequence = -1
    for index, event in enumerate(catalog):
        if not isinstance(event, Mapping):
            raise ValueError(f"event catalog item {index} must be an object")
        event_id = event.get("event_id")
        sequence = event.get("sequence")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError(f"event catalog item {index} has no event_id")
        if event_id in result:
            raise ValueError(f"duplicate event catalog id: {event_id}")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValueError(f"event {event_id} has invalid sequence")
        if sequence <= previous_sequence:
            raise ValueError("event catalog sequence must be strictly increasing")
        call_id = event.get("tool_call_id")
        if isinstance(call_id, str) and call_id:
            if event.get("kind") == "tool_call":
                if call_id in tool_calls:
                    raise ValueError(f"duplicate tool-call id in catalog: {call_id}")
                tool_calls.add(call_id)
            elif event.get("kind") == "tool_result":
                if call_id in tool_results:
                    raise ValueError(f"duplicate tool-result id in catalog: {call_id}")
                tool_results.add(call_id)
        previous_sequence = sequence
        result[event_id] = dict(event)
    return result


def _resolve_event_ids(
    ids: Sequence[str],
    *,
    event_index: Mapping[str, Mapping[str, Any]],
    path: str,
) -> List[Mapping[str, Any]]:
    unresolved = [event_id for event_id in ids if event_id not in event_index]
    if unresolved:
        raise ValueError(f"{path} references unknown event ids: {unresolved}")
    return [event_index[event_id] for event_id in ids]


def _parse_target(value: Any, *, path: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")
    _expect_keys(value, required={"action", "skill_name"}, path=path)
    action = _enum(value["action"], _TARGET_ACTIONS, path=f"{path}.action")
    skill_name = value["skill_name"]
    if skill_name is not None:
        if not isinstance(skill_name, str) or not _SKILL_NAME_RE.fullmatch(skill_name):
            raise ValueError(
                f"{path}.skill_name must be null or a lowercase kebab-case name"
            )
    return {"action": action, "skill_name": skill_name}


def _parse_candidate(
    value: Any,
    *,
    index: int,
    trajectory_id: str,
    event_index: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    path = f"$.candidates[{index}]"
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")
    _expect_keys(
        value,
        required={
            "signal_kind",
            "domain_id",
            "failure",
            "repair",
            "guidance",
            "target",
            "confidence",
        },
        optional={"domain_ext_id"},
        path=path,
    )
    signal_kind = _enum(
        value["signal_kind"], _SIGNAL_KINDS, path=f"{path}.signal_kind"
    )
    domain_id = _enum(value["domain_id"], _DOMAIN_IDS, path=f"{path}.domain_id")
    domain_ext_id = _extension(
        value,
        id_field="domain_id",
        ext_field="domain_ext_id",
        path=path,
    )

    failure_raw = value["failure"]
    if not isinstance(failure_raw, Mapping):
        raise ValueError(f"{path}.failure must be an object")
    _expect_keys(
        failure_raw,
        required={
            "stage_id",
            "mode_id",
            "trigger_id",
            "persistence",
            "evidence_event_ids",
            "correction_event_ids",
            "counter_evidence_event_ids",
        },
        optional={"stage_ext_id", "mode_ext_id", "trigger_ext_id"},
        path=f"{path}.failure",
    )
    stage_id = _enum(
        failure_raw["stage_id"], _STAGE_IDS, path=f"{path}.failure.stage_id"
    )
    mode_id = _enum(
        failure_raw["mode_id"], _MODE_IDS, path=f"{path}.failure.mode_id"
    )
    trigger_id = _enum(
        failure_raw["trigger_id"],
        _TRIGGER_IDS,
        path=f"{path}.failure.trigger_id",
    )
    persistence = _enum(
        failure_raw["persistence"],
        _PERSISTENCE_VALUES,
        path=f"{path}.failure.persistence",
    )
    stage_ext_id = _extension(
        failure_raw,
        id_field="stage_id",
        ext_field="stage_ext_id",
        path=f"{path}.failure",
    )
    mode_ext_id = _extension(
        failure_raw,
        id_field="mode_id",
        ext_field="mode_ext_id",
        path=f"{path}.failure",
    )
    trigger_ext_id = _extension(
        failure_raw,
        id_field="trigger_id",
        ext_field="trigger_ext_id",
        path=f"{path}.failure",
    )
    failure_evidence = _event_ids(
        failure_raw["evidence_event_ids"],
        path=f"{path}.failure.evidence_event_ids",
        required=True,
    )
    correction_evidence = _event_ids(
        failure_raw["correction_event_ids"],
        path=f"{path}.failure.correction_event_ids",
        required=signal_kind == "user_correction",
    )
    failure_counter = _event_ids(
        failure_raw["counter_evidence_event_ids"],
        path=f"{path}.failure.counter_evidence_event_ids",
    )

    repair_raw = value["repair"]
    if not isinstance(repair_raw, Mapping):
        raise ValueError(f"{path}.repair must be an object")
    _expect_keys(
        repair_raw,
        required={
            "strategy_id",
            "pattern_id",
            "status",
            "evidence_event_ids",
            "verification_event_ids",
            "counter_evidence_event_ids",
        },
        optional={"strategy_ext_id"},
        path=f"{path}.repair",
    )
    strategy_id = _enum(
        repair_raw["strategy_id"],
        _REPAIR_STRATEGY_IDS,
        path=f"{path}.repair.strategy_id",
    )
    pattern_id = _bounded_string(
        repair_raw["pattern_id"], path=f"{path}.repair.pattern_id", max_length=192
    )
    if not _EXT_ID_RE.fullmatch(pattern_id):
        raise ValueError(f"{path}.repair.pattern_id must be a namespaced id")
    repair_status = _enum(
        repair_raw["status"], _REPAIR_STATUSES, path=f"{path}.repair.status"
    )
    strategy_ext_id = _extension(
        repair_raw,
        id_field="strategy_id",
        ext_field="strategy_ext_id",
        path=f"{path}.repair",
    )
    repair_evidence = _event_ids(
        repair_raw["evidence_event_ids"],
        path=f"{path}.repair.evidence_event_ids",
        required=repair_status == "verified",
    )
    verification_evidence = _event_ids(
        repair_raw["verification_event_ids"],
        path=f"{path}.repair.verification_event_ids",
        required=repair_status == "verified",
    )
    repair_counter = _event_ids(
        repair_raw["counter_evidence_event_ids"],
        path=f"{path}.repair.counter_evidence_event_ids",
    )
    if repair_status == "proposed" and verification_evidence:
        raise ValueError(f"{path}.repair proposed repair cannot claim verification")

    guidance_raw = value["guidance"]
    if not isinstance(guidance_raw, Mapping):
        raise ValueError(f"{path}.guidance must be an object")
    _expect_keys(
        guidance_raw,
        required={"rule", "applicability", "anti_pattern"},
        path=f"{path}.guidance",
    )
    guidance = {
        "rule": _bounded_string(
            guidance_raw["rule"], path=f"{path}.guidance.rule", max_length=1000
        ),
        "applicability": _bounded_string(
            guidance_raw["applicability"],
            path=f"{path}.guidance.applicability",
            max_length=500,
        ),
        "anti_pattern": _bounded_string(
            guidance_raw["anti_pattern"],
            path=f"{path}.guidance.anti_pattern",
            max_length=500,
        ),
    }
    target = _parse_target(value["target"], path=f"{path}.target")
    confidence_raw = value["confidence"]
    if (
        isinstance(confidence_raw, bool)
        or not isinstance(confidence_raw, (int, float))
        or not math.isfinite(float(confidence_raw))
        or not 0 <= float(confidence_raw) <= 1
    ):
        raise ValueError(f"{path}.confidence must be a finite number in [0, 1]")
    confidence = float(confidence_raw)

    evidence_groups = {
        "failure": failure_evidence,
        "correction": correction_evidence,
        "failure_counter": failure_counter,
        "repair": repair_evidence,
        "verification": verification_evidence,
        "repair_counter": repair_counter,
    }
    all_ids = [item for group in evidence_groups.values() for item in group]
    if len(all_ids) != len(set(all_ids)):
        raise ValueError(f"{path} reuses an event across evidence roles")
    resolved = {
        name: _resolve_event_ids(
            ids, event_index=event_index, path=f"{path}.{name}"
        )
        for name, ids in evidence_groups.items()
    }
    # Evidence-list order is model-controlled and must not affect record IDs,
    # recurrence, or cluster membership. Canonicalize every role by the
    # program-owned catalog sequence before deriving any hashes.
    for name, events in resolved.items():
        events.sort(key=lambda event: int(event["sequence"]))
        evidence_groups[name] = [str(event["event_id"]) for event in events]
    failure_evidence = evidence_groups["failure"]
    correction_evidence = evidence_groups["correction"]
    failure_counter = evidence_groups["failure_counter"]
    repair_evidence = evidence_groups["repair"]
    verification_evidence = evidence_groups["verification"]
    repair_counter = evidence_groups["repair_counter"]

    failure_anchors = list(resolved["failure"])
    if any(
        event.get("kind") != "tool_result" or event.get("outcome") != "failure"
        for event in failure_anchors
    ):
        raise ValueError(
            f"{path} failure evidence must contain only failed tool results"
        )

    if signal_kind == "user_correction":
        if trigger_id != "explicit_user_correction":
            raise ValueError(
                f"{path} user_correction requires trigger explicit_user_correction"
            )
        corrections = resolved["correction"]
        if any(event.get("kind") != "user_message" for event in corrections):
            raise ValueError(
                f"{path} correction evidence must contain only user messages"
            )
        if min(int(correction["sequence"]) for correction in corrections) <= max(
            int(anchor["sequence"]) for anchor in failure_anchors
        ):
            raise ValueError(
                f"{path} user_correction needs a later user-message correction"
            )
    elif signal_kind == "validated_technique":
        if correction_evidence:
            raise ValueError(f"{path} correction evidence requires user_correction")
        if trigger_id != "verified_workflow":
            raise ValueError(
                f"{path} validated_technique requires trigger verified_workflow"
            )
    else:
        if correction_evidence:
            raise ValueError(f"{path} correction evidence requires user_correction")
        if trigger_id in {"explicit_user_correction", "verified_workflow"}:
            raise ValueError(f"{path} signal_kind does not match its trigger")

    if repair_evidence:
        prior_events = resolved["failure"] + resolved["correction"]
        if max(event["sequence"] for event in prior_events) >= min(
            event["sequence"] for event in resolved["repair"]
        ):
            raise ValueError(f"{path} repair evidence must follow failure evidence")
        if any(
            event.get("kind") != "tool_call"
            or not isinstance(event.get("tool_call_id"), str)
            or not event.get("tool_call_id")
            for event in resolved["repair"]
        ):
            raise ValueError(
                f"{path} repair evidence must contain only identified tool calls"
            )
    if verification_evidence:
        if max(event["sequence"] for event in resolved["repair"]) >= min(
            event["sequence"] for event in resolved["verification"]
        ):
            raise ValueError(
                f"{path} verification evidence must follow repair evidence"
            )
        repair_calls = {
            str(event["tool_call_id"]): event.get("tool_name")
            for event in resolved["repair"]
            if event.get("kind") == "tool_call"
            and isinstance(event.get("tool_call_id"), str)
            and event.get("tool_call_id")
        }
        verified_results = {
            str(event["tool_call_id"]): event.get("tool_name")
            for event in resolved["verification"]
            if event.get("kind") == "tool_result"
            and event.get("outcome") == "success"
            and isinstance(event.get("tool_call_id"), str)
            and event.get("tool_call_id")
        }
        if len(verified_results) != len(resolved["verification"]):
            raise ValueError(
                f"{path} verification evidence must contain only successful tool results"
            )
        if not repair_calls:
            raise ValueError(f"{path} repair evidence needs a tool call")
        paired_call_ids = {
            call_id
            for call_id, tool_name in repair_calls.items()
            if verified_results.get(call_id) == tool_name
        }
        if set(verified_results) != set(repair_calls) or not paired_call_ids:
            raise ValueError(
                f"{path} verification needs one successful result per repair tool call"
            )

    failure = {
        "stage_id": stage_id,
        "mode_id": mode_id,
        "trigger_id": trigger_id,
        "persistence": persistence,
        "evidence_event_ids": failure_evidence,
        "correction_event_ids": correction_evidence,
        "counter_evidence_event_ids": failure_counter,
    }
    for field, extension in (
        ("stage_ext_id", stage_ext_id),
        ("mode_ext_id", mode_ext_id),
        ("trigger_ext_id", trigger_ext_id),
    ):
        if extension is not None:
            failure[field] = extension
    repair = {
        "strategy_id": strategy_id,
        "pattern_id": pattern_id,
        "status": repair_status,
        "evidence_event_ids": repair_evidence,
        "verification_event_ids": verification_evidence,
        "counter_evidence_event_ids": repair_counter,
    }
    if strategy_ext_id is not None:
        repair["strategy_ext_id"] = strategy_ext_id

    failure_dimensions = [
        _effective(domain_id, domain_ext_id),
        _effective(stage_id, stage_ext_id),
        _effective(mode_id, mode_ext_id),
        _effective(trigger_id, trigger_ext_id),
    ]
    failure_cluster_key = _canonical_hash("fc1", failure_dimensions)
    memory_subject_id = None
    if repair_status == "verified":
        memory_subject_id = _canonical_hash(
            "ms1",
            [
                failure_cluster_key,
                _effective(strategy_id, strategy_ext_id),
                pattern_id,
            ],
        )
    normalized_subject = {
        "task_domain": failure_dimensions[0],
        "failure_scenario": {
            "stage": failure_dimensions[1],
            "mode": failure_dimensions[2],
            "trigger": failure_dimensions[3],
        },
        "repair_method": {
            "strategy": _effective(strategy_id, strategy_ext_id),
            "pattern": pattern_id,
        },
    }

    block_reasons: List[str] = []
    taxonomy_ids = (domain_id, stage_id, mode_id, trigger_id, strategy_id)
    if "unknown" in taxonomy_ids:
        block_reasons.append("unknown_taxonomy")
    if "other" in taxonomy_ids:
        block_reasons.append("unsupported_extension")
    if persistence != "persistent":
        block_reasons.append("transient_or_unknown")
    if failure_counter or repair_counter:
        block_reasons.append("counter_evidence")
    if repair_status != "verified":
        block_reasons.append("repair_unverified")
    if confidence < _MIN_CONFIDENCE:
        block_reasons.append("low_confidence")
    if target["skill_name"] is None:
        block_reasons.append("missing_target")

    normalized = {
        "schema_version": "failure-record.v1",
        "trajectory_id": trajectory_id,
        "signal_kind": signal_kind,
        "domain_id": domain_id,
        "failure": failure,
        "repair": repair,
        "guidance": guidance,
        "target": target,
        "confidence": confidence,
        "normalized_subject": normalized_subject,
        "failure_cluster_key": failure_cluster_key,
        "memory_subject_id": memory_subject_id,
        "failure_anchor_event_ids": [
            str(event["event_id"])
            for event in sorted(failure_anchors, key=lambda event: int(event["sequence"]))
        ],
        "block_reasons": sorted(set(block_reasons)),
    }
    if domain_ext_id is not None:
        normalized["domain_ext_id"] = domain_ext_id
    record_payload = dict(normalized)
    record_payload.pop("block_reasons", None)
    normalized["record_id"] = _canonical_hash("fr1", record_payload)
    normalized["eligible"] = not normalized["block_reasons"]
    return normalized


def parse_failure_candidates(
    raw: str,
    event_catalog: Sequence[Mapping[str, Any]],
    trajectory_id: str,
) -> List[Dict[str, Any]]:
    """Strictly parse and validate one ``failure-candidates.v1`` payload."""
    trajectory_id = _bounded_string(
        trajectory_id, path="trajectory_id", max_length=256
    )
    root = _strict_json_object(raw)
    _expect_keys(
        root,
        required={"schema_version", "trajectory_id", "candidates"},
        path="$",
    )
    if root["schema_version"] != "failure-candidates.v1":
        raise ValueError("unsupported failure candidate schema version")
    if root["trajectory_id"] != trajectory_id:
        raise ValueError("candidate trajectory_id does not match the review")
    candidates = root["candidates"]
    if not isinstance(candidates, list) or len(candidates) > _MAX_CANDIDATES:
        raise ValueError(f"candidates must be a list with at most {_MAX_CANDIDATES} items")
    event_index = _catalog_index(event_catalog)
    records = [
        _parse_candidate(
            candidate,
            index=index,
            trajectory_id=trajectory_id,
            event_index=event_index,
        )
        for index, candidate in enumerate(candidates)
    ]
    record_ids = [record["record_id"] for record in records]
    if len(record_ids) != len(set(record_ids)):
        raise ValueError("candidate payload contains duplicate records")
    return records


def build_promotable_clusters(
    records: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Group eligible records by normalized failure+repair subject.

    This function intentionally accepts records from one trajectory only;
    event IDs are snapshot-local and cannot safely be merged across catalogs.
    A plain failure needs two independent failed tool results. An explicit user
    correction or a verified successful technique may promote once because the
    correction/verification itself is the second signal.  Duplicate model
    outputs cannot satisfy recurrence because the parser rejects record-id
    duplicates before this function runs.
    """
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise TypeError("records must be a sequence")
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    evidence_subjects: Dict[tuple[Any, ...], set[str]] = defaultdict(set)
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("record must be an object")
        if record.get("eligible") is not True:
            continue
        subject_id = record.get("memory_subject_id")
        if not isinstance(subject_id, str) or not subject_id.startswith("ms1_"):
            continue
        grouped[subject_id].append(record)
        evidence_signature = (
            str(record.get("trajectory_id")),
            tuple(record.get("failure_anchor_event_ids", [])),
            tuple(record["repair"].get("evidence_event_ids", [])),
            tuple(record["repair"].get("verification_event_ids", [])),
        )
        evidence_subjects[evidence_signature].add(subject_id)

    # One verified chain cannot be relabeled as several repair subjects. If
    # the model emits competing pattern IDs for identical source evidence, the
    # semantic normalization is ambiguous and every affected subject fails
    # closed instead of producing several writes from one episode.
    ambiguous_subjects = {
        subject_id
        for subject_ids in evidence_subjects.values()
        if len(subject_ids) > 1
        for subject_id in subject_ids
    }

    result: List[Dict[str, Any]] = []
    for subject_id in sorted(grouped):
        if subject_id in ambiguous_subjects:
            continue
        group = grouped[subject_id]
        record_ids = {record.get("record_id") for record in group}
        if None in record_ids or len(record_ids) != len(group):
            raise ValueError("promotable records must have unique record ids")
        failure_anchor_ids = sorted(
            {
                event_id
                for record in group
                for event_id in record.get("failure_anchor_event_ids", [])
            }
        )
        singleton_exception = any(
            record.get("signal_kind") == "user_correction"
            and bool(record["failure"].get("correction_event_ids"))
            for record in group
        )
        required_anchor_count = 1 if singleton_exception else 2
        if len(failure_anchor_ids) < required_anchor_count:
            continue

        failure_keys = {record.get("failure_cluster_key") for record in group}
        strategies = {record["repair"].get("strategy_id") for record in group}
        patterns = {record["repair"].get("pattern_id") for record in group}
        guidances = {
            json.dumps(record["guidance"], sort_keys=True, separators=(",", ":"))
            for record in group
        }
        targets = {
            json.dumps(record["target"], sort_keys=True, separators=(",", ":"))
            for record in group
        }
        trajectory_ids = {str(record["trajectory_id"]) for record in group}
        if (
            len(failure_keys) != 1
            or len(strategies) != 1
            or len(patterns) != 1
            or len(guidances) != 1
            or len(targets) != 1
            or len(trajectory_ids) != 1
        ):
            continue

        representative = sorted(
            group,
            key=lambda record: (-float(record["confidence"]), str(record["record_id"])),
        )[0]
        failure_evidence = sorted(
            {
                event_id
                for record in group
                for event_id in record["failure"]["evidence_event_ids"]
            }
        )
        repair_evidence = sorted(
            {
                event_id
                for record in group
                for event_id in record["repair"]["evidence_event_ids"]
            }
        )
        verification_evidence = sorted(
            {
                event_id
                for record in group
                for event_id in record["repair"]["verification_event_ids"]
            }
        )
        cluster = {
            # A reflection decision is per failure+verified-repair subject, not
            # merely per failure. This keeps two competing fixes from sharing
            # a decision or satisfying each other's recurrence.
            "cluster_id": subject_id,
            "failure_cluster_key": next(iter(failure_keys)),
            "memory_subject_id": subject_id,
            "normalized_subject": dict(representative["normalized_subject"]),
            "domain_id": representative["domain_id"],
            "failure": {
                "stage_id": representative["failure"]["stage_id"],
                "mode_id": representative["failure"]["mode_id"],
                "trigger_id": representative["failure"]["trigger_id"],
                "persistence": "persistent",
            },
            "repair": {
                "strategy_id": representative["repair"]["strategy_id"],
                "pattern_id": representative["repair"]["pattern_id"],
                "status": "verified",
            },
            "guidance": dict(representative["guidance"]),
            "target": dict(representative["target"]),
            "failure_evidence_event_ids": failure_evidence,
            "failure_anchor_event_ids": failure_anchor_ids,
            "repair_evidence_event_ids": repair_evidence,
            "verification_event_ids": verification_evidence,
            "record_ids": sorted(str(record_id) for record_id in record_ids),
            "trajectory_ids": sorted(trajectory_ids),
            "support_count": len(failure_anchor_ids),
            "candidate_count": len(group),
            "confidence": min(float(record["confidence"]) for record in group),
        }
        result.append(cluster)
    return result


def parse_reflection_decisions(
    raw: str,
    clusters: Sequence[Mapping[str, Any]],
    event_catalog: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Validate ``failure-reflections.v1`` and return accepted clusters only."""
    if not isinstance(clusters, Sequence) or isinstance(clusters, (str, bytes)):
        raise TypeError("clusters must be a sequence")
    cluster_index: Dict[str, Mapping[str, Any]] = {}
    for cluster in clusters:
        if not isinstance(cluster, Mapping):
            raise ValueError("cluster must be an object")
        cluster_id = cluster.get("cluster_id")
        if not isinstance(cluster_id, str) or cluster_id in cluster_index:
            raise ValueError("clusters must have unique string ids")
        cluster_index[cluster_id] = cluster

    root = _strict_json_object(raw)
    _expect_keys(root, required={"schema_version", "decisions"}, path="$")
    if root["schema_version"] != "failure-reflections.v1":
        raise ValueError("unsupported reflection schema version")
    decisions = root["decisions"]
    if not isinstance(decisions, list) or len(decisions) > _MAX_CANDIDATES:
        raise ValueError(
            f"decisions must be a list with at most {_MAX_CANDIDATES} items"
        )
    event_index = _catalog_index(event_catalog)
    seen: set[str] = set()
    accepted: List[Dict[str, Any]] = []

    for index, decision_raw in enumerate(decisions):
        path = f"$.decisions[{index}]"
        if not isinstance(decision_raw, Mapping):
            raise ValueError(f"{path} must be an object")
        _expect_keys(
            decision_raw,
            required={
                "cluster_id",
                "decision",
                "reason",
                "evidence_event_ids",
                "counter_evidence_event_ids",
                "repair_strategy_id",
                "repair_pattern_id",
                "target",
            },
            path=path,
        )
        cluster_id = _bounded_string(
            decision_raw["cluster_id"], path=f"{path}.cluster_id", max_length=80
        )
        if cluster_id not in cluster_index:
            raise ValueError(f"{path} references an unknown cluster")
        if cluster_id in seen:
            raise ValueError(f"{path} duplicates a cluster decision")
        seen.add(cluster_id)
        decision = _enum(
            decision_raw["decision"], _DECISIONS, path=f"{path}.decision"
        )
        reason = _bounded_string(
            decision_raw["reason"], path=f"{path}.reason", max_length=500
        )
        evidence_ids = _event_ids(
            decision_raw["evidence_event_ids"],
            path=f"{path}.evidence_event_ids",
            required=decision == "accept",
        )
        counter_ids = _event_ids(
            decision_raw["counter_evidence_event_ids"],
            path=f"{path}.counter_evidence_event_ids",
        )
        if set(evidence_ids).intersection(counter_ids):
            raise ValueError(f"{path} reuses evidence as counter-evidence")
        resolved_evidence = _resolve_event_ids(
            evidence_ids, event_index=event_index, path=f"{path}.evidence_event_ids"
        )
        resolved_counter = _resolve_event_ids(
            counter_ids,
            event_index=event_index,
            path=f"{path}.counter_evidence_event_ids",
        )
        resolved_evidence.sort(key=lambda event: int(event["sequence"]))
        resolved_counter.sort(key=lambda event: int(event["sequence"]))
        evidence_ids = [str(event["event_id"]) for event in resolved_evidence]
        counter_ids = [str(event["event_id"]) for event in resolved_counter]
        strategy = _enum(
            decision_raw["repair_strategy_id"],
            _REPAIR_STRATEGY_IDS,
            path=f"{path}.repair_strategy_id",
        )
        pattern_id = _bounded_string(
            decision_raw["repair_pattern_id"],
            path=f"{path}.repair_pattern_id",
            max_length=192,
        )
        if not _EXT_ID_RE.fullmatch(pattern_id):
            raise ValueError(f"{path}.repair_pattern_id must be a namespaced id")
        target = _parse_target(decision_raw["target"], path=f"{path}.target")
        cluster = cluster_index[cluster_id]
        if strategy != cluster["repair"]["strategy_id"]:
            raise ValueError(f"{path} changes the verified repair strategy")
        if pattern_id != cluster["repair"]["pattern_id"]:
            raise ValueError(f"{path} changes the normalized repair pattern")
        if target != cluster["target"]:
            raise ValueError(f"{path} changes the proposed curation target")

        if decision == "accept":
            if counter_ids:
                raise ValueError(f"{path} cannot accept unresolved counter-evidence")
            allowed_evidence = set(cluster["failure_evidence_event_ids"])
            allowed_evidence.update(cluster["repair_evidence_event_ids"])
            allowed_evidence.update(cluster["verification_event_ids"])
            if not set(evidence_ids).issubset(allowed_evidence):
                raise ValueError(f"{path} cites evidence outside the cluster")
            if not set(evidence_ids).intersection(cluster["failure_anchor_event_ids"]):
                raise ValueError(f"{path} must cite a failed tool result")
            if not set(evidence_ids).intersection(cluster["repair_evidence_event_ids"]):
                raise ValueError(f"{path} must cite the repair tool call")
            if not set(evidence_ids).intersection(cluster["verification_event_ids"]):
                raise ValueError(f"{path} must cite verified repair evidence")
            cited_repair_calls = {
                str(event["tool_call_id"]): event.get("tool_name")
                for event in resolved_evidence
                if event.get("kind") == "tool_call"
                and isinstance(event.get("tool_call_id"), str)
                and event.get("tool_call_id")
            }
            cited_results = {
                str(event["tool_call_id"]): event.get("tool_name")
                for event in resolved_evidence
                if event.get("kind") == "tool_result"
                and event.get("outcome") == "success"
                and isinstance(event.get("tool_call_id"), str)
                and event.get("tool_call_id")
            }
            if not any(
                cited_results.get(call_id) == tool_name
                for call_id, tool_name in cited_repair_calls.items()
            ):
                raise ValueError(
                    f"{path} must cite a paired repair call and successful result"
                )
            accepted_cluster = dict(cluster)
            accepted_cluster["reflection"] = {
                "decision": "accept",
                "reason": reason,
                "evidence_event_ids": evidence_ids,
                "counter_evidence_event_ids": [],
            }
            accepted.append(accepted_cluster)

    if seen != set(cluster_index):
        missing = sorted(set(cluster_index).difference(seen))
        raise ValueError(f"reflection omitted cluster decisions: {missing}")
    return sorted(accepted, key=lambda cluster: cluster["cluster_id"])


__all__ = [
    "build_event_catalog",
    "parse_failure_candidates",
    "build_promotable_clusters",
    "parse_reflection_decisions",
]

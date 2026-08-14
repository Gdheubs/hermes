"""Advisory consecutive-repeat tool-call reminder (loop hygiene).

Detects consecutive identical tool calls — same tool name plus canonically
identical arguments — and appends a soft, advisory reminder to the CURRENT
tool result when the run length hits a configured threshold. It never
blocks, never vetoes, and never touches tool schemas.

How it works
------------
- Identity: ``(tool_name, canonical_tool_args(args))``. Argument
  canonicalization reuses ``agent.tool_guardrails.canonical_tool_args``
  (compact JSON with recursively sorted keys), so two calls that differ
  only in property order are identical for the detector.
- Chain: one consecutive-run chain per agent instance (the long-lived
  ``AIAgent`` the gateway caches across turns — i.e. per session). A call
  with a different name or canonical arguments resets the chain to 1; an
  identical call increments it. ``reset()`` clears the chain and is called
  when a REAL user message arrives (a new user turn at the top of
  ``run_conversation``, or a mid-turn user correction via
  ``_apply_active_turn_redirect``) — repetition across user input is not
  a loop.
- Thresholds: when the run length hits a configured threshold, a reminder
  is appended to the tail of the current tool result. The first threshold
  gets the gentle tier; later thresholds get the detailed tier naming the
  tool, the run length, and a capped preview of the canonical arguments.
- Injection: the reminder is appended at the very END of the current tool
  result (after untrusted-content wrapping inside
  ``make_tool_result_message``), so the cached conversation prefix stays
  byte-identical and the reminder is never wrapped as untrusted data.

Advisory contract
-----------------
- Never raises: every failure path degrades to "no reminder".
- Never blocks or vetoes tool execution; there is no hard-stop option.
- The chain is an in-memory attribute on the agent instance
  (``_repeat_tool_reminder_state``), never persisted.

Config (config.yaml, ``repeat_tool_reminder`` section)
------------------------------------------------------
    repeat_tool_reminder:
      enabled: true                # master switch (advisory: safe by default)
      thresholds: [3, 5, 8]        # run lengths that trigger a reminder
      include: []                  # wildcard patterns; empty = track every tool
      exclude: []                  # wildcard patterns; matched tools are transparent
      arguments_preview_chars: 500 # cap for the args preview in detailed reminders

``include``/``exclude`` entries are ``*``-wildcard predicates over tool
names (``*`` matches any run of characters; every other character matches
literally). ``exclude`` wins over ``include``. Untracked calls are
transparent: they neither count nor reset the chain.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Mapping, Optional

from agent.tool_guardrails import canonical_tool_args

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLDS = [3, 5, 8]
DEFAULT_PREVIEW_CHARS = 500

# Stable, documented prefix for every reminder this guard emits — the same
# bracket-tag family as the tool-loop-guardrail warnings.
_REMINDER_TAG = "[reminder]"

# Serializes chain mutation across concurrent tool-execution worker threads.
_LOCK = threading.Lock()

# Per-agent chain state attribute (lazily created; see ``_state``).
_STATE_ATTR = "_repeat_tool_reminder_state"


def canonicalize_arguments(value: Any) -> str:
    """Return the canonical string identity for parsed tool arguments.

    Delegates to ``agent.tool_guardrails.canonical_tool_args`` (compact
    JSON with recursively sorted keys — deep key sort), so argument objects
    that differ only in property order canonicalize identically. Non-mapping
    input canonicalizes as ``{}`` (the identity used for calls whose
    arguments could not be parsed).
    """
    return canonical_tool_args(value if isinstance(value, Mapping) else {})


def wildcard_to_regex(pattern: str) -> "re.Pattern[str]":
    """Compile one ``*``-wildcard pattern to an anchored regex.

    ``*`` matches any run of characters; every other regex metacharacter
    is escaped and matched literally.
    """
    escaped = re.escape(pattern).replace("\\*", ".*")
    return re.compile(f"^{escaped}$")


def _tool_is_tracked(tool_name: str, include, exclude) -> bool:
    """Whether a tool participates in the chain.

    Untracked calls are transparent: they neither count nor reset.
    ``exclude`` wins over ``include``; an empty ``include`` means every
    tool is tracked.
    """
    if include and not any(p.match(tool_name) for p in include):
        return False
    if any(p.match(tool_name) for p in exclude):
        return False
    return True


def _preview_arguments(canonical: str, cap: int) -> str:
    """Head-truncate canonical arguments for quoting in detailed reminders.

    Bounds only the model-visible preview — the chain identity always
    compares the FULL canonical string.
    """
    if len(canonical) <= cap:
        return canonical
    return f"{canonical[:cap]}… (+{len(canonical) - cap} more chars)"


def gentle_reminder(tool_name: str, count: int) -> str:
    """The gentle first-threshold reminder.

    Keyed to the first configured threshold (not a literal count) by the
    caller, so a custom first threshold keeps the gentle→detailed
    escalation.
    """
    return (
        f'{_REMINDER_TAG} You have called "{tool_name}" with identical '
        f"arguments {count} times in a row. Carefully analyze the previous "
        "result before calling again: if the task is not complete, try a "
        "different approach or different arguments instead of repeating "
        "the call."
    )


def detailed_reminder(
    tool_name: str,
    count: int,
    canonical_arguments: str,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
) -> str:
    """The detailed later-threshold reminder naming the tool, the run
    length, and a capped preview of the canonical arguments."""
    return (
        f'{_REMINDER_TAG} You have called "{tool_name}" with identical '
        f"arguments {count} times in a row.\n"
        f"- consecutive_calls: {count}\n"
        f"- arguments: {_preview_arguments(canonical_arguments, preview_chars)}\n"
        "The repeated calls are not making progress. Do not call this tool "
        "with these exact arguments again. Inspect the latest result and "
        "choose a different action, different arguments, or finish the task "
        "if enough evidence has been gathered."
    )


def _resolve_config(config: Any) -> dict:
    """Normalize the ``repeat_tool_reminder`` config section, fail-safe.

    Invalid values fall back to defaults; the result never raises. An
    explicitly empty ``thresholds`` list means "never remind".
    """
    if not isinstance(config, Mapping):
        return {
            "enabled": True,
            "thresholds": list(DEFAULT_THRESHOLDS),
            "include": [],
            "exclude": [],
            "preview_chars": DEFAULT_PREVIEW_CHARS,
        }
    thresholds = []
    for value in config.get("thresholds", DEFAULT_THRESHOLDS):
        if isinstance(value, bool) or not isinstance(value, int) or value < 2:
            continue
        thresholds.append(value)
    thresholds = sorted(set(thresholds))
    if "thresholds" not in config:
        thresholds = thresholds or list(DEFAULT_THRESHOLDS)
    enabled = config.get("enabled", True)
    preview = config.get("arguments_preview_chars", DEFAULT_PREVIEW_CHARS)
    return {
        "enabled": enabled if isinstance(enabled, bool) else True,
        "thresholds": thresholds,
        "include": [s for s in config.get("include", []) if isinstance(s, str)],
        "exclude": [s for s in config.get("exclude", []) if isinstance(s, str)],
        "preview_chars": (
            preview
            if isinstance(preview, int) and not isinstance(preview, bool) and preview >= 1
            else DEFAULT_PREVIEW_CHARS
        ),
    }


def _read_config() -> Mapping[str, Any]:
    """Read the live ``repeat_tool_reminder`` config section.

    Uses the cached read-only config loader; any failure (no config file,
    malformed YAML, missing section) degrades to an empty mapping so the
    module falls back to defaults.
    """
    try:
        from hermes_cli.config import load_config_readonly

        section = load_config_readonly().get("repeat_tool_reminder")
        return section if isinstance(section, Mapping) else {}
    except Exception:
        return {}


def _state(agent: Any) -> dict:
    """Lazily create the agent's chain state: the last tracked call's
    identity key and its run length."""
    state = getattr(agent, _STATE_ATTR, None)
    if state is None:
        state = {"key": None, "count": 0}
        setattr(agent, _STATE_ATTR, state)
    return state


def maybe_remind(agent: Any, tool_name: str, args: Any, config: Any = None) -> Optional[str]:
    """Advance the agent's consecutive-repeat chain for one tool call.

    Returns the reminder text to append to the CURRENT tool result when
    the run length hits a configured threshold, else ``None``. Purely
    advisory: never raises and never blocks — any failure degrades to
    ``None``. ``config`` may be injected for tests; production reads the
    live ``repeat_tool_reminder`` section.
    """
    try:
        cfg = _resolve_config(config if config is not None else _read_config())
        if not cfg["enabled"] or not cfg["thresholds"]:
            return None
        include = [wildcard_to_regex(p) for p in cfg["include"]]
        exclude = [wildcard_to_regex(p) for p in cfg["exclude"]]
        if not _tool_is_tracked(tool_name, include, exclude):
            return None
        canonical = canonicalize_arguments(args)
        key = (tool_name, canonical)
        with _LOCK:
            state = _state(agent)
            count = state["count"] + 1 if state["key"] == key else 1
            state["key"] = key
            state["count"] = count
        thresholds = cfg["thresholds"]
        if count not in thresholds:
            return None
        if count == thresholds[0]:
            return gentle_reminder(tool_name, count)
        return detailed_reminder(tool_name, count, canonical, cfg["preview_chars"])
    except Exception:
        logger.debug("repeat_tool_reminder suppressed (advisory)", exc_info=True)
        return None


def reset(agent: Any) -> None:
    """Clear the agent's consecutive-repeat chain.

    Called when a REAL user message arrives (a new user turn, or a mid-turn
    user correction) — repetition across user input is not a loop. Never
    raises.
    """
    try:
        with _LOCK:
            setattr(agent, _STATE_ATTR, {"key": None, "count": 0})
    except Exception:
        pass

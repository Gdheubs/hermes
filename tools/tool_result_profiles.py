"""Tool-aware relevance filtering for tool results before context injection.

``tool_output.*`` caps tool results by *size*; this module caps them by
*relevance*. Each tool type declares a result-handling profile (the
``tool_result_profiles`` section in ``config.yaml``) describing which subset
of its output the agent is likely to need. The filter runs on the result
string right before it is appended to the conversation context, so the
context window is filled with the informative part of a tool result instead
of a raw blob.

Supported modes (``mode`` per tool in ``tool_result_profiles.tools``):

- ``full`` — passthrough (the current behavior; also the behavior for any
  tool without a profile). Compatible with existing installs.
- ``bounded_matches`` — search-like results. Keeps the first N matches and
  the last N matches of the verbose ``matches`` array (the densified
  path-grouped ``matches_text`` block is trimmed by *lines*, since it has
  no per-match boundaries), summarizes the middle, re-serializes the JSON
  envelope.
- ``tail_or_head`` — large read_file pages. Keeps the head and tail lines
  (function defs at the top, the answer near the bottom), drops the middle.
  Small reads pass through untouched (``full_if_under_chars``).
- ``summary`` — patch/write_file style results. The agent already knows what
  it asked for; keep the compact JSON envelope (success/files/targets)
  and drop verbose diff bodies.
- ``smart_tail`` — terminal output. Keeps the head + tail of the ``output``
  field inside the JSON wrapper (banner/version at the top, errors at the
  bottom) while leaving all other result metadata (exit_code, spill paths,
  truncation notes) intact.

Design invariants:

- **Fail-open.** Every filter is defensive: malformed input, unknown tools,
  disabled config, or an unexpected result shape all pass the content
  through unchanged. A relevance filter must never break a tool result.
- **Complement, not replacement.** The mode filter runs before the size
  caps and persistence thresholds, so it can only *shrink* what enters
  context — never raise it above what ``tool_output`` already allows.
- **Full output stays available where it already is.** Tools that write
  their own spill references (e.g. the terminal tool's
  ``full_output_path``) do so before the result reaches the filter and
  are untouched by it. Executor-level persistence runs *after* the filter,
  so a result that still exceeds ``tool_output`` thresholds is spilled in
  its filtered form.
- **Process-lifetime cache.** Profiles are read from config once and cached
  (same pattern as ``tools/tool_output_limits.py``); tests reset the cache
  via ``_reset_tool_result_profiles_cache()``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

# Fail-safe defaults matching hermes_cli/config_defaults.py. Used only if
# config can't be read at all; load_config() normally supplies this exact
# section (deep-merged over user overrides).
DEFAULT_PROFILES: Dict[str, Any] = {
    "enabled": True,
    "tools": {
        "search_files": {
            "mode": "bounded_matches",
            "first_matches": 5,
            "last_matches": 5,
            "middle_summary": "{omitted} additional matches omitted — use a narrower pattern or offset to page through them",
            "middle_summary_lines": "{omitted} additional lines omitted — use a narrower pattern or offset to page through them",
        },
        "read_file": {
            "mode": "tail_or_head",
            "head_lines": 50,
            "tail_lines": 100,
            "full_if_under_chars": 4000,
        },
        "patch": {
            "mode": "summary",
            "keep_keys": [],
            "deny_keys": [],
        },
        "write_file": {
            "mode": "summary",
            "keep_keys": [],
            "deny_keys": [],
        },
        "terminal": {
            "mode": "smart_tail",
            "head_lines": 50,
            "tail_lines": 100,
        },
    },
}

_MODES = frozenset({"full", "bounded_matches", "tail_or_head", "summary", "smart_tail"})

# Keys a patch/write_file ``summary`` profile keeps from the result JSON.
# Everything else (diff bodies, echoed content, verbose metadata) is dropped
# because the agent already knows what it asked the tool to do.
_SUMMARY_KEEP_KEYS = frozenset({
    "success", "error", "status", "message", "note",
    "files_modified", "resolved_path", "warning", "_warning", "_omitted",
})

# search_files emits a trailing "\n\n[Hint: Results truncated. Use
# offset=... to see more, ...]" suffix after the JSON payload. Parse the
# JSON part and re-append the hint, because after filtering the hint is
# *more* important (the model should page for the omitted middle).
_SEARCH_HINT_RE = re.compile(r"\n\n\[Hint:.*$", re.DOTALL)

# Module-level cache — populated on first call.
_cached_profiles: dict | None = None


def _coerce_int(value: Any, default: int) -> int:
    """Return ``value`` as a non-negative int, or ``default`` on any issue."""
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return default
    if iv < 0:
        return default
    return iv


def _coerce_bool(value: Any, default: bool) -> bool:
    """Return ``value`` as a bool, tolerating common YAML truthiness."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return bool(value)
    except Exception:
        return default


def _coerce_str_list(value: Any) -> list[str]:
    """Return ``value`` as a list of strings, dropping non-string entries.

    Used for per-tool ``keep_keys``/``deny_keys`` overrides. Anything that
    is not a list (or a list with garbage in it) resolves to ``[]`` — a
    config typo must never raise during profile resolution.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _middle_note(template: str, omitted: int, suffix: str = "") -> str:
    """Render the omitted-middle summary line from a config template."""
    try:
        text = template.replace("{omitted}", str(omitted)).strip()
    except Exception:
        text = f"{omitted} lines omitted by the relevance filter"
    return f"... [{text}{suffix}] ..."


def get_tool_result_profiles() -> Dict[str, Any]:
    """Return resolved tool-result profiles, reading ``tool_result_profiles``
    from config.

    Structure: ``{"enabled": bool, "tools": {tool_name: {"mode": str,
    ...params}}``. Unknown modes are coerced to ``full`` so a typo in
    config degrades to current behavior instead of failing. This function
    NEVER raises, and caches for the process lifetime — call
    ``_reset_tool_result_profiles_cache()`` in tests that need a fresh read.
    """
    global _cached_profiles
    if _cached_profiles is not None:
        return _cached_profiles
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        section = cfg.get("tool_result_profiles") if isinstance(cfg, dict) else None
        if not isinstance(section, dict):
            section = {}
    except Exception:
        section = {}

    resolved: Dict[str, Any] = {
        "enabled": _coerce_bool(section.get("enabled"), DEFAULT_PROFILES["enabled"]),
        "tools": {},
    }
    raw_tools = section.get("tools")
    if not isinstance(raw_tools, dict):
        raw_tools = DEFAULT_PROFILES["tools"]
    for tool_name, raw_profile in raw_tools.items():
        if not isinstance(raw_profile, dict):
            continue
        mode = raw_profile.get("mode", "full")
        if mode not in _MODES:
            mode = "full"
        profile: Dict[str, Any] = {"mode": mode}
        if mode == "bounded_matches":
            defaults = DEFAULT_PROFILES["tools"].get(tool_name, {})
            profile["first_matches"] = _coerce_int(
                raw_profile.get("first_matches"), defaults.get("first_matches", 5)
            )
            profile["last_matches"] = _coerce_int(
                raw_profile.get("last_matches"), defaults.get("last_matches", 5)
            )
            profile["middle_summary"] = str(
                raw_profile.get("middle_summary")
                or defaults.get(
                    "middle_summary",
                    "{omitted} additional matches omitted — use a narrower pattern or offset to page through them",
                )
            )
            profile["middle_summary_lines"] = str(
                raw_profile.get("middle_summary_lines")
                or defaults.get(
                    "middle_summary_lines",
                    "{omitted} additional lines omitted — use a narrower pattern or offset to page through them",
                )
            )
        elif mode == "summary":
            # Per-tool keep/deny escapes: ``keep_keys`` are preserved in
            # addition to the default set; ``deny_keys`` are dropped even
            # if a default would keep them. Lets new result fields surface
            # (or legacy ones be hidden) without a code change.
            profile["keep_keys"] = _coerce_str_list(raw_profile.get("keep_keys"))
            profile["deny_keys"] = _coerce_str_list(raw_profile.get("deny_keys"))
        elif mode in ("tail_or_head", "smart_tail"):
            defaults = DEFAULT_PROFILES["tools"].get(tool_name, {})
            profile["head_lines"] = _coerce_int(
                raw_profile.get("head_lines"), defaults.get("head_lines", 50)
            )
            profile["tail_lines"] = _coerce_int(
                raw_profile.get("tail_lines"), defaults.get("tail_lines", 100)
            )
            if mode == "tail_or_head":
                profile["full_if_under_chars"] = _coerce_int(
                    raw_profile.get("full_if_under_chars"),
                    defaults.get("full_if_under_chars", 4000),
                )
        resolved["tools"][tool_name] = profile
    _cached_profiles = resolved
    return _cached_profiles


def _reset_tool_result_profiles_cache() -> None:
    """Reset the cached profiles — for tests or after a config hot-reload."""
    global _cached_profiles
    _cached_profiles = None


def _looks_like_json(content: str) -> bool:
    """True when a result string begins with a JSON object/array token."""
    stripped = content.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def _filter_bounded_matches(content: str, profile: Dict[str, Any]) -> str:
    """search_files: keep first N + last N matches, summarize the middle."""
    first_n = _coerce_int(profile.get("first_matches"), 5)
    last_n = _coerce_int(profile.get("last_matches"), 5)
    if first_n <= 0 and last_n <= 0:
        return content
    middle_summary = profile.get("middle_summary", "")
    middle_summary_lines = profile.get("middle_summary_lines", "")
    if not middle_summary_lines:
        middle_summary_lines = middle_summary

    payload = content
    hint = ""
    hint_match = _SEARCH_HINT_RE.search(content)
    if hint_match:
        hint = hint_match.group(0)
        payload = content[: hint_match.start()]

    try:
        data = json.loads(payload)
    except Exception:
        return content
    if not isinstance(data, dict):
        return content

    omitted = 0
    changed = False
    matches = data.get("matches")
    if isinstance(matches, list) and len(matches) > first_n + last_n:
        total = len(matches)
        data["matches"] = matches[:first_n] + matches[-last_n:]
        omitted = total - len(data["matches"])
        data["truncated"] = True
        data["_relevance"] = {
            "mode": "bounded_matches",
            "omitted": omitted,
            "total": total,
        }
        changed = True

    matches_text = data.get("matches_text")
    if isinstance(matches_text, str):
        lines = matches_text.split("\n")
        if len(lines) > first_n + last_n:
            omitted = len(lines) - first_n - last_n
            note = _middle_note(middle_summary_lines, omitted)
            data["matches_text"] = "\n".join(
                lines[:first_n] + [note] + lines[-last_n:]
            )
            data["truncated"] = True
            data["_relevance"] = {
                "mode": "bounded_matches",
                "omitted_lines": omitted,
            }
            changed = True

    if not changed:
        return content
    return json.dumps(data, ensure_ascii=False) + hint


def _filter_tail_or_head(content: str, profile: Dict[str, Any]) -> str:
    """read_file: keep head + tail of large pages, pass small ones through."""
    if len(content) <= _coerce_int(profile.get("full_if_under_chars"), 4000):
        return content
    if _looks_like_json(content):
        # Error/note results (e.g. unsupported file) are not line-numbered
        # pages — leave them intact.
        try:
            json.loads(content)
            return content
        except Exception:
            pass
    head = _coerce_int(profile.get("head_lines"), 50)
    tail = _coerce_int(profile.get("tail_lines"), 100)
    if head <= 0 and tail <= 0:
        return content
    lines = content.split("\n")
    if len(lines) <= head + tail:
        return content
    omitted = len(lines) - head - tail
    note = _middle_note(
        "{omitted} lines omitted by the relevance filter — use offset/limit to page",
        omitted,
        suffix="",
    )
    return "\n".join(lines[:head] + [note] + lines[-tail:])


def _filter_summary(content: str, profile: Dict[str, Any]) -> str:
    """patch/write_file: keep the compact JSON envelope, drop diffs.

    The keep/deny sets are the built-in defaults plus any per-tool
    ``keep_keys``/``deny_keys`` from config, so new result fields can
    surface without a code change (and noisy ones can be suppressed).
    """
    if not _looks_like_json(content):
        return content
    try:
        data = json.loads(content)
    except Exception:
        return content
    if not isinstance(data, dict):
        return content
    keep_set = set(_SUMMARY_KEEP_KEYS)
    keep_set |= set(profile.get("keep_keys") or ())
    keep_set -= set(profile.get("deny_keys") or ())
    keep = {k: v for k, v in data.items() if k in keep_set}
    dropped = sorted(set(data.keys()) - keep_set)
    if not dropped:
        return content
    keep["_relevance"] = {"mode": "summary", "dropped_keys": dropped}
    return json.dumps(keep, ensure_ascii=False)


def _filter_smart_tail(content: str, profile: Dict[str, Any]) -> str:
    """terminal: keep head + tail of the ``output`` field, all metadata."""
    if not _looks_like_json(content):
        return content
    try:
        data = json.loads(content)
    except Exception:
        return content
    if not isinstance(data, dict):
        return content
    output = data.get("output")
    if not isinstance(output, str):
        return content
    head = _coerce_int(profile.get("head_lines"), 50)
    tail = _coerce_int(profile.get("tail_lines"), 100)
    if head <= 0 and tail <= 0:
        return content
    lines = output.split("\n")
    if len(lines) <= head + tail:
        return content
    omitted = len(lines) - head - tail
    spill_path = data.get("full_output_path")
    suffix = f" full output: {spill_path}" if isinstance(spill_path, str) and spill_path else ""
    note = _middle_note(
        "{omitted} lines of output omitted by the relevance filter",
        omitted,
        suffix=suffix,
    )
    data["output"] = "\n".join(lines[:head] + [note] + lines[-tail:])
    data["relevance_note"] = f"{omitted} lines trimmed from output"
    return json.dumps(data, ensure_ascii=False)


def apply_tool_result_filter(tool_name: str, content: Any) -> Any:
    """Relevance-filter a tool result before it enters the agent context.

    Fail-open by design: any error, unknown tool, disabled config, or
    unparseable result shape returns ``content`` unchanged, so tool
    execution is never affected by the config.
    """
    if not isinstance(content, str):
        return content
    try:
        profiles = get_tool_result_profiles()
        if not profiles.get("enabled", True):
            return content
        profile = profiles.get("tools", {}).get(tool_name)
        if not isinstance(profile, dict):
            return content
        mode = profile.get("mode", "full")
        if mode == "bounded_matches":
            return _filter_bounded_matches(content, profile)
        if mode == "tail_or_head":
            return _filter_tail_or_head(content, profile)
        if mode == "summary":
            return _filter_summary(content, profile)
        if mode == "smart_tail":
            return _filter_smart_tail(content, profile)
    except Exception:
        pass
    return content
"""Wire-time context economy (from Whale internal/compact).

Compacts oversized tool results (head+tail+marker) on non-Anthropic-schema
wires + echo family; strips plain-turn reasoning where proven; exposes
raw-vs-replay stats and a post-compaction preflight estimate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.message_sanitization import matches_reasoning_echo_family
from agent.model_metadata import estimate_messages_tokens_rough, estimate_tokens_rough

# Thresholds mirror Whale internal/compact/compact.go.
MAX_TOOL_RESULT_REPLAY_TOKENS = 2000
MAX_TOOL_RESULT_REPLAY_CHARS = 12 * 1024
COMPACTED_TOOL_RESULT_KEEP_RUNES = 3000


@dataclass(frozen=True)
class ReplayCompactionLimits:
    """Per-provider compaction thresholds (config ``replay_compaction``)."""

    max_tokens: int
    max_chars: int
    keep_runes: int


def _coerce_pos_int(value: Any, default: int) -> int:
    # Reject bool (subclasses int) so `true` can't set a 1-token threshold.
    if isinstance(value, bool):
        return default
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return default
    return iv if iv > 0 else default


def replay_compaction_limits(provider: str | None = None) -> ReplayCompactionLimits:
    """Resolve per-provider thresholds from config (fresh each call).

    load_config() re-reads on config-file change (mtime-keyed cache), so
    limits follow live config edits instead of freezing at first use.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        cfg = {}
    section = cfg.get("replay_compaction") if isinstance(cfg, dict) else None
    section = section if isinstance(section, dict) else {}
    overrides = section.get("provider_overrides")
    overrides = overrides if isinstance(overrides, dict) else {}
    merged = dict(section)
    if provider:
        provider_ov = overrides.get(provider.strip().lower())
        if isinstance(provider_ov, dict):
            merged.update(provider_ov)
    return ReplayCompactionLimits(
        max_tokens=_coerce_pos_int(merged.get("max_tokens"), MAX_TOOL_RESULT_REPLAY_TOKENS),
        max_chars=_coerce_pos_int(merged.get("max_chars"), MAX_TOOL_RESULT_REPLAY_CHARS),
        keep_runes=_coerce_pos_int(merged.get("keep_runes"), COMPACTED_TOOL_RESULT_KEEP_RUNES),
    )


_REPLAY_MARKER_TEMPLATE = (
    "[tool result compacted for model replay]\n"
    "original_estimated_tokens={tokens} original_chars={chars} "
    "retained_head_runes={head} retained_tail_runes={tail}\n"
    "Full raw tool result remains in Hermes session history; this provider "
    "replay is abbreviated.\n\n"
    "--- head ---\n{head_text}\n\n"
    "--- omitted ---\n[... omitted {omitted} runes from tool result replay ...]\n\n"
    "--- tail ---\n{tail_text}"
)


def is_deepseek_replay_target(provider: str | None, model: str | None, base_url: str | None) -> bool:
    """DeepSeek-family endpoints (echo-back rule table)."""
    return matches_reasoning_echo_family("deepseek", provider, model, base_url)


def _plain_turn_strip_allowed(
    provider: str | None, model: str | None, base_url: str | None
) -> bool:
    """May plain (non-tool-call) assistant turns omit reasoning_content?

    PROVEN: DeepSeek (Whale client) + MiMo (official docs: only tool-call
    rounds pass it back —
    https://mimo.mi.com/docs/en-US/quick-start/usage-guide/text-generation/deep-thinking).
    PROVEN must-keep: Kimi/Moonshot — the official docs require preserving
    reasoning_content from every previous call ("keep the assistant message
    (including reasoning_content) from every previous API call"; preserved
    thinking is always on for kimi-k3; platform.kimi.com/docs/guide/
    use-thinking-models); openclaw/openclaw#92396 backfills Kimi — stripping
    would break the contract (server discards reasoning_content, silent pauses
    per can1357/oh-my-pi).
    KEPT (enforced): Qwen max/plus lines — hermes sends
    ``parameters.preserve_thinking=true`` on the wire (the quality contract:
    the historical reasoning is always consumed). The parameter is
    per-request; a user setting it false would make the reasoning strippable
    (the strip gate must align before any strip applies).
    """
    if is_deepseek_replay_target(provider, model, base_url):
        return True
    return matches_reasoning_echo_family("mimo", provider, model, base_url)


def tool_result_replay_content(
    content: str, limits: "ReplayCompactionLimits | None" = None
) -> str:
    """Small results verbatim; oversized ones -> head+tail+marker."""
    max_tokens = limits.max_tokens if limits is not None else MAX_TOOL_RESULT_REPLAY_TOKENS
    max_chars = limits.max_chars if limits is not None else MAX_TOOL_RESULT_REPLAY_CHARS
    keep_runes = limits.keep_runes if limits is not None else COMPACTED_TOOL_RESULT_KEEP_RUNES
    estimated_tokens = estimate_tokens_rough(content)
    if estimated_tokens <= max_tokens and len(content) <= max_chars:
        return content
    if len(content) <= keep_runes:
        return content
    head = keep_runes // 2
    tail = keep_runes - head
    return _REPLAY_MARKER_TEMPLATE.format(
        tokens=estimated_tokens,
        chars=len(content),
        head=head,
        tail=tail,
        head_text=content[:head],
        omitted=len(content) - head - tail,
        tail_text=content[-tail:],
    )


@dataclass
class DeepSeekReplayDiagnostics:
    """Raw-vs-replay accounting for one request (Whale Usage mirror)."""

    raw_chars: int = 0
    replay_chars: int = 0
    raw_tokens: int = 0
    replay_tokens: int = 0
    compacted: int = 0
    stripped_reasoning: int = 0

    @property
    def tokens_saved(self) -> int:
        return max(0, self.raw_tokens - self.replay_tokens)

    def summary(self) -> str:
        return (
            "replay economy: tool raw=%d tok replay=%d tok saved=%d "
            "compacted=%d stripped_reasoning=%d"
            % (
                self.raw_tokens,
                self.replay_tokens,
                self.tokens_saved,
                self.compacted,
                self.stripped_reasoning,
            )
        )


def _has_tool_calls(msg: dict) -> bool:
    calls = msg.get("tool_calls")
    return isinstance(calls, list) and len(calls) > 0


def apply_deepseek_replay_compaction(
    messages: list,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    limits: "ReplayCompactionLimits | None" = None,
) -> tuple[list, DeepSeekReplayDiagnostics]:
    """Apply the wire-time economy on the send copy (per-send clones only)."""
    diag = DeepSeekReplayDiagnostics()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "tool":
            content = msg.get("content")
            if isinstance(content, str) and content:
                raw_tokens = estimate_tokens_rough(content)
                replay = tool_result_replay_content(content, limits=limits)
                diag.raw_chars += len(content)
                diag.replay_chars += len(replay)
                diag.raw_tokens += raw_tokens
                diag.replay_tokens += estimate_tokens_rough(replay)
                if replay != content:
                    msg["content"] = replay
                    diag.compacted += 1
        elif role == "assistant" and not _has_tool_calls(msg):
            # Strip plain reasoning only where proven (DeepSeek + MiMo; Kimi not).
            if _plain_turn_strip_allowed(provider, model, base_url) and "reasoning_content" in msg:
                del msg["reasoning_content"]
                diag.stripped_reasoning += 1
    return messages, diag


def merge_replay_usage(usage_dict: dict, diag: DeepSeekReplayDiagnostics) -> None:
    """Add the economy counters to a per-turn usage dict when non-zero."""
    if diag.tokens_saved <= 0 and diag.stripped_reasoning <= 0:
        return
    usage_dict["deepseek_replay_tokens_saved"] = diag.tokens_saved
    usage_dict["deepseek_tool_results_compacted"] = diag.compacted
    usage_dict["deepseek_reasoning_stripped"] = diag.stripped_reasoning


def estimate_request_tokens_after_deepseek_replay(
    messages: list,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    limits: "ReplayCompactionLimits | None" = None,
) -> int:
    """Post-replay wire size (no mutation); reasoning subtraction stays
    echo-gated by proven family."""
    raw = estimate_messages_tokens_rough(messages)
    saved = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "tool":
            content = msg.get("content")
            if isinstance(content, str) and content:
                replay = tool_result_replay_content(content, limits=limits)
                if replay != content:
                    saved += estimate_tokens_rough(content) - estimate_tokens_rough(replay)
        elif role == "assistant" and not _has_tool_calls(msg):
            # The strip deletes the key too, which costs tokens even when the
            # value is empty; the estimate used to miss those ~6 tok/turn
            # (found by fuzzing).
            if _plain_turn_strip_allowed(provider, model, base_url) and "reasoning_content" in msg:
                saved += estimate_tokens_rough(str({"reasoning_content": msg["reasoning_content"]}))
    return max(0, raw - saved)

"""Native OpenAI Responses server-side compaction — safely gated gpt-5.6 routes.

OpenAI's Responses API supports server-side compaction: include
``context_management=[{"type": "compaction", "compact_threshold": N}]`` in a
``/v1/responses`` request and, when the rendered input crosses N tokens, the
server summarizes older context into an opaque ``compaction`` output item
(``encrypted_content``, sealed to the issuing endpoint). Replaying that item
as an input item on later requests stands in for the pruned history, so the
model keeps long-horizon recall without the client ever seeing a summary.
Docs: https://developers.openai.com/api/docs/guides/compaction

Hermes' support is deliberately narrow (live verification, Aug 2026):

* **gpt-5.6 family only.** gpt-5.6 and its variants compact correctly.
  Sending the field to gpt-5.1 / gpt-5.2 reliably fails server-side —
  HTTP 500 on the blocking path and a permanent stall on the streaming
  path (90s watchdog x 3 retries = a dead turn). There is no structured
  "unsupported" rejection to downgrade on, so the only safe gate is an
  explicit model-family check.
* **Trusted routes only:** api.openai.com (API key), the ChatGPT Codex
  backend (subscription OAuth), or an exact normalized HTTP(S) origin listed
  in ``compression.codex_responses_native_trusted_base_urls``. The explicit
  list is intended only for proxies whose compaction checkpoint persistence
  and replay have been verified. xAI and GitHub/Copilot remain hard-excluded
  even if listed; arbitrary relays and local servers never see the field.

Ownership model: Hermes' local compression stays fully armed as the
fallback owner. The native threshold is clamped safely below the local
compressor's trigger so the server compacts first; if it doesn't (native
disabled mid-session, provider hiccup, non-eligible route), the local
summarizer fires exactly as before. There is no new custody state — the
captured compaction items ride the existing ``codex_reasoning_items``
sidecar, which already handles persistence (state.db), gateway session
replay, cross-issuer stamping, and the encrypted-replay kill switch.

This module is dependency-free on purpose so the transport, adapter, and
conversation loop can share the gate without import cycles.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

# Native compaction fires this many tokens below the local compressor's
# trigger so the server always gets the first shot at compaction.
LOCAL_TRIGGER_SAFETY_MARGIN = 8_192

DEFAULT_COMPACT_THRESHOLD = 200_000

# Model-family gate. Substring match on the lowercased model id so dated
# snapshots (gpt-5.6-2026-07-xx) and variants (gpt-5.6-mini) stay eligible.
_ELIGIBLE_MODEL_MARKER = "gpt-5.6"

# These Responses surfaces have provider-specific replay semantics and must not
# become native-compaction eligible through the generic proxy allowlist. The
# per-request provider flags below remain the primary gate; this hostname guard
# keeps the exclusion true even for a misclassified custom-provider profile.
_EXCLUDED_TRUSTED_HOSTS = frozenset(
    {
        "api.x.ai",
        "api.githubcopilot.com",
        "models.github.ai",
        "models.inference.ai.azure.com",
    }
)


def _is_excluded_trusted_hostname(hostname: str) -> bool:
    return hostname in _EXCLUDED_TRUSTED_HOSTS or hostname.endswith(".x.ai")


def is_native_compaction_model(model: Optional[str]) -> bool:
    """True when the model is in the gpt-5.6 family."""
    return _ELIGIBLE_MODEL_MARKER in (model or "").lower()


def _normalized_http_origin(url: Any) -> Optional[Tuple[str, str, Optional[int]]]:
    """Return a canonical HTTP(S) origin tuple, or None for invalid input.

    Paths, queries, fragments, and trailing slashes are intentionally ignored:
    native compaction checkpoints are sealed to the server origin, not a
    particular API path. Scheme, hostname, and effective port must all match.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urlsplit(url.strip())
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if scheme not in {"http", "https"} or not hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    return scheme, hostname, port


def is_direct_openai_route(
    base_url: Optional[str],
    *,
    is_codex_backend: bool = False,
    trusted_base_urls: Any = (),
) -> bool:
    """True for a native-compaction-capable, explicitly trusted route.

    Direct OpenAI and the ChatGPT Codex backend remain built-in trusted routes.
    Other Responses proxies are eligible only when their normalized HTTP(S)
    origin exactly matches an entry in ``trusted_base_urls``. The list defaults
    empty, so upgrading never broadens the existing trust boundary.
    """
    if is_codex_backend:
        return True
    origin = _normalized_http_origin(base_url)
    if origin is None or _is_excluded_trusted_hostname(origin[1]):
        return False
    if origin[1] == "api.openai.com":
        return True
    if not isinstance(trusted_base_urls, (list, tuple)):
        return False
    return any(
        _normalized_http_origin(candidate) == origin
        for candidate in trusted_base_urls
        if isinstance(candidate, str)
    )


def resolve_compact_threshold(
    configured_threshold: Any,
    local_trigger_tokens: Any = None,
) -> int:
    """Clamp the configured native threshold below the local compressor trigger.

    Without the clamp a native threshold above the local trigger would let the
    local summarizer fire first every time, making native compaction dead
    config. ``local_trigger_tokens`` is ``ContextCompressor.threshold_tokens``
    when a compressor is attached, else None.
    """
    try:
        configured = int(configured_threshold)
    except (TypeError, ValueError):
        configured = DEFAULT_COMPACT_THRESHOLD
    if isinstance(configured_threshold, bool) or configured <= 0:
        configured = DEFAULT_COMPACT_THRESHOLD

    local = None
    try:
        if local_trigger_tokens is not None and not isinstance(local_trigger_tokens, bool):
            local = int(local_trigger_tokens)
    except (TypeError, ValueError):
        local = None
    if local is None or local <= 0:
        return configured

    if local > LOCAL_TRIGGER_SAFETY_MARGIN:
        upper = local - LOCAL_TRIGGER_SAFETY_MARGIN
    else:
        upper = max(1_024, int(local * 0.8))
    return max(1_024, min(configured, upper))


def native_compaction_context_management(
    agent: Any,
    *,
    is_codex_backend: bool,
    is_xai_responses: bool = False,
    is_github_responses: bool = False,
) -> Optional[List[Dict[str, Any]]]:
    """Return the ``context_management`` payload for this request, or None.

    None means "do not send the field" — the request is byte-identical to
    pre-feature behavior. All gates are re-checked per request so a
    mid-session model switch or the in-session kill switch
    (``agent.codex_responses_native_compaction = False``, set by the
    conversation loop's rejection recovery) takes effect on the next call.
    """
    if not bool(getattr(agent, "codex_responses_native_compaction", False)):
        return None
    # compression.enabled: false disables ALL automatic compaction, native
    # included — mirrors the codex_app_server_auto contract.
    if not bool(getattr(agent, "compression_enabled", True)):
        return None
    if is_xai_responses or is_github_responses:
        return None
    if not is_native_compaction_model(getattr(agent, "model", None)):
        return None
    if not is_direct_openai_route(
        getattr(agent, "base_url", None),
        is_codex_backend=is_codex_backend,
        trusted_base_urls=getattr(
            agent, "codex_responses_native_trusted_base_urls", ()
        ),
    ):
        return None

    compressor = getattr(agent, "context_compressor", None)
    threshold = resolve_compact_threshold(
        getattr(agent, "codex_responses_compact_threshold", DEFAULT_COMPACT_THRESHOLD),
        getattr(compressor, "threshold_tokens", None) if compressor is not None else None,
    )
    return [{"type": "compaction", "compact_threshold": threshold}]


# Retention budget for plaintext user messages carried across a native
# compaction boundary (mirrors Codex CLI's RETAINED_MESSAGE_TOKEN_BUDGET).
# Live verification (Aug 2026, gpt-5.6 @ api.openai.com): the server renders
# NOTHING placed before a replayed compaction checkpoint — a fact stated in a
# pre-checkpoint input item is invisible to the model ("NONE" recall), while
# the same item placed after the checkpoint recalls perfectly. Without
# retention, every plaintext user ask from before the compaction survives
# only as whatever the opaque server summary kept — the goal-drift failure
# mode. Codex CLI solves this by rebuilding history with user messages
# retained verbatim; ``prune_pre_checkpoint_items`` is our wire-level
# equivalent.
RETAINED_USER_MESSAGE_TOKEN_BUDGET = 64_000


def _approx_tokens(text: str) -> int:
    """Cheap chars//4 token estimate — same shape Codex uses for retention."""
    return max(1, len(text) // 4)


def _user_item_text(item: Dict[str, Any]) -> Optional[str]:
    """Extract the retained-budget text of a user-role input item.

    Returns None when the item carries no measurable text (empty message).
    Multimodal list content is measured by its ``input_text`` parts; images
    count as zero, matching Codex's retention accounting.
    """
    content = item.get("content")
    if isinstance(content, str):
        return content if content.strip() else None
    if isinstance(content, list):
        text = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "input_text"
        )
        return text if text.strip() or content else None
    return None


def prune_pre_checkpoint_items(
    items: List[Dict[str, Any]],
    retained_user_token_budget: int = RETAINED_USER_MESSAGE_TOKEN_BUDGET,
) -> List[Dict[str, Any]]:
    """Restructure Responses input around the newest compaction checkpoint.

    The server drops every input item that precedes a replayed ``compaction``
    item (live-verified Aug 2026), so sending pre-checkpoint history is dead
    weight AND silently erases the user's plaintext asks. When a checkpoint
    is present, rebuild the wire as::

        [checkpoint run] + [retained user messages (newest-first budget)] + [post]

    - The NEWEST contiguous run of checkpoints wins (the server can emit
      more than one compaction item in a single response — live-observed
      Aug 2026 — and they arrive adjacent; a run from a newer response
      cumulatively carries prior windows, so older runs are dropped).
    - Retained user messages are the user-role items from before the
      checkpoint, kept verbatim newest-first within
      ``retained_user_token_budget``; the boundary message is head-truncated
      when it only partially fits (string content only).
    - Everything after the checkpoint is untouched, so function_call /
      function_call_output pairing is preserved (a checkpoint is captured on
      an assistant response, and that response's own calls and their outputs
      are all emitted after its reasoning items).
    - No checkpoint in ``items`` → returned unchanged (self-gating: non-native
      routes and kill-switched sessions never see a restructured wire).

    Deterministic for a given history, so the request prefix stays stable
    across turns and server-side prompt caching keeps working.
    """
    last_cp = None
    for i, item in enumerate(items):
        if isinstance(item, dict) and item.get("type") == "compaction":
            last_cp = i
    if last_cp is None:
        return items

    # Extend backwards over the contiguous run ending at last_cp.
    first_cp = last_cp
    while (
        first_cp > 0
        and isinstance(items[first_cp - 1], dict)
        and items[first_cp - 1].get("type") == "compaction"
    ):
        first_cp -= 1

    pre = items[:first_cp]
    checkpoint_run = items[first_cp : last_cp + 1]
    post = items[last_cp + 1 :]

    retained_reversed: List[Dict[str, Any]] = []
    remaining = max(0, int(retained_user_token_budget))
    for item in reversed(pre):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        # Skip typed items (function_call_output etc. never carry role=user,
        # but stay defensive about future shapes).
        if "type" in item and item.get("type") != "message":
            continue
        if remaining <= 0:
            break
        text = _user_item_text(item)
        if text is None:
            continue
        cost = _approx_tokens(text)
        if cost <= remaining:
            retained_reversed.append(item)
            remaining -= cost
        elif isinstance(item.get("content"), str):
            # Head-truncate the boundary message: goals are usually stated
            # up front, so the head is the valuable end.
            truncated = dict(item)
            truncated["content"] = item["content"][: remaining * 4]
            if truncated["content"].strip():
                retained_reversed.append(truncated)
            remaining = 0
        # Multimodal boundary message that doesn't fit whole: skip rather
        # than rewrite parts.

    return checkpoint_run + list(reversed(retained_reversed)) + post


def is_native_compaction_rejection(error: Any, status_code: Any = None) -> bool:
    """True when a provider error is a STRUCTURED rejection of the
    context_management field.

    Used by the conversation loop's one-shot recovery: strip the field,
    disable native compaction for the rest of the session, retry. Matching
    is deliberately narrow — a transient 5xx/timeout whose body merely
    ECHOES the request (and therefore contains the field name) must NOT
    permanently downgrade native compaction for the session (#82777).

    Two conditions, both required when a status is known:

    * ``status_code`` is 400 (or unknown/None — some transports surface
      only a message string; field-name matching alone is then the best
      available signal, preserving pre-#82777 behavior for them), and
    * the error text names ``context_management`` / ``compact_threshold``
      alongside rejection language ("unknown", "unsupported", "invalid",
      "unexpected", "not permitted"...). A bare field-name echo without
      rejection language does not match.
    """
    text = str(error or "").lower()
    if "context_management" not in text and "compact_threshold" not in text:
        return False
    if status_code is not None:
        try:
            if int(status_code) != 400:
                return False
        except (TypeError, ValueError):
            pass
    rejection_markers = (
        "unknown", "unsupported", "invalid", "unexpected", "not permitted",
        "not allowed", "unrecognized", "extra field", "no such", "bad request",
        "not supported",
    )
    return any(marker in text for marker in rejection_markers)


def has_compaction_checkpoint(items: Any) -> bool:
    """Does this ``codex_reasoning_items`` sidecar carry a compaction checkpoint?

    A ``type: "compaction"`` item is the server-side stand-in for history that
    has already been pruned — cumulative context, not per-turn reasoning. It
    rides the same sidecar as ordinary reasoning items, so anything that
    rewrites or discards that sidecar (or the message carrying it) has to ask
    this question first: the checkpoint exists in exactly one place, and the
    request that loses it loses the compacted history with it.
    """
    return any(
        isinstance(item, dict) and item.get("type") == "compaction"
        for item in (items if isinstance(items, list) else ())
    )


def merge_interim_reasoning_items(
    prior_items: Any,
    new_items: Any,
) -> List[Dict[str, Any]]:
    """Merge ``codex_reasoning_items`` across Codex incomplete-continuation
    dedup, preserving native compaction checkpoints.

    The incomplete-retry path updates a visually-duplicate interim assistant
    message in place with the newer response's replay payload. A checkpoint
    captured on the EARLIER response is a cumulative context carrier the
    continuation won't re-emit (the replayed checkpoint keeps the server
    render under threshold), so a blind overwrite drops the only copy and the
    next request balloons back to full history. Rule: newer items win, but
    prior checkpoints are prepended unless the newer payload carries its own.
    """
    kept_checkpoints = [
        item
        for item in (prior_items if isinstance(prior_items, list) else [])
        if isinstance(item, dict) and item.get("type") == "compaction"
    ]
    new_list = list(new_items) if isinstance(new_items, list) else []
    if has_compaction_checkpoint(new_list) or not kept_checkpoints:
        return new_list
    return kept_checkpoints + new_list

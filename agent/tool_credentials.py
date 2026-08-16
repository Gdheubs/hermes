"""API-key rotation for tool providers (firecrawl, tavily, exa, parallel, ...).

Tool providers authenticate with a single API key (``FIRECRAWL_API_KEY``,
``TAVILY_API_KEY``, ...).  When that key fails for a credential-level reason
(billing exhaustion, rate limit, or auth rejection) the pool may hold more
keys for the *same* provider (``hermes auth add firecrawl`` entries, env
seeds) — retry the call with the next key instead of failing the tool call.

Rotation is strictly provider-internal: never fail over to a *different*
provider's keys, and never touch the credential pool while running as a
profile multiplexer (the per-profile scope is authoritative there).

Key contract: this module never raises beyond what ``fn`` itself raises.
Pool bookkeeping (marking entries exhausted) is best-effort — failures there
are logged at debug and the call flow continues.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, List, Optional

from agent.error_classifier import FailoverReason, classify_api_error

logger = logging.getLogger(__name__)


class ToolCredentialError(Exception):
    """Carries structured HTTP status through tool provider boundaries.

    ``status_code`` is an :class:`int` attribute on the exception itself, so
    ``agent.error_classifier._extract_status_code``-style getattr chains
    (``status_code`` / ``status`` / ``response.status_code``) find it without
    special-casing this class.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        body: Any = None,
        provider_id: str = "",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.provider_id = provider_id


def tool_error_from_exception(exc: Exception, provider_id: str = "") -> ToolCredentialError:
    """Convert an arbitrary provider/SDK exception to a :class:`ToolCredentialError`.

    Never raises.  Status is extracted via the standard getattr chains
    (``status_code``, ``status``, ``response.status_code``, ``response.status``)
    and, failing those, the ``"Error code: <ddd>"`` text pattern on
    ``str(exc)`` (e.g. Firecrawl error envelopes).
    """
    status_code = _extract_status_code(exc)
    return ToolCredentialError(
        str(exc),
        status_code=status_code,
        body=getattr(exc, "body", None),
        provider_id=provider_id,
    )


def _extract_status_code(exc: Exception) -> Optional[int]:
    """Best-effort status extraction: getattr chains, then regex fallback."""
    candidate = getattr(exc, "status_code", None)
    if candidate is None:
        candidate = getattr(exc, "status", None)
    if candidate is None:
        response = getattr(exc, "response", None)
        if response is not None:
            candidate = getattr(response, "status_code", None)
            if candidate is None:
                candidate = getattr(response, "status", None)
    if candidate is not None:
        try:
            status = int(candidate)
        except (TypeError, ValueError):
            status = None
        if status is not None:
            return status
    match = re.search(r"[Ee]rror code:?\s*(\d{3})", str(exc))
    if match:
        return int(match.group(1))
    return None


ROTATE_REASONS = frozenset(
    {FailoverReason.auth, FailoverReason.billing, FailoverReason.rate_limit}
)
"""Failover reasons that justify trying another API key for the same provider.

Everything else (400 format errors, 404 model/endpoint, 5xx server errors,
timeouts, policy blocks, ...) is not a per-key problem and must never rotate.
"""


def run_with_key_rotation(
    provider_id: str,
    fn: Callable[[str], Any],
    *,
    current_key: str = "",
    max_rotations: int = 16,
) -> Any:
    """Run ``fn(api_key)`` once per candidate key, rotating on credential failures.

    Single-shot passthrough — ``fn(current_key)`` with NO pool access — when
    any of:
      - ``provider_id`` is falsy (no provider to rotate within);
      - ``agent.secret_scope.is_multiplex_active()`` is True (profile scope is
        authoritative; never reach into the shared pool);
      - ``load_pool(provider_id)`` is None or the pool has no credentials.

    Otherwise the candidate order is ``current_key`` first (when non-empty),
    then each available pool entry's ``runtime_api_key``/``access_token``,
    deduplicated by value with empties and repeats of ``current_key`` skipped.

    After each failure the error is classified via
    ``agent.error_classifier.classify_api_error(exc, provider=provider_id)``:
      - reason in :data:`ROTATE_REASONS` and another candidate remains →
        best-effort ``pool.mark_exhausted_and_rotate(...)`` (failures logged at
        debug, never propagated), INFO log naming provider + entry label +
        reason, continue with the next candidate;
      - reason NOT in :data:`ROTATE_REASONS` → re-raise immediately
        (400/404/5xx/timeout never rotate);
      - no untried candidate remains (or ``max_rotations`` reached) →
        re-raise WITHOUT marking the failed key. Marking a lone/last key
        would persist a cooldown to auth.json, after which key resolution
        finds nothing and the tool silently disappears for the whole TTL —
        a single transient 429 must not become a multi-hour outage.

    ``load_pool`` is called exactly once per invocation.  ``fn`` is called
    with one argument, the API key; on success its return value is returned.
    """
    if not provider_id:
        return fn(current_key)

    # Lazy imports: credential_pool pulls heavy deps (auth providers, JWT
    # helpers) and secret_scope is process-global state — neither is needed
    # on the passthrough paths above, and lazy imports match repo convention.
    from agent.secret_scope import is_multiplex_active
    if is_multiplex_active():
        # Profile multiplexer: the per-profile scope is authoritative; never
        # touch the shared pool.
        return fn(current_key)

    from agent.credential_pool import load_pool
    pool = load_pool(provider_id)
    if pool is None or not pool.has_credentials():
        # No rotation candidates; behave exactly like the raw single call.
        return fn(current_key)

    def _entry_key(entry) -> str:
        return entry.runtime_api_key or entry.access_token or ""

    def _entry_label(entry) -> str:
        return entry.label or entry.id

    # Key → label map for rotation logs (best-effort; pool read failures
    # must never break the call flow).
    try:
        _labels = {}
        for _entry in pool.entries():
            _labels.setdefault(_entry_key(_entry), _entry_label(_entry))
    except Exception:  # noqa: BLE001
        _labels = {}

    def _mark_exhausted(classified: Any, failed_key: str, next_key: str) -> None:
        """Best-effort pool bookkeeping for a key that has a live alternative."""
        try:
            pool.mark_exhausted_and_rotate(
                status_code=classified.status_code,
                error_context=classified.error_context or None,
                api_key_hint=failed_key or None,
                failure_reason=classified.reason.value,
            )
        except Exception:
            logger.debug(
                "tool credential rotation: pool bookkeeping failed for "
                "provider %r; continuing without marking",
                provider_id,
                exc_info=True,
            )
        logger.info(
            "tool credential rotation: %s entry %s failed (%s); rotating to %s",
            provider_id,
            _labels.get(failed_key, "(unknown)"),
            classified.reason.value,
            _labels.get(next_key, "(unknown)"),
        )

    # Precompute the candidate list up front (current key first, then
    # available pool entries, deduped by value). Precomputing — instead of
    # walking via mark_exhausted_and_rotate's return — lets the failure
    # path know whether a distinct untried key remains BEFORE marking
    # anything: a failed key is only marked exhausted when another
    # candidate exists to rotate to. Marking a lone/last key would write a
    # cooldown to auth.json, after which resolve_provider_secret() finds no
    # key at all and the toolset silently disappears for the whole TTL —
    # turning one transient 429 into a multi-hour outage.
    candidates: List[str] = []
    seen = set()
    if current_key:
        candidates.append(current_key)
        seen.add(current_key)
    try:
        available = pool.available_entries()
    except Exception:  # noqa: BLE001 — pool read must not break the call
        available = []
    for entry in available:
        key = _entry_key(entry)
        if key and key not in seen:
            seen.add(key)
            candidates.append(key)

    if not candidates:
        # Defensive: no current key and every pool entry is cooling down.
        # Mirror the single-shot passthrough rather than inventing a new
        # failure mode.
        return fn(current_key)

    last_exc: Optional[Exception] = None
    for index, key in enumerate(candidates[: max_rotations + 1]):
        try:
            return fn(key)
        except Exception as exc:
            last_exc = exc
            classified = classify_api_error(exc, provider=provider_id)
            has_alternative = index + 1 < len(candidates)
            if classified.reason not in ROTATE_REASONS or not has_alternative:
                raise
            _mark_exhausted(classified, key, candidates[index + 1])

    # Unreachable in practice (the last iteration either returns or
    # raises), but keep the shape explicit.
    if last_exc is not None:  # pragma: no cover
        raise last_exc
    return fn(current_key)  # pragma: no cover

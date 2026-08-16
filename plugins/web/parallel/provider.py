"""Parallel.ai web search + content extraction — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Uses two
distinct Parallel SDK clients:

- ``Parallel`` (sync)        — for :meth:`search`
- ``AsyncParallel`` (async)  — for :meth:`extract`

This is the first plugin to exercise the **async-extract** code path in
the ABC: :meth:`extract` is declared ``async def``, and the dispatcher
in :func:`tools.web_tools.web_extract_tool` detects coroutines via
:func:`inspect.iscoroutinefunction` and awaits.

Both public calls are wrapped in key rotation: the sync search path uses
:func:`agent.tool_credentials.run_with_key_rotation`; the async extract
path uses a module-local async twin of it (see
:func:`_extract_with_key_rotation`) because the SDK is async-native. When
the resolved key fails with an auth/billing/rate-limit error, the call is
retried with the next credential for the same provider (``hermes auth add
parallel`` keys or env-seeded pool entries).

Config keys this provider responds to::

    web:
      search_backend: "parallel"      # explicit per-capability
      extract_backend: "parallel"     # explicit per-capability
      backend: "parallel"             # shared fallback
      # Optional: search mode (default "agentic"; also "fast" or "one-shot")
      # via the PARALLEL_SEARCH_MODE env var.

Env vars::

    PARALLEL_API_KEY=...             # https://parallel.ai (required)
    PARALLEL_SEARCH_MODE=agentic     # optional: agentic|fast|one-shot
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from agent.tool_credentials import ROTATE_REASONS, run_with_key_rotation, tool_error_from_exception
from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

# Key-keyed client caches: ``{api_key: client}``. Rotation hands each
# attempt a different key, so caching per key value reuses the SDK client
# for the common single-key case while never serving a client bound to a
# stale key. The canonical ``tools.web_tools`` slots (``_parallel_client`` /
# ``_async_parallel_client``) are kept in sync so tests that reset them
# between cases still see fresh state.
_parallel_sync_clients: Dict[str, Any] = {}
_parallel_async_clients: Dict[str, Any] = {}


def _resolve_parallel_api_key() -> str:
    """Resolve the Parallel API key for this provider.

    Routes through :func:`tools.tool_backend_helpers.resolve_provider_secret`
    (config → scoped env → ``.env`` → credential pool) so keys added via
    ``hermes auth add parallel`` and env-seeded pool entries are visible,
    not just ``PARALLEL_API_KEY``. Never raises; returns ``""`` on a miss.
    """
    from tools.tool_backend_helpers import resolve_provider_secret

    return resolve_provider_secret("PARALLEL_API_KEY", "parallel")


def _ensure_parallel_sdk_installed() -> None:
    """Trigger lazy install of the parallel SDK if it isn't present.

    Mirrors the lazy-deps pattern used by the legacy implementation.
    Swallows benign ImportError from the lazy_deps helper itself; if the
    SDK is genuinely missing the subsequent ``from parallel import ...``
    raises ImportError that the caller can handle.
    """
    try:
        from tools.lazy_deps import ensure as _lazy_ensure

        _lazy_ensure("search.parallel", prompt=False)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 — surface install hint as ImportError
        raise ImportError(str(exc))


def _get_sync_client(api_key: str = "") -> Any:
    """Lazy-load + cache the sync Parallel client, keyed by API key.

    When ``api_key`` is empty it is resolved like the legacy env read
    (via :func:`_resolve_parallel_api_key`). Raises ``ValueError`` when no
    key is available anywhere; ``ImportError`` when the SDK is missing.
    """
    import tools.web_tools as _wt

    if getattr(_wt, "_parallel_client", None) is None:
        # Tests reset the canonical slot between cases; honor that by
        # dropping the key-keyed cache so a stale client never survives.
        _parallel_sync_clients.clear()

    api_key = api_key or _resolve_parallel_api_key()
    if not api_key:
        raise ValueError(
            "PARALLEL_API_KEY environment variable not set. "
            "Get your API key at https://parallel.ai"
        )

    cached = _parallel_sync_clients.get(api_key)
    if cached is not None:
        return cached

    _ensure_parallel_sdk_installed()
    from parallel import Parallel  # noqa: WPS433 — deliberately lazy

    client = Parallel(api_key=api_key)
    _parallel_sync_clients[api_key] = client
    _wt._parallel_client = client
    return client


def _get_async_client(api_key: str = "") -> Any:
    """Lazy-load + cache the async Parallel client, keyed by API key.

    When ``api_key`` is empty it is resolved like the legacy env read
    (via :func:`_resolve_parallel_api_key`). Raises ``ValueError`` when no
    key is available anywhere; ``ImportError`` when the SDK is missing.
    """
    import tools.web_tools as _wt

    if getattr(_wt, "_async_parallel_client", None) is None:
        # Tests reset the canonical slot between cases; honor that by
        # dropping the key-keyed cache so a stale client never survives.
        _parallel_async_clients.clear()

    api_key = api_key or _resolve_parallel_api_key()
    if not api_key:
        raise ValueError(
            "PARALLEL_API_KEY environment variable not set. "
            "Get your API key at https://parallel.ai"
        )

    cached = _parallel_async_clients.get(api_key)
    if cached is not None:
        return cached

    _ensure_parallel_sdk_installed()
    from parallel import AsyncParallel  # noqa: WPS433 — deliberately lazy

    client = AsyncParallel(api_key=api_key)
    _parallel_async_clients[api_key] = client
    _wt._async_parallel_client = client
    return client


def _reset_clients_for_tests() -> None:
    """Drop both cached clients so tests can re-instantiate cleanly.

    Clears the canonical slots on :mod:`tools.web_tools` (where
    :func:`_get_sync_client` / :func:`_get_async_client` read/write them)
    plus the key-keyed module caches.
    """
    import tools.web_tools as _wt

    _wt._parallel_client = None
    _wt._async_parallel_client = None
    _parallel_sync_clients.clear()
    _parallel_async_clients.clear()


# Backward-compatible aliases for the names that lived in tools.web_tools
# before the migration (matches existing tests + external callers).
_get_parallel_client = _get_sync_client
_get_async_parallel_client = _get_async_client


def _resolve_search_mode() -> str:
    """Return the validated PARALLEL_SEARCH_MODE value (default "agentic")."""
    mode = os.getenv("PARALLEL_SEARCH_MODE", "agentic").lower().strip()
    if mode not in {"fast", "one-shot", "agentic"}:
        mode = "agentic"
    return mode


async def _parallel_extract_attempt(urls: List[str], api_key: str) -> Any:
    """One AsyncParallel extract attempt with the given key.

    SDK errors are wrapped via :func:`tool_error_from_exception` so the
    rotation loop can classify them; key-missing (``ValueError``) and
    SDK-install (``ImportError``) errors propagate as-is to the provider's
    handlers.
    """
    client = _get_async_client(api_key)
    try:
        return await client.beta.extract(urls=urls, full_content=True)
    except Exception as exc:  # noqa: BLE001 — SDK failure
        raise tool_error_from_exception(exc, "parallel") from exc


async def _extract_with_key_rotation(
    urls: List[str],
    current_key: str,
    *,
    max_rotations: int = 16,
) -> Any:
    """Async twin of :func:`agent.tool_credentials.run_with_key_rotation`.

    ``run_with_key_rotation`` is sync (see ``agent/tool_credentials.py``)
    and cannot drive the async-native ``AsyncParallel.beta.extract`` inside
    a running event loop, so this mirrors its semantics inline using the
    same primitives: ``ROTATE_REASONS``, ``tool_error_from_exception``,
    ``classify_api_error``, and ``pool.mark_exhausted_and_rotate``.

    - Single-shot passthrough (no rotation) when the profile secret scope
      is authoritative (multiplex active) or no pool/credentials exist.
    - Otherwise: ``current_key`` first, then each available pool entry's
      runtime key (deduped by value, empties/dups skipped), capped at
      ``max_rotations`` attempts.
    - After each failure, classify; only auth/billing/rate-limit reasons
      rotate (best-effort pool marking, never hard-fails); everything else
      re-raises immediately. Exhausting all candidates re-raises the last
      exception WITHOUT marking the final key — a lone key must never be
      cooled down by its own failure (that would make the toolset vanish
      for the TTL with no alternative to rotate to).
    """
    from agent.credential_pool import load_pool
    from agent.error_classifier import classify_api_error
    from agent.secret_scope import is_multiplex_active

    try:
        multiplex = is_multiplex_active()
    except Exception:  # noqa: BLE001 — scope probe must never hard-fail
        multiplex = False

    pool = None
    if not multiplex:
        try:
            pool = load_pool("parallel")
        except Exception as exc:  # noqa: BLE001 — pool read is best-effort
            logger.debug("Could not load parallel credential pool: %s", exc)

    if multiplex or pool is None or not pool.has_credentials():
        # Single-shot passthrough — nothing to rotate over.
        return await _parallel_extract_attempt(urls, current_key)

    candidates: List[str] = []
    seen: set = set()
    if current_key:
        candidates.append(current_key)
        seen.add(current_key)
    # available_entries() honors cooldown expiry (an exhausted entry whose
    # TTL lapsed is available again), unlike a static last_status filter.
    try:
        available = pool.available_entries()
    except Exception:  # noqa: BLE001 — pool read is best-effort
        available = []
    for entry in available:
        key = str(
            getattr(entry, "runtime_api_key", "")
            or getattr(entry, "access_token", "")
            or ""
        ).strip()
        if key and key not in seen:
            seen.add(key)
            candidates.append(key)
    if not candidates:
        # Pool exists but every entry is exhausted/dead: fall back to the
        # resolved key so the failure surfaces with the normal message.
        candidates = [current_key]

    last_exc: Exception
    for index, candidate in enumerate(candidates):
        if index >= max_rotations:
            break
        try:
            return await _parallel_extract_attempt(urls, candidate)
        except Exception as exc:  # noqa: BLE001 — classify every failure
            last_exc = exc
        try:
            classified = classify_api_error(last_exc, provider="parallel")
        except Exception:  # noqa: BLE001 — classifier must not break rotation
            classified = None
        if (
            classified is not None
            and classified.reason in ROTATE_REASONS
            and index + 1 < len(candidates)
        ):
            try:
                pool.mark_exhausted_and_rotate(
                    status_code=classified.status_code,
                    api_key_hint=candidate,
                    failure_reason=classified.reason.value,
                    error_context=classified.error_context or None,
                )
            except Exception as exc:  # noqa: BLE001 — marking is best-effort
                logger.debug("Parallel pool mark_exhausted_and_rotate failed: %s", exc)
            logger.info(
                "Parallel extract failed with key %r (%s) — rotating to next credential",
                candidate,
                classified.reason.value,
            )
            continue
        raise last_exc
    raise last_exc


class ParallelWebSearchProvider(WebSearchProvider):
    """Parallel.ai search + async extract provider."""

    @property
    def name(self) -> str:
        return "parallel"

    @property
    def display_name(self) -> str:
        return "Parallel"

    def is_available(self) -> bool:
        """Return True when a Parallel API key is configured.

        Resolves through config → scoped env → ``.env`` → credential pool
        (network-free), so keys added via ``hermes auth add parallel``
        count even when no env var is set.
        """
        return bool(_resolve_parallel_api_key())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a Parallel search (sync).

        Uses the ``beta.search`` endpoint with the configured mode
        (``PARALLEL_SEARCH_MODE`` env var, default "agentic"). Limit is
        capped at 20 server-side.
        """
        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return {"success": False, "error": "Interrupted"}

            mode = _resolve_search_mode()
            logger.info(
                "Parallel search: '%s' (mode=%s, limit=%d)", query, mode, limit
            )

            def _run(attempt_key: str) -> Any:
                client = _get_sync_client(attempt_key)
                try:
                    return client.beta.search(
                        search_queries=[query],
                        objective=query,
                        mode=mode,
                        max_results=min(limit, 20),
                    )
                except Exception as exc:  # noqa: BLE001 — SDK failure
                    # Wrap SDK errors so run_with_key_rotation can classify
                    # auth/billing/rate-limit from the SDK payload.
                    raise tool_error_from_exception(exc, "parallel") from exc

            response = run_with_key_rotation(
                "parallel", _run, current_key=_resolve_parallel_api_key()
            )

            web_results = []
            for i, result in enumerate(response.results or []):
                excerpts = result.excerpts or []
                web_results.append(
                    {
                        "url": result.url or "",
                        "title": result.title or "",
                        "description": " ".join(excerpts) if excerpts else "",
                        "position": i + 1,
                    }
                )

            return {"success": True, "data": {"web": web_results}}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except ImportError as exc:
            return {
                "success": False,
                "error": f"Parallel SDK not installed: {exc}",
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Parallel search error: %s", exc)
            return {"success": False, "error": f"Parallel search failed: {exc}"}

    async def extract(
        self, urls: List[str], **kwargs: Any
    ) -> List[Dict[str, Any]]:
        """Extract content from one or more URLs via the async SDK.

        Returns the legacy list-of-results shape that
        :func:`tools.web_tools.web_extract_tool` expects: one entry per
        successful URL plus one entry per failed URL with an ``error``
        field. Errors are not raised — they're returned as per-URL items.
        """
        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return [
                    {"url": u, "error": "Interrupted", "title": ""} for u in urls
                ]

            logger.info("Parallel extract: %d URL(s)", len(urls))
            response = await _extract_with_key_rotation(
                urls, _resolve_parallel_api_key()
            )

            results: List[Dict[str, Any]] = []
            for result in response.results or []:
                content = result.full_content or ""
                if not content:
                    content = "\n\n".join(result.excerpts or [])
                url = result.url or ""
                title = result.title or ""
                results.append(
                    {
                        "url": url,
                        "title": title,
                        "content": content,
                        "raw_content": content,
                        "metadata": {"sourceURL": url, "title": title},
                    }
                )

            for error in response.errors or []:
                results.append(
                    {
                        "url": error.url or "",
                        "title": "",
                        "content": "",
                        "error": error.content or error.error_type or "extraction failed",
                        "metadata": {"sourceURL": error.url or ""},
                    }
                )

            return results
        except ValueError as exc:
            return [{"url": u, "title": "", "content": "", "error": str(exc)} for u in urls]
        except ImportError as exc:
            return [
                {"url": u, "title": "", "content": "", "error": f"Parallel SDK not installed: {exc}"}
                for u in urls
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Parallel extract error: %s", exc)
            return [
                {"url": u, "title": "", "content": "", "error": f"Parallel extract failed: {exc}"}
                for u in urls
            ]

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Parallel",
            "badge": "paid",
            "tag": "Objective-tuned search + parallel page extraction.",
            "env_vars": [
                {
                    "key": "PARALLEL_API_KEY",
                    "prompt": "Parallel API key",
                    "url": "https://parallel.ai",
                },
            ],
        }

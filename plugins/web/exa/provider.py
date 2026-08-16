"""Exa web search + content extraction — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Uses the
official Exa SDK (``exa-py``) which is lazy-loaded via
:func:`tools.lazy_deps.ensure` so that cold-start CLI users don't pay the
SDK import cost when Exa isn't configured.

Config keys this provider responds to::

    web:
      search_backend: "exa"      # explicit per-capability
      extract_backend: "exa"     # explicit per-capability
      backend: "exa"             # shared fallback for both

Env var::

    EXA_API_KEY=...    # https://exa.ai (paid tier; free trial available)

Each public call is wrapped in :func:`agent.tool_credentials.run_with_key_rotation`:
when the resolved key fails with an auth/billing/rate-limit error, the call
is retried with the next credential for the same provider (``hermes auth
add exa`` keys or env-seeded pool entries). The SDK client is built per
attempt key and cached keyed by key value.

The previous in-tree implementation lived at
``tools.web_tools._exa_search`` / ``_exa_extract``; this file is the
canonical replacement. Behavior is bit-for-bit identical aside from the
ABC method-name change and key rotation.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from agent.tool_credentials import run_with_key_rotation, tool_error_from_exception
from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

# Key-keyed Exa client cache: ``{api_key: client}``. Rotation hands each
# attempt a different key, so caching per key value reuses the SDK client
# for the common single-key case while never serving a client bound to a
# stale key. The canonical ``tools.web_tools._exa_client`` slot is kept in
# sync so tests that reset it (``tools.web_tools._exa_client = None``
# between cases) still see fresh state.
_exa_client_cache: Dict[str, Any] = {}


def _resolve_exa_api_key() -> str:
    """Resolve the Exa API key for this provider.

    Routes through :func:`tools.tool_backend_helpers.resolve_provider_secret`
    (config → scoped env → ``.env`` → credential pool) so keys added via
    ``hermes auth add exa`` and env-seeded pool entries are visible, not
    just ``EXA_API_KEY``. Never raises; returns ``""`` on a miss.
    """
    from tools.tool_backend_helpers import resolve_provider_secret

    return resolve_provider_secret("EXA_API_KEY", "exa")


def _get_exa_client(api_key: str = "") -> Any:
    """Lazy-import and cache an Exa SDK client keyed by API key.

    When ``api_key`` is empty it is resolved like the legacy env read
    (via :func:`_resolve_exa_api_key`). Raises ``ValueError`` when no key
    is available anywhere; ``ImportError`` when the SDK is missing.
    """
    import tools.web_tools as _wt

    if getattr(_wt, "_exa_client", None) is None:
        # Tests reset the canonical slot between cases; honor that by
        # dropping the key-keyed cache so a stale client never survives.
        _exa_client_cache.clear()

    api_key = api_key or _resolve_exa_api_key()
    if not api_key:
        raise ValueError(
            "EXA_API_KEY environment variable not set. "
            "Get your API key at https://exa.ai"
        )

    cached = _exa_client_cache.get(api_key)
    if cached is not None:
        return cached

    try:
        from tools.lazy_deps import ensure as _lazy_ensure

        _lazy_ensure("search.exa", prompt=False)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 — lazy_deps surfaces install hints
        raise ImportError(str(exc))

    from exa_py import Exa  # noqa: WPS433 — deliberately lazy

    client = Exa(api_key=api_key)
    client.headers["x-exa-integration"] = "hermes-agent"
    _exa_client_cache[api_key] = client
    _wt._exa_client = client
    return client


def _reset_client_for_tests() -> None:
    """Drop the cached Exa clients so tests can re-instantiate cleanly."""
    import tools.web_tools as _wt

    _wt._exa_client = None
    _exa_client_cache.clear()


class ExaWebSearchProvider(WebSearchProvider):
    """Exa search + extract provider.

    Both methods are sync — Exa's SDK is sync-only. The web_extract_tool
    dispatcher wraps sync extracts via ``asyncio.to_thread`` when it
    needs to keep the event loop responsive.
    """

    @property
    def name(self) -> str:
        return "exa"

    @property
    def display_name(self) -> str:
        return "Exa"

    def is_available(self) -> bool:
        """Return True when an Exa API key is configured.

        Resolves through config → scoped env → ``.env`` → credential pool
        (network-free), so keys added via ``hermes auth add exa`` count
        even when no env var is set.
        """
        return bool(_resolve_exa_api_key())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute an Exa search.

        Returns ``{"success": True, "data": {"web": [{...}, ...]}}`` on
        success, ``{"success": False, "error": str}`` on failure (incl.
        missing API key and SDK install errors).
        """
        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return {"success": False, "error": "Interrupted"}

            logger.info("Exa search: '%s' (limit=%d)", query, limit)

            def _run(attempt_key: str) -> Any:
                client = _get_exa_client(attempt_key)
                try:
                    return client.search(
                        query,
                        num_results=limit,
                        contents={"highlights": True},
                    )
                except Exception as exc:  # noqa: BLE001 — SDK failure
                    # Wrap SDK errors so run_with_key_rotation can classify
                    # auth/billing/rate-limit from the SDK payload.
                    raise tool_error_from_exception(exc, "exa") from exc

            response = run_with_key_rotation(
                "exa", _run, current_key=_resolve_exa_api_key()
            )

            web_results = []
            for i, result in enumerate(response.results or []):
                highlights = result.highlights or []
                web_results.append(
                    {
                        "url": result.url or "",
                        "title": result.title or "",
                        "description": " ".join(highlights) if highlights else "",
                        "position": i + 1,
                    }
                )

            return {"success": True, "data": {"web": web_results}}
        except ValueError as exc:
            # Raised by _get_exa_client when EXA_API_KEY missing
            return {"success": False, "error": str(exc)}
        except ImportError as exc:
            return {"success": False, "error": f"Exa SDK not installed: {exc}"}
        except Exception as exc:  # noqa: BLE001 — surface as failure
            logger.warning("Exa search error: %s", exc)
            return {"success": False, "error": f"Exa search failed: {exc}"}

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract content from one or more URLs via Exa.

        Returns a list of result dicts shaped for the legacy LLM
        post-processing pipeline. On per-URL or whole-batch failure,
        results carry an ``error`` field rather than raising.
        """
        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return [
                    {"url": u, "error": "Interrupted", "title": ""} for u in urls
                ]

            logger.info("Exa extract: %d URL(s)", len(urls))

            def _run(attempt_key: str) -> Any:
                client = _get_exa_client(attempt_key)
                try:
                    return client.get_contents(urls, text=True)
                except Exception as exc:  # noqa: BLE001 — SDK failure
                    raise tool_error_from_exception(exc, "exa") from exc

            response = run_with_key_rotation(
                "exa", _run, current_key=_resolve_exa_api_key()
            )

            results: List[Dict[str, Any]] = []
            for result in response.results or []:
                content = result.text or ""
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
            return results
        except ValueError as exc:
            return [{"url": u, "title": "", "content": "", "error": str(exc)} for u in urls]
        except ImportError as exc:
            return [
                {"url": u, "title": "", "content": "", "error": f"Exa SDK not installed: {exc}"}
                for u in urls
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Exa extract error: %s", exc)
            return [
                {"url": u, "title": "", "content": "", "error": f"Exa extract failed: {exc}"}
                for u in urls
            ]

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Exa",
            "badge": "paid",
            "tag": "Semantic + neural web search with content extraction.",
            "env_vars": [
                {
                    "key": "EXA_API_KEY",
                    "prompt": "Exa API key",
                    "url": "https://exa.ai",
                },
            ],
        }

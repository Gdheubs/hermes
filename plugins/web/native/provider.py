"""Local HTTP fetch + trafilatura extract provider — plugin form.

Subclasses the plugin-facing :class:`agent.web_search_provider.WebSearchProvider`.
No API key required — uses httpx for HTTP GET and trafilatura for
main-content extraction (markdown output, headings preserved).
Extract-only (``supports_search() -> False``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time as time_module
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import httpx

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


# ─── Configuration ───────────────────────────────────────────────────────────
# All behavioral knobs live under ``web.native`` in config.yaml. Defaults
# below mirror ``DEFAULT_CONFIG["web"]["native"]`` in
# ``hermes_cli/config_defaults.py`` and are used verbatim when config is
# unavailable (e.g. a standalone unit-test import) — keep the two in sync.

_DEFAULT_USER_AGENT = "curl/8.21.0"

_NATIVE_DEFAULTS: Dict[str, Any] = {
    "timeout": 30,
    "max_redirects": 5,
    "max_response_bytes": 2000000,
    "cache_ttl": 900,
    "trust_env": False,
    "user_agent": "",
    "trafilatura": True,
    "favor_precision": False,
    "include_links": True,
}

# In-memory cache. Value is ``(written_at, title, content)`` — the title is
# cached alongside the body so a hit returns the same shape as a fresh fetch.
#
# Expiry is lazy: nothing reaps entries in the background, so a lapsed entry
# stops being *readable* immediately but is only *freed* when its key is read
# again, when any successful write purges lapsed entries, or when it is
# evicted by a cap below. Hence two caps — a page count and a total size,
# because 512 entries of extracted text is a very different memory footprint
# depending on the pages.
_WEB_FETCH_CACHE: Dict[str, tuple[float, str, str]] = {}
_MAX_CACHE_ENTRIES = 512
_MAX_CACHE_CHARS = 64 * 1024 * 1024

# trafilatura renders in-page anchor links (`href="#id"`) as a link followed
# by a verbatim repeat of the link text. Documentation sites that link their
# own section headings hit this on every heading:
#   ## [Current version](#latest-release)Current version
# This collapses the duplicate back to plain text.
#
# The URL group is a single unambiguous `[^)]*` and the "is this an in-page
# anchor?" test lives in _collapse_anchor_dups below. Spelling the fragment
# check into the pattern as `[^)]*#[^)]*` made the two quantifiers ambiguous:
# on input like "[a](" + "#" * n with no closing paren, the engine explores
# every split, which is quadratic — ~1 s at 50 KB and minutes at the 2 MB
# response cap, all of it blocking the event loop on content that any fetched
# site controls.
_ANCHOR_DUP_RE = re.compile(r"\[([^\]\[]+)\]\(([^)]*)\)\1")


def _collapse_anchor_dups(text: str) -> str:
    """Collapse ``[label](#anchor)label`` down to ``label``.

    Runs to a fixpoint: trafilatura can emit two duplicated links back to
    back, and a single pass leaves the first one as a new valid dup
    (e.g. ``[X](u#a)[X](u#b)X`` → ``[X](u#a)X``).
    """
    def _replace(match: "re.Match[str]") -> str:
        label, target = match.group(1), match.group(2)
        # Only in-page anchors duplicate this way; a real outbound link
        # followed by matching prose must keep its URL.
        return label if "#" in target else match.group(0)

    while True:
        collapsed = _ANCHOR_DUP_RE.sub(_replace, text)
        if collapsed == text:
            return text
        text = collapsed


def _purge_expired(ttl: float) -> None:
    """Drop every entry whose TTL has lapsed.

    Called on the way into each fetch, not just on a successful write: if
    every later fetch fails there is no write to piggyback on, and the lapsed
    bodies would sit there until the process exits.
    """
    now = time_module.monotonic()
    for k in [k for k, (t, _, _) in _WEB_FETCH_CACHE.items() if now - t >= ttl]:
        _WEB_FETCH_CACHE.pop(k, None)


def _cache_get(key: str, ttl: float) -> Optional[tuple[str, str]]:
    """Return ``(title, content)`` for a live entry, dropping it if it lapsed.

    Freeing on read is what keeps a re-requested page from holding its old
    body until some unrelated write happens to purge it.
    """
    entry = _WEB_FETCH_CACHE.get(key)
    if entry is None:
        return None
    if time_module.monotonic() - entry[0] >= ttl:
        _WEB_FETCH_CACHE.pop(key, None)
        return None
    return entry[1], entry[2]


def _cache_put(key: str, ttl: float, title: str, value: str) -> None:
    """Store an entry, purging lapsed ones and evicting oldest to stay bounded."""
    now = time_module.monotonic()
    _purge_expired(ttl)

    # A single page larger than the whole budget would evict everything else
    # and still not fit — don't cache it at all.
    if len(value) > _MAX_CACHE_CHARS:
        _WEB_FETCH_CACHE.pop(key, None)
        return

    _WEB_FETCH_CACHE.pop(key, None)  # re-insert so ordering reflects the write
    _WEB_FETCH_CACHE[key] = (now, title, value)

    total = sum(len(v) for _, _, v in _WEB_FETCH_CACHE.values())
    while len(_WEB_FETCH_CACHE) > _MAX_CACHE_ENTRIES or total > _MAX_CACHE_CHARS:
        oldest_key = min(_WEB_FETCH_CACHE, key=lambda k: _WEB_FETCH_CACHE[k][0])
        if oldest_key == key:  # never evict what we just wrote
            break
        total -= len(_WEB_FETCH_CACHE.pop(oldest_key)[2])


def _load_native_web_config() -> Dict[str, Any]:
    """Read ``web.native`` from config.yaml, merged over built-in defaults."""
    merged = dict(_NATIVE_DEFAULTS)
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        web_section = cfg.get("web") if isinstance(cfg, dict) else None
        native_section = web_section.get("native") if isinstance(web_section, dict) else None
        if isinstance(native_section, dict):
            merged.update({k: v for k, v in native_section.items() if v is not None})
    except Exception as exc:  # noqa: BLE001 — config optional; fall back to defaults
        logger.debug("Could not load web.native config: %s", exc)
    return merged


def _cfg_int(cfg: Dict[str, Any], key: str) -> int:
    try:
        return int(cfg.get(key, _NATIVE_DEFAULTS[key]))
    except (TypeError, ValueError):
        return int(_NATIVE_DEFAULTS[key])


def _cfg_bool(cfg: Dict[str, Any], key: str) -> bool:
    val = cfg.get(key, _NATIVE_DEFAULTS[key])
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return bool(val)


# Documentation generators (Sphinx, MkDocs, Docusaurus) append a permalink
# marker to every heading, which survives extraction as a trailing link:
#   # Streams[¶](#streams)
# It has to come off before a heading can be compared with the page title.
_TRAILING_LINK_RE = re.compile(r"\s*\[[^\]\[]*\]\([^)]*\)\s*$")


def _starts_with_title(content: str, title: str) -> bool:
    """True when *content* already opens with *title* as its first line.

    trafilatura keeps the page's own H1 in the extracted text, and that H1 is
    usually exactly what ``extract_metadata()`` reports as the title — so
    prepending the title unconditionally printed it twice.
    """
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stripped = stripped.lstrip("#").strip()
        previous = None
        while previous != stripped:
            previous = stripped
            stripped = _TRAILING_LINK_RE.sub("", stripped).strip()
        return stripped.casefold() == title.strip().casefold()
    return False


_SSRF_BLOCKED_ERROR = "Blocked: URL targets a private or internal network address"


async def _fetch_single_url(
    url: str,
    extract_mode: str = "markdown",
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Fetch a single URL and extract readable content via trafilatura.

    Returns ``content`` == ``raw_content``, matching every sibling provider
    (firecrawl, exa, tavily, parallel). The per-page character budget the
    model actually sees is owned by ``web_extract_tool`` via
    ``web.extract_char_limit`` — it re-derives ``content`` from
    ``raw_content`` anyway, so truncating here would be dead work. The real
    bound this provider owns is ``max_response_bytes``, applied while
    reading the socket.
    """
    if not url or not isinstance(url, str):
        return {"url": str(url), "title": "", "content": "", "raw_content": "", "error": "URL is required"}

    if cfg is None:
        cfg = _load_native_web_config()

    url = url.strip()
    timeout = _cfg_int(cfg, "timeout")
    max_redirects = _cfg_int(cfg, "max_redirects")
    max_response_bytes = _cfg_int(cfg, "max_response_bytes")
    cache_ttl = _cfg_int(cfg, "cache_ttl")
    user_agent = str(cfg.get("user_agent") or "").strip() or _DEFAULT_USER_AGENT

    # ── SSRF check ────────────────────────────────────────────────
    from tools.url_safety import async_is_safe_url, normalize_url_for_request

    try:
        normalized_url = normalize_url_for_request(url)
    except Exception:
        normalized_url = url
    if not await async_is_safe_url(normalized_url):
        return {
            "url": url, "title": "", "content": "", "raw_content": "",
            "error": _SSRF_BLOCKED_ERROR,
        }

    # ── Cache check ───────────────────────────────────────────────
    # The mode is part of the key: "markdown" and "text" render the same page
    # differently, so they must not share an entry. ``cache_ttl <= 0`` turns
    # caching off outright — reads AND writes — rather than writing entries
    # that can never be read back.
    caching = cache_ttl > 0
    cache_key = f"{extract_mode}:{normalized_url}"
    if caching:
        # Reclaim lapsed bodies now, so a run of failing fetches (which never
        # reach _cache_put) still frees them.
        _purge_expired(cache_ttl)
        cached = _cache_get(cache_key, cache_ttl)
        if cached is not None:
            cached_title, cached_content = cached
            return {
                "url": url, "title": cached_title,
                "content": cached_content, "raw_content": cached_content,
            }

    # ── Environment trust (proxy + TLS) ─────────────────────────────
    # ``trust_env`` maps 1:1 onto httpx's own ``trust_env`` flag, and httpx
    # treats it as authority for BOTH environment-sourced behaviours:
    #
    #   1. Proxy pickup: HTTP_PROXY/HTTPS_PROXY/ALL_PROXY are honoured only
    #      when trust_env is on (allow_env_proxies in _client.py). With it
    #      off, httpx does NOT read ambient proxy variables even though
    #      passing ``proxy=None`` alone would have left the default True —
    #      so this is what actually switches off an ambient proxy. That also
    #      matters for SSRF: we vet the resolved IP locally, but a proxy
    #      re-resolves the hostname itself, so the address we validated is
    #      not necessarily the one connected to.
    #
    #   2. TLS trust store from env: when verify=True (the default),
    #      SSL_CERT_FILE / SSL_CERT_DIR are only consulted if trust_env is
    #      on; otherwise verification falls back to the bundled certifi
    #      bundle. So trust_env: false affects EVERY https request, not just
    #      proxied ones — a system CA configured via SSL_CERT_FILE will be
    #      ignored and any host signed by it will fail verification.
    use_proxy = _cfg_bool(cfg, "trust_env")
    proxy_url = None
    if use_proxy:
        proxy_url = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or None

    # ── Fetch (manual redirect follow so each hop is SSRF-revalidated) ──
    # httpx's built-in follow_redirects would issue requests to redirect
    # targets before we could vet them, so we disable it and walk the chain
    # ourselves, re-checking async_is_safe_url on every Location before the
    # next request. Mirrors the Firecrawl final-URL re-check (2e12401ed).
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            proxy=proxy_url,
            trust_env=use_proxy,
        ) as client:
            current_url = normalized_url
            html: Optional[str] = None
            content_type = ""
            fetched = False

            for _hop in range(max_redirects + 1):
                # stream() returns as soon as the headers are in, so the
                # Content-Length gate and the read cap below both apply
                # BEFORE the body is in memory. A plain get() would have
                # buffered the whole response first, making any size check
                # after the fact useless as a memory bound.
                async with client.stream("GET", current_url, headers=headers) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            break
                        try:
                            next_url = normalize_url_for_request(urljoin(current_url, location))
                        except Exception:
                            next_url = urljoin(current_url, location)
                        if not await async_is_safe_url(next_url):
                            return {
                                "url": url, "title": "", "content": "", "raw_content": "",
                                "error": _SSRF_BLOCKED_ERROR,
                            }
                        current_url = next_url
                        continue

                    response.raise_for_status()

                    # ── Content-Length gate (headers only, no body read) ──
                    try:
                        content_length = int(response.headers.get("content-length") or 0)
                    except (TypeError, ValueError):
                        content_length = 0
                    if content_length > max_response_bytes:
                        return {
                            "url": url, "title": "", "content": "", "raw_content": "",
                            "error": f"Response too large ({content_length} bytes > {max_response_bytes} cap)",
                        }

                    content_type = response.headers.get("content-type", "").lower()

                    # ── Bounded read ─────────────────────────────────────
                    # Chunked/unknown-length responses advertise no
                    # Content-Length, so the cap has to be enforced while
                    # reading: stop pulling chunks once the budget is spent
                    # and drop the connection.
                    buf = bytearray()
                    async for chunk in response.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) >= max_response_bytes:
                            del buf[max_response_bytes:]
                            break
                    encoding = response.encoding or "utf-8"
                    # errors="replace": the byte cap can land mid-codepoint.
                    body = bytes(buf).decode(encoding, errors="replace")
                    fetched = True

                if "text/" not in content_type and "application/xhtml" not in content_type:
                    # Not a document we can extract from — hand back the
                    # (byte-capped) body verbatim and let the caller budget it.
                    return {
                        "url": url, "title": "", "content": body,
                        "raw_content": body, "content_type": content_type,
                    }
                html = body
                break

            if not fetched or html is None:
                return {
                    "url": url, "title": "", "content": "", "raw_content": "",
                    "error": f"Too many redirects (>{max_redirects})",
                }
    except httpx.TimeoutException:
        return {"url": url, "title": "", "content": "", "raw_content": "", "error": f"Request timed out after {timeout}s"}
    except httpx.HTTPStatusError as e:
        return {"url": url, "title": "", "content": "", "raw_content": "", "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"}
    except Exception as e:
        return {"url": url, "title": "", "content": "", "raw_content": "", "error": f"Fetch failed: {type(e).__name__}: {e}"}

    # ── Extract readable content via trafilatura ─────────────────
    # (no size check needed here — the read above is byte-capped at
    # max_response_bytes, so ``html`` is already bounded.)
    readable_title = ""
    content = None

    if _cfg_bool(cfg, "trafilatura"):
        try:
            import trafilatura

            # with_metadata is deliberately NOT set: it prepends a YAML front
            # matter block ("---\ntitle: …\n---") to the output, which would
            # land in the page text on top of the "# <title>" heading added
            # below. The title we surface comes from extract_metadata().
            extract_kwargs: Dict[str, Any] = {
                "output_format": "txt" if extract_mode == "text" else "markdown",
                "include_links": (
                    False if extract_mode == "text" else _cfg_bool(cfg, "include_links")
                ),
                "include_images": False,
                "include_tables": True,
            }
            if _cfg_bool(cfg, "favor_precision"):
                extract_kwargs["favor_precision"] = True

            content = trafilatura.extract(html, **extract_kwargs)

            try:
                meta = trafilatura.extract_metadata(html)
                if meta is not None:
                    readable_title = meta.title or ""
            except Exception:
                readable_title = ""
        except Exception as e:
            return {"url": url, "title": "", "content": "", "raw_content": "", "error": f"Content extraction failed: {e}"}

    if content is None:
        # trafilatura disabled or found nothing — fall back to raw HTML
        # with script/style stripped
        try:
            from lxml import html as lhtml

            tree = lhtml.fromstring(html)
            if not readable_title:
                readable_title = tree.findtext(".//title", default="")
            for tag in tree.xpath("//script|//style"):
                tag.getparent().remove(tag)
            readable_html = lhtml.tostring(tree, encoding="unicode")
        except Exception:
            readable_html = html

        import html2text

        converter = html2text.HTML2Text()
        converter.body_width = 0
        converter.ignore_links = False
        converter.ignore_images = True
        converter.ignore_emphasis = False
        converter.protect_links = True
        converter.unicode_snob = True
        converter.skip_internal_links = True
        if extract_mode == "text":
            converter.ignore_links = True
            converter.ignore_emphasis = True
        content = converter.handle(readable_html)

    # ── Clean up ──────────────────────────────────────────────────
    content = _collapse_anchor_dups(content)
    content = re.sub(r"\n{4,}", "\n\n\n", content)
    content = content.strip()
    if readable_title and not _starts_with_title(content, readable_title):
        # Plain-text mode gets a bare title line — a markdown "# " heading
        # would be markup the caller explicitly asked not to receive.
        heading = readable_title if extract_mode == "text" else f"# {readable_title}"
        full_content = f"{heading}\n\n{content}"
    else:
        full_content = content
    if caching:
        _cache_put(cache_key, cache_ttl, readable_title or "", full_content)

    return {
        "url": url,
        "title": readable_title or "",
        "content": full_content,
        "raw_content": full_content,
    }


class WebFetchWebSearchProvider(WebSearchProvider):
    """Local HTTP fetch extract provider — no API key needed."""

    @property
    def name(self) -> str:
        return "native"

    @property
    def display_name(self) -> str:
        return "Native Web Fetch"

    def is_available(self) -> bool:
        try:
            import trafilatura  # noqa: F401
            import html2text  # noqa: F401

            return True
        except ImportError:
            return False

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        extract_mode = "markdown"
        fmt = kwargs.get("format")
        if fmt and isinstance(fmt, str) and fmt.lower().strip() == "text":
            extract_mode = "text"

        cfg = _load_native_web_config()
        tasks = [_fetch_single_url(u, extract_mode=extract_mode, cfg=cfg) for u in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final: List[Dict[str, Any]] = []
        for i, r in enumerate(results):
            if isinstance(r, BaseException):
                final.append({
                    "url": urls[i] if i < len(urls) else "",
                    "title": "", "content": "", "raw_content": "", "error": f"Internal error: {r}",
                })
            else:
                final.append(r)
        return final

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Local Web Fetch (web-fetch)",
            "badge": "free · no key · extract only",
            "tag": "Fetches content via httpx + trafilatura — no API key. Pair with any search provider.",
            "env_vars": [],
        }
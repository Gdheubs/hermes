"""Regression tests for the native local-fetch extract provider.

Covers:
- Capability flags (extract-only, no search)
- ``web.native`` config loading / default merge
- SSRF pre-check before any request
- Per-hop redirect re-validation (a public URL must not redirect into a
  private/internal address)
- Too-many-redirects handling
- The happy-path extraction pipeline (skipped when optional deps absent)
"""
from __future__ import annotations

import contextlib
from typing import Any, Dict, List, Optional

import pytest

from plugins.web.native import provider as native


# Long enough that trafilatura treats it as real body content and keeps the
# full structure — short toy documents get stripped down to bare text, which
# would make the markdown-vs-text assertions below pass vacuously.
_RICH_HTML = """<html><head><title>Page Title</title></head><body><article>
<h1>Main Heading</h1>
<p>An opening paragraph that is long enough to be treated as real body content
by the extractor, with <strong>bold text</strong> and
<a href="https://elsewhere.example/page">a link</a> inside it.</p>
<h2>A Subsection</h2>
<p>Another paragraph of reasonably long body content so the extractor keeps the
whole structure intact rather than discarding it as boilerplate.</p>
</article></body></html>"""


# ---------------------------------------------------------------------------
# Fake httpx plumbing
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Stand-in for a streamed httpx response.

    ``text`` is the body; it is served through :meth:`aiter_bytes` in chunks
    so tests exercise the same bounded-read path as production. ``chunks``
    counts how many chunks were actually pulled, which is what proves the
    read stops early instead of draining the whole body.
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: Optional[Dict[str, str]] = None,
        text: str = "",
        is_redirect: bool = False,
        chunk_size: int = 4096,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.is_redirect = is_redirect
        self.reason_phrase = "OK"
        self.encoding = "utf-8"
        self._chunk_size = chunk_size
        self.chunks_read = 0

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("err", request=None, response=self)  # type: ignore[arg-type]

    async def aiter_bytes(self):
        data = self.text.encode("utf-8")
        for start in range(0, len(data), self._chunk_size) or [0]:
            self.chunks_read += 1
            yield data[start:start + self._chunk_size]


class _FakeClient:
    """Async-context-manager stand-in that replays queued responses."""

    def __init__(self, responses: List[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.requested_urls: List[str] = []

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def stream(self, method: str, url: str, headers: Optional[Dict[str, str]] = None):
        self.requested_urls.append(url)
        if not self._responses:
            raise AssertionError(f"unexpected extra request to {url}")
        response = self._responses.pop(0)

        class _Ctx:
            async def __aenter__(self) -> _FakeResponse:
                return response

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()


def _install_fake_client(monkeypatch, responses: List[_FakeResponse]) -> _FakeClient:
    client = _FakeClient(responses)
    captured: Dict[str, Any] = {}

    def _factory(*args: Any, **kwargs: Any) -> _FakeClient:
        captured.update(kwargs)
        return client

    monkeypatch.setattr(native.httpx, "AsyncClient", _factory)
    client.client_kwargs = captured  # type: ignore[attr-defined]
    return client


def _patch_safety(monkeypatch, verdicts: List[bool]) -> None:
    """Patch async_is_safe_url to return successive verdicts from ``verdicts``."""
    seq = list(verdicts)

    async def _fake_safe(url: str) -> bool:
        return seq.pop(0) if seq else True

    import tools.url_safety as url_safety

    monkeypatch.setattr(url_safety, "async_is_safe_url", _fake_safe)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_extract_only(self):
        p = native.WebFetchWebSearchProvider()
        assert p.name == "native"
        assert p.supports_search() is False
        assert p.supports_extract() is True


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_defaults_when_config_missing(self, monkeypatch):
        def _boom():
            raise RuntimeError("no config")

        monkeypatch.setattr("hermes_cli.config.load_config", _boom)
        cfg = native._load_native_web_config()
        assert cfg["timeout"] == native._NATIVE_DEFAULTS["timeout"]
        assert cfg["max_redirects"] == 5
        assert cfg["trafilatura"] is True

    def test_defaults_match_default_config(self):
        """``_NATIVE_DEFAULTS`` is the no-config fallback for the same keys
        ``DEFAULT_CONFIG["web"]["native"]`` ships. If the two drift, a user
        editing config.yaml and a user without one get different behaviour."""
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["web"]["native"] == native._NATIVE_DEFAULTS

    def test_config_keys_are_known_to_the_validator(self):
        """``hermes config set web.native.<key>`` must not warn "unknown key"
        — which is what happens when a section is read from config.yaml but
        never declared in DEFAULT_CONFIG."""
        from hermes_cli.config import _validate_config_key

        for key in native._NATIVE_DEFAULTS:
            is_known, _ = _validate_config_key(f"web.native.{key}")
            assert is_known, f"web.native.{key} is not a recognised config key"

    def test_user_overrides_merge_over_defaults(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"web": {"native": {"timeout": 7, "cache_ttl": 123}}},
        )
        cfg = native._load_native_web_config()
        assert cfg["timeout"] == 7
        assert cfg["cache_ttl"] == 123
        # untouched keys keep their defaults
        assert cfg["max_redirects"] == native._NATIVE_DEFAULTS["max_redirects"]


# ---------------------------------------------------------------------------
# SSRF
# ---------------------------------------------------------------------------


class TestSSRF:
    @pytest.mark.asyncio
    async def test_precheck_blocks_before_request(self, monkeypatch):
        _patch_safety(monkeypatch, [False])
        client = _install_fake_client(monkeypatch, [])

        result = await native._fetch_single_url(
            "http://169.254.169.254/latest/meta-data/",
            cfg=dict(native._NATIVE_DEFAULTS),
        )

        assert "private or internal" in result["error"]
        assert client.requested_urls == []  # never fetched

    @pytest.mark.asyncio
    async def test_redirect_target_is_revalidated(self, monkeypatch):
        # Initial URL is safe, but it redirects to an internal address that
        # must be rejected before the second request is issued.
        _patch_safety(monkeypatch, [True, False])
        redirect = _FakeResponse(
            status_code=302,
            headers={"location": "http://169.254.169.254/"},
            is_redirect=True,
        )
        client = _install_fake_client(monkeypatch, [redirect])

        result = await native._fetch_single_url(
            "https://example.com/redirect",
            cfg=dict(native._NATIVE_DEFAULTS),
        )

        assert "private or internal" in result["error"]
        # Only the initial request happened; the unsafe hop was not followed.
        assert len(client.requested_urls) == 1

    @pytest.mark.asyncio
    async def test_too_many_redirects(self, monkeypatch):
        _patch_safety(monkeypatch, [True] * 20)
        cfg = dict(native._NATIVE_DEFAULTS)
        cfg["max_redirects"] = 2
        loops = [
            _FakeResponse(
                status_code=302,
                headers={"location": "https://example.com/next"},
                is_redirect=True,
            )
            for _ in range(5)
        ]
        _install_fake_client(monkeypatch, loops)

        result = await native._fetch_single_url("https://example.com/", cfg=cfg)
        assert "Too many redirects" in result["error"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestBackendSelection:
    """The extract-only native provider must not hijack search selection."""

    def _register_native_only(self):
        from agent.web_search_registry import register_provider, _reset_for_tests

        _reset_for_tests()
        register_provider(native.WebFetchWebSearchProvider())

    def test_native_not_auto_selected_as_shared_backend(self, monkeypatch):
        from tools import web_tools

        self._register_native_only()
        try:
            # No creds, no configured backend, native available and registered.
            monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
            monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: False)
            monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
            monkeypatch.setattr(native.WebFetchWebSearchProvider, "is_available", lambda self: True)

            # Shared fallback must skip the extract-only provider and keep the
            # search-capable default rather than returning "native".
            assert web_tools._get_backend() != "native"
            assert web_tools._get_search_backend() != "native"
        finally:
            from agent.web_search_registry import _reset_for_tests
            _reset_for_tests()

    def test_web_search_reports_extract_only_backend_clearly(self, monkeypatch):
        """``web.backend: native`` is a misconfiguration — native cannot search.

        The mirror case (a search-only backend asked to extract) already
        returns a typed "search-only" error. Before native there was no
        extract-only provider, so this direction fell through to the generic
        "nothing configured" hint, which is wrong: the user did configure a
        backend.
        """
        import json

        from tools import web_tools

        self._register_native_only()
        try:
            monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "native"})
            monkeypatch.setattr(
                native.WebFetchWebSearchProvider, "is_available", lambda self: True
            )

            result = json.loads(web_tools.web_search_tool("some query", limit=3))

            assert result["success"] is False
            assert "extract-only" in result["error"]
            assert "web.search_backend" in result["error"]
        finally:
            from agent.web_search_registry import _reset_for_tests
            _reset_for_tests()

    def test_native_selected_when_extract_backend_configured(self, monkeypatch):
        from tools import web_tools

        self._register_native_only()
        try:
            monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"extract_backend": "native"})
            monkeypatch.setattr(native.WebFetchWebSearchProvider, "is_available", lambda self: True)

            assert web_tools._get_extract_backend() == "native"
        finally:
            from agent.web_search_registry import _reset_for_tests
            _reset_for_tests()


class TestZeroConfigAutoFallback:
    """A fully unconfigured install with only free plugins should still work:
    ddgs auto-selected for search, native auto-selected for extract.
    """

    def _register_all_plus_native(self):
        from tests.tools.conftest import register_all_web_providers
        from agent.web_search_registry import register_provider

        register_all_web_providers()  # resets + registers the 8 built-ins
        register_provider(native.WebFetchWebSearchProvider())

    def _clear(self, monkeypatch):
        for k in (
            "BRAVE_SEARCH_API_KEY", "SEARXNG_URL", "TAVILY_API_KEY", "EXA_API_KEY",
            "PARALLEL_API_KEY", "FIRECRAWL_API_KEY", "FIRECRAWL_API_URL",
            "FIRECRAWL_GATEWAY_URL", "TOOL_GATEWAY_DOMAIN",
        ):
            monkeypatch.delenv(k, raising=False)
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        monkeypatch.setattr(native.WebFetchWebSearchProvider, "is_available", lambda self: True)

    def test_no_keys_ddgs_installed_auto_selects_ddgs_and_native(self, monkeypatch):
        from tools import web_tools

        self._register_all_plus_native()
        try:
            self._clear(monkeypatch)
            monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
            monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)

            assert web_tools._get_search_backend() == "ddgs"
            assert web_tools._get_extract_backend() == "native"
        finally:
            from agent.web_search_registry import _reset_for_tests
            _reset_for_tests()

    def test_extract_falls_back_to_native_even_without_ddgs(self, monkeypatch):
        from tools import web_tools

        self._register_all_plus_native()
        try:
            self._clear(monkeypatch)
            monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
            monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: False)

            # Shared fallback resolves to the unavailable "firecrawl" default;
            # extract must still be rescued by the available native provider.
            assert web_tools._get_extract_backend() == "native"
        finally:
            from agent.web_search_registry import _reset_for_tests
            _reset_for_tests()

    def test_explicit_search_only_backend_is_respected_not_rescued(self, monkeypatch):
        from tools import web_tools

        self._register_all_plus_native()
        try:
            self._clear(monkeypatch)
            # web.backend EXPLICITLY set to a search-only backend. Auto-rescue
            # must NOT kick in — extract stays on searxng so the dispatcher can
            # surface the clear "search-only" error (existing contract).
            monkeypatch.setenv("SEARXNG_URL", "http://searx.example")
            monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "searxng"})

            assert web_tools._get_search_backend() == "searxng"
            assert web_tools._get_extract_backend() == "searxng"
        finally:
            from agent.web_search_registry import _reset_for_tests
            _reset_for_tests()

    def test_paid_extract_backend_still_wins_over_native(self, monkeypatch):
        from tools import web_tools

        self._register_all_plus_native()
        try:
            self._clear(monkeypatch)
            monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-key")
            monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
            monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)

            # firecrawl is available and extract-capable → it wins; native is
            # NOT force-substituted.
            assert web_tools._get_extract_backend() == "firecrawl"
        finally:
            from agent.web_search_registry import _reset_for_tests
            _reset_for_tests()


class TestExtraction:
    @pytest.mark.asyncio
    async def test_extracts_markdown_from_html(self, monkeypatch):
        pytest.importorskip("trafilatura")
        pytest.importorskip("html2text")

        _patch_safety(monkeypatch, [True])
        html = (
            "<html><head><title>Hello Title</title></head>"
            "<body><article><h1>Heading</h1>"
            "<p>Some readable paragraph content here.</p></article></body></html>"
        )
        ok = _FakeResponse(
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=html,
        )
        _install_fake_client(monkeypatch, [ok])

        result = await native._fetch_single_url(
            "https://example.com/article",
            cfg=dict(native._NATIVE_DEFAULTS),
        )

        assert result.get("error") in (None, "")
        assert "readable paragraph" in result["content"].lower()
        # trafilatura's extract_metadata prefers the page's main H1 heading
        # over the <title> element, so the provider title is the H1 text.
        assert result["title"] == "Heading"

    @pytest.mark.asyncio
    async def test_content_matches_raw_content(self, monkeypatch):
        """The provider does not budget characters itself.

        Every sibling provider (firecrawl, exa, tavily, parallel) returns
        ``content == raw_content`` and lets ``web_extract_tool`` apply
        ``web.extract_char_limit``. That tool re-derives ``content`` from
        ``raw_content``, so a character cap here would be dead work — the only
        limit this provider owns is ``max_response_bytes``.
        """
        pytest.importorskip("trafilatura")
        native._WEB_FETCH_CACHE.clear()
        _patch_safety(monkeypatch, [True])
        _install_fake_client(monkeypatch, [_FakeResponse(
            headers={"content-type": "text/html; charset=utf-8"}, text=_RICH_HTML,
        )])

        result = await native._fetch_single_url(
            "https://example.com/a", cfg=dict(native._NATIVE_DEFAULTS),
        )

        assert result["content"] == result["raw_content"]
        assert "[... truncated ...]" not in result["content"]
        native._WEB_FETCH_CACHE.clear()

    @pytest.mark.asyncio
    async def test_non_text_body_is_returned_verbatim_within_the_byte_cap(self, monkeypatch):
        """Non-extractable content types pass through, bounded by bytes only."""
        native._WEB_FETCH_CACHE.clear()
        _patch_safety(monkeypatch, [True])
        _install_fake_client(monkeypatch, [_FakeResponse(
            headers={"content-type": "application/json"}, text="x" * 5000, chunk_size=500,
        )])

        cfg = dict(native._NATIVE_DEFAULTS)
        cfg["max_response_bytes"] = 1000
        result = await native._fetch_single_url("https://example.com/data", cfg=cfg)

        assert result["content"] == "x" * 1000
        assert result["content"] == result["raw_content"]
        native._WEB_FETCH_CACHE.clear()

    @pytest.mark.asyncio
    async def test_stale_cache_entry_is_not_served(self, monkeypatch):
        """A hit past its TTL must trigger a real refetch, not be returned."""
        native._WEB_FETCH_CACHE.clear()
        _patch_safety(monkeypatch, [True])
        _install_fake_client(monkeypatch, [_FakeResponse(
            headers={"content-type": "text/html"}, text="<html><body><p>fresh body</p></body></html>",
        )])

        cfg = dict(native._NATIVE_DEFAULTS)
        cfg["cache_ttl"] = 900
        key = "markdown:https://example.com/stale"
        # Backdate a stale entry well past the TTL.
        native._WEB_FETCH_CACHE[key] = (
            native.time_module.monotonic() - 5000, "Old", "stale body",
        )

        result = await native._fetch_single_url("https://example.com/stale", cfg=cfg)

        assert "stale body" not in result["content"]
        assert "fresh body" in result["content"]
        native._WEB_FETCH_CACHE.clear()

    @pytest.mark.asyncio
    async def test_cache_ttl_zero_disables_cache_reads_and_writes(self, monkeypatch):
        native._WEB_FETCH_CACHE.clear()
        _patch_safety(monkeypatch, [True, True])
        # Two responses queued: with caching off the second call must refetch.
        _install_fake_client(monkeypatch, [
            _FakeResponse(headers={"content-type": "text/html"},
                          text="<html><body><p>first body</p></body></html>"),
            _FakeResponse(headers={"content-type": "text/html"},
                          text="<html><body><p>second body</p></body></html>"),
        ])

        cfg = dict(native._NATIVE_DEFAULTS)
        cfg["cache_ttl"] = 0

        first = await native._fetch_single_url("https://example.com/x", cfg=cfg)
        second = await native._fetch_single_url("https://example.com/x", cfg=cfg)

        assert "first body" in first["content"]
        assert "second body" in second["content"]  # refetched, not cached
        assert native._WEB_FETCH_CACHE == {}       # nothing was written either
        native._WEB_FETCH_CACHE.clear()

    @pytest.mark.asyncio
    async def test_cache_ttl_is_read_from_config(self, monkeypatch):
        """A short configured TTL expires an entry a long TTL would still serve."""
        native._WEB_FETCH_CACHE.clear()
        _patch_safety(monkeypatch, [True])
        _install_fake_client(monkeypatch, [_FakeResponse(
            headers={"content-type": "text/html"},
            text="<html><body><p>fresh body</p></body></html>",
        )])

        cfg = dict(native._NATIVE_DEFAULTS)
        cfg["cache_ttl"] = 10  # seconds
        key = "markdown:https://example.com/ttl"
        # 60s old: inside the 900s default, outside the configured 10s.
        native._WEB_FETCH_CACHE[key] = (
            native.time_module.monotonic() - 60, "Old", "stale body",
        )

        result = await native._fetch_single_url("https://example.com/ttl", cfg=cfg)

        assert "fresh body" in result["content"]
        native._WEB_FETCH_CACHE.clear()

    @pytest.mark.asyncio
    async def test_content_length_precheck_blocks_huge_body_before_read(self, monkeypatch):
        _patch_safety(monkeypatch, [True])
        # Advertised Content-Length exceeds the cap — must bail before the
        # body is buffered (bounds memory), not after downloading it.
        resp = _FakeResponse(
            status_code=200,
            headers={
                "content-type": "text/html",
                "content-length": "999999999",
            },
            text="",
        )
        _install_fake_client(monkeypatch, [resp])

        cfg = dict(native._NATIVE_DEFAULTS)
        cfg["max_response_bytes"] = 1000
        result = await native._fetch_single_url(
            "https://example.com/huge", cfg=cfg
        )

        assert result["error"] and "too large" in result["error"].lower()
        assert result["content"] == ""

    def test_cache_is_bounded_and_purges(self, monkeypatch):
        # Purge any pre-existing entries so the size assertion is deterministic.
        native._WEB_FETCH_CACHE.clear()
        ttl = 100.0
        for i in range(native._MAX_CACHE_ENTRIES + 50):
            native._cache_put(f"key-{i}", ttl, "", "payload")
        assert len(native._WEB_FETCH_CACHE) <= native._MAX_CACHE_ENTRIES
        # A freshly-written entry survives the cap enforcement.
        assert "key-0" not in native._WEB_FETCH_CACHE  # oldest evicted
        native._WEB_FETCH_CACHE.clear()

    def test_lapsed_entry_is_freed_on_read(self):
        """Expiry is lazy — reading a lapsed key must drop it, not just skip it.

        Otherwise a page's old body stays resident until some unrelated write
        happens to purge it.
        """
        native._WEB_FETCH_CACHE.clear()
        native._cache_put("k", 100.0, "T", "payload")
        native._WEB_FETCH_CACHE["k"] = (
            native.time_module.monotonic() - 1000, "T", "payload",
        )

        assert native._cache_get("k", 100.0) is None
        assert "k" not in native._WEB_FETCH_CACHE  # freed, not merely ignored
        native._WEB_FETCH_CACHE.clear()

    def test_live_entry_is_returned_on_read(self):
        native._WEB_FETCH_CACHE.clear()
        native._cache_put("k", 100.0, "Title", "payload")
        assert native._cache_get("k", 100.0) == ("Title", "payload")
        native._WEB_FETCH_CACHE.clear()

    def test_cache_is_bounded_by_total_size_not_just_count(self):
        """A count cap alone says nothing about memory: 512 entries of
        extracted text can be tens of MB or hundreds."""
        native._WEB_FETCH_CACHE.clear()
        chunk = "x" * (1024 * 1024)  # 1 MB per page
        for i in range(80):          # 80 MB attempted, budget is 64 MB
            native._cache_put(f"key-{i}", 1000.0, "", chunk)

        total = sum(len(v) for _, _, v in native._WEB_FETCH_CACHE.values())
        assert total <= native._MAX_CACHE_CHARS
        assert len(native._WEB_FETCH_CACHE) < 80  # oldest pages evicted
        native._WEB_FETCH_CACHE.clear()

    def test_cap_reached_with_nothing_expired_evicts_oldest_write(self):
        """The caps have to hold even when no entry is anywhere near its TTL.

        Eviction is by write time (FIFO) — reads deliberately do not refresh
        an entry, so a hot page written long ago is still evicted before a
        cold one written recently.
        """
        native._WEB_FETCH_CACHE.clear()
        ttl = 10_000.0  # nothing can lapse during the test
        for i in range(native._MAX_CACHE_ENTRIES):
            native._cache_put(f"k{i:04d}", ttl, "", "payload")
        assert len(native._WEB_FETCH_CACHE) == native._MAX_CACHE_ENTRIES

        native._cache_get("k0000", ttl)  # read the oldest — must not save it
        native._cache_put("newest", ttl, "", "payload")

        assert len(native._WEB_FETCH_CACHE) == native._MAX_CACHE_ENTRIES
        assert "k0000" not in native._WEB_FETCH_CACHE  # oldest write evicted
        assert "k0001" in native._WEB_FETCH_CACHE      # only one was dropped
        assert "newest" in native._WEB_FETCH_CACHE     # the new write survives
        native._WEB_FETCH_CACHE.clear()

    def test_entry_larger_than_the_whole_budget_is_not_cached(self):
        native._WEB_FETCH_CACHE.clear()
        native._cache_put("huge", 1000.0, "", "x" * (native._MAX_CACHE_CHARS + 1))
        assert native._WEB_FETCH_CACHE == {}
        native._WEB_FETCH_CACHE.clear()

    def test_cache_purges_expired_on_write(self, monkeypatch):
        native._WEB_FETCH_CACHE.clear()
        native._cache_put("expired", 100.0, "", "old")
        # Simulate the entry aging past its TTL by backdating its write time.
        native._WEB_FETCH_CACHE["expired"] = (
            native.time_module.monotonic() - 1000,
            "",
            "old",
        )
        native._cache_put("fresh", 100.0, "", "new")  # triggers expired purge
        assert "expired" not in native._WEB_FETCH_CACHE
        assert "fresh" in native._WEB_FETCH_CACHE
        native._WEB_FETCH_CACHE.clear()

    @pytest.mark.asyncio
    async def test_no_metadata_front_matter_in_content(self, monkeypatch):
        """The page text must be clean markdown.

        trafilatura's ``with_metadata=True`` prepends a YAML front matter
        block, which landed in the extracted text on top of the "# <title>"
        heading the provider adds — the title appeared three times.
        """
        pytest.importorskip("trafilatura")
        native._WEB_FETCH_CACHE.clear()
        _patch_safety(monkeypatch, [True])
        html = (
            "<html><head><title>Hello Title</title></head><body><article>"
            "<h1>Heading</h1><p>Some readable paragraph content here.</p>"
            "</article></body></html>"
        )
        _install_fake_client(monkeypatch, [_FakeResponse(
            headers={"content-type": "text/html; charset=utf-8"}, text=html,
        )])

        result = await native._fetch_single_url(
            "https://example.com/a", cfg=dict(native._NATIVE_DEFAULTS),
        )

        content = result["content"]
        assert "---" not in content, f"YAML front matter leaked: {content!r}"
        assert "title:" not in content
        native._WEB_FETCH_CACHE.clear()

    @pytest.mark.asyncio
    async def test_title_is_not_repeated_when_content_already_opens_with_it(
        self, monkeypatch
    ):
        """trafilatura keeps the page H1, which is usually the reported title —
        prepending it again printed the heading twice."""
        pytest.importorskip("trafilatura")
        native._WEB_FETCH_CACHE.clear()
        _patch_safety(monkeypatch, [True])
        _install_fake_client(monkeypatch, [_FakeResponse(
            headers={"content-type": "text/html; charset=utf-8"}, text=_RICH_HTML,
        )])

        result = await native._fetch_single_url(
            "https://example.com/a", cfg=dict(native._NATIVE_DEFAULTS),
        )

        assert result["title"] == "Main Heading"
        assert result["content"].count("Main Heading") == 1, result["content"]
        native._WEB_FETCH_CACHE.clear()

    @pytest.mark.asyncio
    async def test_title_is_prepended_when_body_does_not_carry_it(self, monkeypatch):
        """The heading is still added when the body itself lacks the title."""
        pytest.importorskip("trafilatura")
        native._WEB_FETCH_CACHE.clear()
        _patch_safety(monkeypatch, [True])
        html = (
            "<html><head><title>Standalone Title</title></head><body><article>"
            "<p>A body paragraph long enough to be treated as real content by the "
            "extractor, carrying no heading of its own whatsoever.</p>"
            "<p>A second paragraph so the extractor keeps the document rather than "
            "discarding all of it as boilerplate noise.</p>"
            "</article></body></html>"
        )
        _install_fake_client(monkeypatch, [_FakeResponse(
            headers={"content-type": "text/html; charset=utf-8"}, text=html,
        )])

        result = await native._fetch_single_url(
            "https://example.com/a", cfg=dict(native._NATIVE_DEFAULTS),
        )

        if result["title"]:
            assert result["content"].startswith(f"# {result['title']}")
        native._WEB_FETCH_CACHE.clear()

    @pytest.mark.asyncio
    async def test_text_format_drops_markdown_markup(self, monkeypatch):
        """``format="text"`` must reach the trafilatura path, not just the fallback.

        extract_mode was only consulted in the html2text branch, so with
        trafilatura enabled (the default) a text request silently returned
        markdown with links.
        """
        pytest.importorskip("trafilatura")
        native._WEB_FETCH_CACHE.clear()
        _patch_safety(monkeypatch, [True])
        _install_fake_client(monkeypatch, [_FakeResponse(
            headers={"content-type": "text/html; charset=utf-8"}, text=_RICH_HTML,
        )])

        result = await native._fetch_single_url(
            "https://example.com/a",
            extract_mode="text",
            cfg=dict(native._NATIVE_DEFAULTS),
        )

        content = result["content"]
        assert "a link" in content            # link text is kept
        assert "bold text" in content         # so is emphasised text
        assert "elsewhere.example" not in content  # the URL markup is not
        assert "](" not in content
        assert "**" not in content
        assert not content.startswith("# ")   # no markdown heading in text mode
        native._WEB_FETCH_CACHE.clear()

    @pytest.mark.asyncio
    async def test_cache_hit_returns_same_shape_as_fresh_fetch(self, monkeypatch):
        """A cached hit must carry the title, like the fresh path does."""
        pytest.importorskip("trafilatura")
        native._WEB_FETCH_CACHE.clear()
        _patch_safety(monkeypatch, [True, True])
        html = (
            "<html><head><title>T</title></head><body><article><h1>Heading</h1>"
            "<p>Some readable paragraph content here.</p></article></body></html>"
        )
        # Only ONE response is queued: a second HTTP request would raise.
        _install_fake_client(monkeypatch, [_FakeResponse(
            headers={"content-type": "text/html; charset=utf-8"}, text=html,
        )])
        cfg = dict(native._NATIVE_DEFAULTS)

        fresh = await native._fetch_single_url("https://example.com/a", cfg=cfg)
        cached = await native._fetch_single_url("https://example.com/a", cfg=cfg)

        assert cached["title"] == fresh["title"] == "Heading"
        assert cached["content"] == fresh["content"]
        native._WEB_FETCH_CACHE.clear()

    @pytest.mark.asyncio
    async def test_text_and_markdown_modes_do_not_share_a_cache_entry(self, monkeypatch):
        """The cache key includes the mode — otherwise a text request could be
        served the markdown rendering cached by an earlier call."""
        pytest.importorskip("trafilatura")
        native._WEB_FETCH_CACHE.clear()
        _patch_safety(monkeypatch, [True, True])
        _install_fake_client(monkeypatch, [
            _FakeResponse(headers={"content-type": "text/html"}, text=_RICH_HTML),
            _FakeResponse(headers={"content-type": "text/html"}, text=_RICH_HTML),
        ])
        cfg = dict(native._NATIVE_DEFAULTS)

        md = await native._fetch_single_url("https://example.com/a", cfg=cfg)
        txt = await native._fetch_single_url(
            "https://example.com/a", extract_mode="text", cfg=cfg,
        )

        assert "elsewhere.example" in md["content"]
        assert "elsewhere.example" not in txt["content"]
        native._WEB_FETCH_CACHE.clear()

    @pytest.mark.asyncio
    async def test_proxy_is_not_used_when_trust_env_is_off(self, monkeypatch):
        """``trust_env: false`` must also switch off httpx's own
        environment-proxy pickup — otherwise HTTP_PROXY/HTTPS_PROXY/ALL_PROXY
        silently route the request anyway.
        """
        native._WEB_FETCH_CACHE.clear()
        monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
        _patch_safety(monkeypatch, [True])
        client = _install_fake_client(monkeypatch, [_FakeResponse(
            headers={"content-type": "text/html"}, text="<html><body>x</body></html>",
        )])

        cfg = dict(native._NATIVE_DEFAULTS)
        cfg["trust_env"] = False
        await native._fetch_single_url("https://example.com/a", cfg=cfg)

        kwargs = client.client_kwargs  # type: ignore[attr-defined]
        assert kwargs["proxy"] is None
        assert kwargs["trust_env"] is False
        native._WEB_FETCH_CACHE.clear()

    @pytest.mark.asyncio
    async def test_proxy_is_used_when_trust_env_is_on(self, monkeypatch):
        native._WEB_FETCH_CACHE.clear()
        monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
        _patch_safety(monkeypatch, [True])
        client = _install_fake_client(monkeypatch, [_FakeResponse(
            headers={"content-type": "text/html"}, text="<html><body>x</body></html>",
        )])

        cfg = dict(native._NATIVE_DEFAULTS)
        cfg["trust_env"] = True
        await native._fetch_single_url("https://example.com/b", cfg=cfg)

        kwargs = client.client_kwargs  # type: ignore[attr-defined]
        assert kwargs["proxy"] == "http://proxy.example:3128"
        assert kwargs["trust_env"] is True
        native._WEB_FETCH_CACHE.clear()

    @pytest.mark.asyncio
    async def test_chunked_body_read_stops_at_the_byte_cap(self, monkeypatch):
        """A response with no Content-Length must still be bounded.

        This is the case the header gate cannot catch, so the cap has to be
        enforced while reading. Asserting on ``chunks_read`` proves the read
        stopped early instead of draining the whole body.
        """
        native._WEB_FETCH_CACHE.clear()
        _patch_safety(monkeypatch, [True])
        resp = _FakeResponse(
            headers={"content-type": "application/json"},  # no content-length
            text="x" * 200_000,
            chunk_size=1000,
        )
        _install_fake_client(monkeypatch, [resp])

        cfg = dict(native._NATIVE_DEFAULTS)
        cfg["max_response_bytes"] = 5000
        result = await native._fetch_single_url("https://example.com/stream", cfg=cfg)

        assert len(result["raw_content"]) == 5000
        assert resp.chunks_read == 5, "read did not stop at the cap"
        native._WEB_FETCH_CACHE.clear()


# ---------------------------------------------------------------------------
# Anchor-duplicate cleanup
# ---------------------------------------------------------------------------


class TestAnchorDupCollapse:
    """trafilatura renders in-page anchors as ``[label](#id)label``.

    Documentation sites that link their own section headings produce one of
    these per heading — a single release-notes page can yield over a thousand.
    """

    def test_collapses_in_page_anchor_duplicates(self):
        raw = (
            "# Release notes\n\n"
            "## [Current version](#latest-release)Current version\n\n"
            "### [Version 1.2.0](#v-1-2-0)Version 1.2.0\n"
        )
        out = native._collapse_anchor_dups(raw)
        assert out == (
            "# Release notes\n\n"
            "## Current version\n\n"
            "### Version 1.2.0\n"
        )

    def test_heading_permalink_does_not_defeat_title_dedup(self):
        """Sphinx/MkDocs append a permalink marker to every heading.

        It survives extraction as a trailing link — ``# Streams[¶](#streams)``
        on docs.python.org — which made the heading compare unequal to the
        page title, so the title was prepended and printed twice.
        """
        content = "# Streams[¶](#streams)\n\nBody text."
        assert native._starts_with_title(content, "Streams")

    def test_heading_with_a_different_trailing_link_is_not_the_title(self):
        content = "# Streams and pipes[¶](#streams)\n\nBody."
        assert not native._starts_with_title(content, "Streams")

    def test_collapses_back_to_back_duplicates(self):
        """One pass can expose a new duplicate, so the cleanup runs to a fixpoint."""
        assert native._collapse_anchor_dups("[X](u#a)[X](u#b)X") == "X"

    def test_outbound_links_are_preserved(self):
        """Only in-page anchors duplicate this way — a real link keeps its URL,
        even when the following prose happens to repeat the link text."""
        raw = "[Docs](https://example.com/docs)Docs are useful."
        assert native._collapse_anchor_dups(raw) == raw

    def test_fragment_bearing_outbound_link_still_collapses(self):
        raw = "[Section](https://example.com/page#sec)Section"
        assert native._collapse_anchor_dups(raw) == "Section"

    def test_pathological_input_does_not_blow_up(self):
        """Guards a quadratic-backtracking pattern.

        The previous expression spelled the fragment test into the regex as
        ``[^)]*#[^)]*``. Those two quantifiers are ambiguous, so an unclosed
        ``[a](`` followed by a run of ``#`` made the engine explore every
        split: ~17 s at 200 KB, and minutes at the 2 MB response cap — on
        content any fetched page controls, blocking the event loop.
        """
        import time

        payload = "[a](" + "#" * 200_000
        start = time.perf_counter()
        native._collapse_anchor_dups(payload)
        elapsed = time.perf_counter() - start
        # Linear scan is ~1 ms here; the quadratic version took ~17 s.
        assert elapsed < 2.0, f"anchor cleanup took {elapsed:.1f}s — backtracking?"


# ---------------------------------------------------------------------------
# Real-server size bound
# ---------------------------------------------------------------------------


class TestResponseSizeBoundAgainstRealServer:
    """``max_response_bytes`` must bound what is pulled off the socket.

    These tests talk to a real loopback HTTP server because the bug they pin
    is invisible to a hand-built response double: the provider used to call
    ``client.get()``, which buffers the entire body before returning, so the
    Content-Length check ran *after* the whole page was already in memory.
    A fake response object has nothing to buffer and reports success either
    way — only a real socket shows the difference.
    """

    TOTAL = 16 * 1024 * 1024   # what the server would like to send
    CAP = 64 * 1024            # what the provider is allowed to read
    # Generous ceiling: the kernel socket buffers accept a chunk or two after
    # the client stops reading, so "aborted early" cannot mean "exactly CAP".
    TOLERANCE = 4 * 1024 * 1024

    @contextlib.contextmanager
    def _server(self, *, send_content_length: bool):
        import http.server
        import socketserver
        import threading

        total, written = self.TOTAL, {"n": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802 — stdlib callback name
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                if send_content_length:
                    self.send_header("Content-Length", str(total))
                else:
                    self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                chunk = b"x" * (256 * 1024)
                try:
                    for _ in range(total // len(chunk)):
                        if send_content_length:
                            self.wfile.write(chunk)
                        else:
                            self.wfile.write(b"%X\r\n" % len(chunk) + chunk + b"\r\n")
                        written["n"] += len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client hung up — exactly what we're asserting

            def log_message(self, *args):  # silence stderr noise
                pass

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = Server(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/", written
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def _cfg(self):
        cfg = dict(native._NATIVE_DEFAULTS)
        cfg["max_response_bytes"] = self.CAP
        cfg["trust_env"] = False
        return cfg

    @pytest.mark.asyncio
    async def test_content_length_gate_rejects_before_downloading(self, monkeypatch):
        native._WEB_FETCH_CACHE.clear()
        _patch_safety(monkeypatch, [True])

        with self._server(send_content_length=True) as (url, written):
            result = await native._fetch_single_url(url, cfg=self._cfg())

        assert result["error"] and "too large" in result["error"].lower()
        assert written["n"] < self.TOLERANCE, (
            f"server pushed {written['n']} bytes — the body was downloaded "
            "before the Content-Length gate fired"
        )
        native._WEB_FETCH_CACHE.clear()

    @pytest.mark.asyncio
    async def test_chunked_response_without_content_length_is_still_bounded(
        self, monkeypatch
    ):
        """The case the header gate cannot catch: no Content-Length at all."""
        native._WEB_FETCH_CACHE.clear()
        _patch_safety(monkeypatch, [True])

        with self._server(send_content_length=False) as (url, written):
            result = await native._fetch_single_url(url, cfg=self._cfg())

        # Whatever came back, it is bounded by the cap — not by the 16 MB
        # the server was willing to send.
        assert len(result.get("raw_content", "")) <= self.CAP
        assert written["n"] < self.TOLERANCE, (
            f"server pushed {written['n']} bytes — the read did not stop at the cap"
        )
        native._WEB_FETCH_CACHE.clear()

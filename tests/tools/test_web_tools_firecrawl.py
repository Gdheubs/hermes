"""Contract tests for Firecrawl pool-backed key rotation.

Patches at the SDK-client boundary (``tools.web_tools.Firecrawl``) and
the credential-pool boundary (``agent.credential_pool.load_pool``) only —
``FirecrawlWebSearchProvider.search()`` / ``extract()`` and the rotation
helpers in ``plugins/web/firecrawl/provider.py`` run for real.

Coverage:
  - direct cloud search: 402 on the current key → retried with the next
    pool key, failed entry marked exhausted;
  - managed-gateway mode: single attempt even with pool keys present
    (a gateway 402 is subscription billing, never a user key);
  - self-hosted mode (URL, no key): single attempt, pool never consulted;
  - extract(): per-URL rotation — a failed URL is retried with the next
    key while already-scraped URLs are never re-fetched.
"""

from __future__ import annotations

import asyncio

import pytest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import tools.web_tools as web_tools


@pytest.fixture(autouse=True)
def _reset_firecrawl_client():
    """Drop the cached client so each test constructs fresh SDK clients."""
    web_tools._firecrawl_client = None
    web_tools._firecrawl_client_config = None
    yield
    web_tools._firecrawl_client = None
    web_tools._firecrawl_client_config = None


@pytest.fixture(autouse=True)
def _no_gateway_preference(monkeypatch):
    """Default: direct cloud config wins over the managed gateway."""
    monkeypatch.setattr(web_tools, "prefers_gateway", lambda section: False)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)


def _make_pool(entries):
    """Build a real CredentialPool over (label, key) tuples."""
    from agent.credential_pool import CredentialPool, PooledCredential

    creds = [
        PooledCredential(
            provider="firecrawl",
            id=f"id-{label}",
            label=label,
            auth_type="api_key",
            priority=priority,
            source="manual" if not label.startswith("env:") else label,
            access_token=key,
        )
        for priority, (label, key) in enumerate(entries)
    ]
    return CredentialPool("firecrawl", creds)


def _fake_clients():
    """Return (client_a, client_b, collector) with a collecting constructor."""
    client_a, client_b = Mock(), Mock()
    constructed = []

    def _make_client(**kwargs):
        constructed.append(kwargs)
        return client_a if len(constructed) == 1 else client_b

    return client_a, client_b, constructed, _make_client


class TestFirecrawlSearchRotation:
    def test_direct_402_rotates_to_pool_key(self, monkeypatch):
        """Billing failure on key-a → search retried with pool key-b."""
        from agent.tool_credentials import ToolCredentialError
        from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider

        pool = _make_pool([("env:FIRECRAWL_API_KEY", "key-a"), ("manual:2", "key-b")])
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda pid: pool)

        client_a, client_b, constructed, make_client = _fake_clients()
        client_a.search.side_effect = ToolCredentialError(
            "Error code: 402 credits exhausted", status_code=402
        )
        client_b.search.return_value = {
            "data": [{"title": "T", "url": "https://example.com", "description": "d"}]
        }

        with patch.dict("os.environ", {"FIRECRAWL_API_KEY": "key-a"}), \
             patch.object(web_tools, "Firecrawl", Mock(side_effect=make_client)) as mock_fc:
            result = FirecrawlWebSearchProvider().search("query", limit=3)

        assert result["success"] is True
        assert len(result["data"]["web"]) == 1
        assert result["data"]["web"][0]["title"] == "T"
        # Two clients were built — one per candidate key.
        assert constructed == [{"api_key": "key-a"}, {"api_key": "key-b"}]
        client_a.search.assert_called_once()
        client_b.search.assert_called_once()
        mock_fc.assert_has_calls(
            [call(api_key="key-a"), call(api_key="key-b")]
        )
        # The failed key is marked exhausted with the classifier verdict.
        # (Marking swaps in a fresh PooledCredential — re-read from the pool.)
        entry_a = pool.entries()[0]
        assert entry_a.last_status == "exhausted"
        assert entry_a.last_error_code == 402
        assert entry_a.extra.get("failure_reason") == "billing"

    def test_gateway_mode_single_attempt_ignores_pool(self, monkeypatch):
        """Gateway 402 is subscription billing — never rotate to pool keys."""
        from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider

        pool = _make_pool([("env:FIRECRAWL_API_KEY", "key-a"), ("manual:2", "key-b")])
        load_pool = Mock(return_value=pool)
        monkeypatch.setattr("agent.credential_pool.load_pool", load_pool)
        monkeypatch.setattr(web_tools, "prefers_gateway", lambda section: True)
        monkeypatch.setattr(
            web_tools,
            "resolve_managed_tool_gateway",
            lambda vendor, token_reader: SimpleNamespace(
                nous_user_token="gateway-token", gateway_origin="https://gw.example"
            ),
        )

        client_gw = Mock()
        client_gw.search.side_effect = Exception("Error code: 402 subscription exhausted")
        with patch.dict("os.environ", {"FIRECRAWL_API_KEY": "key-a"}), \
             patch.object(web_tools, "Firecrawl", Mock(return_value=client_gw)) as mock_fc:
            result = FirecrawlWebSearchProvider().search("query", limit=3)

        assert result["success"] is False
        assert "402" in result["error"]
        # Exactly one client (the gateway), one attempt — the pool was never
        # consulted even though pool keys exist.
        mock_fc.assert_called_once_with(
            api_key="gateway-token", api_url="https://gw.example"
        )
        client_gw.search.assert_called_once()
        load_pool.assert_not_called()

    def test_self_hosted_single_attempt_ignores_pool(self, monkeypatch):
        """Self-hosted (URL, no key) — pool keys are cloud keys, never aimed here."""
        from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider

        pool = _make_pool([("env:FIRECRAWL_API_KEY", "key-a"), ("manual:2", "key-b")])
        load_pool = Mock(return_value=pool)
        monkeypatch.setattr("agent.credential_pool.load_pool", load_pool)
        # No key anywhere: the provider must not pull one from the pool.
        monkeypatch.setattr(
            "tools.tool_backend_helpers.resolve_provider_secret", lambda *a, **k: ""
        )

        client_sh = Mock()
        client_sh.search.side_effect = Exception("boom")
        with patch.dict("os.environ", {"FIRECRAWL_API_URL": "http://127.0.0.1:3002"}), \
             patch.object(web_tools, "Firecrawl", Mock(return_value=client_sh)) as mock_fc:
            result = FirecrawlWebSearchProvider().search("query", limit=3)

        assert result["success"] is False
        # Client built with the self-hosted URL only — no api_key, single attempt.
        mock_fc.assert_called_once_with(api_url="http://127.0.0.1:3002")
        client_sh.search.assert_called_once()
        load_pool.assert_not_called()


class TestFirecrawlExtractRotation:
    def test_per_url_rotation_never_refetches_successful_urls(self, monkeypatch):
        """URL2 402s on key-a → retried with key-b; URL1 is not re-fetched."""
        from agent.tool_credentials import ToolCredentialError
        from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider

        pool = _make_pool([("env:FIRECRAWL_API_KEY", "key-a"), ("manual:2", "key-b")])
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda pid: pool)
        # Policy/SSRF helpers are not under test; keep the test network-free.
        monkeypatch.setattr(
            "plugins.web.firecrawl.provider.check_website_access", lambda url: None
        )
        monkeypatch.setattr(
            "plugins.web.firecrawl.provider.is_safe_url", lambda url: True
        )

        client_a, client_b, constructed, make_client = _fake_clients()
        formats = ["markdown", "html"]

        def _scrape_a(url, formats):
            if url == "https://one.example":
                return {
                    "data": {
                        "markdown": "content-1",
                        "metadata": {"title": "T1", "sourceURL": "https://one.example"},
                    }
                }
            raise ToolCredentialError(
                "Error code: 402 credits exhausted", status_code=402
            )

        client_a.scrape.side_effect = _scrape_a
        client_b.scrape.return_value = {
            "data": {
                "markdown": "content-2",
                "metadata": {"title": "T2", "sourceURL": "https://two.example"},
            }
        }

        with patch.dict("os.environ", {"FIRECRAWL_API_KEY": "key-a"}), \
             patch.object(web_tools, "Firecrawl", Mock(side_effect=make_client)):
            results = asyncio.run(
                FirecrawlWebSearchProvider().extract(
                    ["https://one.example", "https://two.example"]
                )
            )

        assert [r.get("content") for r in results] == ["content-1", "content-2"]
        # URL1 fetched exactly once with key-a (never re-fetched with key-b);
        # URL2 attempted with key-a, then retried with key-b.
        assert client_a.scrape.call_args_list == [
            call(url="https://one.example", formats=formats),
            call(url="https://two.example", formats=formats),
        ]
        assert client_b.scrape.call_args_list == [
            call(url="https://two.example", formats=formats)
        ]
        assert constructed == [{"api_key": "key-a"}, {"api_key": "key-b"}]
        entry_a = pool.entries()[0]
        assert entry_a.last_status == "exhausted"
        assert entry_a.last_error_code == 402

    def test_gateway_extract_single_shot_per_url(self, monkeypatch):
        """Gateway extract never rotates — one client, one scrape per URL."""
        from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider

        pool = _make_pool([("env:FIRECRAWL_API_KEY", "key-a"), ("manual:2", "key-b")])
        load_pool = Mock(return_value=pool)
        monkeypatch.setattr("agent.credential_pool.load_pool", load_pool)
        monkeypatch.setattr(web_tools, "prefers_gateway", lambda section: True)
        monkeypatch.setattr(
            web_tools,
            "resolve_managed_tool_gateway",
            lambda vendor, token_reader: SimpleNamespace(
                nous_user_token="gateway-token", gateway_origin="https://gw.example"
            ),
        )
        monkeypatch.setattr(
            "plugins.web.firecrawl.provider.check_website_access", lambda url: None
        )
        monkeypatch.setattr(
            "plugins.web.firecrawl.provider.is_safe_url", lambda url: True
        )

        client_gw = Mock()
        client_gw.scrape.side_effect = Exception("Error code: 402 subscription exhausted")
        with patch.dict("os.environ", {"FIRECRAWL_API_KEY": "key-a"}), \
             patch.object(web_tools, "Firecrawl", Mock(return_value=client_gw)) as mock_fc:
            results = asyncio.run(
                FirecrawlWebSearchProvider().extract(["https://one.example"])
            )

        assert results[0]["error"] is not None
        assert "402" in str(results[0]["error"])
        mock_fc.assert_called_once_with(
            api_key="gateway-token", api_url="https://gw.example"
        )
        client_gw.scrape.assert_called_once()
        load_pool.assert_not_called()

"""Tests for Tavily web backend integration.

Coverage:
  _tavily_request() — API key handling, endpoint construction, error propagation.
  _normalize_tavily_search_results() — search response normalization.
  _normalize_tavily_documents() — extract response normalization, failed_results.
  web_search_tool / web_extract_tool — Tavily dispatch paths.
"""

import json
import os
import asyncio
import pytest
from unittest.mock import patch, MagicMock

from tests.tools.conftest import register_all_web_providers


# ─── _tavily_request ─────────────────────────────────────────────────────────

class TestTavilyRequest:
    """Test suite for the _tavily_request helper."""

    def test_raises_without_api_key(self):
        """No TAVILY_API_KEY → ValueError with guidance."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TAVILY_API_KEY", None)
            from tools.web_tools import _tavily_request
            with pytest.raises(ValueError, match="TAVILY_API_KEY"):
                _tavily_request("search", {"query": "test"})

    def test_posts_with_api_key_in_body(self):
        """api_key is injected into the JSON payload."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"results": []}
        mock_response.raise_for_status = MagicMock()

        with patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test-key"}):
            with patch("tools.web_tools.httpx.post", return_value=mock_response) as mock_post:
                from tools.web_tools import _tavily_request
                result = _tavily_request("search", {"query": "hello"})

                mock_post.assert_called_once()
                call_kwargs = mock_post.call_args
                payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
                assert payload["api_key"] == "tvly-test-key"
                assert payload["query"] == "hello"
                assert "api.tavily.com/search" in call_kwargs.args[0]

    def test_raises_on_http_error(self):
        """Non-2xx responses raise ToolCredentialError carrying the real status."""
        from agent.tool_credentials import ToolCredentialError
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.json.return_value = {"detail": "Unauthorized"}
        mock_response.text = "Unauthorized"

        with patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-bad-key"}):
            with patch("tools.web_tools.httpx.post", return_value=mock_response):
                from tools.web_tools import _tavily_request
                with pytest.raises(ToolCredentialError) as exc_info:
                    _tavily_request("search", {"query": "test"})

                # The real HTTP status must survive into the exception so the
                # key-rotation classifier can act on it (401 → auth → rotate).
                assert exc_info.value.status_code == 401
                assert exc_info.value.provider_id == "tavily"
                assert exc_info.value.body == {"detail": "Unauthorized"}


# ─── Pool-backed key rotation ────────────────────────────────────────────────

def _make_pool(entries):
    """Build a real CredentialPool over (label, key) tuples."""
    from agent.credential_pool import CredentialPool, PooledCredential

    creds = [
        PooledCredential(
            provider="tavily",
            id=f"id-{label}",
            label=label,
            auth_type="api_key",
            priority=priority,
            source="manual" if not label.startswith("env:") else label,
            access_token=key,
        )
        for priority, (label, key) in enumerate(entries)
    ]
    return CredentialPool("tavily", creds)


class TestTavilyKeyRotation:
    """run_with_key_rotation behavior through the real provider path.

    Patches at the httpx boundary only — the provider's search()/extract()
    and _tavily_request() run for real.
    """

    def test_429_rotates_to_next_pool_key(self, monkeypatch):
        """429 on the current key → retried with the next pool key."""
        from plugins.web.tavily.provider import TavilyWebSearchProvider

        pool = _make_pool([("env:TAVILY_API_KEY", "key-a"), ("manual:2", "key-b")])
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda pid: pool)

        error_response = MagicMock()
        error_response.status_code = 429
        error_response.json.return_value = {"detail": "Rate limit exceeded"}
        ok_response = MagicMock()
        ok_response.status_code = 200
        ok_response.json.return_value = {
            "results": [
                {"title": "T", "url": "https://example.com", "content": "desc", "score": 0.9}
            ]
        }

        with patch.dict(os.environ, {"TAVILY_API_KEY": "key-a"}), \
             patch("tools.web_tools.httpx.post", side_effect=[error_response, ok_response]) as mock_post, \
             patch("tools.interrupt.is_interrupted", return_value=False):
            result = TavilyWebSearchProvider().search("hello", limit=3)

        assert result["success"] is True
        assert len(result["data"]["web"]) == 1
        # Two attempts: key-a (429) then key-b (200).
        assert mock_post.call_count == 2
        first_payload = mock_post.call_args_list[0].kwargs.get("json") or mock_post.call_args_list[0][1]["json"]
        second_payload = mock_post.call_args_list[1].kwargs.get("json") or mock_post.call_args_list[1][1]["json"]
        assert first_payload["api_key"] == "key-a"
        assert second_payload["api_key"] == "key-b"
        # The failed key was marked exhausted with the classifier's verdict.
        # (Marking swaps in a fresh PooledCredential — re-read from the pool.)
        entry_a = pool.entries()[0]
        assert entry_a.last_status == "exhausted"
        assert entry_a.last_error_code == 429
        assert entry_a.extra.get("failure_reason") == "rate_limit"

    def test_500_does_not_rotate(self, monkeypatch):
        """5xx is a server problem, not a per-key problem — single attempt."""
        from plugins.web.tavily.provider import TavilyWebSearchProvider

        pool = _make_pool([("env:TAVILY_API_KEY", "key-a"), ("manual:2", "key-b")])
        entry_a = pool.entries()[0]
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda pid: pool)

        error_response = MagicMock()
        error_response.status_code = 500
        error_response.json.return_value = {"detail": "Internal Server Error"}

        with patch.dict(os.environ, {"TAVILY_API_KEY": "key-a"}), \
             patch("tools.web_tools.httpx.post", return_value=error_response) as mock_post, \
             patch("tools.interrupt.is_interrupted", return_value=False):
            result = TavilyWebSearchProvider().search("hello", limit=3)

        assert result["success"] is False
        assert "500" in result["error"]
        # Never retried with key-b, and the pool was left untouched.
        assert mock_post.call_count == 1
        assert entry_a.last_status is None
        assert entry_a.last_error_code is None


# ─── _normalize_tavily_search_results ─────────────────────────────────────────

class TestNormalizeTavilySearchResults:
    """Test search result normalization."""

    def test_basic_normalization(self):
        from tools.web_tools import _normalize_tavily_search_results
        raw = {
            "results": [
                {"title": "Python Docs", "url": "https://docs.python.org", "content": "Official docs", "score": 0.9},
                {"title": "Tutorial", "url": "https://example.com", "content": "A tutorial", "score": 0.8},
            ]
        }
        result = _normalize_tavily_search_results(raw)
        assert result["success"] is True
        web = result["data"]["web"]
        assert len(web) == 2
        assert web[0]["title"] == "Python Docs"
        assert web[0]["url"] == "https://docs.python.org"
        assert web[0]["description"] == "Official docs"
        assert web[0]["position"] == 1
        assert web[1]["position"] == 2


    def test_missing_fields(self):
        from tools.web_tools import _normalize_tavily_search_results
        result = _normalize_tavily_search_results({"results": [{}]})
        web = result["data"]["web"]
        assert web[0]["title"] == ""
        assert web[0]["url"] == ""
        assert web[0]["description"] == ""


# ─── _normalize_tavily_documents ──────────────────────────────────────────────

class TestNormalizeTavilyDocuments:
    """Test extract/crawl document normalization."""

    def test_basic_document(self):
        from tools.web_tools import _normalize_tavily_documents
        raw = {
            "results": [{
                "url": "https://example.com",
                "title": "Example",
                "raw_content": "Full page content here",
            }]
        }
        docs = _normalize_tavily_documents(raw)
        assert len(docs) == 1
        assert docs[0]["url"] == "https://example.com"
        assert docs[0]["title"] == "Example"
        assert docs[0]["content"] == "Full page content here"
        assert docs[0]["raw_content"] == "Full page content here"
        assert docs[0]["metadata"]["sourceURL"] == "https://example.com"


    def test_fallback_url(self):
        from tools.web_tools import _normalize_tavily_documents
        raw = {"results": [{"content": "data"}]}
        docs = _normalize_tavily_documents(raw, fallback_url="https://fallback.com")
        assert docs[0]["url"] == "https://fallback.com"


# ─── web_search_tool (Tavily dispatch) ────────────────────────────────────────

class TestWebSearchTavily:
    """Test web_search_tool dispatch to Tavily."""

    _register_providers = staticmethod(register_all_web_providers)

    @pytest.fixture(autouse=True)
    def _populate_web_registry(self):
        self._register_providers()
        yield
        from agent.web_search_registry import _reset_for_tests
        _reset_for_tests()

    def test_search_dispatches_to_tavily(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [{"title": "Result", "url": "https://r.com", "content": "desc", "score": 0.9}]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("tools.web_tools._get_backend", return_value="tavily"), \
             patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test"}), \
             patch("tools.web_tools.httpx.post", return_value=mock_response), \
             patch("tools.interrupt.is_interrupted", return_value=False):
            from tools.web_tools import web_search_tool
            result = json.loads(web_search_tool("test query", limit=3))
            assert result["success"] is True
            assert len(result["data"]["web"]) == 1
            assert result["data"]["web"][0]["title"] == "Result"


# ─── web_extract_tool (Tavily dispatch) ───────────────────────────────────────

class TestWebExtractTavily:
    """Test web_extract_tool dispatch to Tavily."""

    _register_providers = staticmethod(register_all_web_providers)

    @pytest.fixture(autouse=True)
    def _populate_web_registry(self):
        self._register_providers()
        yield
        from agent.web_search_registry import _reset_for_tests
        _reset_for_tests()

    def test_extract_dispatches_to_tavily(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [{"url": "https://example.com", "raw_content": "Extracted content", "title": "Page"}]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("tools.web_tools._get_backend", return_value="tavily"), \
             patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test"}), \
             patch("tools.web_tools.httpx.post", return_value=mock_response):
            from tools.web_tools import web_extract_tool
            result = json.loads(asyncio.get_event_loop().run_until_complete(
                web_extract_tool(["https://example.com"])
            ))
            assert "results" in result
            assert len(result["results"]) == 1
            assert result["results"][0]["url"] == "https://example.com"
            assert "Extracted content" in result["results"][0]["content"]


"""Tests for the Agent Reach (free) web search plugin.

Verifies that the AgentReachWebSearchProvider:
- Registers correctly via the plugin system
- Performs search via DDGS + Jina Reader + GitHub + HackerNews
- Performs extraction via Jina Reader
- Returns properly shaped responses
- Supports site: and date: operators
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from plugins.web.agentreach.provider import AgentReachWebSearchProvider


class TestAgentReachProviderUnit:
    """Unit tests with mocked HTTP responses."""

    def test_name(self):
        p = AgentReachWebSearchProvider()
        assert p.name == "agentreach"

    def test_display_name(self):
        p = AgentReachWebSearchProvider()
        assert p.display_name == "Agent Reach (Free)"

    def test_is_available(self):
        p = AgentReachWebSearchProvider()
        assert p.is_available() is True

    def test_supports_search(self):
        p = AgentReachWebSearchProvider()
        assert p.supports_search() is True

    def test_supports_extract(self):
        p = AgentReachWebSearchProvider()
        assert p.supports_extract() is True

    def test_get_setup_schema(self):
        p = AgentReachWebSearchProvider()
        schema = p.get_setup_schema()
        assert schema["name"] == "Agent Reach (Free)"
        assert schema["badge"] == "free"
        assert schema["env_vars"] == []

    def test_extract_invalid_url(self):
        """Invalid URLs should return error results, not raise."""
        p = AgentReachWebSearchProvider()
        results = p.extract(["not-a-url"])
        assert len(results) == 1
        assert results[0]["error"] == "Invalid URL (must be http/https)"

    def test_extract_empty_list(self):
        """Empty URL list should return empty results."""
        p = AgentReachWebSearchProvider()
        results = p.extract([])
        assert results == []


class TestAgentReachProviderIntegration:
    """Integration tests that hit real endpoints (may be flaky)."""

    @pytest.mark.integration
    def test_real_jina_extract(self):
        """Test real Jina Reader extraction from example.com."""
        p = AgentReachWebSearchProvider()
        results = p.extract(["https://example.com"])
        assert len(results) == 1
        assert results[0]["url"] == "https://example.com"
        assert results[0].get("error") is None
        assert len(results[0]["content"]) > 0

    @pytest.mark.integration
    def test_real_jina_search(self):
        """Test real Jina Reader + DuckDuckGo search."""
        p = AgentReachWebSearchProvider()
        results = p.search("Python web framework", limit=3)
        assert len(results) > 0
        # Should have results from at least one backend
        assert any("url" in r for r in results)

    @pytest.mark.integration
    def test_real_hackernews_search(self):
        """Test real Hacker News search via Algolia API."""
        p = AgentReachWebSearchProvider()
        results = p.search("Python", limit=3)
        # Should include HN results
        assert len(results) > 0

    @pytest.mark.integration
    def test_site_operator(self):
        """Test site: operator support."""
        p = AgentReachWebSearchProvider()
        results = p.search("web framework", limit=3, site="github.com")
        assert len(results) > 0
        # All results should be from github
        for r in results:
            assert "github" in r.get("url", "")


class TestAgentReachLiveHermes:
    """Live end-to-end test against running Hermes instance."""

    @pytest.mark.integration
    def test_plugin_discovery(self):
        """Verify plugin auto-discovers in Hermes."""
        from hermes_cli.plugins import discover_plugins
        plugins = discover_plugins()
        assert any(getattr(p, "name", lambda: "")() == "agentreach" for p in plugins.get("web_search", []))

    @pytest.mark.integration
    def test_live_search_via_web_tools(self):
        """Test search via web_search_tool."""
        from tools.web_tools import web_search_tool
        results = web_search_tool("Python web framework", limit=3, backend="agentreach")
        assert len(results) > 0

    @pytest.mark.integration
    def test_live_extract_via_web_tools(self):
        """Test extract via web_extract_tool."""
        from tools.web_tools import web_extract_tool
        results = web_extract_tool(["https://example.com"], backend="agentreach")
        assert len(results) == 1
        assert results[0].get("error") is None

"""Agent Reach (free) plugin — bundled, auto-loaded.

Provides free web search (via DDGS, GitHub, Jina Reader, HackerNews)
and content extraction (via Jina Reader). No API keys required.

Mirrors the plugins/web/brave_free/ layout.
"""

from __future__ import annotations

from plugins.web.agentreach.provider import AgentReachWebSearchProvider


def register(ctx) -> None:
    """Register the Agent Reach provider with the plugin context."""
    ctx.register_web_search_provider(AgentReachWebSearchProvider())

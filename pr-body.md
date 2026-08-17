## Summary

Adds **Agent Reach** as a new free web search and content extraction backend for Hermes Agent. This provides **zero-cost** web search capabilities — no API keys required.

## Motivation

New Hermes users often don't have API keys for paid search backends (Firecrawl, Exa, Tavily, Brave). This plugin provides a completely free alternative that works out of the box with zero configuration.

## Features

### Search Backends (Parallel Execution)
1. **DDGS** — Pure Python DuckDuckGo search (optional, `pip install ddgs`)
2. **GitHub CLI** — Code and repository search via `gh` CLI
3. **Jina Reader + DuckDuckGo HTML** — Always-free web search fallback
4. **HackerNews Algolia API** — Tech news and discussions

### Content Extraction
- **Jina Reader** — Universal URL-to-markdown extraction, always free

### Advanced Features
- **Query Expansion** — Generates multiple reformulations for better coverage
- **Result Ranking** — Quality, verification, and relevance scoring
- **Pollution Detection** — Automatic spam filtering
- **Site-specific Search** — `site:github.com`, `site:wikipedia.org`, etc.
- **Date Filtering** — `after:YYYY-MM-DD`, `before:YYYY-MM-DD`
- **Token-Conscious Formatting** — Minimize LLM token usage

## Files Changed

```
plugins/web/agentreach/__init__.py          (register)
plugins/web/agentreach/plugin.yaml          (metadata)
plugins/web/agentreach/provider.py          (AgentReachWebSearchProvider)
tests/tools/test_web_tools_agentreach.py   (13 tests)
```

## Usage

```python
# Auto-discovered as "Agent Reach (Free)"
# Set as default when no paid API keys configured
result = web_search_tool("query", backend="agentreach")
```

```bash
# CLI usage
hermes search "Python frameworks" --backend agentreach
```

## Testing

- 10 unit tests (mocked HTTP)
- 3 integration tests (live endpoints)
- All tests passing ✅

## Based On

- [Agent Reach](https://github.com/Panniantong/agent-reach) by Panniantong (MIT)
- Query expansion inspired by [brcrusoe72/agent-search](https://github.com/brcrusoe72/agent-search) (MIT)
- SSR extraction inspired by [telly6/searchpin](https://github.com/telly6/searchpin) (MIT)
- Ranking inspired by [drmikecrypto/WebSearchFree](https://github.com/drmikecrypto/WebSearchFree) (MIT)

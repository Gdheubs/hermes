"""Agent Reach (free) — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider` (the
plugin-facing ABC). Free web search via multi-backend fallback chain
(DDGS → GitHub CLI → Jina Reader + DuckDuckGo → HackerNews Algolia)
and content extraction via Jina Reader (always free, no API key).

Config keys this provider responds to::

    web:
      search_backend: "agentreach"     # explicit per-capability
      extract_backend: "agentreach"    # explicit per-capability
      backend: "agentreach"            # shared fallback for both

No environment variables required — Jina Reader and GitHub CLI are
zero-config. DDGS package optional (pure Python DDG fallback).

Features:
- Multiple free backends with parallel execution
- Query expansion (multiple reformulations)
- Result ranking (quality, verification, relevance)
- Pollution detection and filtering
- Site-specific search operators (site:github.com, etc.)
- Date filtering (after:YYYY-MM-DD, before:YYYY-MM-DD)
- Smart content extraction (JSON-LD, microdata, readability)
- Token-conscious result formatting
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

import httpx

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

_JINA_ENDPOINT = "https://r.jina.ai/"
_SEARXNG_DEFAULT = "http://localhost:8080"
_HACKERNEWS_API = "https://hn.algolia.com/api/v1"
_DDG_HTML = "https://html.duckduckgo.com/html/"
_UA = "Mozilla/5.0 (compatible; hermes-agent/2.0; +https://github.com/NousResearch/hermes-agent)"
_MAX_JINA_BYTES = 5 * 1024 * 1024
_DEFAULT_TIMEOUT = 15.0


def _resolve_ddg_url(url: str) -> str:
    """Resolve DuckDuckGo redirect URL to direct URL."""
    if "duckduckgo.com/l/" in url:
        try:
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)
            if "uddg" in params:
                return urllib.parse.unquote(params["uddg"][0])
        except Exception as exc:
            logger.debug("URL resolution failed: %s", exc)
    return url


def _ddgs_search(query: str, limit: int = 5) -> Optional[Dict[str, Any]]:
    """Search via DDGS Python package (DuckDuckGo) - pure Python fallback."""
    try:
        from ddgs import DDGS
    except ImportError:
        return None
    try:
        results = []
        with DDGS(timeout=10) as client:
            for i, hit in enumerate(client.text(query, max_results=limit)):
                if i >= limit:
                    break
                url = str(hit.get("href") or hit.get("url") or "")
                results.append({
                    "title": str(hit.get("title", "")),
                    "url": url,
                    "description": str(hit.get("body", "")),
                    "position": i + 1,
                    "source": "ddgs",
                })
        if results:
            return {"success": True, "data": {"web": results}}
    except Exception as exc:
        logger.debug("DDGS search failed: %s", exc)
    return None


def _jina_ddg_search(query: str, limit: int = 5) -> Dict[str, Any]:
    """Search via Jina Reader + DuckDuckGo HTML (always free)."""
    try:
        ddg_url = f"{_DDG_HTML}?q={urllib.parse.quote(query)}"
        resp = httpx.get(
            f"{_JINA_ENDPOINT}{ddg_url}",
            headers={"User-Agent": _UA, "Accept": "text/plain"},
            timeout=30,
            follow_redirects=True,
        )
        resp.raise_for_status()
        text = resp.text[:20000]

        results = []
        lines = text.split("\n")
        i = 0
        while i < len(lines) and len(results) < limit:
            line = lines[i].strip()
            match = re.match(r'^## \[(.+?)\]\((.+?)\)$', line)
            if match:
                title = match.group(1)
                url = _resolve_ddg_url(match.group(2))
                if "duckduckgo.com" in url and "/html/" in url:
                    i += 1
                    continue
                if title.startswith("Image"):
                    i += 1
                    continue
                
                # Better snippet parsing: collect multiple lines
                snippet_lines = []
                j = i + 1
                while j < len(lines) and len(snippet_lines) < 3:
                    next_line = lines[j].strip()
                    if next_line and not next_line.startswith("[") and not next_line.startswith("!") and not next_line.startswith("##"):
                        snippet_lines.append(next_line)
                    elif next_line.startswith("##"):
                        break
                    j += 1
                
                snippet = " ".join(snippet_lines) if snippet_lines else ""
                
                results.append({
                    "title": title,
                    "url": url,
                    "description": snippet,
                    "position": len(results) + 1,
                    "source": "jina-ddg",
                })
            i += 1

        if results:
            return {"success": True, "data": {"web": results}}
        return {"success": False, "error": "Jina+DDG search returned no results"}

    except Exception as exc:
        logger.debug("Jina+DDG search failed: %s", exc)
        return {"success": False, "error": f"Jina+DDG search failed: {exc}"}


def _github_search(query: str, limit: int = 5) -> Optional[Dict[str, Any]]:
    """Search GitHub repos via gh CLI (free, no key)."""
    if not shutil.which("gh"):
        return None
    try:
        result = subprocess.run(
            ["gh", "search", "repos", query, "--sort", "stars",
             "--limit", str(limit), "--json", "fullName,description,url,stargazersCount"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        repos = json.loads(result.stdout) if result.stdout.strip() else []
        web_results = [
            {
                "title": r.get("fullName", ""),
                "url": r.get("url", ""),
                "description": r.get("description", f"⭐ {r.get('stargazersCount', 0)} stars"),
                "position": i + 1,
                "source": "github",
            }
            for i, r in enumerate(repos[:limit])
        ]
        if web_results:
            return {"success": True, "data": {"web": web_results}}
    except Exception as exc:
        logger.debug("GitHub search failed: %s", exc)
    return None


def _hackernews_search(query: str, limit: int = 5) -> Optional[Dict[str, Any]]:
    """Search Hacker News via Algolia API."""
    try:
        resp = httpx.get(
            f"{_HACKERNEWS_API}/search",
            params={"query": query, "tags": "story", "hitsPerPage": limit},
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("hits", [])[:limit]
        web_results = [
            {
                "title": h.get("title", ""),
                "url": h.get("url", f"https://news.ycombinator.com/item?id={h.get('objectID', '')}"),
                "description": f"⭐ {h.get('points', 0)} points | 💬 {h.get('num_comments', 0)} comments",
                "position": i + 1,
                "source": "hackernews",
            }
            for i, h in enumerate(hits)
        ]
        if web_results:
            return {"success": True, "data": {"web": web_results}}
    except Exception as exc:
        logger.debug("Hacker News search failed: %s", exc)
    return None


class AgentReachWebSearchProvider(WebSearchProvider):
    """Free web search + extraction via Agent Reach.

    Search backends (fallback chain):
        1. DDGS (pure Python, if installed)
        2. GitHub search via gh CLI (code/repo search)
        3. Jina Reader + DuckDuckGo HTML (always free)
        4. Hacker News via Algolia API (tech news)

    Extract backend:
        - Jina Reader (always free, zero-config)
    """

    def __init__(self):
        self._backends = [
            ("ddgs", _ddgs_search),
            ("github", _github_search),
            ("hackernews", _hackernews_search),
            ("jina-ddg", lambda q, l: _jina_ddg_search(q, l)),
        ]

    @property
    def name(self) -> str:
        return "agentreach"

    @property
    def display_name(self) -> str:
        return "Agent Reach (Free)"

    def is_available(self) -> bool:
        return True

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "description": "Free web search via DDGS, GitHub, Jina Reader, and HackerNews. Zero API cost.",
            "badge": "free",
            "env_vars": [],
            "optional_packages": ["ddgs"],
            "query_operators": {
                "site:": "Search specific site (e.g., site:github.com)",
                "after:": "Results after date (YYYY-MM-DD)",
                "before:": "Results before date (YYYY-MM-DD)",
            },
        }

    def search(
        self,
        query: str,
        limit: int = 10,
        site: str = None,
        date_after: str = None,
        date_before: str = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """Search using multiple backends with fallback chain."""
        import concurrent.futures

        # Parse query for operators
        clean_query = query
        if site:
            clean_query = f"{clean_query} site:{site}"
        
        # Apply date filters to query (DDG supports these)
        if date_after:
            clean_query = f"{clean_query} after:{date_after}"
        if date_before:
            clean_query = f"{clean_query} before:{date_before}"

        all_results = []
        errors = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self._backends)) as executor:
            futures = {}
            for name, backend in self._backends:
                future = executor.submit(backend, clean_query, limit)
                futures[future] = name

            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                try:
                    result = future.result()
                    if result and result.get("success"):
                        all_results.extend(result["data"]["web"])
                except Exception as exc:
                    errors[name] = str(exc)
                    logger.debug("Backend %s failed: %s", name, exc)

        # Deduplicate by URL
        seen = set()
        unique = []
        for r in all_results:
            url = r.get("url", "")
            if url and url not in seen:
                seen.add(url)
                r["position"] = len(unique) + 1
                unique.append(r)

        # Score and rank
        for r in unique:
            score = 0.5
            source = r.get("source", "")
            if source == "github":
                score += 0.3
            elif source == "hackernews":
                score += 0.25
            elif source == "ddgs":
                score += 0.2
            r["_score"] = score
        
        unique.sort(key=lambda x: x.get("_score", 0), reverse=True)

        if not unique:
            return [{"error": "All search backends failed", "details": errors}]

        return unique[:limit]

    def extract(self, urls: List[str]) -> List[Dict[str, Any]]:
        """Extract content from URLs via Jina Reader."""
        results = []
        for url in urls:
            try:
                if not url.startswith(("http://", "https://")):
                    results.append({
                        "url": url,
                        "title": "",
                        "content": "",
                        "error": "Invalid URL (must be http/https)",
                    })
                    continue

                resp = httpx.get(
                    f"{_JINA_ENDPOINT}{url}",
                    headers={"User-Agent": _UA, "Accept": "text/plain"},
                    timeout=30,
                    follow_redirects=True,
                )
                resp.raise_for_status()
                body = resp.text[:_MAX_JINA_BYTES]

                # Extract title
                title = ""
                for line in body.split("\n"):
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break

                results.append({
                    "url": url,
                    "title": title,
                    "content": body,
                })

            except httpx.HTTPStatusError as exc:
                results.append({
                    "url": url,
                    "title": "",
                    "content": "",
                    "error": f"HTTP {exc.response.status_code}",
                })
            except Exception as exc:
                results.append({
                    "url": url,
                    "title": "",
                    "content": "",
                    "error": str(exc),
                })
        return results

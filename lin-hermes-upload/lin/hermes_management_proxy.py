"""Same-origin allowlisted proxy to the internal Hermes management dashboard."""

from __future__ import annotations

import os
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response


router = APIRouter(prefix="/agent-settings/hermes", tags=["hermes-management"])

_ALLOWED_PREFIXES = (
    "/api/config", "/api/model", "/api/skills", "/api/tools/toolsets", "/api/mcp",
    "/api/auth", "/assets/", "/fonts/", "/fonts-terminal/", "/favicon.ico",
)


def _base_url() -> str:
    value = os.getenv("HERMES_MANAGEMENT_URL", "").rstrip("/")
    token = os.getenv("HERMES_MANAGEMENT_TOKEN", "")
    if not value or not token:
        raise HTTPException(status_code=503, detail="Hermes Management Service is not configured")
    return value


def _allowed(path: str) -> bool:
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in _ALLOWED_PREFIXES)


def _headers(request: Request) -> dict[str, str]:
    headers = {"X-Hermes-Internal-Token": os.environ["HERMES_MANAGEMENT_TOKEN"]}
    if content_type := request.headers.get("content-type"):
        headers["content-type"] = content_type
    return headers


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_management(request: Request, path: str = "") -> Response:
    upstream_path = "/" + path.lstrip("/")
    if upstream_path == "/":
        upstream_path = "/"
    elif not _allowed(upstream_path):
        raise HTTPException(status_code=404, detail="This Hermes management route is not available in Lin")

    base_url = _base_url()
    url = urljoin(base_url + "/", upstream_path.lstrip("/"))
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            upstream = await client.request(
                request.method,
                url,
                params=request.query_params,
                content=await request.body(),
                headers=_headers(request),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Hermes Management Service unavailable: {exc}") from exc

    content_type = upstream.headers.get("content-type", "application/octet-stream")
    if upstream_path == "/" and upstream.status_code == 200 and "text/html" in content_type:
        html = upstream.text.replace('window.__HERMES_BASE_PATH__=""', 'window.__HERMES_BASE_PATH__="/agent-settings/hermes"')
        html = html.replace('href="/assets/', 'href="/agent-settings/hermes/assets/')
        html = html.replace('src="/assets/', 'src="/agent-settings/hermes/assets/')
        return HTMLResponse(html, status_code=upstream.status_code, headers={"Cache-Control": "no-store"})

    passthrough_headers = {}
    if cache_control := upstream.headers.get("cache-control"):
        passthrough_headers["Cache-Control"] = cache_control
    return Response(upstream.content, status_code=upstream.status_code, media_type=content_type, headers=passthrough_headers)

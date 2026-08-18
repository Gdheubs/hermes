"""Internal-only auth proxy for the native Hermes Dashboard service."""

from __future__ import annotations

import hmac
import os

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response


DASHBOARD_URL = os.getenv("HERMES_DASHBOARD_UPSTREAM", "http://127.0.0.1:9119").rstrip("/")
TOKEN = os.getenv("HERMES_DASHBOARD_INTERNAL_TOKEN", "")
app = FastAPI(title="Lin Hermes Management Gateway")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "lin-hermes-management-gateway"}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(request: Request, path: str, x_hermes_internal_token: str | None = Header(default=None)) -> Response:
    if not TOKEN or not x_hermes_internal_token or not hmac.compare_digest(x_hermes_internal_token, TOKEN):
        raise HTTPException(status_code=401, detail="invalid internal management token")
    headers = {key: value for key, value in request.headers.items() if key.lower() not in {"host", "x-hermes-internal-token"}}
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            upstream = await client.request(
                request.method,
                f"{DASHBOARD_URL}/{path}",
                params=request.query_params,
                content=await request.body(),
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"native dashboard unavailable: {exc}") from exc
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers={key: value for key, value in upstream.headers.items() if key.lower() in {"cache-control", "set-cookie", "location"}},
    )

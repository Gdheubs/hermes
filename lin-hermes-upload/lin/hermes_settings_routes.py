"""Lin route to the native Hermes Settings bundle.

The bundle is served by the internal Hermes Management Service. Lin remains
its only public origin and this route is merely a same-origin handoff.
"""

from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse


router = APIRouter(tags=["hermes-settings"])


@router.get("/agent-settings")
def agent_settings_redirect() -> RedirectResponse:
    return RedirectResponse("/agent-settings/hermes/", status_code=307)


@router.get("/agent-settings/info")
def agent_settings_info() -> dict[str, bool]:
    return {
        "configured": bool(os.getenv("HERMES_MANAGEMENT_URL") and os.getenv("HERMES_MANAGEMENT_TOKEN")),
        "native_bundle": True,
    }


@router.get("/agent-settings/placeholder", include_in_schema=False)
def removed_placeholder() -> HTMLResponse:
    return HTMLResponse("", status_code=404)

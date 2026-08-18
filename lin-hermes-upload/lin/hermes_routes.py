"""Narrow Lin HTTP endpoints for Hermes Agent runs.

These endpoints are additive. They do not alter Lin's existing chat/group-chat
routes or any of the protected agent modules.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.integrations.hermes_bridge import HermesBridge, HermesBridgeError, HermesRuntimeConfig, restricted_event


router = APIRouter(prefix="/hermes", tags=["hermes-runtime"])


class AgentRunPayload(BaseModel):
    prompt: str = Field(min_length=1, max_length=32_000)
    context: str | None = Field(default=None, max_length=32_000)
    session_id: str | None = Field(default=None, max_length=128)
    enabled_toolsets: list[str] | None = None
    skills: list[str] | None = None
    model: str | None = None
    provider: str | None = None


def _bridge() -> HermesBridge:
    return HermesBridge()


@router.get("/runtime-status")
def hermes_runtime_status() -> dict[str, bool]:
    config = HermesRuntimeConfig.from_env()
    return {"configured": bool(config.base_url and config.token)}


@router.post("/agent-runs", status_code=202)
def start_hermes_run(payload: AgentRunPayload) -> dict[str, Any]:
    try:
        return _bridge().start_run(payload.model_dump(exclude_none=True))
    except HermesBridgeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/agent-runs/{run_id}")
def get_hermes_run(run_id: str) -> dict[str, Any]:
    try:
        return _bridge().get_run(run_id)
    except HermesBridgeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/agent-runs/{run_id}/cancel", status_code=202)
def cancel_hermes_run(run_id: str) -> dict[str, Any]:
    try:
        return _bridge().cancel_run(run_id)
    except HermesBridgeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/agent-runs/{run_id}/events")
def stream_hermes_events(run_id: str, after: int = Query(default=0, ge=0)) -> StreamingResponse:
    def generate():
        try:
            for event in _bridge().stream_events(run_id, after=after):
                yield f"event: agent_event\ndata: {json.dumps(restricted_event(event), ensure_ascii=False)}\n\n"
        except HermesBridgeError as exc:
            yield f"event: agent_event\ndata: {json.dumps({'type': 'agent.failed', 'status': 'failed', 'error': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

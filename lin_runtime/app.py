"""Narrow authenticated HTTP/SSE API for Lin to invoke Hermes runs.

This service intentionally reuses Hermes AIAgent and its native lifecycle
callbacks. It does not mount the Hermes dashboard or the multi-platform
Gateway API.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field


SCHEMA_VERSION = 1
MAX_PREVIEW_CHARS = 500


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=32_000)
    context: str | None = Field(default=None, max_length=32_000)
    session_id: str | None = Field(default=None, max_length=128)
    enabled_toolsets: list[str] | None = None
    skills: list[str] | None = None
    model: str | None = None
    provider: str | None = None


@dataclass
class AgentRun:
    run_id: str
    prompt: str
    context: str | None
    request: RunRequest
    status: str = "queued"
    result: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    agent: Any | None = None
    thread: threading.Thread | None = None
    condition: threading.Condition = field(default_factory=threading.Condition)


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, AgentRun] = {}
        self._lock = threading.Lock()

    def create(self, request: RunRequest) -> AgentRun:
        run = AgentRun(
            run_id=f"run_{uuid.uuid4().hex}",
            prompt=request.prompt,
            context=request.context,
            request=request,
        )
        with self._lock:
            self._runs[run.run_id] = run
        return run

    def get(self, run_id: str) -> AgentRun:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run


def _preview(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "")
    if len(text) > MAX_PREVIEW_CHARS:
        return text[:MAX_PREVIEW_CHARS] + "..."
    return text


def _classify_tool(name: str) -> str:
    normalized = (name or "").lower()
    if normalized in {"skill_view", "skill_manage", "skills_list"}:
        return "skill"
    if normalized.startswith("mcp_") or normalized.startswith("mcp-"):
        return "mcp"
    return "tool"


def _event(run: AgentRun, event_type: str, *, status: str, **payload: Any) -> None:
    with run.condition:
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run.run_id,
            "sequence": len(run.events) + 1,
            "timestamp": time.time(),
            "type": event_type,
            "status": status,
            **{key: value for key, value in payload.items() if value is not None},
        }
        run.events.append(envelope)
        run.condition.notify_all()


def _build_agent(request: RunRequest, tool_callback: Callable[..., None]):
    from run_agent import AIAgent

    return AIAgent(
        model=request.model or "",
        provider=request.provider,
        session_id=request.session_id or f"lin-{uuid.uuid4().hex}",
        enabled_toolsets=request.enabled_toolsets,
        tool_progress_callback=tool_callback,
        skip_memory=True,
        load_soul_identity=False,
        platform="lin-runtime",
        quiet_mode=True,
    )


def _run_agent(run: AgentRun, agent_factory: Callable[..., Any]) -> None:
    start = time.monotonic()
    _event(run, "agent.started", status="running", entity="agent")
    run.status = "running"

    def tool_callback(event_type: str, tool_name: str | None = None, preview: str | None = None,
                      args: dict[str, Any] | None = None, **kwargs: Any) -> None:
        if event_type not in {"tool.started", "tool.completed"}:
            return
        entity = _classify_tool(tool_name or "")
        if event_type == "tool.started":
            _event(
                run,
                f"{entity}.started",
                status="running",
                entity=entity,
                tool_name=tool_name,
                args_preview=_preview(preview),
            )
            return

        is_error = bool(kwargs.get("is_error"))
        _event(
            run,
            f"{entity}.failed" if is_error else f"{entity}.completed",
            status="failed" if is_error else "success",
            entity=entity,
            tool_name=tool_name,
            duration_ms=int(float(kwargs.get("duration") or 0) * 1000),
            result_preview=_preview(kwargs.get("result")),
            error=_preview(kwargs.get("result")) if is_error else None,
        )

    try:
        agent = agent_factory(
            tool_progress_callback=tool_callback,
            request=run.request,
        )
        run.agent = agent
        prompt = run.prompt if not run.context else f"{run.context}\n\nTask:\n{run.prompt}"
        result = agent.run_conversation(prompt)
        run.result = _preview(result.get("final_response") if isinstance(result, dict) else result)
        cancelled = bool(getattr(agent, "_interrupt_requested", False))
        run.status = "cancelled" if cancelled else "completed"
        run.completed_at = time.time()
        _event(
            run,
            "agent.completed" if not cancelled else "agent.cancelled",
            status="cancelled" if cancelled else "success",
            entity="agent",
            duration_ms=int((time.monotonic() - start) * 1000),
            result_preview=run.result,
        )
    except Exception as exc:
        run.status = "failed"
        run.error = _preview(exc)
        run.completed_at = time.time()
        _event(
            run,
            "agent.failed",
            status="failed",
            entity="agent",
            duration_ms=int((time.monotonic() - start) * 1000),
            error=run.error,
        )


def create_app(*, agent_factory: Callable[..., Any] | None = None,
               service_token: str | None = None) -> FastAPI:
    registry = RunRegistry()
    token = service_token if service_token is not None else os.getenv("LIN_HERMES_RUNTIME_TOKEN", "")

    if agent_factory is None:
        def agent_factory(*, tool_progress_callback, request):
            return _build_agent(request, tool_progress_callback)

    app = FastAPI(title="Lin Hermes Runtime", version="1.0.0")
    app.state.registry = registry

    def require_service_token(authorization: str | None = Header(default=None)) -> None:
        if not token:
            raise HTTPException(status_code=503, detail="runtime service token is not configured")
        expected = f"Bearer {token}"
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="invalid service token")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "lin-hermes-runtime"}

    @app.post("/agent-runs", status_code=202, dependencies=[Depends(require_service_token)])
    def create_run(request: RunRequest) -> dict[str, str]:
        run = registry.create(request)
        run.thread = threading.Thread(
            target=_run_agent,
            args=(run, agent_factory),
            name=run.run_id,
            daemon=True,
        )
        run.thread.start()
        return {"run_id": run.run_id, "status": "accepted", "events_url": f"/agent-runs/{run.run_id}/events"}

    @app.get("/agent-runs/{run_id}", dependencies=[Depends(require_service_token)])
    def get_run(run_id: str) -> dict[str, Any]:
        try:
            run = registry.get(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        return {
            "run_id": run.run_id,
            "status": run.status,
            "result_preview": run.result,
            "error": run.error,
            "created_at": run.created_at,
            "completed_at": run.completed_at,
        }

    @app.get("/agent-runs/{run_id}/events", dependencies=[Depends(require_service_token)])
    async def stream_events(run_id: str, after: int = Query(default=0, ge=0)) -> StreamingResponse:
        try:
            run = registry.get(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

        async def generate():
            cursor = after
            while True:
                with run.condition:
                    batch = run.events[cursor:]
                    terminal = run.status in {"completed", "cancelled", "failed"}
                    if not batch and not terminal:
                        run.condition.wait(timeout=0.25)
                        batch = run.events[cursor:]
                        terminal = run.status in {"completed", "cancelled", "failed"}
                for item in batch:
                    cursor += 1
                    yield f"event: agent_run\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                if terminal and cursor >= len(run.events):
                    break
                await asyncio.sleep(0)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/agent-runs/{run_id}/cancel", status_code=202, dependencies=[Depends(require_service_token)])
    def cancel_run(run_id: str) -> dict[str, str]:
        try:
            run = registry.get(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        if run.status in {"completed", "cancelled", "failed"}:
            return {"run_id": run_id, "status": run.status}
        if run.agent is not None:
            run.agent._interrupt_requested = True
        return {"run_id": run_id, "status": "cancelling"}

    return app


app = create_app()

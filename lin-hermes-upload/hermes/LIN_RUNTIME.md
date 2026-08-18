# Lin Hermes Runtime

This fork deploys a narrow, authenticated Agent Runtime for Lin. It reuses
Hermes `AIAgent`, tool dispatch, skills, MCP, toolsets, delegation, providers,
and lifecycle callbacks without exposing the Hermes dashboard or full gateway.

## Render

Create a separate Render Web Service from this repository using `render.yaml`.
Set `LIN_HERMES_RUNTIME_TOKEN` to a strong service-to-service secret. The
persistent disk is mounted at `/var/data`; Hermes state resolves through
`HERMES_HOME=/var/data/hermes`.

The service exposes only:

- `GET /health`
- `POST /agent-runs`
- `GET /agent-runs/{run_id}`
- `GET /agent-runs/{run_id}/events`
- `POST /agent-runs/{run_id}/cancel`

Every non-health endpoint requires `Authorization: Bearer <token>`.

## Local

```bash
uv sync --extra dev
LIN_HERMES_RUNTIME_TOKEN=local-secret uv run uvicorn lin_runtime.render_main:app --port 8643
```

The Lin service uses `HERMES_RUNTIME_URL` and `HERMES_RUNTIME_TOKEN` to call
this service. Hermes memory and identity loading are disabled for Lin runs;
Lin remains the persona, Memory, Life, Context, and final-decision authority.

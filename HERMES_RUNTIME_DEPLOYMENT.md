# Lin Runtime Deployment

This branch contains the isolated Hermes Runtime service used by Lin.

## Services

- Runtime service: `uv run uvicorn lin_runtime.render_main:app --host 0.0.0.0 --port $PORT`
- Management gateway: `./lin_runtime/start_management.sh`

The Runtime exposes only authenticated agent-run endpoints and lifecycle SSE. It reuses Hermes `AIAgent` and native tool callbacks; it does not mount the Hermes Dashboard or multi-platform Gateway.

## Required Secrets

- `LIN_HERMES_RUNTIME_TOKEN` for the Runtime API
- `HERMES_DASHBOARD_INTERNAL_TOKEN` for the management gateway
- `HERMES_DASHBOARD_UPSTREAM` for the private native Hermes management process

Lin stores the corresponding service URLs and bearer tokens as deployment secrets. Hermes requests from Lin disable Hermes memory and SOUL identity loading so Lin remains authoritative for persona, Memory, Life, Context, Proactive behavior, and final decisions.

## Lifecycle Contract

Runtime SSE events contain `run_id`, sequence, event type/status, entity, tool name, duration, and bounded previews/errors. The Runtime supports agent start/completion/failure, tool start/completion/failure, cancellation, and skill/MCP classification through Hermes's native tool callback surface.

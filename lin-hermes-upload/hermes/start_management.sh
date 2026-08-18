#!/bin/sh
set -eu

: "${HERMES_DASHBOARD_INTERNAL_TOKEN:?HERMES_DASHBOARD_INTERNAL_TOKEN is required}"

export HERMES_DASHBOARD_UPSTREAM="http://127.0.0.1:9119"
unset HERMES_WEB_DIST HERMES_SERVE_HEADLESS HERMES_DESKTOP

PROJECT_ROOT=$(pwd)
DASHBOARD_LOG=/tmp/hermes-dashboard.log
DASHBOARD_STATUS=/tmp/hermes-dashboard.exit
DASHBOARD_COMMAND="uv run --project $PROJECT_ROOT --no-sync hermes dashboard --host 127.0.0.1 --port 9119 --no-open --skip-build"

rm -f "$DASHBOARD_LOG" "$DASHBOARD_STATUS"
: > "$DASHBOARD_LOG"

echo "[management] cwd=$PROJECT_ROOT"
echo "[management] dashboard_command=$DASHBOARD_COMMAND"
echo "[management] dashboard_upstream=$HERMES_DASHBOARD_UPSTREAM"
echo "[management] port=${PORT:-10000}"
echo "[management] hermes_home=${HERMES_HOME:-<unset>}"
echo "[management] web_dist=${HERMES_WEB_DIST:-<unset>}"
echo "[management] serve_headless=${HERMES_SERVE_HEADLESS:-<unset>}"
echo "[management] desktop=${HERMES_DESKTOP:-<unset>}"
echo "[management] uv=$(command -v uv || true)"
echo "[management] python=$(command -v python || true)"

# Keep stdout/stderr and the exit status of the Dashboard child observable in
# Render logs. Do not print secret-valued environment variables.
echo "[management] starting dashboard child"
(
    echo "[dashboard-child] shell_pid=$$"
    echo "[dashboard-child] cwd=$(pwd)"
    echo "[dashboard-child] exec=$DASHBOARD_COMMAND"
    set +e
    uv run --project "$PROJECT_ROOT" --no-sync hermes dashboard --host 127.0.0.1 --port 9119 --no-open --skip-build >>"$DASHBOARD_LOG" 2>&1
    rc=$?
    printf '%s\n' "$rc" > "$DASHBOARD_STATUS"
    echo "[dashboard-child] exit_code=$rc"
    exit "$rc"
) &
dashboard_pid=$!
echo "[management] dashboard_pid=$dashboard_pid"

cleanup() {
    if kill -0 "$dashboard_pid" 2>/dev/null; then
        echo "[management] stopping dashboard_pid=$dashboard_pid"
        kill "$dashboard_pid" 2>/dev/null || true
    fi
}
trap cleanup INT TERM EXIT

ready=0
for _ in $(seq 1 60); do
    if curl --silent --show-error --fail --max-time 2 http://127.0.0.1:9119/ >/dev/null; then
        ready=1
        echo "[management] dashboard_ready pid=$dashboard_pid"
        break
    fi
    if [ -f "$DASHBOARD_STATUS" ]; then
        rc=$(cat "$DASHBOARD_STATUS")
        echo "[management] dashboard_exited pid=$dashboard_pid exit_code=$rc"
        echo "[management] dashboard_output_begin"
        sed -n '1,400p' "$DASHBOARD_LOG"
        echo "[management] dashboard_output_end"
        exit 1
    fi
    if kill -0 "$dashboard_pid" 2>/dev/null; then
        echo "[management] dashboard_not_ready pid=$dashboard_pid child_alive=true"
    else
        echo "[management] dashboard_not_ready pid=$dashboard_pid child_alive=false exit_code=unknown"
    fi
    sleep 1
done

if [ "$ready" -ne 1 ]; then
    echo "[management] dashboard_timeout pid=$dashboard_pid"
    echo "[management] dashboard_output_begin"
    sed -n '1,400p' "$DASHBOARD_LOG"
    echo "[management] dashboard_output_end"
    exit 1
fi

cd lin-hermes-upload/hermes
exec uv run --project "$PROJECT_ROOT" --no-sync uvicorn management_gateway:app --host 0.0.0.0 --port "${PORT:-10000}"

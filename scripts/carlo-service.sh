#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(/usr/bin/dirname "$0")" && pwd -P)"
INSTALL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
CONFIG_ROOT="${XDG_CONFIG_HOME:-$HOME/.config}/carlo"
STATE_ROOT="${CARLO_STATE_ROOT:-$HOME/.local/var/carlo}"
CONFIG_FILE="$CONFIG_ROOT/.env.production"

if [[ ! -r "$CONFIG_FILE" ]]; then
    echo "CARLO configuration is missing: $CONFIG_FILE" >&2
    exit 1
fi

set -a
. "$CONFIG_FILE"
set +a

export CARLO_ARTIFACT_ROOT="${CARLO_ARTIFACT_ROOT:-$STATE_ROOT/artifacts}"
export CARLO_FRONTEND_DIST="$INSTALL_ROOT/frontend/dist"

cd "$INSTALL_ROOT/backend"

run_service() {
    api_pid=""
    worker_pid=""
    cleanup() {
        trap - EXIT INT TERM
        [[ -z "$api_pid" ]] || kill "$api_pid" 2>/dev/null || true
        [[ -z "$worker_pid" ]] || kill "$worker_pid" 2>/dev/null || true
        for _ in {1..20}; do
            if ! kill -0 "$api_pid" 2>/dev/null && ! kill -0 "$worker_pid" 2>/dev/null; then
                break
            fi
            sleep 0.1
        done
        [[ -z "$api_pid" ]] || kill -KILL "$api_pid" 2>/dev/null || true
        [[ -z "$worker_pid" ]] || kill -KILL "$worker_pid" 2>/dev/null || true
        [[ -z "$api_pid" ]] || wait "$api_pid" 2>/dev/null || true
        [[ -z "$worker_pid" ]] || wait "$worker_pid" 2>/dev/null || true
    }
    trap cleanup EXIT
    trap 'exit 0' INT TERM
    "$0" api &
    api_pid=$!
    "$0" worker &
    worker_pid=$!
    while kill -0 "$api_pid" 2>/dev/null && kill -0 "$worker_pid" 2>/dev/null; do
        sleep 1
    done
    return 1
}

case "${1:-}" in
    service)
        run_service
        ;;
    api)
        exec .venv/bin/uvicorn carlo.main:app --log-config logging.ini --log-level "$(printf %s "${LOG_LEVEL:-INFO}" | tr '[:upper:]' '[:lower:]')" --host "${CARLO_BIND_HOST:-0.0.0.0}" --port "${CARLO_PORT:-8000}"
        ;;
    worker)
        exec .venv/bin/python -m carlo.worker
        ;;
    migrate)
        exec .venv/bin/alembic upgrade head
        ;;
    bootstrap-admin)
        exec .venv/bin/python -m carlo.admin
        ;;
    check)
        exec .venv/bin/python -m carlo.production
        ;;
    *)
        echo "usage: $0 {service|api|worker|migrate|bootstrap-admin|check}" >&2
        exit 2
        ;;
esac

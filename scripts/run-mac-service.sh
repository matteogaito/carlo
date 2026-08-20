#!/bin/bash
set -euo pipefail

export HOME=/Users/carlo
CONFIG_FILE=/Users/carlo/.config/carlo/.env.production
INSTALL_ROOT=/usr/local/lib/carlo

if [[ ! -r "$CONFIG_FILE" ]]; then
    echo "CARLO configuration is missing: $CONFIG_FILE" >&2
    exit 1
fi

set -a
. "$CONFIG_FILE"
set +a

export CARLO_ARTIFACT_ROOT=/usr/local/var/carlo/artifacts
export CARLO_WORKTREE_ROOT=/usr/local/var/carlo/worktrees
export CARLO_FRONTEND_DIST="$INSTALL_ROOT/frontend/dist"

cd "$INSTALL_ROOT/backend"
case "${1:-}" in
    api)
        exec .venv/bin/uvicorn carlo.main:app --host "${CARLO_BIND_HOST:-0.0.0.0}" --port "${CARLO_PORT:-8000}"
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
        echo "usage: $0 {api|worker|migrate|bootstrap-admin|check}" >&2
        exit 2
        ;;
esac

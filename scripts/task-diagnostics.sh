#!/bin/bash
set -euo pipefail

mode="${1:-}"
task_id="${2:-}"
value="${3:-}"
config_file="${CARLO_DIAGNOSTICS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/carlo/.env.production}"
curl_bin="${CARLO_CURL_BIN:-curl}"

case "$task_id" in
    ""|*[!A-Za-z0-9._-]*) echo "invalid task id" >&2; exit 2 ;;
esac
case "$value" in
    ""|*[!0-9]*) [[ -z "$value" ]] || { echo "invalid cursor/tail" >&2; exit 2; } ;;
esac

token="$(sed -n 's/^CARLO_DIAGNOSTICS_TOKEN=//p' "$config_file" | tail -n 1)"
port="$(sed -n 's/^CARLO_PORT=//p' "$config_file" | tail -n 1)"
[[ -n "$token" ]] || { echo "diagnostics token is not configured" >&2; exit 2; }
port="${port:-8000}"

case "$mode" in
    events) url="http://127.0.0.1:$port/api/diagnostics/tasks/$task_id/events?after=${value:-0}&limit=1000" ;;
    logs) url="http://127.0.0.1:$port/api/diagnostics/tasks/$task_id/logs?tail=${value:-16000}" ;;
    *) echo "usage: $0 {events|logs} TASK_ID [CURSOR_OR_TAIL]" >&2; exit 2 ;;
esac

exec "$curl_bin" -fsS -H "Authorization: Bearer $token" "$url"

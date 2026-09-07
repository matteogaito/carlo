#!/bin/bash
# install-mac-dispatch.sh -- entry point behind `sudo make install-mac`.
#
# Two jobs, decided by whether a request is pending:
#
# 1. Headless GUI-test relay. The xcodebuild shim (scripts/xcodebuild-shim.sh,
#    installed at $SERVICE_HOME/.npm-global/bin/xcodebuild) relays `xcodebuild
#    test` invocations from sessionless service accounts by dropping a request
#    here: LaunchServices can only launch the GUI test runner inside the
#    console user's Aqua session, and `sudo make install-mac` is the only
#    root privilege the service account holds. The dispatcher runs the
#    requested command in that session and exits with its exit code.
#
# 2. The normal full CARLO install (delegates to install-mac.sh).
set -uo pipefail

REQ_DIR=/var/tmp/carlo-xcb
REQ="$REQ_DIR/req"
SCRIPT="$REQ_DIR/run.sh"
SCRIPT_DIR="$(/usr/bin/dirname "$0")"
INSTALLER="$SCRIPT_DIR/install-mac.sh"

REQ_RC=0
run_request() {
    local req_pid console_user
    req_pid="$(/bin/cat "$REQ" 2>/dev/null)"
    console_user="$(/usr/bin/stat -f "%Su" /dev/console 2>/dev/null)"
    [[ "$req_pid" =~ ^[0-9]+$ ]] || return 1
    /bin/kill -0 "$req_pid" 2>/dev/null || return 1
    [[ -n "$console_user" && -x "$SCRIPT" ]] || return 1
    /bin/launchctl asuser "$(/usr/bin/stat -f "%u" /dev/console)" /usr/bin/su "$console_user" -c "$SCRIPT"
    REQ_RC=$?
    return 0
}

if [[ -f "$REQ" && -f "$SCRIPT" ]]; then
    if run_request; then
        /bin/rm -f "$REQ" "$SCRIPT"
        exit $REQ_RC
    fi
fi
if [[ -f "$REQ" || -f "$SCRIPT" ]]; then
    # Stale or unusable request: report it, never mask it with a reinstall.
    /bin/rm -f "$REQ" "$SCRIPT"
    echo "install-mac: GUI test request rejected (stale or no console session)" >&2
    exit 1
fi

# Normal path: full CARLO install, exactly as before.
export CARLO_SOURCE_ROOT="${CARLO_SOURCE_ROOT:-$SCRIPT_DIR/..}"
export CARLO_UV_BIN="${CARLO_UV_BIN:-$(command -v uv || true)}"
export CARLO_NPM_BIN="${CARLO_NPM_BIN:-$(command -v npm || true)}"
export CARLO_PSQL_BIN="${CARLO_PSQL_BIN:-$(command -v psql || true)}"
exec /bin/bash "$INSTALLER" install

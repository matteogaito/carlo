#!/bin/bash
set -euo pipefail

SERVICE_USER=carlo
SERVICE_HOME=/Users/carlo
INSTALL_ROOT=/usr/local/lib/carlo
STATE_ROOT=/usr/local/var/carlo
CONFIG_FILE="$SERVICE_HOME/.config/carlo/.env.production"
API_PLIST=/Library/LaunchDaemons/com.carlo.api.plist
WORKER_PLIST=/Library/LaunchDaemons/com.carlo.worker.plist
SCRIPT_DIR="$(/usr/bin/dirname "$0")"

die() {
    echo "install-mac: $*" >&2
    exit 1
}

[[ "$(uname -s)" == Darwin ]] || die "macOS is required"
[[ "$EUID" -eq 0 ]] || die "run: sudo make install-mac"

uninstall_daemons() {
    /bin/launchctl bootout system/com.carlo.api 2>/dev/null || true
    /bin/launchctl bootout system/com.carlo.worker 2>/dev/null || true
    /bin/bash "$SCRIPT_DIR/wait-launchd-unloaded.sh" com.carlo.api
    /bin/bash "$SCRIPT_DIR/wait-launchd-unloaded.sh" com.carlo.worker
    /bin/rm -f "$API_PLIST" "$WORKER_PLIST"
}

if [[ "${1:-install}" == uninstall ]]; then
    uninstall_daemons
    echo "CARLO daemons removed; application, configuration, and data were preserved."
    exit 0
fi

[[ "${1:-install}" == install ]] || die "usage: $0 {install|uninstall}"
[[ -n "${CARLO_SOURCE_ROOT:-}" && -d "$CARLO_SOURCE_ROOT/backend" ]] || die "invalid CARLO_SOURCE_ROOT"

UV_BIN="${CARLO_UV_BIN:-$(command -v uv || true)}"
NPM_BIN="${CARLO_NPM_BIN:-$(command -v npm || true)}"
PSQL_BIN="${CARLO_PSQL_BIN:-$(command -v psql || true)}"
[[ -x "$UV_BIN" ]] || die "uv was not found"
[[ -x "$NPM_BIN" ]] || die "npm was not found"
export PATH="$(/usr/bin/dirname "$NPM_BIN"):$(/usr/bin/dirname "$UV_BIN"):${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"

next_id() {
    /usr/bin/dscl . -list "$1" UniqueID | /usr/bin/awk 'BEGIN { n=500 } $2 ~ /^[0-9]+$/ && $2 >= n && $2 < 60000 { n=$2+1 } END { print n }'
}

if ! /usr/bin/dscl . -read /Users/$SERVICE_USER >/dev/null 2>&1; then
    if ! /usr/bin/dscl . -read /Groups/$SERVICE_USER >/dev/null 2>&1; then
        gid="$(next_id /Groups)"
        /usr/bin/dscl . -create /Groups/$SERVICE_USER
        /usr/bin/dscl . -create /Groups/$SERVICE_USER PrimaryGroupID "$gid"
    fi
    gid="$(/usr/bin/dscl . -read /Groups/$SERVICE_USER PrimaryGroupID | /usr/bin/awk '{print $2}')"
    uid="$(next_id /Users)"
    /usr/bin/dscl . -create /Users/$SERVICE_USER
    /usr/bin/dscl . -create /Users/$SERVICE_USER UniqueID "$uid"
    /usr/bin/dscl . -create /Users/$SERVICE_USER PrimaryGroupID "$gid"
    /usr/bin/dscl . -create /Users/$SERVICE_USER NFSHomeDirectory "$SERVICE_HOME"
    /usr/bin/dscl . -create /Users/$SERVICE_USER UserShell /usr/bin/false
    /usr/bin/dscl . -create /Users/$SERVICE_USER IsHidden 1
    /usr/bin/dscl . -create /Users/$SERVICE_USER Password '*'
    service_group="$SERVICE_USER"
else
    existing_home="$(/usr/bin/dscl . -read /Users/$SERVICE_USER NFSHomeDirectory 2>/dev/null | /usr/bin/awk '{print $2}')"
    [[ "$existing_home" == "$SERVICE_HOME" ]] || die "existing user carlo has an unexpected home: $existing_home"
    service_group="$(/usr/bin/id -gn "$SERVICE_USER")"
fi

/usr/bin/install -d -m 750 -o "$SERVICE_USER" -g "$service_group" "$SERVICE_HOME" "$SERVICE_HOME/.config" "$SERVICE_HOME/.config/carlo"
/usr/bin/install -d -m 755 -o root -g wheel "$INSTALL_ROOT"
/usr/bin/install -d -m 750 -o "$SERVICE_USER" -g "$service_group" "$STATE_ROOT" "$STATE_ROOT/artifacts" "$STATE_ROOT/worktrees" "$STATE_ROOT/log" "$STATE_ROOT/uv-cache"

if [[ ! -f "$CONFIG_FILE" ]]; then
    [[ -f "$CARLO_SOURCE_ROOT/.env.production" ]] || die "create $CARLO_SOURCE_ROOT/.env.production first"
    /usr/bin/install -m 600 -o "$SERVICE_USER" -g "$service_group" "$CARLO_SOURCE_ROOT/.env.production" "$CONFIG_FILE"
else
    /bin/chmod 600 "$CONFIG_FILE"
    /usr/sbin/chown "$SERVICE_USER:$service_group" "$CONFIG_FILE"
fi

/usr/bin/rsync -a \
    --exclude .git --exclude .worktrees --exclude .env --exclude .env.production \
    --exclude backend/.venv --exclude frontend/node_modules --exclude frontend/dist \
    "$CARLO_SOURCE_ROOT/" "$INSTALL_ROOT/"
/usr/sbin/chown -R root:wheel "$INSTALL_ROOT"
/bin/chmod 755 "$INSTALL_ROOT/scripts/install-mac.sh" "$INSTALL_ROOT/scripts/run-mac-service.sh"

UV_CACHE_DIR="$STATE_ROOT/uv-cache" "$UV_BIN" sync --project "$INSTALL_ROOT/backend" --no-dev
"$NPM_BIN" ci --prefix "$INSTALL_ROOT/frontend"
"$NPM_BIN" run build --prefix "$INSTALL_ROOT/frontend"

database_url="$(/usr/bin/sed -n 's/^CARLO_DATABASE_URL=//p' "$CONFIG_FILE" | /usr/bin/tail -n 1)"
if [[ "$database_url" == postgresql+psycopg:///carlov3 ]]; then
    [[ -x "$PSQL_BIN" ]] || die "psql was not found"
    caller="${SUDO_USER:-}"
    [[ -n "$caller" && "$caller" != root ]] || die "local PostgreSQL setup requires: sudo make install-mac"
    [[ "$caller" =~ ^[A-Za-z_][A-Za-z0-9._-]*$ ]] || die "unsupported installer username"
    /usr/bin/sudo -u "$caller" "$PSQL_BIN" -v ON_ERROR_STOP=1 postgres -c \
        "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'carlo') THEN CREATE ROLE carlo LOGIN; END IF; END \$\$;"
    /usr/bin/sudo -u "$caller" "$PSQL_BIN" -v ON_ERROR_STOP=1 postgres -c \
        'ALTER DATABASE carlov3 OWNER TO carlo;'
    /usr/bin/sudo -u "$caller" "$PSQL_BIN" -v ON_ERROR_STOP=1 carlov3 -c \
        "REASSIGN OWNED BY \"$caller\" TO carlo; ALTER SCHEMA public OWNER TO carlo;"
fi

/usr/bin/sudo -u "$SERVICE_USER" -H "$INSTALL_ROOT/scripts/run-mac-service.sh" migrate

if [[ "$database_url" == postgresql+psycopg:///carlov3 ]]; then
    /usr/bin/sudo -u "$SERVICE_USER" "$PSQL_BIN" -v ON_ERROR_STOP=1 carlov3 -c \
        'GRANT USAGE, CREATE ON SCHEMA public TO carlo; GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO carlo; GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO carlo; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON TABLES TO carlo; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES TO carlo;'
fi

/usr/bin/sudo -u "$SERVICE_USER" -H "$INSTALL_ROOT/scripts/run-mac-service.sh" bootstrap-admin
/usr/bin/sudo -u "$SERVICE_USER" -H "$INSTALL_ROOT/scripts/run-mac-service.sh" check

write_plist() {
    label="$1"
    mode="$2"
    output="$3"
    error_output="$4"
    destination="$5"
    /bin/cat >"$destination" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$label</string>
<key>UserName</key><string>carlo</string>
<key>GroupName</key><string>$service_group</string>
<key>ProgramArguments</key><array><string>/usr/local/lib/carlo/scripts/run-mac-service.sh</string><string>$mode</string></array>
<key>EnvironmentVariables</key><dict><key>HOME</key><string>/Users/carlo</string></dict>
<key>WorkingDirectory</key><string>/usr/local/lib/carlo/backend</string>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>ThrottleInterval</key><integer>10</integer>
<key>StandardOutPath</key><string>$output</string>
<key>StandardErrorPath</key><string>$error_output</string>
</dict></plist>
PLIST
    /bin/chmod 644 "$destination"
    /usr/sbin/chown root:wheel "$destination"
    /usr/bin/plutil -lint "$destination" >/dev/null
}

uninstall_daemons
write_plist com.carlo.api api "$STATE_ROOT/log/api.log" "$STATE_ROOT/log/api.error.log" "$API_PLIST"
write_plist com.carlo.worker worker "$STATE_ROOT/log/worker.log" "$STATE_ROOT/log/worker.error.log" "$WORKER_PLIST"
/bin/launchctl bootstrap system "$API_PLIST"
/bin/launchctl bootstrap system "$WORKER_PLIST"
/bin/launchctl enable system/com.carlo.api
/bin/launchctl enable system/com.carlo.worker
/bin/launchctl kickstart -k system/com.carlo.api
/bin/launchctl kickstart -k system/com.carlo.worker

echo "CARLO is installed and running at boot."
echo "Configuration: $CONFIG_FILE"
echo "Status: make status-mac"

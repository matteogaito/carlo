#!/bin/bash
set -euo pipefail

action="${1:-deploy}"
platform="$(uname -s)"
service_uid="$(id -u)"
service_home="$HOME"
config_root="${XDG_CONFIG_HOME:-$service_home/.config}/carlo"
install_root="$service_home/.local/lib/carlo"
state_root="$service_home/.local/var/carlo"
config_file="$config_root/.env.production"
runner="$install_root/scripts/carlo-service.sh"
service_log="$state_root/log/service.log"
service_error_log="$state_root/log/service.error.log"

die() {
    echo "deploy: $*" >&2
    exit 1
}

[[ "$platform" == Darwin || "$platform" == Linux ]] || die "unsupported platform: $platform"

mac_plist_dir="$service_home/Library/LaunchAgents"
mac_service_plist="$mac_plist_dir/com.carlo.service.plist"
mac_agent_plist="$mac_plist_dir/com.carlo.agent.plist"
mac_api_plist="$mac_plist_dir/com.carlo.api.plist"
mac_worker_plist="$mac_plist_dir/com.carlo.worker.plist"
linux_unit_dir="${XDG_CONFIG_HOME:-$service_home/.config}/systemd/user"
linux_service_unit="$linux_unit_dir/carlo.service"
linux_api_unit="$linux_unit_dir/carlo-api.service"
linux_worker_unit="$linux_unit_dir/carlo-worker.service"

stop_services() {
    if [[ "$platform" == Darwin ]]; then
        /bin/launchctl bootout "gui/$service_uid/com.carlo.service" 2>/dev/null || true
        if [[ -x "$install_root/scripts/wait-launchd-unloaded.sh" ]]; then
            "$install_root/scripts/wait-launchd-unloaded.sh" \
                com.carlo.service "gui/$service_uid"
        fi
        /bin/launchctl bootout "gui/$service_uid/com.carlo.agent" 2>/dev/null || true
        /bin/launchctl bootout "gui/$service_uid/com.carlo.api" 2>/dev/null || true
        /bin/launchctl bootout "gui/$service_uid/com.carlo.worker" 2>/dev/null || true
    else
        systemctl --user stop carlo.service carlo-api.service carlo-worker.service 2>/dev/null || true
    fi
}

case "$action" in
    status)
        if [[ "$platform" == Darwin ]]; then
            /bin/launchctl print "gui/$service_uid/com.carlo.service"
        else
            systemctl --user status carlo.service
        fi
        exit
        ;;
    logs)
        if [[ "$platform" == Darwin ]]; then
            /usr/bin/tail -n 100 "$service_log" "$service_error_log"
        else
            journalctl --user -u carlo.service -n 100 --no-pager
        fi
        exit
        ;;
    undeploy)
        stop_services
        if [[ "$platform" == Darwin ]]; then
            /bin/rm -f "$mac_service_plist" "$mac_agent_plist" "$mac_api_plist" "$mac_worker_plist"
        else
            systemctl --user disable carlo.service carlo-api.service carlo-worker.service 2>/dev/null || true
            /bin/rm -f "$linux_service_unit" "$linux_api_unit" "$linux_worker_unit"
            systemctl --user daemon-reload
        fi
        echo "CARLO services removed; application, configuration, and data were preserved."
        exit
        ;;
    deploy) ;;
    *) die "usage: $0 {deploy|status|logs|undeploy}" ;;
esac

[[ -n "${CARLO_SOURCE_ROOT:-}" && -d "$CARLO_SOURCE_ROOT/backend" ]] || die "invalid CARLO_SOURCE_ROOT"
uv_bin="${CARLO_UV_BIN:-$(command -v uv || true)}"
npm_bin="${CARLO_NPM_BIN:-$(command -v npm || true)}"
[[ -x "$uv_bin" ]] || die "uv was not found"
[[ -x "$npm_bin" ]] || die "npm was not found"
export PATH="$(dirname "$npm_bin"):$(dirname "$uv_bin"):${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"

/bin/mkdir -p "$config_root" "$install_root" "$state_root/artifacts" "$state_root/log" "$state_root/uv-cache"
/bin/chmod 700 "$config_root"
/bin/chmod 750 "$state_root" "$state_root/artifacts" "$state_root/log" "$state_root/uv-cache"

if [[ ! -f "$config_file" ]]; then
    [[ -f "$CARLO_SOURCE_ROOT/.env.production" ]] || die "create $CARLO_SOURCE_ROOT/.env.production first"
    /usr/bin/install -m 600 "$CARLO_SOURCE_ROOT/.env.production" "$config_file"
else
    /bin/chmod 600 "$config_file"
fi

ensure_encryption_key() {
    current_key="$(sed -n 's/^CARLO_CREDENTIAL_ENCRYPTION_KEY=//p' "$config_file" | tail -n 1)"
    if [[ -n "$current_key" && "$current_key" != *CHANGE_ME* ]]; then
        return
    fi
    generated_key="$(openssl rand -base64 32)"
    [[ -n "$generated_key" ]] || die "could not generate credential encryption key"
    temporary_config="$(mktemp "$config_file.tmp.XXXXXX")"
    awk -v replacement="$generated_key" '
        BEGIN { found = 0 }
        /^CARLO_CREDENTIAL_ENCRYPTION_KEY=/ {
            if (!found) print "CARLO_CREDENTIAL_ENCRYPTION_KEY=" replacement
            found = 1
            next
        }
        { print }
        END {
            if (!found) print "CARLO_CREDENTIAL_ENCRYPTION_KEY=" replacement
        }
    ' "$config_file" >"$temporary_config"
    /bin/chmod 600 "$temporary_config"
    /bin/mv "$temporary_config" "$config_file"
}

ensure_encryption_key

/usr/bin/rsync -a \
    --exclude .git --exclude .worktrees --exclude .env --exclude .env.production \
    --exclude backend/.venv --exclude frontend/node_modules --exclude frontend/dist \
    "$CARLO_SOURCE_ROOT/" "$install_root/"
/bin/rm -f "$install_root/scripts/run-service.sh"
/bin/chmod 755 "$runner" "$install_root/scripts/deploy.sh"

UV_CACHE_DIR="$state_root/uv-cache" "$uv_bin" sync --project "$install_root/backend" --no-dev
"$npm_bin" ci --prefix "$install_root/frontend"
"$npm_bin" run build --prefix "$install_root/frontend"

CARLO_STATE_ROOT="$state_root" "$runner" migrate
CARLO_STATE_ROOT="$state_root" "$runner" bootstrap-admin
CARLO_STATE_ROOT="$state_root" "$runner" check

write_mac_plist() {
    label="$1"
    mode="$2"
    output="$3"
    error_output="$4"
    plist="$5"
    /bin/cat >"$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$label</string>
<key>ProgramArguments</key><array><string>$runner</string><string>$mode</string></array>
<key>EnvironmentVariables</key><dict><key>HOME</key><string>$service_home</string><key>XDG_CONFIG_HOME</key><string>${XDG_CONFIG_HOME:-$service_home/.config}</string><key>CARLO_STATE_ROOT</key><string>$state_root</string><key>CARLO_NPM_EXECUTABLE</key><string>$npm_bin</string><key>PATH</key><string>$(dirname "$npm_bin"):/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
<key>WorkingDirectory</key><string>$install_root/backend</string>
<key>LimitLoadToSessionType</key><string>Aqua</string>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>ThrottleInterval</key><integer>10</integer>
<key>StandardOutPath</key><string>$output</string>
<key>StandardErrorPath</key><string>$error_output</string>
</dict></plist>
PLIST
    /bin/chmod 644 "$plist"
    /usr/bin/plutil -lint "$plist" >/dev/null
}

write_linux_unit() {
    mode="$1"
    unit="$2"
    /bin/cat >"$unit" <<UNIT
[Unit]
Description=CARLO $mode
After=network-online.target

[Service]
Type=simple
Environment="HOME=$service_home"
Environment="XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-$service_home/.config}"
Environment="CARLO_STATE_ROOT=$state_root"
Environment="CARLO_NPM_EXECUTABLE=$npm_bin"
ExecStart=$runner $mode
WorkingDirectory=$install_root/backend
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
UNIT
    /bin/chmod 644 "$unit"
}

stop_services
if [[ "$platform" == Darwin ]]; then
    /bin/mkdir -p "$mac_plist_dir"
    /bin/rm -f "$mac_agent_plist" "$mac_api_plist" "$mac_worker_plist"
    write_mac_plist com.carlo.service service "$service_log" "$service_error_log" "$mac_service_plist"
    if /bin/launchctl print "gui/$service_uid" >/dev/null 2>&1; then
        /bin/bash "$install_root/scripts/launchd-bootstrap.sh" \
            "gui/$service_uid" "$mac_service_plist" com.carlo.service
        echo "CARLO is running in the current graphical session."
    else
        echo "CARLO will start at the next graphical login."
    fi
else
    /bin/mkdir -p "$linux_unit_dir"
    systemctl --user disable carlo-api.service carlo-worker.service 2>/dev/null || true
    /bin/rm -f "$linux_api_unit" "$linux_worker_unit"
    write_linux_unit service "$linux_service_unit"
    systemctl --user daemon-reload
    systemctl --user enable --now carlo.service
    echo "CARLO is running in the current systemd user session."
fi

echo "Configuration: $config_file"
echo "Status: make status"

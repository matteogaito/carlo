#!/bin/bash
set -euo pipefail

domain="${1:?launchd domain is required}"
plist="${2:?launchd plist is required}"
label="${3:?launchd label is required}"
launchctl_bin="${CARLO_LAUNCHCTL_BIN:-/bin/launchctl}"
sleep_bin="${CARLO_SLEEP_BIN:-/bin/sleep}"

if "$launchctl_bin" print "$domain/$label" >/dev/null 2>&1; then
    "$launchctl_bin" kickstart -k "$domain/$label"
    exit 0
fi

for _ in {1..10}; do
    if "$launchctl_bin" bootstrap "$domain" "$plist" 2>/dev/null; then
        exit 0
    fi
    "$sleep_bin" 0.2
done

echo "Could not bootstrap $label in $domain" >&2
exit 1

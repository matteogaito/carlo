#!/bin/bash
set -euo pipefail

label="${1:?launchd label is required}"
launchctl_bin="${CARLO_LAUNCHCTL_BIN:-/bin/launchctl}"
sleep_bin="${CARLO_SLEEP_BIN:-/bin/sleep}"

for _ in {1..100}; do
    if ! "$launchctl_bin" print "system/$label" >/dev/null 2>&1; then
        exit 0
    fi
    "$sleep_bin" 0.1
done

echo "Timed out waiting for $label to unload" >&2
exit 1

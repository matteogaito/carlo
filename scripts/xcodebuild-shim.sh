#!/bin/bash
# xcodebuild shim for headless (sessionless) macOS service accounts.
#
# xcodebuild's `test` action launches the GUI test runner through
# LaunchServices in the caller's Aqua (GUI) login session. A service account
# that only has background/SSH sessions (e.g. the hidden `carlo` worker
# account) has no Aqua session, so the launch fails with RunningBoard error 5
# / OSLaunchdErrorDomain 125 "Domain does not support specified action".
#
# When this shim detects a test action running as a user different from the
# console (Aqua) user, it relays the invocation to the console user's session
# via the CARLO installer's root privilege (sudo /usr/bin/make install-mac ->
# scripts/install-mac-dispatch.sh). Everything else passes through untouched.
set -uo pipefail

REAL=/usr/bin/xcodebuild
REQ_DIR=/var/tmp/carlo-xcb

is_test_action=0
for arg in "$@"; do
    case "$arg" in
        test|build-for-testing|test-without-building) is_test_action=1 ;;
    esac
done

if [[ "$is_test_action" -eq 1 ]]; then
        uid="$(id -u)"
        console_user="$(/usr/bin/stat -f "%Su" /dev/console 2>/dev/null)"
        console_uid="$(/usr/bin/stat -f "%u" /dev/console 2>/dev/null)"
        if [[ -n "$console_user" && -n "$console_uid" && "$uid" != "$console_uid" ]]; then
            /bin/mkdir -p "$REQ_DIR"
            /bin/chmod 711 "$REQ_DIR" 2>/dev/null || true
            script="$REQ_DIR/run.sh"
            {
                printf '#!/bin/bash\n'
                printf 'cd %q || exit 1\n' "$PWD"
                printf 'exec '
                printf '%q ' "$REAL" "$@"
                printf '\n'
            } >"$script"
            /bin/chmod 755 "$script"
            /bin/rm -f "$REQ_DIR/req"
            printf '%s\n' "$$" >"$REQ_DIR/req"
            # Root via the only sudo-allowed command; the dispatcher runs the
            # request in the console user's Aqua session and returns its exit code.
            (cd /usr/local/lib/carlo && /usr/bin/sudo /usr/bin/make install-mac)
            exit $?
        fi
    fi
exec "$REAL" "$@"

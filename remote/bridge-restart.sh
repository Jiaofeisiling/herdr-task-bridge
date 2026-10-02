#!/usr/bin/env bash
# Restart the bridge under its supervisor, and say honestly whether it came back.
#
# This is the one place the restart sequence is defined. The `bridge-restart`
# shell function in bridge-aliases.sh only calls it. It used to be the function
# itself, which failed in a way nothing reported: run from a child shell --
# which is how an agent runs it -- the function exists but the plain variable
# it was defined beside, `_BRIDGE_ROOT`, does not, so the path it launched was
# /remote/bridge-supervisor.sh. By then it had already killed the old bridge;
# nothing started; and it returned success, because its last command was a
# status listing that cannot fail. The bridge stayed down for about two
# minutes, during a deploy.
#
# So, in order: work out where this checkout is from where this file is, not
# from the environment; check that a new bridge can be started *before*
# stopping the old one -- taking it down is the only step that cannot be
# undone, and the bridge is the way back in; and afterwards, wait until it
# answers, and fail if it does not.

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUPERVISOR="$ROOT/remote/bridge-supervisor.sh"
LOG_FILE="$ROOT/sentinel-bridge/bridge.log"
URL="http://127.0.0.1:${SENTINEL_BRIDGE_PORT:-8765}/health"

# Tunable for tests; the defaults are what a real host wants.
SETTLE="${BRIDGE_SETTLE_SEC:-2}"
WAIT="${BRIDGE_START_WAIT_SEC:-30}"

refuse() {
    echo "bridge-restart: refusing -- $1; nothing was stopped" >&2
    exit 2
}

[ -f "$SUPERVISOR" ] || refuse "$SUPERVISOR does not exist"
command -v screen >/dev/null 2>&1 || refuse "screen is not installed"
command -v python3 >/dev/null 2>&1 || refuse "python3 is not installed"

# Tears down any existing `bridge` screen session (supervisor loop + whatever
# bridge.py it is currently running) plus any orphaned bare bridge.py from
# before the supervisor loop existed. See bridge-supervisor.sh for why
# bridge.py is never launched directly in screen.
#
# The pkill pattern is anchored (^...$) on purpose: this is invoked remotely as
# `bash -c "..."`, and that wrapping bash -c's own argv contains the literal
# string "python3 bridge.py", so an unanchored `pkill -f 'python3 bridge.py'`
# matches and kills the wrapping shell too, silently truncating whatever ran
# after it. Hit this for real. Anchoring works because the wrapper's command
# line always starts with "bash", never "python3".
screen -S bridge -X quit >/dev/null 2>&1
pkill -f '^python3 bridge\.py$' 2>/dev/null
sleep "$SETTLE"

screen -dmS bridge bash "$SUPERVISOR"
sleep "$SETTLE"

came_back=""
deadline=$((SECONDS + WAIT))
while [ "$SECONDS" -lt "$deadline" ]; do
    if health="$(curl -sS -m 3 "$URL" 2>/dev/null)"; then
        came_back=1
        break
    fi
    sleep 1
done

echo "--- health ---"
if [ -n "$came_back" ]; then
    echo "$health"
else
    echo "(no answer from $URL)"
fi

echo "--- last 20 log lines ---"
tail -n 20 "$LOG_FILE" 2>/dev/null || echo "(no log file yet: $LOG_FILE)"

if [ -z "$came_back" ]; then
    echo "bridge-restart: FAILED -- the bridge did not answer $URL within ${WAIT}s;" \
         "the log above says why" >&2
    exit 1
fi

echo "bridge-restart: ok"

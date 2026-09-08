#!/usr/bin/env bash
# Restart-on-crash loop for bridge.py, with output logged persistently.
#
# Run this inside `screen -dmS bridge` -- never run bridge.py directly in
# screen. If bridge.py exits for any reason (crash, OOM, an operator
# `kill`), a screen window whose direct child is bridge.py itself closes
# right along with it by default, silently, with no record of why (this
# bit us for real: bridge.py died at some point, the `bridge` screen
# session terminated with it, and there was no log file, so the crash
# reason was unrecoverable). Wrapping bridge.py in this loop means
# screen's direct child is the loop, not bridge.py, so the window survives
# any number of bridge.py crashes and every one of them gets logged.

set -u

BRIDGE_ROOT="$HOME/herdr-task-bridge"
BRIDGE_DIR="$BRIDGE_ROOT/sentinel-bridge"
LOG_FILE="$BRIDGE_DIR/bridge.log"

# Deployment-specific settings live in an untracked file next to this one
# (see bridge.env.example). Real HPC project codes, usernames and absolute
# cluster paths must not be committed -- see CONTRIBUTING.md's "Sensitive
# paths" -- so anything site-specific belongs there, not in this script.
# Sourcing it here rather than relying on ~/.bashrc means the settings hold
# no matter how the supervisor was launched.
ENV_FILE="$BRIDGE_ROOT/remote/bridge.env"
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi

export SENTINEL_AGENT="${SENTINEL_AGENT:-sentinel-opencode}"

if [ -n "${SENTINEL_RESULT_DIR:-}" ]; then
    export SENTINEL_RESULT_DIR
    mkdir -p "$SENTINEL_RESULT_DIR" 2>/dev/null || true
fi

cd "$BRIDGE_DIR" || exit 1

while true; do
    echo "$(date -Is) starting bridge.py (SENTINEL_AGENT=$SENTINEL_AGENT, SENTINEL_RESULT_DIR=${SENTINEL_RESULT_DIR:-<default>})" >> "$LOG_FILE"

    # -u is load-bearing. Python block-buffers stdout when it is a file
    # rather than a TTY, and bridge-restart stops bridge.py with SIGTERM,
    # whose default handler exits without flushing -- so every startup
    # banner, request line, reminder and task log sat in an unflushed
    # buffer and was thrown away. The log held only the two lines this
    # script echoes itself, which is why a v7 deploy appeared to produce
    # no startup output at all.
    python3 -u bridge.py >> "$LOG_FILE" 2>&1
    exit_code=$?

    echo "$(date -Is) bridge.py exited (code $exit_code) -- restarting in 5s" >> "$LOG_FILE"
    sleep 5
done

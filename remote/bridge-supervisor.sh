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

BRIDGE_DIR="$HOME/herdr-task-bridge/sentinel-bridge"
LOG_FILE="$BRIDGE_DIR/bridge.log"
export SENTINEL_AGENT="${SENTINEL_AGENT:-sentinel-opencode}"

cd "$BRIDGE_DIR" || exit 1

while true; do
    echo "$(date -Is) starting bridge.py (SENTINEL_AGENT=$SENTINEL_AGENT)" >> "$LOG_FILE"

    python3 bridge.py >> "$LOG_FILE" 2>&1
    exit_code=$?

    echo "$(date -Is) bridge.py exited (code $exit_code) -- restarting in 5s" >> "$LOG_FILE"
    sleep 5
done

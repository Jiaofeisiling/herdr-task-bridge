# Shortcuts for the git-pull + screen restart dance documented in the
# repo's README ("部署" section). Source this from your ~/.bashrc so it
# doesn't matter which terminal tab you're in:
#
#   echo 'source ~/herdr-task-bridge/remote/bridge-aliases.sh' >> ~/.bashrc
#
# Keep this file and the README's "部署" section in sync -- this is the
# only place the actual restart sequence should be defined; the README
# should describe it, not duplicate it verbatim.

_BRIDGE_ROOT="$HOME/herdr-task-bridge"
_BRIDGE_DIR="$_BRIDGE_ROOT/sentinel-bridge"
_BRIDGE_LOG="$_BRIDGE_DIR/bridge.log"

bridge-pull() {
    ( cd "$_BRIDGE_ROOT" && git pull )
}

bridge-status() {
    echo "--- screen ---"
    screen -ls 2>&1 | grep -i bridge || echo "(no 'bridge' screen session)"
    echo "--- process ---"
    pgrep -af bridge.py || echo "(not running)"
    echo "--- health ---"
    curl -sS -m 5 http://127.0.0.1:8765/health && echo
    echo "--- last 20 log lines ---"
    tail -n 20 "$_BRIDGE_LOG" 2>/dev/null || echo "(no log file yet: $_BRIDGE_LOG)"
}

bridge-attach() {
    screen -r bridge
}

bridge-logs() {
    tail -n 50 -f "$_BRIDGE_LOG"
}

# Tears down any existing `bridge` screen session (supervisor loop +
# whatever bridge.py it's currently running) plus any orphaned bare
# bridge.py from before the supervisor loop existed, then starts a fresh
# supervised instance. See remote/bridge-supervisor.sh for why bridge.py
# is never launched directly in screen.
bridge-restart() {
    screen -S bridge -X quit >/dev/null 2>&1
    pkill -f 'bridge-supervisor.sh' 2>/dev/null
    pkill -f 'python3 bridge.py' 2>/dev/null
    sleep 2

    screen -dmS bridge bash "$_BRIDGE_ROOT/remote/bridge-supervisor.sh"
    sleep 2

    bridge-status
}

# Pull latest code, then restart. This is the one-liner for "deploy".
bridge-deploy() {
    bridge-pull && bridge-restart
}

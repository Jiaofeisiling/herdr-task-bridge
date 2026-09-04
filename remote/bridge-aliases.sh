# Shortcuts for the git-pull + screen restart dance documented in the
# repo's README ("部署" section). Source this from your ~/.bashrc so it
# doesn't matter which terminal tab you're in:
#
#   echo 'source ~/herdr-task-bridge/remote/bridge-aliases.sh' >> ~/.bashrc
#
# Keep this file and the README's "部署" section in sync -- this is the
# only place the actual restart sequence should be defined; the README
# should describe it, not duplicate it verbatim.

_BRIDGE_DIR="$HOME/herdr-task-bridge/sentinel-bridge"
_BRIDGE_AGENT="sentinel-opencode"

bridge-pull() {
    ( cd "$HOME/herdr-task-bridge" && git pull )
}

bridge-status() {
    echo "--- screen ---"
    screen -ls 2>&1 | grep -i bridge || echo "(no 'bridge' screen session)"
    echo "--- process ---"
    pgrep -af bridge.py || echo "(not running)"
    echo "--- health ---"
    curl -sS -m 5 http://127.0.0.1:8765/health && echo
}

bridge-attach() {
    screen -r bridge
}

# Stops whatever bridge.py is currently running and starts a fresh one
# from $_BRIDGE_DIR inside a named `screen` session (see README: a bare
# `&` background leaves the process impossible to find/attach to later).
bridge-restart() {
    local pid
    pid=$(pgrep -f 'python3 bridge.py')

    if [ -n "$pid" ]; then
        echo "Stopping bridge.py (PID $pid)..."
        kill "$pid"
        sleep 2
    fi

    screen -dmS bridge bash -c "cd '$_BRIDGE_DIR' && SENTINEL_AGENT=$_BRIDGE_AGENT python3 bridge.py"
    sleep 2

    bridge-status
}

# Pull latest code, then restart. This is the one-liner for "deploy".
bridge-deploy() {
    bridge-pull && bridge-restart
}

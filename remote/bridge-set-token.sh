#!/usr/bin/env bash
# Make an access token for the bridge and put it in remote/bridge.env.
#
#     bash remote/bridge-set-token.sh             # first time
#     bash remote/bridge-set-token.sh --rotate    # replace the one in use
#
# The token is the only thing standing between every user of a shared login
# node and the agents (anyone who can reach the port can have them run
# commands). This runs in your own terminal and prints the token once, on
# stdout, so that you can give the same value to the clients; nobody else has
# to see it. It is written to bridge.env, which only you can read.
#
# It will not replace a token that is already set unless asked to with
# --rotate: doing so locks out every client until each has been updated.
# Afterwards run bridge-restart; the bridge reads the token only at startup.

set -u
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/remote/bridge.env"

rotate=""
case "${1:-}" in
    "") ;;
    --rotate) rotate=1 ;;
    *) echo "usage: bridge-set-token.sh [--rotate]" >&2; exit 2 ;;
esac

if [ -f "$ENV_FILE" ] && grep -q '^SENTINEL_BRIDGE_TOKEN=' "$ENV_FILE" && [ -z "$rotate" ]; then
    echo "bridge-set-token: a token is already set in $ENV_FILE." >&2
    echo "  Replacing it locks out every client until each is updated." >&2
    echo "  If that is what you want, run again with --rotate." >&2
    exit 1
fi

# 256 random bits, as 64 hex characters. openssl first, then Python, then the
# kernel's source directly; whichever the host has.
if command -v openssl >/dev/null 2>&1; then
    token="$(openssl rand -hex 32)"
elif command -v python3 >/dev/null 2>&1; then
    token="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
else
    token="$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')"
fi

case "$token" in
    *[!0-9a-f]* | "") echo "bridge-set-token: could not make a token" >&2; exit 1 ;;
esac
[ "${#token}" -eq 64 ] || { echo "bridge-set-token: could not make a token" >&2; exit 1; }

# Keep every other line; drop any old token; add the new one.
tmp="$(mktemp "${ENV_FILE}.XXXXXX")" || exit 1
if [ -f "$ENV_FILE" ]; then
    grep -v '^SENTINEL_BRIDGE_TOKEN=' "$ENV_FILE" > "$tmp" || true
fi
printf 'SENTINEL_BRIDGE_TOKEN="%s"\n' "$token" >> "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$ENV_FILE"

echo "bridge-set-token: wrote the token to $ENV_FILE (readable only by you)." >&2
echo "  Give the same value to each client as SENTINEL_BRIDGE_TOKEN, then run bridge-restart." >&2
echo "  It is shown below once. It is not shown again." >&2
printf '%s\n' "$token"

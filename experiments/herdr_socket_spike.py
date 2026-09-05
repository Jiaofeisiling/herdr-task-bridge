"""Feasibility spike for talking to herdr's local Socket API directly,
instead of shelling out to the `herdr` CLI the way sentinel-bridge/bridge.py
does today.

NOT production code. Doesn't import or modify anything in sentinel-bridge/.
Deliberately a throwaway script -- see docs/superpowers/plans/ once (if)
this graduates into an actual bridge.py rewrite.

Connection model: one fresh Unix-domain-socket connection per call, closed
right after the matching response line arrives. First version of this
script tried to reuse a single long-lived connection for several sequential
calls and got `[Errno 32] Broken pipe` on the second call -- herdr's socket
server appears to close the connection after answering one request, at
least for plain request/response calls (as opposed to a subscription
stream). Confirmed live against the real remote herdr instance, not a
guess.

One consequence: `events.subscribe` below only proves the subscribe
request itself is acknowledged. It closes the connection right after, so
it can NOT demonstrate that events actually arrive -- doing that would need
a persistent connection kept open and read from concurrently with issuing
the prompt on a separate connection (a second iteration, if this pass looks
promising enough to bother).

Field names below (`target`, not `pane_id`, for agent.get/agent.prompt;
`wait.until` as an array of AgentStatus, not a bare string) come from
reading `herdr api schema --json` directly (`$defs/AgentTarget`,
`$defs/AgentPromptParams`, `$defs/AgentPromptWaitOptions`,
`$defs/AgentStatus`), not from the (looser/paraphrased) public docs page.

What this checks, in order, printing one clearly-tagged line per step so
the result is easy to pull out of a noisy terminal capture:

  1. ping                                     -- socket reachable at all?
  2. agent.list                               -- how do we find our target?
  3. agent.get(target)                        -- structured status, no CLI parsing
  4. events.subscribe(pane.agent_status_changed) -- does the ack come back clean?
  5. pane.read(target)                        -- final output text

agent.prompt with `wait` is deliberately NOT exercised here. Two ways
were tried and rejected, in case someone's tempted to retry either:

  - Running it against the agent named on the command line is
    self-referential when this script is itself dispatched as a command
    that agent is executing -- the agent is "working" (running this very
    script) for the whole duration, so `wait` can only time out.
  - An earlier revision added a `--scratch-pane-test` flag that called
    `pane.split` to create a disposable pane to test against instead.
    `pane.split` is NOT a hidden/headless resource -- it's a real UI
    action that visibly splits the actual terminal layout the human is
    looking at. Running it live did exactly that (an unwanted visible
    split), and its cleanup (pane.close) didn't reliably fire, leaving a
    stray pane needing manual closing. Reverted; do not reintroduce.

The remaining option, if this is worth finishing: run this script from a
plain terminal (not dispatched through the agent under test) targeting an
agent that's already idle for some other reason -- no new pane, no new
agent instance, no side effect.

Usage:
    python3 herdr_socket_spike.py [agent_name]

Env:
    HERDR_SOCKET_PATH  overrides ~/.config/herdr/herdr.sock (matches herdr's
                        own env var name, see herdr.dev/docs/socket-api/)
"""

import json
import os
import socket
import sys


SOCKET_PATH = os.environ.get(
    "HERDR_SOCKET_PATH",
    os.path.expanduser("~/.config/herdr/herdr.sock"),
)

AGENT_NAME = sys.argv[1] if len(sys.argv) > 1 else "sentinel-opencode"


class HerdrClient:
    def __init__(self, path):
        self.path = path
        self._next_id = 0

    def call(self, method, params=None, timeout=30):
        self._next_id += 1
        req_id = f"spike{self._next_id}"
        req = {"id": req_id, "method": method, "params": params or {}}

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self.path)
            sock.sendall((json.dumps(req) + "\n").encode("utf-8"))

            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError(
                        "socket closed before a full response line arrived"
                    )
                buf += chunk

            line, _, _ = buf.partition(b"\n")
            return json.loads(line.decode("utf-8"))
        finally:
            sock.close()


def tag(label, payload):
    print(f"SPIKE::{label}:: " + json.dumps(payload, ensure_ascii=False))


def main():
    summary = {"agent_name": AGENT_NAME, "steps": {}}
    client = HerdrClient(SOCKET_PATH)

    # 1. ping
    try:
        resp = client.call("ping")
        tag("ping", resp)
        summary["steps"]["ping"] = "ok" if "result" in resp else "fail"
    except Exception as e:
        tag("ping", {"ok": False, "error": str(e)})
        summary["steps"]["ping"] = "fail"
        tag("SUMMARY", summary)
        return

    # 2. find our target (agent.list's own pane_id field -- confirmed to
    #    be what agent.get/agent.prompt's "target" param expects)
    target = None
    try:
        resp = client.call("agent.list")
        tag("agent.list", resp)
        agents = resp.get("result", {}).get("agents", [])
        for a in agents:
            if a.get("name") == AGENT_NAME:
                target = a.get("pane_id")
                break
        summary["steps"]["agent.list"] = "ok" if agents else "empty"
        summary["target"] = target
    except Exception as e:
        tag("agent.list", {"ok": False, "error": str(e)})
        summary["steps"]["agent.list"] = "fail"

    if target is None:
        tag("NOTE", {
            "message": (
                f"could not resolve target for agent_name={AGENT_NAME!r} "
                "from agent.list -- steps below will be skipped"
            )
        })
        summary["steps"]["remaining"] = "skipped, no target"
        tag("SUMMARY", summary)
        return

    # 3. structured status, no CLI/JSON-in-stdout parsing needed
    try:
        resp = client.call("agent.get", {"target": target})
        tag("agent.get", resp)
        summary["steps"]["agent.get"] = "ok" if "result" in resp else "fail"
    except Exception as e:
        tag("agent.get", {"ok": False, "error": str(e)})
        summary["steps"]["agent.get"] = "fail"

    # 4. subscribe ack only -- see module docstring for why this can't
    #    prove event delivery under the one-connection-per-call model.
    try:
        resp = client.call("events.subscribe", {
            "subscriptions": [
                {"type": "pane.agent_status_changed", "pane_id": target}
            ]
        })
        tag("events.subscribe", resp)
        summary["steps"]["events.subscribe"] = "ok" if "result" in resp else "fail"
    except Exception as e:
        tag("events.subscribe", {"ok": False, "error": str(e)})
        summary["steps"]["events.subscribe"] = "fail"

    # 5. read pane output for comparison against agent.prompt's own result
    #    payload (does the socket API already give us clean text, or do we
    #    still need extract_task_response-style scraping?)
    try:
        resp = client.call("pane.read", {
            "pane_id": target, "source": "recent", "lines": 30,
        })
        tag("pane.read", resp)
        summary["steps"]["pane.read"] = "ok" if "result" in resp else "fail"
    except Exception as e:
        tag("pane.read", {"ok": False, "error": str(e)})
        summary["steps"]["pane.read"] = "fail"

    tag("NOTE", {
        "message": (
            "agent.prompt(wait=...) intentionally not exercised here -- "
            "see module docstring for the two approaches already tried "
            "and rejected (self-reference, and pane.split visibly "
            "disrupting the real terminal layout)"
        )
    })

    tag("SUMMARY", summary)


if __name__ == "__main__":
    main()

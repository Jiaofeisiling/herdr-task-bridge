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

What it checks, in order, printing one clearly-tagged line per step so the
result is easy to pull out of a noisy terminal capture:

  1. ping                                     -- socket reachable at all?
  2. agent.list                               -- how do we find our pane_id?
  3. agent.get(pane_id)                       -- structured status, no CLI parsing
  4. events.subscribe(pane.agent_status_changed) -- does the ack come back clean?
  5. agent.prompt(..., wait={...})            -- structured completion,
                                                  no SENTINEL_DONE_<token>
                                                  marker-scraping needed?
  6. pane.read(pane_id)                       -- final output text

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
import time


SOCKET_PATH = os.environ.get(
    "HERDR_SOCKET_PATH",
    os.path.expanduser("~/.config/herdr/herdr.sock"),
)

AGENT_NAME = sys.argv[1] if len(sys.argv) > 1 else "sentinel-opencode"

TEST_PROMPT = (
    "这是 herdr socket API 可行性 spike 的测试请求，"
    "不是真实任务。请仅回复 ok，不要执行任何命令、不要修改任何文件。"
)


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

    # 2. find our pane_id
    pane_id = None
    try:
        resp = client.call("agent.list")
        tag("agent.list", resp)
        agents = resp.get("result", {}).get("agents", [])
        for a in agents:
            if a.get("name") == AGENT_NAME or a.get("agent_name") == AGENT_NAME:
                pane_id = a.get("pane_id")
                break
        summary["steps"]["agent.list"] = "ok" if agents else "empty"
        summary["pane_id"] = pane_id
    except Exception as e:
        tag("agent.list", {"ok": False, "error": str(e)})
        summary["steps"]["agent.list"] = "fail"

    if pane_id is None:
        tag("NOTE", {
            "message": (
                f"could not resolve pane_id for agent_name={AGENT_NAME!r} "
                "from agent.list -- steps below will be skipped"
            )
        })
        summary["steps"]["remaining"] = "skipped, no pane_id"
        tag("SUMMARY", summary)
        return

    # 3. structured status, no CLI/JSON-in-stdout parsing needed
    try:
        resp = client.call("agent.get", {"pane_id": pane_id})
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
                {"type": "pane.agent_status_changed", "pane_id": pane_id}
            ]
        })
        tag("events.subscribe", resp)
        summary["steps"]["events.subscribe"] = "ok" if "result" in resp else "fail"
    except Exception as e:
        tag("events.subscribe", {"ok": False, "error": str(e)})
        summary["steps"]["events.subscribe"] = "fail"

    # 5. the real test: does agent.prompt's built-in `wait` give a clean
    #    structured completion signal, replacing SENTINEL_DONE_<token>
    #    marker-scraping?
    start = time.time()
    try:
        resp = client.call(
            "agent.prompt",
            {
                "pane_id": pane_id,
                "text": TEST_PROMPT,
                "wait": {"until": "done", "timeout_ms": 60000},
            },
            timeout=75,
        )
        elapsed = time.time() - start
        tag("agent.prompt", resp)
        summary["steps"]["agent.prompt"] = "ok" if "result" in resp else "fail"
        summary["agent.prompt_elapsed_s"] = round(elapsed, 1)
    except Exception as e:
        tag("agent.prompt", {"ok": False, "error": str(e)})
        summary["steps"]["agent.prompt"] = "fail"

    # 6. read final pane output for comparison against agent.prompt's own
    #    result payload (does the socket API already give us clean text,
    #    or do we still need extract_task_response-style scraping?)
    try:
        resp = client.call("pane.read", {
            "pane_id": pane_id, "source": "recent", "lines": 30,
        })
        tag("pane.read", resp)
        summary["steps"]["pane.read"] = "ok" if "result" in resp else "fail"
    except Exception as e:
        tag("pane.read", {"ok": False, "error": str(e)})
        summary["steps"]["pane.read"] = "fail"

    tag("SUMMARY", summary)


if __name__ == "__main__":
    main()

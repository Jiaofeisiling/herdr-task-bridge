"""Feasibility spike for talking to herdr's local Socket API directly,
instead of shelling out to the `herdr` CLI the way sentinel-bridge/bridge.py
does today.

NOT production code. Doesn't import or modify anything in sentinel-bridge/.
Deliberately a throwaway script -- see docs/superpowers/plans/ once (if)
this graduates into an actual bridge.py rewrite.

What it checks, in order, printing one clearly-tagged line per step so the
result is easy to pull out of a noisy terminal capture:

  1. connect + ping                          -- socket reachable at all?
  2. agent.list / session.snapshot            -- how do we find our pane_id?
  3. agent.get(pane_id)                       -- structured status, no CLI parsing
  4. events.subscribe(pane.agent_status_changed) -- push instead of poll?
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


class HerdrSocket:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect(path)
        self.buf = b""
        self._next_id = 0

    def _read_line(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("herdr socket closed unexpectedly")
            self.buf += chunk

        line, _, self.buf = self.buf.partition(b"\n")
        return json.loads(line.decode("utf-8"))

    def call(self, method, params=None, timeout=None):
        self._next_id += 1
        req_id = f"spike{self._next_id}"

        req = {"id": req_id, "method": method, "params": params or {}}
        self.sock.sendall((json.dumps(req) + "\n").encode("utf-8"))

        if timeout is not None:
            self.sock.settimeout(timeout)

        # Events for other subscriptions can interleave on the same
        # connection -- skip any message that isn't our response id.
        events = []
        while True:
            msg = self._read_line()
            if msg.get("id") == req_id:
                return msg, events
            events.append(msg)


def tag(label, payload):
    print(f"SPIKE::{label}:: " + json.dumps(payload, ensure_ascii=False))


def main():
    summary = {"agent_name": AGENT_NAME, "steps": {}}

    try:
        client = HerdrSocket(SOCKET_PATH)
    except Exception as e:
        tag("connect", {"ok": False, "error": str(e)})
        summary["steps"]["connect"] = "fail"
        tag("SUMMARY", summary)
        return

    summary["steps"]["connect"] = "ok"

    # 1. ping
    try:
        resp, _ = client.call("ping")
        tag("ping", resp)
        summary["steps"]["ping"] = "ok" if "result" in resp else "fail"
    except Exception as e:
        tag("ping", {"ok": False, "error": str(e)})
        summary["steps"]["ping"] = "fail"

    # 2. find our pane_id
    pane_id = None
    try:
        resp, _ = client.call("agent.list")
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
        resp, _ = client.call("agent.get", {"pane_id": pane_id})
        tag("agent.get", resp)
        summary["steps"]["agent.get"] = "ok" if "result" in resp else "fail"
    except Exception as e:
        tag("agent.get", {"ok": False, "error": str(e)})
        summary["steps"]["agent.get"] = "fail"

    # 4. subscribe to status-change events for this pane
    sub_ok = False
    try:
        resp, _ = client.call("events.subscribe", {
            "subscriptions": [
                {"type": "pane.agent_status_changed", "pane_id": pane_id}
            ]
        })
        tag("events.subscribe", resp)
        sub_ok = "result" in resp
        summary["steps"]["events.subscribe"] = "ok" if sub_ok else "fail"
    except Exception as e:
        tag("events.subscribe", {"ok": False, "error": str(e)})
        summary["steps"]["events.subscribe"] = "fail"

    # 5. the real test: agent.prompt with a built-in wait, and see whether
    #    the status-change event(s) show up interleaved on the same
    #    connection while we wait for the response.
    start = time.time()
    try:
        resp, events = client.call(
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
        for ev in events:
            tag("interleaved_event", ev)

        summary["steps"]["agent.prompt"] = "ok" if "result" in resp else "fail"
        summary["agent.prompt_elapsed_s"] = round(elapsed, 1)
        summary["interleaved_event_count"] = len(events)
    except Exception as e:
        tag("agent.prompt", {"ok": False, "error": str(e)})
        summary["steps"]["agent.prompt"] = "fail"

    # 6. read final pane output for comparison against agent.prompt's own
    #    result payload (does the socket API already give us clean text,
    #    or do we still need extract_task_response-style scraping?)
    try:
        resp, _ = client.call("pane.read", {
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

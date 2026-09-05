"""Validate agent.prompt(wait=...) against a disposable, genuinely idle
target, using a new tab instead of pane.split (see herdr_socket_spike.py's
docstring for why pane.split was rejected: it visibly splits the real
terminal layout the human is looking at). tab.create defaults to
focus=false, so it should not steal the view.

NOT production code. Opt-in, real cost: this starts an actual `claude`
agent instance and prompts it once.

Confirmed schema shapes (via dump_schema_def.py against the real
installed herdr, not the docs page):
  - tab.create params (TabCreateParams): cwd, env, focus (default false),
    label, workspace_id -- all optional
  - tab.close params (TabTarget): {"tab_id": "<string>"}
  - pane.read/pane.close params (PaneTarget): {"pane_id": "<string>"}
  - agent.start params (AgentStartParams): required name, kind, pane_id
  - agent.get/agent.prompt params: {"target": "<pane_id>"}
  - agent.prompt.wait.until: array of AgentStatus
    ("idle"|"working"|"blocked"|"done"|"unknown")

tab.create's response shape (does it hand back a pane_id for the tab's
initial pane, or do we need a separate call to find it?) was NOT in the
request schema -- that's a runtime question, which is exactly what step 1
below is for. If no pane_id comes back, the script stops after printing
the raw response instead of guessing.

Usage:
    python3 herdr_socket_spike_tab_test.py
"""

import json
import os
import socket
import time


SOCKET_PATH = os.environ.get(
    "HERDR_SOCKET_PATH",
    os.path.expanduser("~/.config/herdr/herdr.sock"),
)

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
        req_id = f"tabtest{self._next_id}"
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
    print(f"TABTEST::{label}:: " + json.dumps(payload, ensure_ascii=False))


def find_first_str(obj, keys):
    """Best-effort: look for any of `keys` anywhere in a nested dict."""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and isinstance(obj[k], str):
                return obj[k]
        for v in obj.values():
            found = find_first_str(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_first_str(item, keys)
            if found:
                return found
    return None


def main():
    client = HerdrClient(SOCKET_PATH)
    summary = {"steps": {}}
    tab_id = None
    pane_id = None

    # 1. create the tab. focus=false so it shouldn't steal the view.
    try:
        resp = client.call("tab.create", {
            "focus": False,
            "label": "herdr-socket-spike",
        })
        tag("tab.create", resp)
        summary["steps"]["tab.create"] = "ok" if "result" in resp else "fail"

        result = resp.get("result", {})
        tab_id = find_first_str(result, ["tab_id"])
        pane_id = find_first_str(result, ["pane_id"])
        summary["tab_id"] = tab_id
        summary["pane_id"] = pane_id
    except Exception as e:
        tag("tab.create", {"ok": False, "error": str(e)})
        summary["steps"]["tab.create"] = "fail"
        tag("SUMMARY", summary)
        return

    if pane_id is None:
        tag("NOTE", {
            "message": (
                "tab.create did not hand back a pane_id in its result -- "
                "printed the raw response above so the actual field name "
                "can be added to find_first_str's key list. Stopping here "
                "so nothing is left half-created without knowing its id."
            )
        })
        summary["steps"]["remaining"] = "skipped, no pane_id from tab.create"
        tag("SUMMARY", summary)
        return

    try:
        resp = client.call("agent.start", {
            "name": "herdr-socket-spike-tabtest",
            "kind": "claude",
            "pane_id": pane_id,
            "timeout_ms": 30000,
        }, timeout=40)
        tag("agent.start", resp)
        summary["steps"]["agent.start"] = "ok" if "result" in resp else "fail"
    except Exception as e:
        tag("agent.start", {"ok": False, "error": str(e)})
        summary["steps"]["agent.start"] = "fail"

    start = time.time()
    try:
        resp = client.call(
            "agent.prompt",
            {
                "target": pane_id,
                "text": TEST_PROMPT,
                "wait": {"until": ["done", "idle"], "timeout_ms": 60000},
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

    try:
        resp = client.call("pane.read", {
            "pane_id": pane_id, "source": "recent", "lines": 40,
        })
        tag("pane.read", resp)
        summary["steps"]["pane.read"] = "ok" if "result" in resp else "fail"
    except Exception as e:
        tag("pane.read", {"ok": False, "error": str(e)})
        summary["steps"]["pane.read"] = "fail"

    # Cleanup: close the whole tab (should take its pane and the agent
    # instance down with it). tab_id is required for this, not pane_id.
    if tab_id:
        try:
            resp = client.call("tab.close", {"tab_id": tab_id})
            tag("tab.close", resp)
            summary["steps"]["tab.close"] = "ok" if "result" in resp else "fail"
        except Exception as e:
            tag("tab.close", {"ok": False, "error": str(e)})
            summary["steps"]["tab.close"] = "fail"
            tag("NOTE", {"message": f"tab.close failed -- tab_id={tab_id} may need manual cleanup"})
    else:
        tag("NOTE", {
            "message": (
                f"no tab_id was found to clean up with -- pane_id={pane_id} "
                "may need manual closing"
            )
        })

    tag("SUMMARY", summary)


if __name__ == "__main__":
    main()

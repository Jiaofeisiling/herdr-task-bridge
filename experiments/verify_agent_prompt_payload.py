"""Answer one question: does herdr's `agent.prompt` (with `wait`) hand the
agent's reply back in its own response payload, or does the caller still have
to scrape the terminal with `pane.read`?

That answer decides how much of bridge.py's prompt machinery can go away:

  - If the reply IS in the payload -> the SENTINEL_DONE_<token> marker,
    build_recovery_prompt(), and extract_task_response()'s "find the last
    '● '" heuristic are all obsolete. bridge.py just reads a field.
  - If it is NOT -> the marker contract stays, and the fix has to happen at
    the prompt/extraction layer instead (paired markers, or having the agent
    write its result to a file).

Run this FROM one agent's shell, TARGETING a different agent. Do not point it
at the agent that is executing it: that agent is "working" for as long as
this script runs, so `wait` can only ever time out (hit for real earlier in
this project's history).

Usage:
    python3 verify_agent_prompt_payload.py <target-pane-or-name>

    # e.g. run this in the w1:p3 agent's shell:
    python3 verify_agent_prompt_payload.py w1:p9

Env:
    HERDR_SOCKET_PATH  overrides ~/.config/herdr/herdr.sock
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

# Distinctive enough that finding it in a payload is unambiguous evidence,
# and short enough that a one-line reply is a cheap request.
NEEDLE = "PROBE_REPLY_7F3A9C"

PROBE_PROMPT = (
    f"请只回复一行：{NEEDLE}\n"
    "不要执行任何命令，不要读写任何文件，不要做别的事。"
)


def call(method, params=None, timeout=30):
    """One request per connection -- herdr's socket closes after answering
    one plain RPC (verified: reusing a connection gives EPIPE on the 2nd)."""
    req = {"id": f"verify-{method}", "method": method, "params": params or {}}

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(SOCKET_PATH)
        sock.sendall((json.dumps(req) + "\n").encode("utf-8"))

        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("socket closed before a full response")
            buf += chunk

        return json.loads(buf.partition(b"\n")[0].decode("utf-8"))
    finally:
        sock.close()


def tag(label, payload):
    print(f"VERIFY::{label}:: " + json.dumps(payload, ensure_ascii=False))


def main():
    if len(sys.argv) < 2:
        tag("ERROR", {"message": "need a target pane_id or agent name"})
        return

    target = sys.argv[1]
    verdict = {"target": target, "needle": NEEDLE}

    try:
        tag("status_before", call("agent.get", {"target": target}))
    except Exception as e:
        tag("status_before", {"ok": False, "error": str(e)})
        tag("VERDICT", {**verdict, "result": "target unreachable"})
        return

    # The actual question.
    started = time.time()
    try:
        prompt_resp = call(
            "agent.prompt",
            {
                "target": target,
                "text": PROBE_PROMPT,
                "wait": {"until": ["done", "idle"], "timeout_ms": 90000},
            },
            timeout=120,
        )
    except Exception as e:
        tag("agent.prompt", {"ok": False, "error": str(e)})
        tag("VERDICT", {**verdict, "result": "agent.prompt failed"})
        return

    elapsed = round(time.time() - started, 1)
    tag("agent.prompt", prompt_resp)

    payload_text = json.dumps(prompt_resp, ensure_ascii=False)
    needle_in_payload = NEEDLE in payload_text

    verdict["elapsed_s"] = elapsed
    verdict["prompt_response_keys"] = sorted(
        prompt_resp.get("result", {}).keys()
    ) if isinstance(prompt_resp.get("result"), dict) else None
    verdict["payload_bytes"] = len(payload_text)
    verdict["reply_in_prompt_payload"] = needle_in_payload

    tag("status_after", call("agent.get", {"target": target}))

    # For comparison: what the current terminal-scraping approach would see.
    try:
        read_resp = call("pane.read", {
            "pane_id": target, "source": "recent", "lines": 25,
        })
        read_text = json.dumps(read_resp, ensure_ascii=False)
        verdict["reply_in_pane_read"] = NEEDLE in read_text
        tag("pane.read_excerpt", {"chars": len(read_text), "tail": read_text[-1200:]})
    except Exception as e:
        tag("pane.read", {"ok": False, "error": str(e)})
        verdict["reply_in_pane_read"] = None

    if needle_in_payload:
        verdict["result"] = (
            "agent.prompt returns the reply -- marker/extraction machinery "
            "can be retired"
        )
    else:
        verdict["result"] = (
            "agent.prompt does NOT return the reply -- the fix belongs at the "
            "prompt/extraction layer, not the transport"
        )

    tag("VERDICT", verdict)


if __name__ == "__main__":
    main()

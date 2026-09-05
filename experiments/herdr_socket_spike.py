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

What the default run checks, in order, printing one clearly-tagged line
per step so the result is easy to pull out of a noisy terminal capture:

  1. ping                                     -- socket reachable at all?
  2. agent.list                               -- how do we find our target?
  3. agent.get(target)                        -- structured status, no CLI parsing
  4. events.subscribe(pane.agent_status_changed) -- does the ack come back clean?
  5. pane.read(target)                        -- final output text

Step 5.5 (agent.prompt with `wait`) is deliberately NOT run against the
agent named on the command line by default: doing so from inside a shell
command that agent itself is executing is self-referential -- the agent is
"working" (running this very script) for the whole duration, so `wait`
can only time out. Run this script from a plain terminal, targeting an
agent that is actually idle, to test that path meaningfully.

`--scratch-pane-test` goes one step further: it creates a brand-new pane
(pane.split), starts a real `claude` agent in it (agent.start -- this is a
real, billed agent instance, not a mock), sends the wait-based prompt to
*that* fresh pane instead, reads the result, and closes the pane
afterwards. Opt-in on purpose since it has a real cost and side effect,
unlike everything else in this script.

Usage:
    python3 herdr_socket_spike.py [agent_name] [--scratch-pane-test]

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

_args = [a for a in sys.argv[1:] if not a.startswith("--")]
AGENT_NAME = _args[0] if _args else "sentinel-opencode"
RUN_SCRATCH_PANE_TEST = "--scratch-pane-test" in sys.argv[1:]

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


def run_scratch_pane_test(client, workspace_id):
    """Create a disposable pane + real `claude` agent, prompt it with
    `wait`, read the result, then close the pane. Never touches any
    existing session/pane."""

    result = {"steps": {}}
    pane_id = None

    try:
        resp = client.call("pane.split", {
            "direction": "right",
            "workspace_id": workspace_id,
            "focus": False,
        })
        tag("scratch.pane.split", resp)
        pane_id = (
            resp.get("result", {}).get("pane_id")
            or resp.get("result", {}).get("id")
        )
        result["steps"]["pane.split"] = "ok" if pane_id else "fail (no pane_id in result)"
        result["pane_id"] = pane_id
    except Exception as e:
        tag("scratch.pane.split", {"ok": False, "error": str(e)})
        result["steps"]["pane.split"] = "fail"
        return result

    if pane_id is None:
        return result

    try:
        # kind="claude" mirrors the existing w1:p9 pane on this host, which
        # is known to start with zero extra args.
        resp = client.call("agent.start", {
            "name": "herdr-socket-spike-scratch",
            "kind": "claude",
            "pane_id": pane_id,
            "timeout_ms": 30000,
        }, timeout=40)
        tag("scratch.agent.start", resp)
        result["steps"]["agent.start"] = "ok" if "result" in resp else "fail"
    except Exception as e:
        tag("scratch.agent.start", {"ok": False, "error": str(e)})
        result["steps"]["agent.start"] = "fail"

    # The real test: agent.prompt's `wait` against a genuinely idle,
    # unrelated-to-anything-else pane.
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
        tag("scratch.agent.prompt", resp)
        result["steps"]["agent.prompt"] = "ok" if "result" in resp else "fail"
        result["agent.prompt_elapsed_s"] = round(elapsed, 1)
    except Exception as e:
        tag("scratch.agent.prompt", {"ok": False, "error": str(e)})
        result["steps"]["agent.prompt"] = "fail"

    try:
        resp = client.call("pane.read", {
            "pane_id": pane_id, "source": "recent", "lines": 40,
        })
        tag("scratch.pane.read", resp)
        result["steps"]["pane.read"] = "ok" if "result" in resp else "fail"
    except Exception as e:
        tag("scratch.pane.read", {"ok": False, "error": str(e)})
        result["steps"]["pane.read"] = "fail"

    try:
        resp = client.call("pane.close", {"pane_id": pane_id})
        tag("scratch.pane.close", resp)
        result["steps"]["pane.close"] = "ok" if "result" in resp else "fail"
    except Exception as e:
        tag("scratch.pane.close", {"ok": False, "error": str(e)})
        result["steps"]["pane.close"] = "fail"
        tag("NOTE", {
            "message": (
                f"pane.close failed -- pane_id={pane_id} may need manual "
                "cleanup"
            )
        })

    return result


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
    workspace_id = None
    try:
        resp = client.call("agent.list")
        tag("agent.list", resp)
        agents = resp.get("result", {}).get("agents", [])
        for a in agents:
            if a.get("name") == AGENT_NAME:
                target = a.get("pane_id")
                workspace_id = a.get("workspace_id")
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

    # 5.5. agent.prompt(wait=...) against a disposable, genuinely idle
    #      pane -- see run_scratch_pane_test's docstring. Opt-in: real
    #      cost, real side effect (a new pane + a real agent instance).
    if RUN_SCRATCH_PANE_TEST:
        summary["scratch_pane_test"] = run_scratch_pane_test(client, workspace_id)
    else:
        tag("NOTE", {
            "message": (
                "skipped agent.prompt(wait=...) validation -- rerun with "
                "--scratch-pane-test to create a disposable pane+agent and "
                "test it properly (real cost/side effect, opt-in)"
            )
        })

    tag("SUMMARY", summary)


if __name__ == "__main__":
    main()

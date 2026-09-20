#!/usr/bin/env python3
"""Post-deployment smoke test against a running bridge.

Run this after deploying, from the Windows side (or anywhere the bridge's
port is reachable):

    python scripts/smoke.py
    python scripts/smoke.py --url http://127.0.0.1:8766

This is an operator tool, not part of the pytest suite: it needs a real
bridge, a real herdr and at least one live agent, none of which CI has.

It checks the channel before it checks anything else, and refuses to run
the functional checks if the channel is not healthy. A previous ad-hoc
version did not, and reported fifteen functional failures during a
tunnel outage -- every one of them a lie about the bridge. The failure
being diagnosed at the time was the mirror image of the same mistake, so
the guard is the point of this file, not an extra.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8765"
PROBE_TIMEOUT = 8


class ChannelDown(RuntimeError):
    pass


def request(url, method="GET", body=None, timeout=30, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Sentinel-Token"] = token

    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
    )

    try:
        response = urllib.request.urlopen(req, timeout=timeout)
        return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def check_channel(base, token):
    """Tell a dead channel apart from a dead bridge before testing anything.

    The three states look different and want different fixes, and only
    the last one makes a functional result meaningful:

      connection refused  -- nothing is listening; the tunnel is down
      connects, no reply  -- the local forwarder is listening but its
                             path to the host is gone (a half-dead SSH
                             forward, which is easy to mistake for a
                             broken bridge because the port still exists)
      a JSON reply        -- the bridge is answering
    """
    try:
        status, body = request(base + "/health", timeout=PROBE_TIMEOUT, token=token)
    except urllib.error.URLError as e:
        raise ChannelDown(
            f"cannot reach {base} ({e.reason}).\n"
            "  Nothing is listening. Reconnect VS Code to the host so the\n"
            "  port forward is re-established, then run this again."
        )
    except TimeoutError:
        raise ChannelDown(
            f"connected to {base} but it never replied within "
            f"{PROBE_TIMEOUT}s.\n"
            "  The local port is still being forwarded, but the forward's\n"
            "  path to the host is dead. Disconnect and reconnect VS Code --\n"
            "  reloading the window usually does not rebuild the forward.\n"
            "  The bridge process on the host is probably fine."
        )

    if status != 200 or not body.get("ok"):
        raise ChannelDown(
            f"{base}/health answered {status}: {json.dumps(body)[:200]}\n"
            "  The channel works but the bridge is unhealthy."
        )

    return body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--token", default=None)
    args = parser.parse_args()

    base = args.url.rstrip("/")

    try:
        health = check_channel(base, args.token)
    except ChannelDown as e:
        print(f"CHANNEL DOWN: {e}")
        print("\nNo functional checks were run -- during an outage they would\n"
              "all fail and none of those failures would be about the bridge.")
        return 2

    print(f"bridge v{health['version']} at {base}")
    print(f"  default_agent: {health['default_agent'] or '<auto-select>'}")
    print(f"  worker_alive:  {health['worker_alive']}")

    results = []

    def check(name, passed, detail=""):
        results.append((passed, name, detail))

    status, agents_body = request(base + "/agents", token=args.token)
    agents = agents_body.get("agents", [])
    check("/agents lists live agents", status == 200 and bool(agents),
          f"{len(agents)} agent(s)")

    if not agents:
        print("\nNo agents are running, so nothing can be dispatched.")
        print("Start one on the host, then run this again.")
        return 2

    # Identifiers are read from what is actually live rather than hard-coded:
    # herdr drops an agent's name when the agent process restarts, and pane
    # ids shift when panes are recreated, so any fixed value here would rot.
    identifiers = []
    families = set()
    for agent in agents:
        # Guarded because this reads whatever herdr reported, not anything
        # this script produced. The same assumption -- every entry is a
        # dict with an identifier -- was a live crash in the bridge's own
        # agent selection, so it does not get to reappear in the tool
        # written to catch such things.
        if not isinstance(agent, dict):
            continue
        identifier = agent.get("name") or agent.get("pane_id")
        if identifier:
            identifiers.append(identifier)
        if agent.get("agent"):
            families.add(agent["agent"])

    if not identifiers:
        print("\nAgents are running but none has a name or a pane id, so "
              "none can be addressed. Check `herdr agent list` on the host.")
        return 2

    print(f"  agents: {', '.join(identifiers)}")
    print(f"  families: {', '.join(sorted(families))}\n")

    for identifier in identifiers:
        status, body = request(
            f"{base}/ready?agent={identifier}", token=args.token
        )
        check(f"/ready resolves {identifier}", status == 200 and body.get("ok"),
              f"status={body.get('agent_status')} reason={body.get('reason', '-')}")

    for family in sorted(families):
        status, body = request(
            f"{base}/ready?agent={family}", token=args.token
        )
        check(f"/ready resolves runtime family '{family}'",
              status == 200 and body.get("ok"))

    status, body = request(
        base + "/ready?agent=definitely-not-an-agent", token=args.token
    )
    check("unknown agent answers 404 agent_not_found, listing live agents",
          status == 404 and body.get("reason") == "agent_not_found"
          and any(i in body.get("error", "") for i in identifiers))

    # The dispatch path resolves separately from the status path, and once
    # shipped with resolution on the latter only -- a runtime family
    # answered /ready and failed /ask.
    status, body = request(
        base + "/ask",
        method="POST",
        body={"task": "x", "agent": "definitely-not-an-agent", "timeout_ms": 5000},
        timeout=20,
        token=args.token,
    )
    check("/ask reports an unknown agent as agent_not_found, not a dead channel",
          status == 404 and body.get("reason") == "agent_not_found")

    for name, payload in [
        ("empty task", {"task": "   "}),
        ("empty agent", {"task": "x", "agent": ""}),
        ("timeout below the floor", {"task": "x", "timeout_ms": 999}),
    ]:
        status, _ = request(base + "/delegate", method="POST", body=payload,
                            token=args.token)
        check(f"/delegate rejects {name}", status == 400)

    status, _ = request(base + "/read?agent=%s&lines=99999" % identifiers[0],
                        token=args.token)
    check("/read rejects an out-of-range line count", status == 400)

    status, body = request(base + "/quota", token=args.token)
    blocked = body.get("blocked_agents", [])
    check("/quota answers", status == 200,
          f"{len(blocked)} circuit(s) open")
    for circuit in blocked:
        print(f"    circuit: {circuit['agent']} since {circuit['detected_at']}")

    print()
    for passed, name, detail in results:
        mark = "PASS" if passed else "FAIL"
        suffix = f"  ({detail})" if detail else ""
        print(f"  {mark}  {name}{suffix}")

    failed = [name for passed, name, _ in results if not passed]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")

    if failed:
        print("\nFailed:")
        for name in failed:
            print(f"  - {name}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

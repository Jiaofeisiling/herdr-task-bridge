#!/usr/bin/env python3
"""Acceptance run against a live bridge and its real agents.

smoke.py asks "is the deployment healthy?" and sends nothing to an agent. This
asks "do the newer behaviors work on this host?": structured results,
dependencies, cancellation, the event stream and idempotent retries. The unit
and end-to-end tests prove them against a fake herdr; only this proves them
against yours.

    python scripts/acceptance.py                        # prints the plan only
    python scripts/acceptance.py --yes                  # runs it
    python scripts/acceptance.py --yes --report run.md  # and writes a report

It creates four small probe tasks, and two of them really run on a live agent,
which costs a little of its quota, so it does nothing without --yes. The probes
tell the agent not to run commands, touch files or use the cluster, and carry
slurm_policy dry_run_only, so nothing can be submitted. The other two (one
cancelled while it waits, and one waiting on that) never start: they are never
sent to any agent, which is itself one of the things being checked.

The report is meant to be shared. It names the kind of agent that ran
(opencode, claude), never which one, and nothing about the host: no hostnames,
paths, task ids or pane ids.

Exit codes: 0 every check passed (or was skipped with a reason), 1 a check
failed, 2 nothing was concluded -- the channel is down, the bridge is too old,
no agent is running, or --yes was not given.
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smoke  # noqa: E402  (request, check_channel: one copy of both)

MIN_VERSION = 27   # the first bridge with workflows, cancellation and events
TERMINAL = ("done", "error", "orphaned", "quota_exhausted", "cancelled")
PROBE = (
    "Acceptance probe. Do not run any commands, do not modify any files and "
    "do not use the cluster. Reply with the single word OK."
)


class Run:
    def __init__(self, base, token, patience):
        self.base = base
        self.token = token
        self.patience = patience
        self.checks = []          # (state, name, detail)
        self.sent = []            # task ids this run created

    def call(self, method, path, body=None, timeout=30, token=True):
        return smoke.request(
            self.base + path, method=method, body=body, timeout=timeout,
            token=self.token if token else None,
        )

    def record(self, state, name, detail=""):
        self.checks.append((state, name, detail))

    def check(self, name, condition, detail=""):
        self.record("PASS" if condition else "FAIL", name, detail)
        return bool(condition)

    def skip(self, name, why):
        self.record("SKIP", name, why)

    def delegate(self, **extra):
        status, body = self.call(
            "POST", "/delegate",
            {"task": PROBE, "slurm_policy": "dry_run_only", **extra},
        )
        if status != 202:
            raise RuntimeError(f"/delegate answered {status}: {json.dumps(body)[:200]}")
        self.sent.append(body["task_id"])
        return body["task_id"]

    def task(self, task_id):
        return self.call("GET", f"/tasks/{task_id}")[1].get("task", {})

    def wait(self, task_id, label, timeout=None):
        """The task once it is in a final state, or None if it never got there.

        Says what it is waiting for, because a real agent can take minutes over
        a probe and a script that prints nothing looks hung. Never the task id.
        """
        started = time.monotonic()
        deadline = started + (timeout or self.patience)
        last_status, last_said = None, started

        while time.monotonic() < deadline:
            task = self.task(task_id)
            status = task.get("status")

            if status in TERMINAL:
                return task

            now = time.monotonic()
            if status != last_status or now - last_said >= 15:
                print(f"  waiting for {label}: {status} ({now - started:.0f} s)", flush=True)
                last_status, last_said = status, now

            time.sleep(1.5)

        return None

    def tidy(self):
        """Cancel whatever is still waiting, so a failed run leaves nothing queued."""
        for task_id in self.sent:
            try:
                self.call("POST", f"/tasks/{task_id}/cancel", {})
            except Exception:
                pass


def moves(events):
    return [(e["from_status"], e["to_status"]) for e in events]


def parse_time(stamp):
    return datetime.datetime.fromisoformat(stamp) if stamp else None


def run_probes(run):
    workflow = f"acceptance-{uuid.uuid4().hex[:8]}"
    key = f"{workflow}-a"

    # -- submit: a, then two tasks that wait, one behind the other ------------
    a = run.delegate(workflow_id=workflow, idempotency_key=key)

    status, again = run.call("POST", "/delegate", {
        "task": PROBE, "slurm_policy": "dry_run_only",
        "workflow_id": workflow, "idempotency_key": key,
    })
    run.check(
        "idempotency: a retry with the same key returns the original task",
        status == 202 and again.get("task_id") == a and again.get("replayed") is True,
        f"status {status}",
    )

    y = run.delegate(workflow_id=workflow, depends_on=[a])
    z = run.delegate(workflow_id=workflow, depends_on=[y])
    b = run.delegate(workflow_id=workflow, depends_on=[a])

    # -- cancel y while it still waits on a; z waits on y ----------------------
    status, body = run.call("POST", f"/tasks/{y}/cancel", {})
    cancelled_in_time = status == 200
    if not cancelled_in_time:
        run.skip(
            "cancellation, and its cascade, before a task starts",
            f"the cancel answered {status}: the agent was quicker than the "
            "probe. Run it again",
        )
    else:
        z_final = run.wait(z, "the cascade to cancel the task behind it", timeout=60)
        y_now, z_now = run.task(y), run.task(z)
        run.check(
            "cancellation: a task cancelled while it waited was never sent",
            y_now.get("status") == "cancelled" and y_now.get("started_at") is None
            and "never sent" in (y_now.get("error_text") or ""),
        )
        run.check(
            "cancellation cascades to the task that waited on it, also never sent",
            bool(z_final) and z_now.get("status") == "cancelled"
            and z_now.get("started_at") is None
            and "never sent" in (z_now.get("error_text") or ""),
        )

    # -- the tasks that really run ----------------------------------------------
    a_final = run.wait(a, "the first probe")
    b_final = run.wait(b, "the probe that depends on it")

    if not a_final or a_final["status"] != "done":
        run.check(
            "the first probe is delivered",
            False,
            f"ended {a_final['status'] if a_final else 'no result in time'}",
        )
    else:
        run.check("the first probe is delivered",
                  bool((a_final.get("result_text") or "").strip()))

        fmt = a_final.get("result_format")
        data = a_final.get("result_data")
        consistent = (
            (fmt == "json" and isinstance(data, dict)
             and isinstance(data.get("summary"), str) and data["summary"].strip())
            or (fmt == "text" and data is None)
        )
        run.check(
            "structured result: served as fields when the agent wrote JSON, "
            "as text when it did not",
            consistent,
            f"this agent answered in {fmt}",
        )

    if b_final and b_final["status"] == "done" and a_final:
        run.check(
            "dependency: the waiting task started only after what it waited on finished",
            parse_time(b_final["started_at"]) >= parse_time(a_final["finished_at"]),
        )
    else:
        run.check("dependency: the waiting task ran after its dependency", False,
                  f"ended {b_final['status'] if b_final else 'no result in time'}")

    # -- the event stream ---------------------------------------------------------
    status, body = run.call("GET", f"/events?task_id={a}")
    events = body.get("events", [])
    run.check(
        "event stream: a task's whole life is recorded, in order",
        status == 200 and moves(events) == [
            (None, "queued"), ("queued", "running"), ("running", "done"),
        ],
        f"{len(events)} event(s)",
    )

    cursor = body.get("next", 0)
    status, after = run.call("GET", f"/events?task_id={a}&after={cursor}")
    run.check(
        "event stream: resuming from the cursor repeats nothing",
        status == 200 and after.get("events") == [] and after.get("next") == cursor,
    )

    started = time.monotonic()
    status, polled = run.call("GET", f"/events?task_id={a}&after={cursor}&wait=2",
                              timeout=20)
    took = time.monotonic() - started
    run.check(
        "event stream: a long poll waits, then comes back empty",
        status == 200 and polled.get("events") == [] and 1.5 <= took < 10,
        f"{took:.1f}s",
    )

    # -- the workflow as a whole ----------------------------------------------------
    status, flow = run.call("GET", f"/workflows/{workflow}")
    if cancelled_in_time and a_final and b_final:
        run.check(
            "workflow: counts and state add up",
            status == 200
            and flow.get("counts") == {"done": 2, "cancelled": 2}
            and flow.get("state") == "failed",
            f"{flow.get('counts')} -> {flow.get('state')}",
        )
    else:
        run.skip("workflow: counts and state add up",
                 "the run did not reach the state this check expects")

    if run.token:
        status, _ = run.call("GET", "/events", token=False)
        run.check("auth: the event stream refuses a caller without the token",
                  status == 401, f"status {status}")


def write_report(path, health, families, run):
    passed = sum(1 for s, _, _ in run.checks if s == "PASS")
    failed = sum(1 for s, _, _ in run.checks if s == "FAIL")
    skipped = sum(1 for s, _, _ in run.checks if s == "SKIP")
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    lines = [
        "# Acceptance run",
        "",
        f"- Date: {today} (UTC)",
        f"- Bridge version: {health['version']}",
        f"- Agent runtimes: {', '.join(sorted(families)) or 'unknown'}",
        f"- Result: {passed} passed, {failed} failed, {skipped} skipped",
        "",
        "| Result | Check | Detail |",
        "|---|---|---|",
    ]
    for state, name, detail in run.checks:
        lines.append(f"| {state} | {name} | {detail} |")

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=smoke.DEFAULT_URL)
    # Read from the environment by default: a command line is visible to every
    # user in the process list and stays in the shell history.
    parser.add_argument("--token", default=os.environ.get("SENTINEL_BRIDGE_TOKEN"),
                        help="access token (default: $SENTINEL_BRIDGE_TOKEN)")
    parser.add_argument("--yes", action="store_true",
                        help="actually send the probe tasks")
    parser.add_argument("--patience", type=int, default=600,
                        help="seconds to wait for a probe to finish (default 600)")
    parser.add_argument("--report", default=None, help="write a shareable report here")
    args = parser.parse_args()

    base = args.url.rstrip("/")

    try:
        health = smoke.check_channel(base, args.token)
    except smoke.ChannelDown as e:
        print(f"CHANNEL DOWN: {e}")
        print("\nNothing was run: during an outage every check would fail and "
              "none of those failures would be about the bridge.")
        return 2

    version = health.get("version", 0)
    print(f"bridge v{version} at {base}")

    if version < MIN_VERSION:
        print(f"\nThis needs bridge v{MIN_VERSION} or later (workflows, cancellation, "
              "events). Deploy the current version, then run this again.")
        return 2

    status, agents_body = smoke.request(base + "/agents", token=args.token)
    agents = [a for a in agents_body.get("agents", []) if isinstance(a, dict)]
    families = {a["agent"] for a in agents if a.get("agent")}

    if status != 200 or not agents:
        print("\nNo agent is running, so nothing can be dispatched. "
              "Start one on the host, then run this again.")
        return 2

    print(f"agent runtimes: {', '.join(sorted(families))}")

    if not args.yes:
        print(
            "\nThis would create 4 small probe tasks. Two really run on a live "
            "agent; the other two are cancelled before they start and are "
            "never sent. It then reads the results, the event stream and the "
            "workflow back. The probes forbid running commands, touching "
            "files and using the cluster. Nothing has been sent.\n\n"
            "Run it with --yes."
        )
        return 2

    run = Run(base, args.token, args.patience)
    try:
        run_probes(run)
    except (RuntimeError, urllib.error.URLError, TimeoutError) as e:
        run.record("FAIL", "the run completed", str(e)[:200])
    finally:
        run.tidy()

    print()
    for state, name, detail in run.checks:
        print(f"  {state}  {name}" + (f"  ({detail})" if detail else ""))

    failed = [name for state, name, _ in run.checks if state == "FAIL"]
    passed = sum(1 for s, _, _ in run.checks if s == "PASS")
    print(f"\n{passed} passed, {len(failed)} failed, "
          f"{sum(1 for s, _, _ in run.checks if s == 'SKIP')} skipped")

    if args.report:
        write_report(args.report, health, families, run)
        print(f"report written to {args.report}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

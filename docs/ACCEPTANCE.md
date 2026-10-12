# Acceptance on a real host

The unit and end-to-end tests run the bridge against a fake herdr. They show
the logic is right; they cannot show that it works against your herdr, your
agents and your network path. `scripts/acceptance.py` does, and this file is
where its runs are recorded.

## Running it

From anywhere the bridge's port is reachable (usually the Windows side, over
the SSH forward):

```bash
python scripts/acceptance.py                          # prints the plan, sends nothing
python scripts/acceptance.py --yes --report run.md    # runs it, writes a report
```

If the bridge has a token (it must, from v28), the script reads it from the
`SENTINEL_BRIDGE_TOKEN` environment variable or the file `sentinel.ps1 login`
writes, and then also checks that the
event stream refuses a caller without it. Do not type the token on the command
line: on a shared login node that line is visible to every other user in the
process list while it runs, and it stays in your shell history. (`--token` still
exists and wins over the variable, for a machine where neither matters.) While a
probe runs the script says what it is waiting for; a real agent can take minutes
over one. `--patience` (seconds, default 600) is how long it waits for a probe.

It checks the channel first and stops with exit 2 if the channel is down, the
bridge is older than v27, or no agent is running, because in those cases every
failure would be about the channel and none about the bridge.

## What it costs and what it can touch

It creates four small probe tasks. Two really run on a live agent and cost a
little of its quota. The other two are cancelled before they start and are
never sent. The probes tell the agent not to run commands, modify files or use
the cluster, and carry `slurm_policy: dry_run_only`, so nothing can be
submitted. Anything still waiting when the run ends is cancelled.

## What each check proves

| Check | Passes when |
|---|---|
| idempotency | A second delegate with the same key returns the first task, flagged as a replay |
| cancellation, never sent | A task cancelled while it waited on another has no `started_at` and says it was never sent |
| cancellation cascades | The task that waited on the cancelled one was cancelled too, also never sent |
| the first probe is delivered | The agent's answer arrived through the result file |
| structured result | `result_format` is `json` with a `summary`, or `text` with no `result_data`. Either is valid: it records which this agent produces |
| dependency | The waiting task started only after the task it waited on had finished |
| event stream, whole life | The first task's events are exactly created, started, finished, in order |
| event stream, cursor | Asking again from the returned cursor repeats nothing |
| event stream, long poll | A wait with nothing to report takes about as long as asked, then returns empty |
| workflow | The counts and state of the whole workflow add up |
| auth (with a token) | The event stream answers 401 without the token |

A `SKIP` carries its reason. The usual one is that the agent finished the first
probe before the script could cancel the one waiting on it; run it again.

## What it does not cover

It does not kill or restart the bridge, submit a Slurm job, or trigger a
provider's quota. Those are covered against the fake herdr in
`sentinel-bridge/test_e2e.py`, and are not something to do to a live bridge for
the sake of a report. Before relying on them in earnest, check them by hand on
a quiet host.

## Runs

The report names the agent's runtime (`opencode`, `claude`) and nothing about
the host: no hostnames, paths, task ids or pane ids. Paste each one below under
its date, newest first, with the commit it was run at.

### 2026-10-11 (UTC), bridge v27 at `2cfc4a6`

Deployed to the reference host (NeSI) and run against two live agents (`agy`,
`cursor`), with no authentication token set, so the auth check did not run.

- Date: 2026-10-11 (UTC)
- Bridge version: 27
- Agent runtimes: agy, cursor
- Result: 10 passed, 0 failed, 0 skipped

| Result | Check | Detail |
|---|---|---|
| PASS | idempotency: a retry with the same key returns the original task | status 202 |
| PASS | cancellation: a task cancelled while it waited was never sent |  |
| PASS | cancellation cascades to the task that waited on it, also never sent |  |
| PASS | the first probe is delivered |  |
| PASS | structured result: served as fields when the agent wrote JSON, as text when it did not | this agent answered in json |
| PASS | dependency: the waiting task started only after what it waited on finished |  |
| PASS | event stream: a task's whole life is recorded, in order | 3 event(s) |
| PASS | event stream: resuming from the cursor repeats nothing |  |
| PASS | event stream: a long poll waits, then comes back empty | 2.2s |
| PASS | workflow: counts and state add up | {'done': 2, 'cancelled': 2} -> failed |

The deployment itself was a second, unplanned check. It was done by delegating
the steps to a live agent: back up the task database, `git pull --ff-only`, run
`remote/bridge-restart.sh`. The restart marked that very task `orphaned`, as
documented; the agent then delivered its report, and the new bridge recovered it
on its own (`recovered_at` set, and "Recovered 1 late result" in its log). Exit
status 0 from the restart script, `/health` reporting version 27, and the
database backed up first (about 13 MB).

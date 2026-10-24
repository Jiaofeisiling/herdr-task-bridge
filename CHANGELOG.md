# Changelog

The bridge reports its own version as `BRIDGE_VERSION` in `/health`; entries
are listed against it, and releases are tagged `v4.x.y`. Earlier history is in
`git log`. Bridge v27 was deployed to the reference host but never tagged; it
is folded into 4.1.0.

## Unreleased

- Expire only the quota-row snapshot that was checked, preserving concurrent
  circuit renewals. Persist already-read synchronous answers after shutdown
  changes their state, even if the original result file is unavailable.
- Drain background task execution and settlement during graceful shutdown,
  refuse further prompts/reminders once shutdown begins, and let only the
  first signal control completion of the shutdown drain.
- Never resend a failed prompt through a newly resolved target outside its
  dispatch lock. Unknown delivery errors retain the original execution target.
- Retry synchronous failure settlement after database outages, retain task IDs
  on unexpected execution errors, and keep optional routing diagnostics from
  preventing successful result persistence.
- Recover available late parent results before cancelling dependent tasks during
  normal operation, with cancellation decisions serialized against adoption.
- Keep advisory startup discovery failures and Windows cross-drive paths from
  preventing the HTTP service from starting.
- Persist synchronous asks before dispatch and commit their results before HTTP
  delivery. Retry interrupted result/state persistence without resending work.
- Hold an OS lock per database before startup recovery, reserve the listener
  before changing tasks, recover answers before scheduling dependencies, and
  stop claiming queued work when shutdown begins.
- Merge agent alias locks without losing active holders. Keep the actual
  execution agent on failed fallback attempts as well as successful ones.
- Compare stable provider refusal evidence and repeated occurrences without
  interpreting ordinary text appended to an old error as a new refusal.
- Share one execution deadline across discovery, diagnostics, failover and result
  reminders. Bound queued-task explanations and filter lists after late recovery.
- Include dispatch locks in readiness, resolve runtime-family quota resets, and
  keep execution uncertain when a quick task loses its result.
- Validate restart health responses, use the configured port and script checkout,
  and recognise the supervisor's unbuffered Python command in process checks.
- Add isolated regressions, native Bash deployment-script tests on Windows when
  available, and explicit UTF-8 subprocess output for the E2E suite.
- Detect Windows from the native platform when securing the client token file;
  a missing `OS` environment variable must not bypass ACL protection or its tests.
  Include the audit regression module in the Windows CI run.

## 4.1.0 (bridge v28)

### Breaking

- **The bridge does not start without an access token.** With no
  `SENTINEL_BRIDGE_TOKEN`, or one shorter than 16 characters, `bridge.py` exits
  with status 2. `SENTINEL_ALLOW_NO_AUTH=1` runs open knowingly. Before
  deploying this version, make a token with `remote/bridge-set-token.sh` and
  give it to every client. `bridge-restart` checks first (`bridge.py --check`)
  and stops nothing if the new bridge would refuse, so a missing token costs a
  message, not an outage.

### Security

- A token set in `bridge.env` now reaches `bridge.py`. The supervisor only
  sourced the file, which sets shell variables without exporting them, so the
  token the example file showed never took effect and the bridge ran open
  without saying so.
- `remote/bridge-set-token.sh` makes a 256-bit token in the operator's own
  terminal, shows it once, and writes it to `bridge.env` (mode 600).
- Files are private: a private umask, an existing `tasks.db` tightened to mode
  600, warnings (with the command) when the database directory or the result
  directory is open to others, and `bridge.log` and `bridge.env` set to 600 by
  the supervisor.
- Request bodies over `SENTINEL_MAX_BODY_BYTES` (1 MiB) are refused unread, and
  a connection that stalls for `SENTINEL_REQUEST_TIMEOUT_SEC` is dropped.
- An audit trail: every request that is not a read, and every refusal of the
  token, is recorded before the reply goes out. `GET /audit`. No token, query
  string or request body is stored. `sentinel.ps1` sends `X-Sentinel-Client`.
- The supervisor stops instead of restarting every five seconds when
  `bridge.py` refuses its own settings.

### Performance

- Indexes on `tasks(status, created_at)`, `tasks(created_at)` and
  `task_events(workflow_id, seq)`. On a 20,000-task database the worker's poll
  went from 65 ms to 2.3 ms, the queue-cap count in `/delegate` (which ran under
  the write lock) from 65 ms to 2.2 ms, and `GET /tasks` from 361 ms to 2.4 ms.
  `tasks` is never pruned, so these cost more the longer a deployment runs.
- An idle worker starts a task when it is queued instead of at its next poll,
  which took about a second on average. Submit-to-done through a real bridge
  with an instant fake agent: 3.04 s to 1.09 s.

### Added (v28)

- `SENTINEL_TASK_RETENTION_DAYS`: delete finished tasks older than this, once a
  day, with their events. Off by default. It keeps anything queued or running
  and anything a waiting task depends on, reads ages as times (the database holds
  stamps with different UTC offsets), and pauses between batches so that a
  `/delegate` arriving meanwhile is not starved of the write lock: 20,000 tasks
  take about 5 s and the worst concurrent wait was under 200 ms (1.7 s without
  the pause).

### Upgrading to 4.1.0: tighten what already exists

The bridge makes what it creates private and tightens its own database, but only
warns about a directory it did not create, and cannot know about copies made by
hand. After upgrading, `chmod 700` the directory holding `tasks.db` and the result
directory, and `chmod 600` any hand-made backup of the database. On the reference
host a deployment backup made with a plain `cp` was mode 644 in a 755 directory,
and the result files were 644 in a 2755 one: readable by every other user of the
login node. See the README's "After upgrading".

### Fixed (after 4.1.0)

- **A client without the token could not tell why it was refused.** It printed
  `{"error": "unauthorized"}` and exited 1, the same as any failed request, so an
  AI assistant could retry, or go looking for the secret. A refusal now exits 7
  with `AUTHENTICATION REQUIRED` or `AUTHENTICATION FAILED`, says what to do, and
  tells an assistant to stop and ask the user. `wait` gives up at once on it
  instead of riding it out like a lost channel.
- `sentinel.ps1 login` stores the token in `~/.sentinel/token` (readable by its
  owner alone), asking with the input hidden and never taking it as an argument.
  The client reads that file when `SENTINEL_BRIDGE_TOKEN` is not set: an
  environment variable only reaches processes started after it was set, and not
  every tool inherits it (a sandboxed assistant, a window that was already open).
  `smoke.py` and `acceptance.py` read the same file.

- `scripts/acceptance.py` and `scripts/smoke.py` read the token from
  `SENTINEL_BRIDGE_TOKEN` by default. They took it only as `--token <value>`,
  which on a shared login node is visible to every user in the process list and
  stays in the shell history. `--token` still works and wins.
- `acceptance.py` says what it is waiting for. A real agent can take minutes
  over a probe, and a script that printed nothing looked hung and was
  interrupted.

## Bridge v27 (deployed, not tagged; part of 4.1.0)

### Added

- **Task state machine.** One table, `TASK_TRANSITIONS`, and one function,
  `transition()`, for every status change. It is a compare-and-set, so two paths
  racing to finish a task cannot both win.
- **Structured results.** An agent may write its result as a JSON object
  (`status`, `summary`, `job_ids`, `artifacts`, `metrics`, `warnings`,
  `changed_files`). Served as `result_data` with `result_format`; plain text is
  unchanged.
- **Workflows.** `workflow_id`, `depends_on`, the `cancelled` status,
  `POST /tasks/<id>/cancel`, `GET /workflows/<id>`. A failed step cancels
  everything downstream of it before any of it is sent to an agent.
- **Event stream.** Every status change is written to `task_events` in the same
  transaction as the change. `GET /events` with a cursor and a long poll.
- **Opt-in re-pointing** of a queued task whose agent is gone (`reroute: any`).
  The default is unchanged: such a task is failed, unsent.
- Client: `cancel`, `workflow` and `events` commands; `delegate` takes
  `-WorkflowId`, `-DependsOn` and `-Reroute`; `wait` exits 6 for a cancelled
  task.
- `scripts/acceptance.py` and `docs/ACCEPTANCE.md`: a check of the newer
  behavior against a real bridge and real agents.
- `sentinel-bridge/test_e2e.py` and `fake_herdr.py`: end-to-end tests that run
  the real bridge process.

### Changed

- `complete_task`, `orphan_task` and `quota_exhausted_task` act only on a
  `running` task, and return whether they moved it. A worker whose task was
  settled meanwhile no longer completes it, and leaves its result file for late
  adoption.
- The delegation prompt invites the agent to write JSON; plain text remains a
  valid answer. `/ask` replies carry `result.format` and `result.data` beside
  `result.text`.
- CI runs every test file in `sentinel-bridge`.

### Fixed

- A result file is kept until the commit that records it has landed, and an
  agent addressed by name and by pane id shares one lock.
- A NaN or Infinity metric no longer makes a task's reply invalid JSON.
- An out-of-range `/events` cursor is a 400, not a 500.

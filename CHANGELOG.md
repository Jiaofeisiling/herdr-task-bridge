# Changelog

The bridge reports its own version as `BRIDGE_VERSION` in `/health`; entries
are listed against it. Earlier history is in `git log`.

## Unreleased (bridge v28)

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

## Bridge v27

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

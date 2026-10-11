# Changelog

The bridge reports its own version as `BRIDGE_VERSION` in `/health`; entries
are listed against it. Earlier history is in `git log`.

## Unreleased (bridge v27)

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

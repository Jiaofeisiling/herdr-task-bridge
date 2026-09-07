<div align="center">
  <img src="assets/logo.webp" alt="herdr-task-bridge logo" width="180">
</div>

# Herdr Task Bridge

[![tests](https://github.com/Jiaofeisiling/herdr-task-bridge/actions/workflows/tests.yml/badge.svg)](https://github.com/Jiaofeisiling/herdr-task-bridge/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

English | [简体中文](README.zh-CN.md)

`herdr-task-bridge` lets a Windows PowerShell client delegate work to persistent Herdr agent sessions on a reachable Linux host. It provides synchronous requests, a durable asynchronous queue, per-agent mutual exclusion, and recoverable task status.

The maintained reference deployment is **Windows ↔ NeSI ↔ Herdr agents**. The bridge is not NeSI-specific: it can be deployed on another HPC system or a rented Linux server when the operator supplies a reachable Linux host, Python 3, the `herdr` CLI, and an appropriate private network or port-forwarding path.

> [!IMPORTANT]
> This repository is a bridge, not a cluster-management product. Site-specific SSH, port forwarding, scheduler, authentication, storage, quota, and agent-permission configuration remain the deployer's responsibility. Test any deployment against your own security and data-governance rules before delegating work.

## What it does

```
Windows PowerShell client (sentinel.ps1)
        │ HTTP over a private connection or SSH port forward
        ▼
bridge.py on a Linux host (ThreadingHTTPServer + SQLite)
        │ herdr CLI
        ▼
Persistent Herdr-managed agent session(s)
```

The service has two execution modes:

- **Synchronous** — `/ask` and `/prompt` hold the selected agent's lock and keep the request open until the agent finishes.
- **Asynchronous** — `/delegate` stores a task in SQLite and returns a `task_id` immediately. A background worker executes queued tasks and exposes their state through `/tasks/<id>`.

Locks are **per agent**, so work directed to different agents is isolated. The asynchronous worker itself is intentionally single-threaded; this release does not promise parallel execution of queued tasks across agents. `/health` is independent of Herdr and SQLite, so it answers whether the bridge process is alive even when agents or tasks are busy.

## Quick start

This example assumes that a private connection from Windows to the remote bridge already exists (for example, a VS Code Remote-SSH local port forward to `127.0.0.1:8765`). Run it from the repository root:

```powershell
.\sentinel.ps1 health
.\sentinel.ps1 ready

$id = (.\sentinel.ps1 delegate "summarize the current directory; do not modify files" | ConvertFrom-Json).task_id
.\sentinel.ps1 wait $id
```

If the host has more than one Herdr agent, inspect them and select one explicitly:

```powershell
.\sentinel.ps1 agents
.\sentinel.ps1 ask -Agent "your-agent-name" "check disk usage"
```

## Command reference

| Command | HTTP | Endpoint | Purpose |
|---|---|---|---|
| `health` | GET | `/health` | Process liveness only; does not contact Herdr or SQLite. |
| `agents` | GET | `/agents` | Lists Herdr-managed agents and their status. |
| `ready` | GET | `/ready` | Reports whether the selected agent can accept work (`idle` or `done`). |
| `status` | GET | `/status` | Returns the raw `herdr agent get` response. |
| `read` | GET | `/read` | Reads recent agent-terminal lines for diagnosis. |
| `quota` | GET | `/quota` | Lists agents temporarily blocked after a provider quota or balance failure. |
| `quota-reset` | POST | `/quota/reset` | Clears one agent's quota circuit with `-Agent`, or all circuits when deliberately called without it. |
| `delegate <task>` | POST | `/delegate` | Queues a task and returns a `task_id`; default server timeout is six hours. |
| `task <task_id>` | GET | `/tasks/<id>` | Gets task state: `queued`, `running`, `done`, `error`, `orphaned`, or `quota_exhausted`. |
| `wait <task_id>` | GET | `/tasks/<id>` | Polls every three seconds until a terminal state, then prints the result or error. |
| `tasks` | GET | `/tasks` | Lists the 20 most recent tasks. |
| `ask <task>` | POST | `/ask` | Runs synchronously and returns the agent result; returns `409` if that agent is busy. |
| `prompt <task>` | POST | `/prompt` | Runs synchronously but does not extract a result. |

Common client options are `-Agent <name>`, `-TimeoutMs <milliseconds>` (for `ask`, `prompt`, and `delegate`), and `-Lines <count>` (for `read`). Agent names are trimmed, must be non-empty, and are limited to 200 characters. Request timeouts must be between 1 second and 6 hours.

## Task lifecycle and results

```
queued → running → done
              ↓        ↑ (one result-file reminder)
    error / orphaned / quota_exhausted
```

- `orphaned` means the bridge lost the completion signal, not that the remote task necessarily failed. Do **not** blindly retry it; inspect the task's real effects first.
- `error` means the bridge confirmed an execution or result-collection failure.
- `quota_exhausted` means every eligible fallback was also quota-blocked or reported a quota/balance failure. The task was not retried after that result.
- On restart, a task that was `running` becomes `orphaned`. The bridge never reruns it automatically.

Agents return results by writing one file per task under `SENTINEL_RESULT_DIR`; the bridge reads and removes the file. This avoids terminal scraping, truncation, UI noise, and coupling to an agent's terminal format. If the file is missing after an agent finishes, the bridge sends one narrowly scoped reminder to write the result file only. A second failure is reported as `error` with terminal output retained for diagnosis.

The result directory must be writable by both the bridge process and the selected agent. Grant only that narrow write permission; do not weaken an agent's general approval policy merely to collect results.

### Active quota failover

The bridge recognises common provider signals such as Claude 5-hour/weekly usage limits, HTTP `429`, OpenCode API `402`, and insufficient API credit or balance. On detection it opens a durable circuit for the failed agent, skips the otherwise normal result-file reminder, and actively tries an eligible alternate agent.

With automatic discovery, an alternate must be a **different runtime family** (for example, Claude ↔ OpenCode) so the bridge does not simply move to another session that may share the same exhausted account. Set `SENTINEL_QUOTA_FAILOVER_AGENTS` to an ordered, explicit allowlist when your deployment has known independent fallback accounts. An unavailable fallback leaves asynchronous work queued for another attempt; only exhausted eligible fallbacks produce `quota_exhausted`.

After a provider's reset or an account recharge, verify the account outside the bridge and clear its circuit deliberately:

```powershell
sentinel quota
sentinel quota-reset -Agent "your-agent-name"
```

Do not clear a circuit merely because an agent is `idle`: provider limits can leave an agent idle while its account remains unavailable.

## Configuration and security

The remote service reads the following environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `SENTINEL_BRIDGE_PORT` | `8765` | Listening port. |
| `HERDR_BIN` | `herdr` | Path to the Herdr executable. |
| `SENTINEL_AGENT` | `sentinel` | Default target agent when none is specified. |
| `SENTINEL_DB` | `~/sentinel-bridge/tasks.db` | SQLite task-queue path. |
| `SENTINEL_RESULT_DIR` | System temp directory / `sentinel-bridge-results` | Shared directory for agent result files. |
| `SENTINEL_BRIDGE_TOKEN` | unset | Optional shared-secret authentication token. |
| `SENTINEL_MAX_QUEUE_DEPTH` | `50` | Maximum number of queued asynchronous tasks. |
| `SENTINEL_QUOTA_FAILOVER_AGENTS` | unset | Comma-separated, ordered fallback-agent allowlist after a quota failure. |

Copy [`remote/bridge.env.example`](remote/bridge.env.example) to the untracked `remote/bridge.env` for deployment-specific values. Never commit real hostnames, project identifiers, paths, usernames, prompts, or tokens; see [CONTRIBUTING.md](CONTRIBUTING.md) for the project's sensitive-data rules.

Authentication is disabled until `SENTINEL_BRIDGE_TOKEN` is set on both the Linux service and Windows client. Use an ASCII-only, high-entropy token and protect the network path as well. `/health` intentionally remains unauthenticated so it can be used for simple liveness probes.

## Reference deployment: NeSI

The maintained deployment uses a Git clone on a Linux host reachable from NeSI, a `screen` session, and the restart loop in [`remote/bridge-supervisor.sh`](remote/bridge-supervisor.sh). The helper functions in [`remote/bridge-aliases.sh`](remote/bridge-aliases.sh) are the authoritative implementation of the reference update flow:

```bash
echo 'source ~/herdr-task-bridge/remote/bridge-aliases.sh' >> ~/.bashrc
source ~/.bashrc

bridge-deploy    # pull and restart
bridge-status    # inspect screen, process, health, and logs
```

The supervisor preserves a `screen` session and logs restarts to `sentinel-bridge/bridge.log`; do not run `bridge.py` directly in `screen`. For another HPC system or Linux server, adapt only the deployment details (clone location, service supervisor, private connectivity, environment file, and agent permissions). The bridge API and its safety contract remain the same.

On Windows, optionally load [`sentinel.profile.ps1`](sentinel.profile.ps1) from your PowerShell profile to use `sentinel` from any directory:

```powershell
. "E:\herdr-task-bridge\sentinel.profile.ps1"
sentinel health
sentinel ask "check disk usage"
```

## Development and verification

The remote runtime uses only the Python standard library. The test dependency is local to development:

```bash
cd sentinel-bridge
python -m venv .venv
.venv/Scripts/python.exe -m pip install pytest  # Windows
.venv/bin/python -m pip install pytest          # Linux/macOS
.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

The Python suite has 111 mocked tests and does not require a real Herdr installation or network. The Windows client has a 16-test Pester suite that runs against a local HTTP stub:

```powershell
Install-Module -Name Pester -RequiredVersion 5.6.1 -Scope CurrentUser  # first time only
Invoke-Pester -Path .\sentinel.Tests.ps1 -Output Detailed
```

CI runs Python tests and the Pester suite under both Windows PowerShell 5.1 and PowerShell Core. Unit tests do not replace a carefully scoped live verification in your own environment.

## Contributing and design history

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request, especially its rules for preventing institutional or research data from entering Git history. The detailed design and implementation history is in [docs/superpowers/plans/2026-08-30-sentinel-bridge-v2.2-v2.3.md](docs/superpowers/plans/2026-08-30-sentinel-bridge-v2.2-v2.3.md).

Licensed under the [MIT License](LICENSE).

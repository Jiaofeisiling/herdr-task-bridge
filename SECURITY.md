# Security Policy

## Supported Versions

Only the latest commit on `master` is supported. There are no released
versions/tags yet; always update to the current `master` before reporting an
issue.

## Threat Model

`bridge.py` is designed to be reached only via a local port-forward
(`127.0.0.1:8765` on both ends of a VS Code Remote-SSH tunnel — see
[README.md](README.md#架构)). It binds to `127.0.0.1` only, never
`0.0.0.0`, so it is not directly reachable over the network by default.

Loopback is not private on a shared login node: every user of the machine can
reach `127.0.0.1:8765`, and through the bridge **have the agents run commands
as you**. So the access token (`SENTINEL_BRIDGE_TOKEN`) is the one control that
matters, and the bridge does not start without one:

- With no token, or one shorter than 16 characters, `bridge.py` refuses to
  start (exit 2). Running open is possible but has to be asked for by name,
  with `SENTINEL_ALLOW_NO_AUTH=1`; it then still warns at every start.
- `remote/bridge-set-token.sh` makes a 256-bit token in your own terminal,
  shows it once, and writes it to `remote/bridge.env` (mode 600). It will not
  replace a token in use without `--rotate`.
- `bridge-restart` asks the new bridge whether it would accept its settings
  (`bridge.py --check`) **before** stopping the running one, so a refusal costs
  a message rather than an outage.
- Everything except `/health` needs the token, compared in constant time.
  `/health` answers without it so that a probe can tell the process is alive.

What the token does not protect, and what is done about it:

- **The data on disk.** `tasks.db` holds every task and result in the clear.
  The bridge runs with a private umask, tightens an existing database file to
  mode 600, and warns, with the command, when its database or result directory
  is open to other users (it does not change a directory it did not make).
  `bridge.log` and `bridge.env` are made mode 600 by the supervisor.
- **Exhausting the bridge.** A request body over `SENTINEL_MAX_BODY_BYTES`
  (1 MiB) is refused with 413 without being read, and a connection that does
  not finish its request within `SENTINEL_REQUEST_TIMEOUT_SEC` (30 s) is
  dropped.
- **Knowing what was done.** Every request that is not a read, and every
  refusal of the token, is written to an audit table before the reply goes
  out (`GET /audit`). It stores no token, query string or request body. With one
  shared token it cannot say *who*; the `X-Sentinel-Client` label a client
  sends is only a label.

What this is not: the bridge sends text to an agent and the agent decides what
to run, so it cannot allow-list commands or directories. `slurm_policy` is a
statement of what the agent was told, not a barrier. Anyone holding the token
can do anything the agents can, so treat the token like the account's password
and rotate it (`--rotate`) if it may have been seen.

### The result file is a clean data channel off the host

Delegated tasks return their answer by writing a file under
`SENTINEL_RESULT_DIR`, which the bridge reads in full and hands back to
whoever submitted the task (see README's "结果是怎么传回来的"). Anything the
agent writes there leaves the machine intact — no truncation, no
transformation.

That is the point of the design, but it is worth stating plainly: a task
phrased as "show me the contents of X" will faithfully deliver X, including
if X is a credentials file. The delegation prompt tells the agent not to put
secrets in the result, and the agents observed in practice do push back —
one declined to dump a settings file precisely because it might contain API
keys and the result "等同于把凭据外传". Do not rely on that judgement as a
control: the operator submitting tasks is the control.

This matters more on a shared HPC host than it would locally. See
CONTRIBUTING.md's "Sensitive paths" section for the related rule about not
letting that data reach commits either.

## Reporting a Vulnerability

If you find a security issue beyond the threat model above (e.g. a way to
bypass `SENTINEL_BRIDGE_TOKEN` when it is set, an injection vector into the
`herdr`/Sentinel command line, or a way to reach the bridge from outside
`127.0.0.1`), please **do not open a public issue**. Instead, use
[GitHub's private vulnerability reporting](https://github.com/Jiaofeisiling/herdr-task-bridge/security/advisories/new)
for this repository, or contact the maintainer
([@Jiaofeisiling](https://github.com/Jiaofeisiling)) directly.

Please include:

- The affected file/endpoint and a minimal reproduction
- What you expected vs. what happened
- Any relevant `bridge.py` version/commit hash (`version` field in `/health`)

There is no bug bounty; this is a small personal-use tool shared publicly.
Reports are still very welcome and will be credited unless you ask otherwise.

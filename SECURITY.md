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

Authentication (`SENTINEL_BRIDGE_TOKEN`) is opt-in and off by default — with
it disabled, **any local process that can reach the bound port can submit
tasks to the Sentinel session**, including shell commands executed on your
behalf. This is documented, expected behavior for a single-user local tool,
not a vulnerability in itself. Treat any host where you run `bridge.py` as
trusting every local process on that host.

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

# Contributing

This project bridges a Windows client to a persistent Claude Code session
("Sentinel") running on an HPC cluster (NeSI). That means real institutional
and research data can very easily end up in a commit, a test fixture, a docs
example, or a commit message if you're not paying attention. **Read the
"Sensitive paths" section below before your first commit.**

## Sensitive paths — read this before committing

Never commit any of the following, in code, tests, docs, or commit messages:

- **Institution/project identifiers**: real HPC project codes, group names,
  usernames, cluster hostnames. Use placeholders (`your-project-code`,
  `your-username`, `hpc-login-node`) in examples instead.
- **Real Slurm job IDs, run IDs, or dataset/experiment names.** If you need
  an example, invent one (`job 123456`, `experiment-foo`) rather than
  reusing a real one from your own session history.
- **Secrets**: `SENTINEL_BRIDGE_TOKEN` values, SSH hosts, API keys, or
  anything else that belongs in an environment variable. `.gitignore`
  already excludes `.env`, `.env.*`, `*.local`, and `*secrets*` — use one of
  those filenames for any local convenience file, and never override the
  pattern with `git add -f`.
- **Real task/prompt text** delegated through the bridge. If you're adding
  an example (in `README.md`, a docstring, a test), write a generic
  stand-in ("check disk usage", "summarize the current directory") rather
  than pasting an actual task you ran, since real tasks tend to describe
  real institutional work.

Before opening a PR, run a quick audit for anything that slipped in anyway:

```bash
git diff main... | grep -iE "your-project-code|your-username|<any other identifier specific to your own environment>"
```

(Swap in the actual strings relevant to your own NeSI project/account —
there's no generic pattern that catches all of these, so this has to be a
manual check based on what you know your own environment looks like.)

If something sensitive already landed in history, don't just fix it in a new
commit — the old commit still has it. Flag it in the PR/issue instead of
silently force-pushing over shared history.

## Development setup

The system Python (a conda install) refuses `pip install` for non-admin
users. Every task in this repo uses a project-local virtualenv instead:

```bash
cd sentinel-bridge
python -m venv .venv
.venv/Scripts/python.exe -m pip install pytest   # Windows
# .venv/bin/python -m pip install pytest          # Linux/macOS
```

Run the suite before opening a PR:

```bash
cd sentinel-bridge
.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

All tests mock `run_herdr`/`get_agent_status` — none of them need a real
`herdr` binary or network access.

`sentinel.ps1` (the Windows client) has no automated tests yet; changes to
it need manual verification against a real deployed bridge before merging.
Say so explicitly in your PR description if you couldn't test against a live
bridge.

## Code style

- Follow the patterns already in `bridge.py`/`test_bridge.py`/`sentinel.ps1`
  rather than introducing a new one for the same kind of problem.
- TDD: write the failing test first for any behavior change.
- Keep `/health` free of any dependency on `herdr`, the database, or
  anything else that could make it slow or fail for a reason unrelated to
  "is the bridge process itself alive" — that's the one invariant the whole
  design leans on.
- `AGENT_LOCK` (via `acquire_agent_for_delegation()`) is the single
  chokepoint for anything that talks to Sentinel. Don't add a second lock or
  a code path that calls `run_herdr("agent", "prompt", ...)` without going
  through it.

## Where the design decisions are recorded

`docs/superpowers/plans/2026-08-30-sentinel-bridge-v2.2-v2.3.md` has the
full task-by-task history of how the current design came to be, including
several real bugs found only through live testing against the deployed
bridge (not caught by unit tests, since they mock the parts that actually
broke). Worth skimming before a large change, to avoid re-introducing
something that was already tried and reverted.

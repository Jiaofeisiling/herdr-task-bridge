# Socket-API / prompt-contract findings

Durable notes from the exploratory work in this directory, so the same
questions don't get re-investigated from scratch. Everything here was
measured against a real herdr instance (v0.8.2, protocol 20), not inferred
from the docs site — where the two disagreed, the live schema won.

## Does `agent.prompt` return the agent's reply? — No

Measured by `verify_agent_prompt_payload.py`, run cross-agent (executed in
one agent's shell, targeting a different one):

```json
{"elapsed_s": 2.0,
 "prompt_response_keys": ["agent", "type"],
 "payload_bytes": 463,
 "reply_in_prompt_payload": false,
 "reply_in_pane_read": true}
```

`agent.prompt`'s response is a ~463-byte status ack (`agent`, `type`), not a
content payload. The reply text is only reachable through `pane.read`.

**Consequence for a bridge.py rewrite:** the socket API solves *completion
detection* but not *result extraction*. Switching transports alone does not
retire the `SENTINEL_DONE_<token>` marker machinery.

## What the socket API does solve — completion detection

`agent.prompt` with `wait: {"until": ["done", "idle"], "timeout_ms": N}`
returned in 2.0s with the target correctly transitioning idle -> done. That
is a real, structured completion signal, which is exactly what
`build_recovery_prompt()` exists to work around today (it re-prompts the
agent to restate its result when the marker is missing). A `wait`-based
design makes that second model call unnecessary.

## Why terminal scraping is the wrong place to put the result

From the same run's `pane.read`:

- the response carried `"truncated": true` — terminal reads get cut off
- the surrounding text contained TUI chrome the agent never "said":
  `✻ Brewed for 1s`, `LSP / LSPs are disabled`,
  `✔ Update installed · Restart to update`, box-drawing rules,
  `⏵⏵ auto mode on (shift+tab to cycle)`

`extract_task_response()` currently locates the reply's *start* by searching
backwards for `"● "`, which is Claude Code's bullet. Agents that don't render
that (OpenCode, observed live) fall through to the "last 4000 characters"
fallback and drag all of the above into the result, plus the delegation
prompt itself, plus whatever the previous conversation turn left on screen.

A result file (`/tmp/sentinel-result-<token>.json`) has none of these
properties: no rendering, no truncation, no agent-kind-specific markup, and
no dependence on what else happens to be on screen.

## Connection model

herdr's socket closes after answering one plain RPC. Reusing a connection
for a second request gives `[Errno 32] Broken pipe`. One connection per
call.

## Request shapes that differ from the public docs

Read from `herdr api schema --json`, not the docs page:

- `agent.get` / `agent.prompt` take `{"target": "<pane_id or name>"}` —
  not `pane_id`. Both a pane id (`w1:p9`) and an agent name
  (`sentinel-opencode`) resolve.
- `agent.prompt`'s `wait.until` is an **array** of `AgentStatus`
  (`idle` | `working` | `blocked` | `done` | `unknown`), not a bare string.
- `pane.read` / `pane.close` take `{"pane_id": ...}` (`PaneTarget`).
- `tab.close` takes `{"tab_id": ...}` (`TabTarget`).

## Approaches tried and rejected

- **Prompting the agent that is executing the probe.** Self-referential: it
  stays `working` for the script's whole lifetime, so `wait` can only time
  out. Always run cross-agent.
- **`pane.split` to get a disposable test pane.** Not a headless resource —
  it visibly splits the real terminal layout the operator is looking at, and
  its `pane.close` cleanup did not reliably fire. Use `tab.create`
  (`focus: false`) if a scratch surface is ever needed again.
- **`tab.create` + `agent.start`.** The tab and its pane are created fine and
  `tab.close` cleans up properly, but `agent.start` rejected the fresh pane
  with `agent_pane_busy: "not an available shell"` — the pane's shell isn't
  ready at the moment `tab.create` returns. Unresolved; not needed once
  cross-agent testing was available.

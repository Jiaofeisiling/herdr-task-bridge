"""A stand-in for the `herdr` CLI, so the real bridge can be run without herdr.

Used by test_e2e.py, which starts bridge.py as a real process and drives it
over real HTTP. It is also a way to try the bridge on a machine that has no
herdr at all.

How it is run. The bridge calls `[HERDR_BIN, "agent", "prompt", ...]`. Point
HERDR_BIN at a Python interpreter and put this file, named `agent`, in the
bridge's working directory: `python agent prompt ...` then runs it, with the
same arguments herdr would have been given. That works unchanged on Windows,
where a script cannot be executed directly and a `.cmd` wrapper cannot carry
the multi-line prompt as an argument.

What it does is set by `config.json` in $FAKE_HERDR_DIR, read on every call so
a test can change it between steps:

    {"agents": [{"name": "alpha", "pane_id": "w1:p1", "agent": "opencode",
                 "agent_status": "idle", "cwd": "/work"}],
     "behavior": {"alpha": {"mode": "answer", "text": "done", "delay": 0}}}

Modes for `prompt`:
    answer   write `text` to the result file the prompt names (after `delay`
             seconds), then exit 0. The default.
    quota    refuse as a provider out of quota would (exit 1).
    fail     refuse with an ordinary error (exit 1).

Every prompt is appended to prompts.jsonl, so a test can say what reached an
agent and, as much to the point, what did not.
"""
import json
import os
import re
import sys
import time

STATE = os.environ.get("FAKE_HERDR_DIR", ".")


def _path(name):
    return os.path.join(STATE, name)


def _config():
    with open(_path("config.json"), encoding="utf-8") as f:
        return json.load(f)


def _find(config, identifier):
    for agent in config["agents"]:
        if identifier in (agent.get("name"), agent.get("pane_id")):
            return agent
    return None


def _busy_marker(agent):
    return _path("busy-" + agent["pane_id"].replace(":", "_"))


def _as_listed(agent):
    """The agent as herdr would list it: working while it is running a prompt."""
    listed = dict(agent)
    if os.path.exists(_busy_marker(agent)):
        listed["agent_status"] = "working"
    return listed


def _fail(code, message, status=1):
    print(json.dumps({"error": {"code": code, "message": message}}), file=sys.stderr)
    sys.exit(status)


def cmd_list(args, config):
    print(json.dumps({"result": {"agents": [_as_listed(a) for a in config["agents"]]}}))


def cmd_get(args, config):
    agent = _find(config, args[0])
    if agent is None:
        _fail("agent_not_found", f"agent target {args[0]} not found")
    print(json.dumps({"result": {"agent": _as_listed(agent)}}))


def cmd_read(args, config):
    agent = _find(config, args[0])
    if agent is None:
        _fail("agent_not_found", f"agent target {args[0]} not found")
    try:
        with open(_path("terminal-" + agent["pane_id"].replace(":", "_") + ".txt"),
                  encoding="utf-8") as f:
            print(f.read())
    except FileNotFoundError:
        pass


def cmd_prompt(args, config):
    identifier, prompt = args[0], args[1]
    agent = _find(config, identifier)
    if agent is None:
        _fail("agent_not_found", f"agent target {identifier} not found")

    with open(_path("prompts.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"agent": agent["name"], "prompt": prompt}, ensure_ascii=False) + "\n")

    behavior = config.get("behavior", {}).get(agent["name"], {})
    mode = behavior.get("mode", "answer")

    if mode == "quota":
        print("Error: Usage limit reached. Resets at 4pm.", file=sys.stderr)
        sys.exit(1)

    if mode == "fail":
        print("Error: the build broke", file=sys.stderr)
        sys.exit(1)

    marker = _busy_marker(agent)
    open(marker, "w").close()
    try:
        # Test-only gate: exercise overlap without depending on relative speeds.
        release_file = behavior.get("release_file")
        deadline = time.monotonic() + 60
        while release_file and not os.path.exists(release_file):
            if time.monotonic() >= deadline:
                raise RuntimeError("fake agent release gate timed out")
            time.sleep(0.05)
        time.sleep(float(behavior.get("delay", 0)))

        # The delegation prompt says where the result goes, on the line after
        # "写入：" -- the same line the reminder prompt uses.
        match = re.search(r"写入：\s*\n(.*result-[0-9a-f]+\.txt)", prompt)
        if match:
            with open(match.group(1).strip(), "w", encoding="utf-8") as f:
                f.write(behavior.get("text", "done"))
    finally:
        try:
            os.remove(marker)
        except OSError:
            pass

    print(json.dumps({"result": {"status": "done"}}))


COMMANDS = {"list": cmd_list, "get": cmd_get, "read": cmd_read, "prompt": cmd_prompt}


def main(argv):
    if argv and argv[0] == "agent":
        argv = argv[1:]

    if not argv or argv[0] not in COMMANDS:
        _fail("unknown_command", f"fake herdr does not know {argv[:1]}", 2)

    COMMANDS[argv[0]](argv[1:], _config())


if __name__ == "__main__":
    main(sys.argv[1:])

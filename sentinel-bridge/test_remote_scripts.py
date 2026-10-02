"""Tests for the deployment scripts in remote/, run for real under bash.

The commands they drive on the host -- screen, pkill, curl -- are replaced by
stubs that record what they were asked to do, so what matters about a restart
can be asserted: what it stopped, what it started, and what it told its caller.
"""
import os
import shutil
import stat
import subprocess
import sys

import pytest

REMOTE = os.path.join(os.path.dirname(__file__), "..", "remote")
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")


# A restart that cannot start anything new must not stop anything: the bridge
# is the only way back in. On the real host `bridge-restart` was run from a
# child shell where `_BRIDGE_ROOT` -- a plain, unexported variable -- was
# unset, so the path it launched was /remote/bridge-supervisor.sh. It had
# already killed the old bridge by then, started nothing, and still returned 0:
# the last command in the function was a status listing that cannot fail. The
# bridge was down for about two minutes, during a deploy.


def _stub(path, body):
    with open(path, "w", newline="\n") as fh:
        fh.write("#!/usr/bin/env bash\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def _host(tmp_path, supervisor=True):
    """A fake $HOME holding a checkout, plus a PATH of recording stubs."""
    home = tmp_path / "home"
    root = home / "herdr-task-bridge"
    (root / "remote").mkdir(parents=True)
    (root / "sentinel-bridge").mkdir()

    for name in ("bridge-restart.sh", "bridge-aliases.sh"):
        shutil.copy(os.path.join(REMOTE, name), root / "remote" / name)

    if supervisor:
        _stub(root / "remote" / "bridge-supervisor.sh", "exit 0\n")

    calls = tmp_path / "calls.log"
    stubs = tmp_path / "stubs"
    stubs.mkdir()

    for name in ("screen", "pkill", "pgrep"):
        _stub(stubs / name, f'echo "{name} $*" >> "{calls.as_posix()}"\nexit 0\n')

    # `curl` answers like a healthy bridge unless told otherwise.
    _stub(
        stubs / "curl",
        f'echo "curl $*" >> "{calls.as_posix()}"\n'
        'if [ "${STUB_HEALTH:-ok}" = ok ]; then echo \'{"ok": true, "version": 99}\'; exit 0; fi\n'
        "exit 7\n",
    )

    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "PATH": str(stubs) + os.pathsep + env["PATH"],
        "BRIDGE_SETTLE_SEC": "0",
        "BRIDGE_START_WAIT_SEC": "2",
    })
    env.pop("_BRIDGE_ROOT", None)

    return root, calls, env


def _run(args, env, cwd=None):
    return subprocess.run(
        [BASH, *args], env=env, cwd=cwd, capture_output=True, text=True, timeout=60
    )


def _calls(path):
    return path.read_text().splitlines() if path.exists() else []


def test_a_restart_that_cannot_start_a_new_bridge_stops_nothing(tmp_path):
    root, calls, env = _host(tmp_path, supervisor=False)

    result = _run([str(root / "remote" / "bridge-restart.sh")], env)

    assert result.returncode != 0
    assert "nothing was stopped" in result.stderr
    # No quit, no kill: taking the old bridge down is the one irreversible step.
    assert not any(c.startswith(("screen", "pkill")) for c in _calls(calls))


def test_a_restart_that_does_not_come_back_says_so_and_fails(tmp_path):
    root, calls, env = _host(tmp_path)
    env["STUB_HEALTH"] = "dead"

    result = _run([str(root / "remote" / "bridge-restart.sh")], env)

    assert result.returncode != 0
    assert "did not answer" in result.stderr


def test_a_restart_that_comes_back_succeeds_and_starts_the_right_script(tmp_path):
    root, calls, env = _host(tmp_path)

    result = _run([str(root / "remote" / "bridge-restart.sh")], env)

    assert result.returncode == 0
    log = _calls(calls)
    assert any(c.startswith("screen") and "-X quit" in c for c in log)
    assert any(c.startswith("pkill") for c in log)
    started = [c for c in log if c.startswith("screen") and "-dmS" in c]
    assert len(started) == 1
    # The supervisor inside *this* checkout, not a path built from a variable
    # that may be empty.
    assert started[0].endswith("bridge-supervisor.sh")
    assert "herdr-task-bridge" in started[0]


def test_the_shell_function_works_from_a_child_shell_without_the_variable(tmp_path):
    root, calls, env = _host(tmp_path)
    aliases = root / "remote" / "bridge-aliases.sh"

    # How an agent reaches it: the function exported into a child shell, which
    # does not inherit the unexported `_BRIDGE_ROOT` it was defined alongside.
    result = _run(
        ["-c", f'source "{aliases.as_posix()}"; export -f bridge-restart; '
               'unset _BRIDGE_ROOT; bash -c bridge-restart'],
        env,
    )

    assert result.returncode == 0, result.stderr
    assert any("-dmS" in c for c in _calls(calls))


def test_the_function_does_not_stop_the_bridge_if_it_cannot_find_the_script(tmp_path):
    root, calls, env = _host(tmp_path)
    aliases = root / "remote" / "bridge-aliases.sh"
    env["HOME"] = str(tmp_path / "nowhere")      # no checkout under $HOME at all

    result = _run(
        ["-c", f'source "{aliases.as_posix()}"; unset _BRIDGE_ROOT; bridge-restart'],
        env,
    )

    assert result.returncode != 0
    assert not any(c.startswith(("screen", "pkill")) for c in _calls(calls))

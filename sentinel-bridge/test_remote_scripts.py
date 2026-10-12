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


# -- the supervisor and the settings it reads --------------------------------
#
# bridge.env.example tells an operator to write SENTINEL_BRIDGE_TOKEN="..." in
# bridge.env. The supervisor only *sourced* that file, which sets shell
# variables without exporting them, and exported just a few by name. So the
# token never reached bridge.py, and the bridge ran with no authentication while
# its operator believed it was protected.


def _supervisor_world(tmp_path, env_lines):
    root, calls, env = _host(tmp_path)
    shutil.copy(os.path.join(REMOTE, "bridge-supervisor.sh"), root / "remote" / "bridge-supervisor.sh")
    (root / "remote" / "bridge.env").write_text("\n".join(env_lines) + "\n", newline="\n")

    seen = tmp_path / "seen.txt"
    stubs = tmp_path / "stubs"
    # What bridge.py would have been given, written down instead of served.
    _stub(
        stubs / "python3",
        f'echo "token=${{SENTINEL_BRIDGE_TOKEN-UNSET}} failover=${{SENTINEL_QUOTA_FAILOVER_AGENTS-UNSET}}" '
        f'>> "{seen.as_posix()}"\nexit 0\n',
    )
    # The supervisor restarts bridge.py forever; end it after the first pass.
    _stub(stubs / "sleep", "kill -9 $PPID\n")

    return root, env, seen


def test_a_token_in_bridge_env_reaches_bridge_py_without_an_export(tmp_path):
    root, env, seen = _supervisor_world(tmp_path, [
        'SENTINEL_BRIDGE_TOKEN="0123456789abcdef0123456789abcdef"',
        'SENTINEL_QUOTA_FAILOVER_AGENTS="a,b"',
    ])

    _run([str(root / "remote" / "bridge-supervisor.sh")], env)

    assert seen.read_text().strip() == (
        "token=0123456789abcdef0123456789abcdef failover=a,b"
    )


def test_the_supervisor_runs_with_nothing_set_when_there_is_no_env_file(tmp_path):
    root, env, seen = _supervisor_world(tmp_path, [])
    (root / "remote" / "bridge.env").unlink()

    _run([str(root / "remote" / "bridge-supervisor.sh")], env)

    assert seen.read_text().strip() == "token=UNSET failover=UNSET"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX permission bits")
def test_the_log_and_the_env_file_are_private_to_the_user(tmp_path):
    root, env, seen = _supervisor_world(tmp_path, ['SENTINEL_BRIDGE_TOKEN="0123456789abcdef0123456789abcdef"'])
    log = root / "sentinel-bridge" / "bridge.log"
    log.write_text("an older line\n")
    os.chmod(log, 0o644)
    os.chmod(root / "remote" / "bridge.env", 0o644)

    _run([str(root / "remote" / "bridge-supervisor.sh")], env)

    assert os.stat(log).st_mode & 0o077 == 0
    assert os.stat(root / "remote" / "bridge.env").st_mode & 0o077 == 0


# -- a refusal to start must not cost the running bridge -------------------------


def _check_stub(tmp_path):
    """python3 as the supervisor and the restart script will call it.

    `bridge.py --check` exits with $STUB_CHECK_EXIT; anything else is a normal
    run, which writes down what it was given and exits with $STUB_RUN_EXIT.
    """
    stubs = tmp_path / "stubs"
    seen = tmp_path / "seen.txt"
    checked = tmp_path / "checked.txt"
    _stub(
        stubs / "python3",
        'if [ "${2:-}" = "--check" ]; then\n'
        f'  echo "check token=${{SENTINEL_BRIDGE_TOKEN-UNSET}}" >> "{checked.as_posix()}"\n'
        '  exit "${STUB_CHECK_EXIT:-0}"\n'
        "fi\n"
        f'echo "token=${{SENTINEL_BRIDGE_TOKEN-UNSET}} failover=${{SENTINEL_QUOTA_FAILOVER_AGENTS-UNSET}}" >> "{seen.as_posix()}"\n'
        'exit "${STUB_RUN_EXIT:-0}"\n',
    )
    return seen, checked


def test_the_supervisor_can_check_its_settings_without_starting_anything(tmp_path):
    root, env, _ = _supervisor_world(tmp_path, ['SENTINEL_BRIDGE_TOKEN="0123456789abcdef0123456789abcdef"'])
    seen, checked = _check_stub(tmp_path)

    result = _run([str(root / "remote" / "bridge-supervisor.sh"), "--check"], env)

    assert result.returncode == 0
    # It looked at the settings bridge.env gives, exported, as a real start would.
    assert checked.read_text().strip() == "check token=0123456789abcdef0123456789abcdef"
    assert not seen.exists()                     # and started nothing


def test_a_failed_check_is_reported_with_its_status(tmp_path):
    root, env, _ = _supervisor_world(tmp_path, [])
    _check_stub(tmp_path)
    env["STUB_CHECK_EXIT"] = "2"

    assert _run([str(root / "remote" / "bridge-supervisor.sh"), "--check"], env).returncode == 2


def test_the_supervisor_stops_instead_of_retrying_a_refusal(tmp_path):
    # Exit 2 means the settings were refused. Restarting every five seconds
    # cannot change that, only fill the log.
    root, env, seen = _supervisor_world(tmp_path, [])
    _check_stub(tmp_path)
    env["STUB_RUN_EXIT"] = "2"

    result = _run([str(root / "remote" / "bridge-supervisor.sh")], env)

    # `sleep` is the stub that kills the supervisor: reaching it means it looped.
    assert result.returncode == 2
    log = (root / "sentinel-bridge" / "bridge.log").read_text()
    assert "not restarting" in log


def test_a_restart_whose_settings_would_be_refused_stops_nothing(tmp_path):
    root, calls, env = _host(tmp_path)
    shutil.copy(os.path.join(REMOTE, "bridge-supervisor.sh"), root / "remote" / "bridge-supervisor.sh")
    _check_stub(tmp_path)
    env["STUB_CHECK_EXIT"] = "2"

    result = _run([str(root / "remote" / "bridge-restart.sh")], env)

    assert result.returncode != 0
    assert "nothing was stopped" in result.stderr
    assert not any(c.startswith(("screen", "pkill")) for c in _calls(calls))


def test_a_restart_whose_settings_are_fine_goes_ahead(tmp_path):
    root, calls, env = _host(tmp_path)
    shutil.copy(os.path.join(REMOTE, "bridge-supervisor.sh"), root / "remote" / "bridge-supervisor.sh")
    _check_stub(tmp_path)

    result = _run([str(root / "remote" / "bridge-restart.sh")], env)

    assert result.returncode == 0, result.stderr
    assert any("-dmS" in c for c in _calls(calls))


# -- making an access token --------------------------------------------------------
#
# The token is the only thing standing between every user of a shared login node
# and the agents. Making one should not mean remembering a command, and it
# should not be a secret that passed through anyone else's hands: the script runs
# in the operator's own terminal and shows it once.

import re


def _token_world(tmp_path, existing=None):
    root, calls, env = _host(tmp_path)
    shutil.copy(os.path.join(REMOTE, "bridge-set-token.sh"), root / "remote" / "bridge-set-token.sh")
    env_file = root / "remote" / "bridge.env"
    if existing is not None:
        env_file.write_text(existing, newline="\n")
    return root, env, env_file


def _token_lines(env_file):
    return [ln for ln in env_file.read_text().splitlines() if ln.startswith("SENTINEL_BRIDGE_TOKEN=")]


def test_set_token_makes_a_long_random_token_and_shows_it_once(tmp_path):
    root, env, env_file = _token_world(tmp_path)

    result = _run([str(root / "remote" / "bridge-set-token.sh")], env)

    assert result.returncode == 0, result.stderr
    [line] = _token_lines(env_file)
    token = line.split("=", 1)[1].strip('"')
    assert re.fullmatch(r"[0-9a-f]{64}", token)
    assert result.stdout.strip() == token         # shown exactly once, on stdout
    assert token not in result.stderr


def test_two_tokens_are_never_the_same(tmp_path):
    root, env, env_file = _token_world(tmp_path)
    first = _run([str(root / "remote" / "bridge-set-token.sh")], env).stdout.strip()
    env_file.unlink()

    second = _run([str(root / "remote" / "bridge-set-token.sh")], env).stdout.strip()

    assert first != second


def test_set_token_keeps_the_rest_of_the_file(tmp_path):
    root, env, env_file = _token_world(tmp_path, 'SENTINEL_AGENT_PRIORITY="claude,opencode"\n# a note\n')

    _run([str(root / "remote" / "bridge-set-token.sh")], env)

    text = env_file.read_text()
    assert 'SENTINEL_AGENT_PRIORITY="claude,opencode"' in text
    assert "# a note" in text
    assert len(_token_lines(env_file)) == 1


def test_set_token_will_not_replace_a_token_that_is_in_use(tmp_path):
    original = 'SENTINEL_BRIDGE_TOKEN="' + "a" * 40 + '"\n'
    root, env, env_file = _token_world(tmp_path, original)

    result = _run([str(root / "remote" / "bridge-set-token.sh")], env)

    # Replacing it locks out every client until each is updated.
    assert result.returncode != 0
    assert "--rotate" in result.stderr
    assert env_file.read_text() == original


def test_rotating_replaces_the_token_and_leaves_one(tmp_path):
    root, env, env_file = _token_world(
        tmp_path, 'SENTINEL_BRIDGE_TOKEN="' + "a" * 40 + '"\nSENTINEL_AGENT_PRIORITY="claude"\n'
    )

    result = _run([str(root / "remote" / "bridge-set-token.sh"), "--rotate"], env)

    assert result.returncode == 0, result.stderr
    [line] = _token_lines(env_file)
    assert "a" * 40 not in env_file.read_text()
    assert result.stdout.strip() in line
    assert 'SENTINEL_AGENT_PRIORITY="claude"' in env_file.read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX permission bits")
def test_the_file_that_holds_the_token_is_private(tmp_path):
    root, env, env_file = _token_world(tmp_path)

    _run([str(root / "remote" / "bridge-set-token.sh")], env)

    assert os.stat(env_file).st_mode & 0o077 == 0

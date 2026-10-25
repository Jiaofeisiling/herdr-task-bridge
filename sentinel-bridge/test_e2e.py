"""End-to-end tests: the real bridge process, real HTTP, a real SQLite file.

Everything in test_bridge.py runs inside one process with the herdr call
replaced. These tests start bridge.py as a process of its own, in front of the
fake herdr in fake_herdr.py, so what is exercised is what actually happens
around the code: a process killed without warning, a restart onto the same
database, requests arriving at the same moment from several threads.

Each scenario states what the claim is and what would have to be true for it
to be broken.
"""
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, "bridge.py")
FAKE_HERDR = os.path.join(HERE, "fake_herdr.py")

# Worker polls every two seconds, so anything that has to be picked up takes
# at least that long; the ceiling is for a loaded CI machine, not the usual.
PATIENCE = 40


def wait_for(predicate, timeout=PATIENCE, interval=0.1, what="the condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Bridge:
    """One running bridge.py process."""

    def __init__(self, process, port, log_path, token=None):
        self.process = process
        self.port = port
        self.log_path = log_path
        self.token = token

    def request(self, method, path, body=None, timeout=15, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            headers = {"Content-Type": "application/json"}
            if self.token and token:
                headers["X-Sentinel-Token"] = self.token
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, {} if body is None else body, **kw)

    def task(self, task_id):
        return self.get(f"/tasks/{task_id}")[1]["task"]

    def wait_status(self, task_id, *statuses):
        return wait_for(
            lambda: (t := self.task(task_id))["status"] in statuses and t,
            what=f"task {task_id} to reach {statuses}",
        )

    def kill(self):
        """Die without warning: no signal handler, no shutdown, nothing flushed."""
        self.process.kill()
        self.process.wait(timeout=10)

    def log(self):
        with open(self.log_path, encoding="utf-8", errors="replace") as f:
            return f.read()


class World:
    """A directory holding the fake herdr's state, the database and the results."""

    def __init__(self, root):
        self.root = root
        self.fake = root / "fake"
        self.results = root / "results"
        self.db = root / "tasks.db"
        self.bridges = []

        self.fake.mkdir()
        self.results.mkdir()
        # `python agent ...` runs this file; see fake_herdr.py for why.
        shutil.copy(FAKE_HERDR, self.fake / "agent")

    def configure(self, agents, behavior=None):
        listed = [
            {"name": a["name"], "pane_id": a.get("pane_id", f"w1:p{i + 1}"),
             "agent": a.get("agent", "opencode"),
             "agent_status": a.get("agent_status", "idle"), "cwd": str(self.root)}
            for i, a in enumerate(agents)
        ]
        with open(self.fake / "config.json", "w", encoding="utf-8") as f:
            json.dump({"agents": listed, "behavior": behavior or {}}, f)

    def prompts(self):
        try:
            with open(self.fake / "prompts.jsonl", encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def start_bridge(self, **env):
        port = _free_port()
        # What the developer's own shell exports must not leak into the bridge
        # under test; what the test asks for is applied on top, afterwards.
        inherited_env = {
            k: v for k, v in os.environ.items()
            if k not in ("SENTINEL_AGENT", "SENTINEL_BRIDGE_TOKEN",
                         "SENTINEL_QUOTA_FAILOVER_AGENTS", "SENTINEL_AGENT_PRIORITY")
        }
        environment = {
            **inherited_env,
            "HERDR_BIN": sys.executable,
            "FAKE_HERDR_DIR": str(self.fake),
            "SENTINEL_BRIDGE_PORT": str(port),
            "SENTINEL_DB": str(self.db),
            "SENTINEL_RESULT_DIR": str(self.results),
            "PYTHONUTF8": "1",
            # Most scenarios are not about authentication, and the bridge will
            # not start open unless told to; the ones that are pass a token.
            "SENTINEL_ALLOW_NO_AUTH": "1",
            **{k: str(v) for k, v in env.items()},
        }
        log_path = self.root / f"bridge-{len(self.bridges)}.log"
        # A file, not a pipe: nobody reads the bridge's output while a test
        # runs, and a full pipe would block it.
        log = open(log_path, "w", encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, BRIDGE], cwd=self.fake, env=environment,
            stdout=log, stderr=subprocess.STDOUT,
        )
        log.close()

        bridge = Bridge(process, port, log_path, token=env.get("SENTINEL_BRIDGE_TOKEN"))
        self.bridges.append(bridge)

        def healthy():
            if process.poll() is not None:
                raise AssertionError("the bridge exited at startup:\n" + bridge.log())
            try:
                return bridge.get("/health", timeout=2)[0] == 200
            except OSError:
                return False

        wait_for(healthy, what="the bridge to answer /health")
        return bridge

    def close(self):
        for bridge in self.bridges:
            if bridge.process.poll() is None:
                bridge.process.kill()
                bridge.process.wait(timeout=10)


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


ALPHA = {"name": "alpha", "agent": "opencode"}
BETA = {"name": "beta", "agent": "claude"}


def delegate(bridge, task, **extra):
    status, body = bridge.post("/delegate", {"task": task, **extra})
    assert status == 202, body
    return body["task_id"]


# -- the happy path, through every layer -----------------------------------


def test_a_task_runs_through_the_real_process_and_its_structured_result_is_served(world):
    result = {"status": "success", "summary": "disk is fine", "job_ids": ["4242"]}
    world.configure([ALPHA], {"alpha": {"text": json.dumps(result)}})
    bridge = world.start_bridge()

    task_id = delegate(bridge, "check the disk")
    task = bridge.wait_status(task_id, "done", "error")

    assert task["status"] == "done"
    assert task["result_format"] == "json"
    assert task["result_data"]["job_ids"] == ["4242"]
    assert len(world.prompts()) == 1


# -- a process killed without warning --------------------------------------


def test_real_worker_pool_finishes_another_agent_while_first_is_still_running(world):
    release = world.root / "release-alpha"
    world.configure([ALPHA, BETA], {
        "alpha": {"release_file": str(release), "text": "alpha complete"},
        "beta": {"text": "beta complete"},
    })
    bridge = world.start_bridge(SENTINEL_WORKER_CONCURRENCY=2)
    first = delegate(bridge, "first alpha", agent="alpha")
    try:
        wait_for(lambda: any(p["agent"] == "alpha" for p in world.prompts()))
        second = delegate(bridge, "second alpha alias", agent="w1:p1")
        independent = delegate(bridge, "independent beta", agent="beta")
        assert bridge.wait_status(independent, "done", "error")["status"] == "done"
        assert bridge.task(first)["status"] == "running"
        assert bridge.task(second)["status"] == "queued"
        assert sorted(p["agent"] for p in world.prompts()) == ["alpha", "beta"]
    finally:
        release.write_text("released", encoding="utf-8")
    assert bridge.wait_status(first, "done", "error")["status"] == "done"
    assert bridge.wait_status(second, "done", "error")["status"] == "done"
    assert sorted(p["agent"] for p in world.prompts()) == ["alpha", "alpha", "beta"]


def test_a_killed_bridge_orphans_its_running_task_and_never_reruns_it(world):
    # The claim: when the bridge dies mid-task the work may or may not have
    # happened, so it must not be run again. It would be broken if the task
    # came back as queued, or if the agent were prompted a second time.
    world.configure([ALPHA], {"alpha": {"delay": 4, "text": "the answer"}})
    first = world.start_bridge()
    task_id = delegate(first, "a long job")
    first.wait_status(task_id, "running")
    # `running` only says the worker has claimed the task. Kill it before the
    # prompt has left and nothing was ever sent -- a different case, and one
    # with no agent to give an answer back.
    wait_for(world.prompts, what="the prompt to reach the agent")

    first.kill()
    second = world.start_bridge()

    task = second.task(task_id)
    assert task["status"] == "orphaned"
    assert "restarted" in task["error_text"]

    # Give the worker several polls in which it could, wrongly, rerun it.
    time.sleep(5)
    assert second.task(task_id)["status"] in ("orphaned", "done")
    assert len(world.prompts()) == 1


def test_the_answer_of_a_killed_bridges_agent_is_recovered_after_the_restart(world):
    # The claim: an agent that finishes after the bridge lost track of it does
    # not lose its answer. Broken if the task stayed orphaned with no result.
    world.configure([ALPHA], {"alpha": {"delay": 3, "text": "the late answer"}})
    first = world.start_bridge()
    task_id = delegate(first, "a long job")
    first.wait_status(task_id, "running")
    wait_for(world.prompts, what="the prompt to reach the agent")

    first.kill()
    second = world.start_bridge()
    assert second.task(task_id)["status"] == "orphaned"

    task = wait_for(
        lambda: (t := second.task(task_id))["status"] == "done" and t,
        what="the late result to be adopted",
    )

    assert task["result_text"] == "the late answer"
    assert task["recovered_at"] is not None
    # What went wrong is kept, not rewritten.
    assert "restarted" in task["error_text"]
    assert len(world.prompts()) == 1


def test_p1_second_process_cannot_orphan_a_live_instances_task(world):
    world.configure([ALPHA], {"alpha": {"delay": 3, "text": "the answer"}})
    first = world.start_bridge()
    task_id = delegate(first, "a bounded task")
    first.wait_status(task_id, "running")
    wait_for(world.prompts, what="the first instance to dispatch")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("SENTINEL_")}
    environment.update({
        "HERDR_BIN": sys.executable, "FAKE_HERDR_DIR": str(world.fake),
        "SENTINEL_BRIDGE_PORT": str(_free_port()), "SENTINEL_DB": str(world.db),
        "SENTINEL_RESULT_DIR": str(world.results), "SENTINEL_ALLOW_NO_AUTH": "1",
        "PYTHONUTF8": "1",
    })
    second = subprocess.Popen(
        [sys.executable, BRIDGE], cwd=world.fake, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        output, _ = second.communicate(timeout=2)
        assert second.returncode != 0, output
        assert b"already" in output.lower(), output
        assert first.task(task_id)["status"] == "running"
    finally:
        if second.poll() is None:
            second.kill()
            second.communicate(timeout=5)
        first.wait_status(task_id, "done")
    assert len(world.prompts()) == 1


# -- the same request, many times at once -----------------------------------


def test_concurrent_retries_with_one_idempotency_key_make_exactly_one_task(world):
    # The claim: a retry storm cannot queue the work twice. Broken if two
    # threads both found the key unclaimed, and so submitted two jobs.
    world.configure([ALPHA], {"alpha": {"text": "ok"}})
    bridge = world.start_bridge()
    barrier = threading.Barrier(12)
    replies = []

    def retry():
        barrier.wait()
        replies.append(bridge.post("/delegate", {"task": "submit the job", "idempotency_key": "job-1"}))

    threads = [threading.Thread(target=retry) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert {status for status, _ in replies} == {202}
    assert len({body["task_id"] for _, body in replies}) == 1
    assert sum(1 for _, body in replies if body.get("replayed")) == 11

    bridge.wait_status(replies[0][1]["task_id"], "done")
    assert len(world.prompts()) == 1
    assert len(bridge.get("/tasks?limit=200")[1]["tasks"]) == 1


def test_the_queue_limit_holds_when_requests_arrive_together(world):
    # The claim: the cap cannot be overfilled by requests racing the count.
    # The agent is held busy so nothing leaves the queue while it is filled.
    world.configure([{**ALPHA, "agent_status": "working"}])
    bridge = world.start_bridge(SENTINEL_MAX_QUEUE_DEPTH=5)
    barrier = threading.Barrier(20)
    statuses = []

    def submit(i):
        barrier.wait()
        statuses.append(bridge.post("/delegate", {"task": f"job {i}", "agent": "alpha"})[0])

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(statuses) == [202] * 5 + [429] * 15
    assert len(bridge.get("/tasks?status=queued&limit=200")[1]["tasks"]) == 5
    assert world.prompts() == []


# -- a provider that runs out ------------------------------------------------


def test_a_quota_failure_moves_the_task_to_another_provider_and_says_so(world):
    # The claim: running out of quota on one provider does not strand the task.
    # Broken if it ended quota_exhausted although beta was free and idle.
    world.configure(
        [ALPHA, BETA],
        {"alpha": {"mode": "quota"}, "beta": {"text": "answered by beta"}},
    )
    bridge = world.start_bridge()

    task_id = delegate(bridge, "audit", agent="alpha")
    task = bridge.wait_status(task_id, "done", "error", "quota_exhausted")

    assert task["status"] == "done"
    assert task["result_text"] == "answered by beta"
    assert "beta" in task["error_text"] and "alpha" in task["error_text"]
    assert [p["agent"] for p in world.prompts()] == ["alpha", "beta"]
    assert "alpha" in json.dumps(bridge.get("/quota")[1])


# -- workflows ----------------------------------------------------------------


def test_a_chain_runs_in_dependency_order(world):
    world.configure([ALPHA], {"alpha": {"text": "ok"}})
    bridge = world.start_bridge()

    a = delegate(bridge, "step one", workflow_id="run-1")
    b = delegate(bridge, "step two", workflow_id="run-1", depends_on=[a])
    c = delegate(bridge, "step three", workflow_id="run-1", depends_on=[b])

    bridge.wait_status(c, "done", "error", "cancelled")

    assert [("step one" in p["prompt"], "step two" in p["prompt"], "step three" in p["prompt"])
            for p in world.prompts()] == [(True, False, False), (False, True, False), (False, False, True)]
    assert bridge.get("/workflows/run-1")[1]["state"] == "done"


def test_a_failure_cancels_what_follows_and_nothing_downstream_reaches_an_agent(world):
    # The claim: work that depended on a failed step is never sent. Broken if
    # any prompt after the first reached the agent.
    world.configure([ALPHA], {"alpha": {"mode": "fail"}})
    bridge = world.start_bridge()

    a = delegate(bridge, "step one", workflow_id="run-1")
    b = delegate(bridge, "step two", workflow_id="run-1", depends_on=[a])
    c = delegate(bridge, "step three", workflow_id="run-1", depends_on=[b])

    bridge.wait_status(c, "cancelled")

    assert bridge.task(a)["status"] == "error"
    assert bridge.task(b)["status"] == "cancelled"
    assert "never sent" in bridge.task(c)["error_text"]
    assert len(world.prompts()) == 1
    assert bridge.get("/workflows/run-1")[1]["state"] == "failed"


# -- the event stream ---------------------------------------------------------


def test_the_event_stream_records_a_tasks_whole_life_over_http(world):
    world.configure([ALPHA], {"alpha": {"text": "ok"}})
    bridge = world.start_bridge()

    task_id = delegate(bridge, "audit")
    bridge.wait_status(task_id, "done")

    events = bridge.get(f"/events?task_id={task_id}")[1]["events"]

    assert [(e["from_status"], e["to_status"]) for e in events] == [
        (None, "queued"), ("queued", "running"), ("running", "done"),
    ]


# -- the acceptance script itself ---------------------------------------------
#
# scripts/acceptance.py is for running against a real bridge and real agents.
# It is only worth carrying out there if it is known to work, so it is run here
# against the real bridge and the fake herdr.

ACCEPTANCE = os.path.join(HERE, "..", "scripts", "acceptance.py")


def _isolated_env(world, **extra):
    """The developer's own environment, minus any token they keep for real use.

    The scripts under test look in $SENTINEL_BRIDGE_TOKEN and then in a stored
    file; a test that expects none must not find the one on this machine.
    """
    empty = world.root / "no-config"
    empty.mkdir(exist_ok=True)
    environment = {k: v for k, v in os.environ.items() if k != "SENTINEL_BRIDGE_TOKEN"}
    environment["SENTINEL_CONFIG_DIR"] = str(empty)
    environment.update(extra)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def test_script_subprocess_output_is_utf8_even_with_a_legacy_parent_encoding(world, monkeypatch):
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setenv("PYTHONIOENCODING", "cp936")
    done = subprocess.run(
        [sys.executable, "-c", "print(chr(0x4e2d))"], env=_isolated_env(world),
        capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    assert done.returncode == 0 and done.stdout.strip() == "中"


def _run_acceptance(world, bridge, *extra):
    report = world.root / "report.md"
    done = subprocess.run(
        [sys.executable, ACCEPTANCE, "--url", f"http://127.0.0.1:{bridge.port}",
         "--yes", "--report", str(report), *extra],
        capture_output=True, text=True, encoding="utf-8", timeout=240,
        env=_isolated_env(world),
    )
    return done, report


def test_the_acceptance_script_passes_against_a_working_bridge(world):
    world.configure(
        [{**ALPHA, "name": "secret-agent-name"}],
        {"secret-agent-name": {"text": json.dumps({"status": "success", "summary": "OK"})}},
    )
    bridge = world.start_bridge()

    done, report = _run_acceptance(world, bridge)

    assert done.returncode == 0, done.stdout + done.stderr
    text = report.read_text(encoding="utf-8")
    assert "FAIL" not in text
    for claim in ("structured result", "dependency", "never sent", "event", "idempotency"):
        assert claim in text.lower(), claim

    # A report is meant to be shared: it names what kind of agent ran, never
    # which agent, and nothing about the host.
    assert "secret-agent-name" not in text
    assert "w1:p" not in text
    assert str(world.root) not in text


def test_the_acceptance_script_sends_nothing_without_being_told_to(world):
    world.configure([ALPHA], {"alpha": {"text": "ok"}})
    bridge = world.start_bridge()

    done = subprocess.run(
        [sys.executable, ACCEPTANCE, "--url", f"http://127.0.0.1:{bridge.port}"],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        env=_isolated_env(world),
    )

    assert done.returncode == 2
    assert "--yes" in done.stdout
    assert world.prompts() == []
    assert bridge.get("/tasks")[1]["tasks"] == []


def test_the_acceptance_script_refuses_to_run_when_the_channel_is_down(world):
    done = subprocess.run(
        [sys.executable, ACCEPTANCE, "--url", f"http://127.0.0.1:{_free_port()}", "--yes"],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        env=_isolated_env(world),
    )

    assert done.returncode == 2
    assert "CHANNEL DOWN" in done.stdout


# -- authentication, across a real process ---------------------------------------


def test_a_bridge_with_a_token_answers_only_those_who_hold_it(world):
    world.configure([ALPHA], {"alpha": {"text": "ok"}})
    bridge = world.start_bridge(SENTINEL_BRIDGE_TOKEN="k" * 32)

    # /health is open on purpose, so a probe can tell the bridge is alive.
    assert bridge.get("/health", token=False)[0] == 200
    assert bridge.get("/tasks", token=False)[0] == 401
    assert bridge.post("/delegate", {"task": "x"}, token=False)[0] == 401
    assert bridge.get("/events", token=False)[0] == 401

    task_id = delegate(bridge, "audit")             # the token is sent for it
    assert bridge.wait_status(task_id, "done")["status"] == "done"

    # Nothing from the unauthenticated attempts reached an agent.
    assert len(world.prompts()) == 1


def test_the_acceptance_script_checks_authentication_when_given_a_token(world):
    world.configure([ALPHA], {"alpha": {"text": json.dumps({"summary": "OK"})}})
    token = "z" * 32
    bridge = world.start_bridge(SENTINEL_BRIDGE_TOKEN=token)

    done, report = _run_acceptance(world, bridge, "--token", token)

    assert done.returncode == 0, done.stdout + done.stderr
    text = report.read_text(encoding="utf-8")
    assert "auth: the event stream refuses a caller without the token" in text
    assert "FAIL" not in text
    assert token not in text


# -- the token should not have to be typed on a command line --------------------
#
# On a shared login node a command line is visible to every other user in the
# process list for as long as it runs, and it stays in the shell's history. The
# bridge's own token handling was made careful about that; the scripts that talk
# to it took the token only as an argument.


def _run_with_env(world, bridge, extra_env, *args):
    report = world.root / "report.md"
    environment = _isolated_env(world, **extra_env)
    done = subprocess.run(
        [sys.executable, ACCEPTANCE, "--url", f"http://127.0.0.1:{bridge.port}",
         "--yes", "--report", str(report), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=240, env=environment,
    )
    return done, report


def test_the_acceptance_script_reads_the_token_from_the_environment(world):
    world.configure([ALPHA], {"alpha": {"text": json.dumps({"summary": "OK"})}})
    token = "e" * 32
    bridge = world.start_bridge(SENTINEL_BRIDGE_TOKEN=token)

    done, report = _run_with_env(world, bridge, {"SENTINEL_BRIDGE_TOKEN": token})

    assert done.returncode == 0, done.stdout + done.stderr
    assert "auth: the event stream refuses a caller without the token" in report.read_text(encoding="utf-8")
    assert token not in done.stdout + done.stderr + report.read_text(encoding="utf-8")


def test_a_token_given_on_the_command_line_still_wins(world):
    world.configure([ALPHA], {"alpha": {"text": json.dumps({"summary": "OK"})}})
    token = "f" * 32
    bridge = world.start_bridge(SENTINEL_BRIDGE_TOKEN=token)

    done, _ = _run_with_env(world, bridge, {"SENTINEL_BRIDGE_TOKEN": "wrong-" * 6}, "--token", token)

    assert done.returncode == 0, done.stdout + done.stderr


def test_the_acceptance_script_says_what_it_is_waiting_for(world):
    # Real agents take a while to answer a probe, and the script used to print
    # nothing during that time. It looked hung, and was interrupted.
    world.configure([ALPHA], {"alpha": {"delay": 3, "text": json.dumps({"summary": "OK"})}})
    bridge = world.start_bridge()

    done, _ = _run_acceptance(world, bridge)

    assert done.returncode == 0, done.stdout + done.stderr
    assert "waiting for" in done.stdout
    # What it is waiting for, in words; never an id, which is not for sharing.
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}", done.stdout)


def test_the_smoke_test_reads_the_token_from_the_environment(world):
    world.configure([ALPHA], {"alpha": {"text": "ok"}})
    token = "s" * 32
    bridge = world.start_bridge(SENTINEL_BRIDGE_TOKEN=token)
    smoke = os.path.join(HERE, "..", "scripts", "smoke.py")
    environment = _isolated_env(world, SENTINEL_BRIDGE_TOKEN=token)

    with_token = subprocess.run(
        [sys.executable, smoke, "--url", f"http://127.0.0.1:{bridge.port}"],
        capture_output=True, text=True, encoding="utf-8", timeout=120, env=environment)
    environment.pop("SENTINEL_BRIDGE_TOKEN")
    without = subprocess.run(
        [sys.executable, smoke, "--url", f"http://127.0.0.1:{bridge.port}"],
        capture_output=True, text=True, encoding="utf-8", timeout=120, env=environment)

    assert with_token.returncode == 0, with_token.stdout + with_token.stderr
    # And it is the token that made the difference.
    assert without.returncode != 0


def test_the_acceptance_script_finds_a_token_stored_by_login(world, tmp_path):
    # `sentinel.ps1 login` keeps the token in a file; the scripts read the same
    # one, so it is stored once and not typed again for each tool.
    world.configure([ALPHA], {"alpha": {"text": json.dumps({"summary": "OK"})}})
    token = "l" * 32
    bridge = world.start_bridge(SENTINEL_BRIDGE_TOKEN=token)
    config = tmp_path / "config"
    config.mkdir()
    (config / "token").write_text(token + "\r\n")

    done, report = _run_with_env(world, bridge, {"SENTINEL_CONFIG_DIR": str(config)})

    assert done.returncode == 0, done.stdout + done.stderr
    assert "auth: the event stream refuses a caller without the token" in report.read_text(encoding="utf-8")
    assert token not in done.stdout + done.stderr


def test_the_environment_variable_beats_the_stored_file_for_the_scripts_too(world, tmp_path):
    world.configure([ALPHA], {"alpha": {"text": json.dumps({"summary": "OK"})}})
    token = "m" * 32
    bridge = world.start_bridge(SENTINEL_BRIDGE_TOKEN=token)
    config = tmp_path / "config"
    config.mkdir()
    (config / "token").write_text("a-stale-token-" + "x" * 20)

    done, _ = _run_with_env(
        world, bridge, {"SENTINEL_CONFIG_DIR": str(config), "SENTINEL_BRIDGE_TOKEN": token}
    )

    assert done.returncode == 0, done.stdout + done.stderr

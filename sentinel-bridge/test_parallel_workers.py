"""Bounded task concurrency; only temporary SQLite and simulated agents."""
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

import bridge


AGENTS = [
    {"name": f"agent-{index}", "pane_id": f"window:p{index}",
     "agent": "opencode", "agent_status": "idle"}
    for index in range(8)
]


def eventually(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "condition did not become true before the deadline"


@pytest.fixture(autouse=True)
def isolated_bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "_agent_locks", {})
    monkeypatch.setattr(bridge, "_queued_agent_locks", {})
    monkeypatch.setattr(bridge, "_pending_sync_completions", {})
    monkeypatch.setattr(bridge, "_pending_sync_failures", {})
    monkeypatch.setattr(bridge, "_shutting_down", False)
    monkeypatch.setattr(bridge, "_worker_thread", None)
    monkeypatch.setattr(bridge, "_worker_stop", None)
    monkeypatch.setattr(bridge, "_worker_drain_deadline", None)
    monkeypatch.setattr(bridge, "_worker_last_prune", {})
    monkeypatch.setattr(bridge, "_inflight", 0)
    monkeypatch.setattr(bridge, "_work_available", threading.Event())
    monkeypatch.setattr(bridge, "WORKER_POLL_SECONDS", 0.01)
    monkeypatch.setattr(bridge, "TASK_RETENTION_DAYS", 0)
    monkeypatch.setattr(bridge, "list_agents", lambda: (AGENTS, {"ok": True}))
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a: ("idle", {"ok": True}))
    # Any accidentally unmocked remote access is a test failure.
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: pytest.fail("live Herdr access"))
    bridge.init_db()


@pytest.fixture
def start_pool(monkeypatch):
    pools, releases = [], []

    def start(max_workers=2, release=None):
        stop = threading.Event()
        pool = bridge.TaskWorkerPool(stop, max_workers=max_workers)
        pools.append((pool, stop))
        if release is not None:
            releases.append(release)
        monkeypatch.setattr(bridge, "_worker_thread", pool)
        monkeypatch.setattr(bridge, "_worker_stop", stop)
        pool.start()
        return pool

    yield start
    for release in releases:
        release.set()
    for _, stop in pools:
        stop.set()
    bridge._work_available.set()
    for pool, _ in pools:
        pool.join(timeout=5)
        assert not pool.is_alive(), "test left a task pool running"
        assert not any(worker.is_alive() for worker in pool.workers)


def task(agent, text="work"):
    return bridge.create_task(text, 10000, agent)


def test_distinct_agents_execute_while_the_first_agent_is_waiting(monkeypatch, start_pool):
    ids = [task("agent-0"), task("agent-1")]
    started = {ident: threading.Event() for ident in ids}
    release = threading.Event()

    def execute(**kwargs):
        ident = kwargs["task_id"]
        assert bridge.get_task(ident)["status"] == "running"
        started[ident].set()
        assert release.wait(5)
        return "answer " + ident

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    pool = start_pool(release=release)
    assert all(event.wait(3) for event in started.values()), "second agent was serialized"
    assert pool.is_alive() and pool.healthy()
    release.set()
    eventually(lambda: all(bridge.get_task(ident)["status"] == "done" for ident in ids))


@pytest.mark.parametrize("second_agent", ["agent-0", "window:p0"])
def test_same_agent_and_its_alias_remain_serial(monkeypatch, start_pool, second_agent):
    first = task("agent-0", "first")
    second = task(second_agent, "second")
    healthy = task("agent-1", "healthy")
    first_started, second_started, healthy_started = [threading.Event() for _ in range(3)]
    release = threading.Event()
    executed = []

    def execute(**kwargs):
        ident = kwargs["task_id"]
        executed.append(ident)
        if ident == first:
            first_started.set()
            assert release.wait(5)
        elif ident == second:
            second_started.set()
            assert release.is_set(), "same physical agent was used concurrently"
        elif ident == healthy:
            healthy_started.set()
        return "answer"

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    start_pool(max_workers=3, release=release)
    assert first_started.wait(3)
    assert healthy_started.wait(3), "busy aliases starved a different agent"
    assert not second_started.wait(0.1)
    assert bridge.get_task(second)["status"] == "queued"
    release.set()
    eventually(lambda: all(bridge.get_task(ident)["status"] == "done"
                           for ident in (first, second, healthy)))
    assert Counter(executed) == Counter((first, second, healthy))


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_pool_never_exceeds_its_execution_limit(monkeypatch, start_pool, limit):
    ids = [task(agent["name"]) for agent in AGENTS[:6]]
    release, filled = threading.Event(), threading.Event()
    guard = threading.Lock()
    active = maximum = 0
    executed = []

    def execute(**kwargs):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
            executed.append(kwargs["task_id"])
            if active == limit:
                filled.set()
        try:
            assert release.wait(5)
            return "answer"
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    start_pool(max_workers=limit, release=release)
    assert filled.wait(3)
    with guard:
        assert active == maximum == limit
    assert sum(bridge.get_task(ident)["status"] == "queued" for ident in ids) == 6 - limit
    release.set()
    eventually(lambda: all(bridge.get_task(ident)["status"] == "done" for ident in ids))
    assert maximum == limit
    assert Counter(executed) == Counter(ids)


def test_competing_workers_cannot_dispatch_the_same_queued_row_twice(monkeypatch, start_pool):
    ident = task("agent-0")
    real_peek = bridge.peek_next_task
    simultaneous_peeks = threading.Barrier(4)
    seen = set()
    guard = threading.Lock()
    executed = []

    def peek(*args, **kwargs):
        row = real_peek(*args, **kwargs)
        if row and row["task_id"] == ident:
            name = threading.current_thread().name
            with guard:
                first_peek = name not in seen
                seen.add(name)
            if first_peek:
                simultaneous_peeks.wait(timeout=3)
        return row

    def execute(**kwargs):
        executed.append(kwargs["task_id"])
        return "once"

    monkeypatch.setattr(bridge, "peek_next_task", peek)
    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    start_pool(max_workers=4)
    eventually(lambda: bridge.get_task(ident)["status"] == "done")
    assert len(seen) == 4
    assert executed == [ident]


def test_parallel_workers_wait_for_dependencies_before_dispatch(monkeypatch, start_pool):
    upstream = task("agent-0", "upstream")
    dependent = bridge.enqueue_task("dependent", 10000, "agent-1", depends_on=[upstream])[0]
    independent = task("agent-2", "independent")
    upstream_started, independent_started, dependent_started = [
        threading.Event() for _ in range(3)]
    release = threading.Event()

    def execute(**kwargs):
        ident = kwargs["task_id"]
        if ident == upstream:
            upstream_started.set()
            assert release.wait(5)
        elif ident == independent:
            independent_started.set()
        elif ident == dependent:
            assert bridge.get_task(upstream)["status"] == "done"
            dependent_started.set()
        return "answer"

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    start_pool(max_workers=3, release=release)
    assert upstream_started.wait(3) and independent_started.wait(3)
    assert not dependent_started.wait(0.1)
    assert bridge.get_task(dependent)["status"] == "queued"
    release.set()
    eventually(lambda: bridge.get_task(dependent)["status"] == "done")


def test_quota_fallback_obeys_the_actual_targets_execution_lock(monkeypatch, start_pool):
    agents = [dict(AGENTS[0]), dict(AGENTS[1], agent="claude")]
    monkeypatch.setattr(bridge, "list_agents", lambda: (agents, {"ok": True}))
    bridge.mark_agent_quota_blocked("agent-1", "insufficient credits")
    first = task("agent-0", "occupy fallback")
    fallback = task("agent-1", "use fallback when available")
    first_started, fallback_started = threading.Event(), threading.Event()
    release = threading.Event()
    executed = []

    def execute(**kwargs):
        ident, agent = kwargs["task_id"], kwargs["agent_name"]
        executed.append((ident, agent))
        assert agent == "agent-0"
        if ident == first:
            first_started.set()
            assert release.wait(5)
        else:
            assert release.is_set(), "failover bypassed the occupied target lock"
            fallback_started.set()
        return "answer"

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    start_pool(release=release)
    assert first_started.wait(3)
    assert not fallback_started.wait(0.1)
    release.set()
    eventually(lambda: bridge.get_task(fallback)["status"] == "done")
    assert Counter(executed) == Counter(((first, "agent-0"), (fallback, "agent-0")))
    assert bridge.get_task(fallback)["executed_agent"] == "agent-0"


@pytest.mark.parametrize("second_agent", ["agent-0", "window:p0"])
def test_requested_agent_is_serial_even_when_idle_fallback_targets_differ(
        monkeypatch, start_pool, second_agent):
    agents = [dict(AGENTS[0]), dict(AGENTS[1], agent="claude"),
              dict(AGENTS[2], agent="codex")]
    monkeypatch.setattr(bridge, "list_agents", lambda: (agents, {"ok": True}))
    monkeypatch.setattr(bridge, "QUOTA_FAILOVER_AGENTS", ["agent-1", "agent-2"])
    bridge.mark_agent_quota_blocked("agent-0", "insufficient credits")
    first = task("agent-0", "first fallback")
    second = task(second_agent, "second fallback")
    started, second_started, release = [threading.Event() for _ in range(3)]
    executed = []

    def execute(**kwargs):
        ident, agent = kwargs["task_id"], kwargs["agent_name"]
        executed.append((ident, agent))
        if ident == first:
            assert agent == "agent-1"
            started.set()
            assert release.wait(5)
        else:
            second_started.set()
            assert release.is_set(), "requested-agent queue lock was bypassed by failover"
        return "answer"

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    start_pool(max_workers=3, release=release)
    assert started.wait(3)
    assert not second_started.wait(0.1)
    assert bridge.get_task(second)["status"] == "queued"
    # agent-2 is idle throughout; the second primary task must not claim it.
    release.set()
    eventually(lambda: bridge.get_task(second)["status"] == "done")
    assert Counter(ident for ident, _ in executed) == Counter((first, second))


def test_unsettled_agent_stays_serial_while_other_agents_continue(monkeypatch, start_pool):
    first = task("agent-0", "first")
    second = task("agent-0", "second")
    healthy = task("agent-1", "healthy")
    failed, healthy_started, recovered = [threading.Event() for _ in range(3)]
    real_complete = bridge.complete_task
    executed = []

    def persist(*args, **kwargs):
        if args[0] == first and not recovered.is_set():
            failed.set()
            raise sqlite3.OperationalError("first agent result cannot persist yet")
        return real_complete(*args, **kwargs)

    def execute(**kwargs):
        ident = kwargs["task_id"]
        executed.append(ident)
        if ident == second:
            assert recovered.is_set(), "same agent advanced before its prior settlement"
        if ident == healthy:
            healthy_started.set()
        return "answer"

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    monkeypatch.setattr(bridge, "complete_task", persist)
    start_pool(max_workers=3, release=recovered)
    assert failed.wait(3) and healthy_started.wait(3)
    assert bridge.get_task(first)["status"] == "running"
    assert bridge.get_task(second)["status"] == "queued"
    recovered.set()
    eventually(lambda: all(bridge.get_task(ident)["status"] == "done"
                           for ident in (first, second, healthy)))
    assert Counter(executed) == Counter((first, second, healthy))


def test_pool_health_fails_when_one_member_exits_and_does_not_restart_it(
        monkeypatch, start_pool):
    started = [threading.Event(), threading.Event()]
    unexpected_exit, release_busy = threading.Event(), threading.Event()
    calls = []

    def worker(stop):
        index = int(threading.current_thread().name.rsplit("-", 1)[1]) - 1
        calls.append(index)
        started[index].set()
        if index == 0:
            assert unexpected_exit.wait(5)
        else:
            assert release_busy.wait(5)

    monkeypatch.setattr(bridge, "task_worker", worker)
    pool = start_pool(release=release_busy)
    try:
        assert all(event.wait(3) for event in started)
        assert pool.healthy() and bridge.worker_is_healthy() is True
        unexpected_exit.set()
        eventually(lambda: not pool.workers[0].is_alive())
        assert pool.is_alive() and pool.workers[1].is_alive()
        assert not pool.healthy()
        assert bridge.worker_is_healthy() is False
        assert pool.stop_event.is_set()
        assert sorted(calls) == [0, 1], "failed workers must not replay work by restarting"
    finally:
        unexpected_exit.set()
        release_busy.set()


@pytest.mark.parametrize("value", [0, 33, "0", "33", "four", "1.5", True, None, " 2 ", "２"])
def test_invalid_worker_concurrency_is_reported_by_startup_checks(monkeypatch, value):
    monkeypatch.setattr(bridge, "WORKER_CONCURRENCY", value)
    with pytest.raises(ValueError, match="SENTINEL_WORKER_CONCURRENCY"):
        bridge.validate_worker_concurrency(value)
    assert any("SENTINEL_WORKER_CONCURRENCY" in problem
               for problem in bridge.startup_security_problems())
    with pytest.raises(ValueError, match="SENTINEL_WORKER_CONCURRENCY"):
        bridge.TaskWorkerPool(threading.Event())


@pytest.mark.parametrize("value", [1, 32, "1", "32"])
def test_worker_concurrency_accepts_both_supported_boundaries(monkeypatch, value):
    monkeypatch.setattr(bridge, "WORKER_CONCURRENCY", value)
    assert bridge.validate_worker_concurrency(value) == int(value)
    assert not any("SENTINEL_WORKER_CONCURRENCY" in problem
                   for problem in bridge.startup_security_problems())
    pool = bridge.TaskWorkerPool(threading.Event())
    assert len(pool.workers) == int(value)


@pytest.mark.parametrize("outcome, finish_name, expected", [
    ("success", "complete_task", "done"),
    ("timeout", "orphan_task", "orphaned"),
    ("exception", "fail_task", "error"),
])
def test_settlement_retries_do_not_execute_again_or_block_other_agents(
        monkeypatch, start_pool, outcome, finish_name, expected):
    first = task("agent-0")
    failed_persistence, healthy_started = threading.Event(), threading.Event()
    real_finish = getattr(bridge, finish_name)
    attempts, executed = [], []

    def persist(*args, **kwargs):
        if args[0] == first:
            attempts.append(1)
            if len(attempts) <= 2 or not healthy_started.is_set():
                failed_persistence.set()
                raise sqlite3.OperationalError("database is locked")
        return real_finish(*args, **kwargs)

    def execute(**kwargs):
        ident = kwargs["task_id"]
        executed.append(ident)
        if ident != first:
            healthy_started.set()
            return "healthy"
        if outcome == "timeout":
            raise TimeoutError("agent may still be executing")
        if outcome == "exception":
            raise RuntimeError("simulated agent failure")
        return "recovered answer"

    monkeypatch.setattr(bridge, finish_name, persist)
    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    start_pool()
    assert failed_persistence.wait(3)
    second = task("agent-1")
    assert healthy_started.wait(3)
    eventually(lambda: bridge.get_task(first)["status"] == expected)
    eventually(lambda: bridge.get_task(second)["status"] == "done")
    assert len(attempts) >= 3
    assert Counter(executed) == Counter((first, second))


class ShutdownServer:
    def __init__(self):
        self.closed = threading.Event()

    def shutdown(self):
        pass

    def server_close(self):
        self.closed.set()


def test_shutdown_waits_for_every_worker_and_leaves_waiting_tasks_queued(monkeypatch, start_pool):
    ids = [task("agent-0"), task("agent-1")]
    started = {ident: threading.Event() for ident in ids}
    releases = {ident: threading.Event() for ident in ids}
    executed = []
    server, finished, result = ShutdownServer(), threading.Event(), []

    def execute(**kwargs):
        ident = kwargs["task_id"]
        executed.append(ident)
        started[ident].set()
        assert releases[ident].wait(5)
        return "answer"

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    pool = start_pool(release=releases[ids[0]])
    # Ensure teardown also frees the independently blocked second worker.
    try:
        assert all(event.wait(3) for event in started.values())
        waiting = task("agent-0", "must remain queued")
        shutdown = threading.Thread(target=lambda: (
            result.append(bridge.graceful_shutdown(server, grace_sec=3)), finished.set()),
            daemon=True)
        shutdown.start()
        assert server.closed.wait(2)
        releases[ids[0]].set()
        eventually(lambda: bridge.get_task(ids[0])["status"] == "done")
        assert not finished.wait(0.1), "shutdown returned while another worker was executing"
        releases[ids[1]].set()
        shutdown.join(timeout=4)
        assert finished.is_set() and result == [True]
        assert not pool.is_alive()
        assert not any(worker.is_alive() for worker in pool.workers)
        assert all(bridge.get_task(ident)["status"] == "done" for ident in ids)
        assert bridge.get_task(waiting)["status"] == "queued"
        assert Counter(executed) == Counter(ids)
    finally:
        for release in releases.values():
            release.set()
        if "shutdown" in locals():
            shutdown.join(timeout=4)


def test_shutdown_retries_transient_result_persistence_inside_the_grace_period(
        monkeypatch, start_pool):
    ident = task("agent-0")
    started, release = threading.Event(), threading.Event()
    server = ShutdownServer()
    real_complete = bridge.complete_task
    attempts, executed, result = [], [], []

    def execute(**kwargs):
        executed.append(kwargs["task_id"])
        started.set()
        assert release.wait(5)
        return "answer survives shutdown"

    def persist(*args, **kwargs):
        attempts.append(1)
        if len(attempts) <= 2:
            raise sqlite3.OperationalError("database is locked during shutdown")
        return real_complete(*args, **kwargs)

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    monkeypatch.setattr(bridge, "complete_task", persist)
    pool = start_pool(release=release)
    assert started.wait(3)
    shutdown = threading.Thread(target=lambda: result.append(
        bridge.graceful_shutdown(server, grace_sec=3)), daemon=True)
    shutdown.start()
    try:
        assert server.closed.wait(2)
        release.set()
        shutdown.join(timeout=4)
        assert result == [True]
        assert not pool.is_alive()
        assert bridge.get_task(ident)["status"] == "done"
        assert bridge.get_task(ident)["result_text"] == "answer survives shutdown"
        assert executed == [ident] and len(attempts) == 3
    finally:
        release.set()
        shutdown.join(timeout=4)


def test_shutdown_deadline_orphans_all_workers_and_late_settlement_cannot_overwrite(
        monkeypatch, start_pool):
    ids = [task("agent-0"), task("agent-1")]
    started = {ident: threading.Event() for ident in ids}
    release = threading.Event()
    executed = []

    def execute(**kwargs):
        ident = kwargs["task_id"]
        executed.append(ident)
        started[ident].set()
        assert release.wait(5)
        if ident == ids[1]:
            raise RuntimeError("late failure after shutdown")
        Path(bridge.result_file_path(ident)).write_text("late answer", encoding="utf-8")
        return "late answer"

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    pool = start_pool(release=release)
    assert all(event.wait(3) for event in started.values())
    assert bridge.graceful_shutdown(ShutdownServer(), grace_sec=0.05) is False
    before = {ident: bridge.get_task(ident) for ident in ids}
    assert all(row["status"] == "orphaned" for row in before.values())
    assert pool.is_alive(), "grace timeout must not pretend it killed agent execution"
    release.set()
    pool.join(timeout=4)
    assert not pool.is_alive()
    for ident in ids:
        after = bridge.get_task(ident)
        assert after["status"] == "orphaned"
        assert after["error_text"] == before[ident]["error_text"]
        assert after["result_text"] is None
    assert Path(bridge.result_file_path(ids[0])).read_text(encoding="utf-8") == "late answer"
    assert Counter(executed) == Counter(ids)


def test_idle_pool_prunes_once_instead_of_once_per_worker(monkeypatch, start_pool):
    prunes, passes = [], []
    real_peek = bridge.peek_next_task

    def peek(*args, **kwargs):
        passes.append(1)
        return real_peek(*args, **kwargs)

    monkeypatch.setattr(bridge, "TASK_RETENTION_DAYS", 1)
    monkeypatch.setattr(bridge, "prune_old_tasks", lambda: (prunes.append(1) or 0))
    monkeypatch.setattr(bridge, "peek_next_task", peek)
    start_pool(max_workers=3)
    eventually(lambda: len(passes) >= 12)
    assert prunes == [1]


def test_prune_clock_is_scoped_to_database(monkeypatch, tmp_path):
    prunes = []
    monkeypatch.setattr(bridge, "TASK_RETENTION_DAYS", 1)
    monkeypatch.setattr(bridge, "prune_old_tasks", lambda: (prunes.append(bridge.DB_PATH) or 0))
    bridge._prune_worker_history()
    bridge._prune_worker_history()
    monkeypatch.setattr(bridge, "DB_PATH", str(tmp_path / "another.db"))
    bridge._prune_worker_history()
    assert len(prunes) == 2 and prunes[0] != prunes[1]

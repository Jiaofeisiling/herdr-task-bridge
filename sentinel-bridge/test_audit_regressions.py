"""Regression cases from the repository audit; no live Herdr or deployment."""
import io
import json
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest

import bridge

REAL_LIST_AGENTS = bridge.list_agents


AGENTS = [
    {"name": "primary", "pane_id": "w1:p1", "agent": "opencode", "agent_status": "idle"},
    {"name": "backup", "pane_id": "w1:p2", "agent": "claude", "agent_status": "idle"},
]


@pytest.fixture(autouse=True)
def isolated_bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "_agent_locks", {})
    monkeypatch.setattr(bridge, "_pending_sync_completions", {})
    monkeypatch.setattr(bridge, "_pending_sync_failures", {})
    monkeypatch.setattr(bridge, "_shutting_down", False)
    monkeypatch.setattr(bridge, "list_agents", lambda: (AGENTS, {"ok": True}))
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a: ("idle", {"ok": True}))
    bridge.init_db()


def request(path, body=None):
    handler = object.__new__(bridge.Handler)
    handler.path = path
    handler.command = "GET" if body is None else "POST"
    raw = json.dumps(body).encode() if body is not None else b""
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)
    handler.check_auth = lambda: True
    handler.replies = []
    handler.send_json = lambda obj, status=200: handler.replies.append((status, obj))
    return handler


def result_file(task_id, text="delivered"):
    Path(bridge.result_file_path(task_id)).write_text(text, encoding="utf-8")


def test_p1_old_refusal_with_new_output_does_not_replay_work(monkeypatch):
    before = "Previous request failed: insufficient credits"
    after = before + "\nCurrent task performed its side effect; result write denied."
    executed = []
    monkeypatch.setattr(bridge, "_terminal_read", lambda *a: (before, None))
    monkeypatch.setattr(bridge, "_read_terminal_tail", lambda *a: after)

    def deliver(agent, prompt, timeout_ms, task_id):
        if "side effect" in prompt:
            executed.append(agent)
        if agent == "backup":
            result_file(task_id)

    monkeypatch.setattr(bridge, "deliver_prompt", deliver)
    with pytest.raises(bridge.SentinelResultMissingError):
        bridge.run_with_quota_failover("primary", lambda agent:
            bridge.execute_sentinel_task(agent, "task-audit", "perform side effect", 1000))
    assert executed == ["primary"]
    assert bridge.get_agent_quota_block("primary") is None


def test_p1_alias_merge_keeps_every_active_and_cached_lock(monkeypatch):
    monkeypatch.setattr(bridge, "list_agents", lambda: (None, {"ok": False}))
    named = bridge.get_agent_lock("primary")
    pane = bridge.get_agent_lock("w1:p1")
    assert named is not pane
    pane.acquire()
    monkeypatch.setattr(bridge, "list_agents", lambda: (AGENTS, {"ok": True}))
    merged = bridge.get_agent_lock("primary")
    try:
        assert not merged.acquire(blocking=False)
        assert not named.acquire(blocking=False)
    finally:
        pane.release()
    assert merged.acquire(blocking=False)
    try:
        assert not pane.acquire(blocking=False)
    finally:
        merged.release()


def test_p1_alias_merge_waits_for_two_existing_holders(monkeypatch):
    monkeypatch.setattr(bridge, "list_agents", lambda: (None, {"ok": False}))
    named = bridge.get_agent_lock("primary")
    pane = bridge.get_agent_lock("w1:p1")
    named.acquire()
    pane.acquire()
    monkeypatch.setattr(bridge, "list_agents", lambda: (AGENTS, {"ok": True}))
    merged = bridge.get_agent_lock("primary")
    named.release()
    try:
        assert not merged.acquire(blocking=False)
    finally:
        pane.release()
    assert merged.acquire(blocking=False)
    merged.release()


def test_p1_sync_ask_is_durable_before_dispatch_and_after_disconnect(monkeypatch):
    ids = []

    def execute(agent, task_id, *args):
        row = bridge.get_task(task_id)
        assert row is not None and row["status"] == "running"
        ids.append(task_id)
        result_file(task_id, "answer survived disconnect")
        return "answer survived disconnect"

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    handler = request("/ask", {"task": "audit", "agent": "primary"})

    def disconnect(*args, **kwargs):
        raise BrokenPipeError("response lost")

    handler.send_json = disconnect
    with pytest.raises(BrokenPipeError):
        handler._do_POST()
    row = bridge.get_task(ids[0])
    assert row["status"] == "done"
    assert row["result_text"] == "answer survived disconnect"


def test_p1_worker_retries_completion_without_reexecuting(monkeypatch):
    task_id = bridge.create_task("audit", 1000, "primary")
    stop = threading.Event()
    completed = bridge.complete_task
    executed, attempts = [], []

    def execute(**kwargs):
        executed.append(kwargs["task_id"])
        result_file(task_id)
        return "delivered"

    def persist(*args, **kwargs):
        attempts.append(1)
        if len(attempts) <= 2:
            raise sqlite3.OperationalError("database is locked")
        moved = completed(*args, **kwargs)
        stop.set()
        return moved

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    monkeypatch.setattr(bridge, "complete_task", persist)
    monkeypatch.setattr(bridge, "WORKER_POLL_SECONDS", 0.001)
    worker = threading.Thread(target=bridge.task_worker, args=(stop,), daemon=True)
    worker.start()
    worker.join(timeout=2)
    try:
        assert bridge.get_task(task_id)["status"] == "done"
        assert executed == [task_id]
        assert len(attempts) == 3
    finally:
        stop.set()
        bridge._work_available.set()
        worker.join(timeout=2)


def test_p2_identical_refusal_appended_during_turn_is_detected():
    error = "Error: insufficient credits for this request. Please add funds.\n"
    assert bridge.fresh_provider_refusal(error, error + error) is not None


@pytest.mark.parametrize("status, expected", [("done", ["done"]), ("error", [])])
def test_p2_task_filter_applies_after_late_recovery(status, expected):
    task_id = bridge.create_task("audit", 1000, "primary")
    bridge.claim_task(task_id)
    bridge.fail_task(task_id, "file not delivered yet")
    result_file(task_id)
    handler = request("/tasks?status=" + status)
    handler._do_GET()
    assert [row["status"] for row in handler.replies[-1][1]["tasks"]] == expected


def test_p2_runtime_family_reset_clears_the_resolved_circuit():
    bridge.mark_agent_quota_blocked("primary", "insufficient credits")
    assert bridge.clear_agent_quota_blocks("opencode") == 1
    assert bridge.get_agent_quota_block("primary") is None


def test_p2_ready_reports_dispatch_lock():
    with bridge.get_agent_lock("primary"):
        handler = request("/ready?agent=primary")
        handler._do_GET()
        reply = handler.replies[-1][1]
        assert reply["ready"] is False
        assert reply["reason"] == "locked"


def test_p2_worker_does_not_dispatch_after_shutdown(monkeypatch):
    task_id = bridge.create_task("audit", 1000, "primary")
    monkeypatch.setattr(bridge, "_shutting_down", True)
    assert bridge.claim_task(task_id) is False
    monkeypatch.setattr(bridge, "execute_sentinel_task", lambda **k: pytest.fail("dispatched during shutdown"))
    worker = threading.Thread(target=bridge.task_worker, daemon=True)
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert bridge.get_task(task_id)["status"] == "queued"


def test_p2_fallback_timeout_preserves_the_actual_executor(monkeypatch):
    bridge.mark_agent_quota_blocked("primary", "insufficient credits")
    called = []

    def execute(agent, *args):
        called.append(agent)
        raise TimeoutError("may still be executing")

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    handler = request("/ask", {"task": "audit", "agent": "primary"})
    handler._do_POST()
    status, reply = handler.replies[-1]
    assert status == 504 and called == ["backup"]
    assert bridge.get_task(reply["task_id"])["executed_agent"] == "backup"


def test_p2_fast_work_without_result_does_not_claim_it_never_ran(monkeypatch):
    monkeypatch.setattr(bridge, "_terminal_read", lambda *a: ("idle", None))
    monkeypatch.setattr(bridge, "_read_terminal_tail", lambda *a: "file write denied")
    monkeypatch.setattr(bridge, "deliver_prompt", lambda *a: None)
    with pytest.raises(bridge.SentinelResultMissingError) as error:
        bridge.execute_sentinel_task("primary", "task-audit", "perform a small operation", 1000)
    message = str(error.value).lower()
    assert "may already have executed" in message
    assert "too quickly to have run" not in message
    assert "never started" not in message


def test_p2_task_and_reminder_share_one_deadline(monkeypatch):
    now, budgets = [0.0], []
    monkeypatch.setattr(bridge.time, "monotonic", lambda: now[0])

    def before(*args):
        now[0] += 0.2
        return "idle", None

    def tail(*args):
        now[0] += 0.2
        return "idle"

    def deliver(agent, prompt, timeout_ms, task_id):
        budgets.append(timeout_ms)
        now[0] += 0.4 if len(budgets) == 1 else 0.05
        if len(budgets) == 2:
            result_file(task_id)

    monkeypatch.setattr(bridge, "_terminal_read", before)
    monkeypatch.setattr(bridge, "_read_terminal_tail", tail)
    monkeypatch.setattr(bridge, "deliver_prompt", deliver)
    bridge.execute_sentinel_task("primary", "task-audit", "audit", 1000)
    assert 0 < budgets[0] <= 800
    assert 0 < budgets[1] <= 200


def test_p2_queue_diagnostics_have_a_short_total_budget(monkeypatch):
    task_id = bridge.create_task("audit", 1000, "primary")
    monkeypatch.setattr(bridge, "list_agents", REAL_LIST_AGENTS)

    budgets = []
    def hung_herdr(*args, **kwargs):
        budgets.append(kwargs["timeout"])
        raise bridge.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(bridge.subprocess, "run", hung_herdr)
    handler = request("/tasks/" + task_id)
    handler._do_GET()
    assert handler.replies[-1][0] == 200
    assert budgets and all(0 < budget <= 1 for budget in budgets)


def test_p2_sync_completion_is_retried_after_database_recovers(monkeypatch):
    complete = bridge.complete_task
    attempts = []

    def execute(agent, task_id, *args):
        result_file(task_id)
        return "delivered"

    def temporarily_locked(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return complete(*args, **kwargs)

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    monkeypatch.setattr(bridge, "complete_task", temporarily_locked)
    handler = request("/ask", {"task": "audit", "agent": "primary"})
    handler._do_POST_guarded()
    status, reply = handler.replies[-1]
    assert status == 503 and reply["task_id"]
    lookup = request("/tasks/" + reply["task_id"])
    lookup._do_GET()
    assert lookup.replies[-1][1]["task"]["status"] == "done"


def test_refusal_identity_ignores_sidebar_but_keeps_explicit_size_figures():
    before = "Prompt tokens limit exceeded: 100 > 90 │ Context: 3%"
    moved = "Prompt tokens limit exceeded: 100 > 90 │ Context: 4%"
    assert bridge.fresh_provider_refusal(before, moved) is None
    assert "101" in bridge.fresh_provider_refusal(before, moved.replace("100", "101"))


def test_an_old_size_refusal_becoming_more_visible_is_not_new():
    partial = "Prompt tokens limit exceeded"
    complete = partial + ": 100 > 90"
    assert bridge.fresh_provider_refusal(partial, complete) is None
    assert bridge.fresh_provider_refusal(complete, partial) is None


def test_instance_lock_is_released_without_deleting_its_file():
    with bridge.bridge_instance():
        with pytest.raises(bridge.InstanceAlreadyRunningError):
            with bridge.bridge_instance():
                pytest.fail("second owner admitted")
    assert Path(bridge.DB_PATH + ".lock").exists()
    with bridge.bridge_instance():
        pass


def test_worker_retries_timeout_settlement_without_resending(monkeypatch):
    task_id = bridge.create_task("audit", 1000, "primary")
    stop = threading.Event()
    original = bridge.orphan_task
    executions, attempts = [], []

    def timeout(**kwargs):
        executions.append(kwargs["task_id"])
        raise TimeoutError("may still be running")

    def persist(*args):
        attempts.append(1)
        if len(attempts) < 3:
            raise sqlite3.OperationalError("database is locked")
        moved = original(*args)
        stop.set()
        return moved

    monkeypatch.setattr(bridge, "execute_sentinel_task", timeout)
    monkeypatch.setattr(bridge, "orphan_task", persist)
    monkeypatch.setattr(bridge, "WORKER_POLL_SECONDS", 0.001)
    worker = threading.Thread(target=bridge.task_worker, args=(stop,), daemon=True)
    worker.start()
    worker.join(timeout=2)
    try:
        assert bridge.get_task(task_id)["status"] == "orphaned"
        assert executions == [task_id] and len(attempts) == 3
    finally:
        stop.set()
        bridge._work_available.set()
        worker.join(timeout=2)


class StartupServer:
    def __init__(self, *args, **kwargs):
        pass

    def server_bind(self):
        pass

    def server_activate(self):
        pass

    def server_close(self):
        pass


def test_startup_recovers_answers_before_dependency_cancellation(monkeypatch):
    upstream, _ = bridge.enqueue_task("audit", 1000, "primary")
    child, _ = bridge.enqueue_task("next step", 1000, "primary", depends_on=(upstream,))
    bridge.claim_task(upstream)
    result_file(upstream)
    monkeypatch.setattr(bridge, "startup_security_problems", lambda: [])
    monkeypatch.setattr(bridge, "harden_permissions", lambda: [])
    monkeypatch.setattr(bridge, "ThreadingHTTPServer", StartupServer)
    started = []

    def start_worker(server):
        started.append(1)
        bridge.cancel_blocked_tasks()
        assert bridge.get_task(upstream)["status"] == "done"
        assert bridge.get_task(child)["status"] == "queued"

    monkeypatch.setattr(bridge, "serve_bridge", start_worker)
    assert bridge.main() == 0 and started == [1]


def test_an_unavailable_listener_prevents_database_recovery(monkeypatch):
    task_id = bridge.create_task("audit", 1000, "primary")
    bridge.claim_task(task_id)

    class BusyPort(StartupServer):
        def server_bind(self):
            raise OSError("Address already in use")

    monkeypatch.setattr(bridge, "startup_security_problems", lambda: [])
    monkeypatch.setattr(bridge, "harden_permissions", lambda: [])
    monkeypatch.setattr(bridge, "ThreadingHTTPServer", BusyPort)
    assert bridge.main() == 1
    assert bridge.get_task(task_id)["status"] == "running"


@pytest.mark.parametrize("error", [
    '{"error":{"code":"transport_error","message":"lost response"}}',
    'connection closed after dispatch',
    '{"error":{"code":"agent_not_found"}}',
])
def test_prompt_failure_never_retargets_outside_the_held_agent_lock(monkeypatch, error):
    dispatched = []

    def run(*args, **kwargs):
        dispatched.append(args[2])
        return {"ok": False, "stdout": "", "stderr": error}

    monkeypatch.setattr(bridge, "run_herdr", run)
    monkeypatch.setattr(bridge, "resolve_agent", lambda _: "backup")
    monkeypatch.setattr(bridge, "_read_terminal_tail", lambda *a: "diagnostic")
    with pytest.raises(bridge.SentinelPromptError):
        bridge._run_herdr_prompt("primary", "perform side effect", 1000)
    assert dispatched == ["primary"]


def test_sync_timeout_settlement_is_retried_without_resending(monkeypatch):
    original = bridge._transition_where
    executed, writes = [], []

    def execute(agent, task_id, *args):
        executed.append(task_id)
        raise TimeoutError("delivery uncertain")

    def persist(conn, to, *args, **kwargs):
        if to == "orphaned":
            writes.append(1)
            if len(writes) <= 2:
                raise sqlite3.OperationalError("database is locked")
        return original(conn, to, *args, **kwargs)

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    monkeypatch.setattr(bridge, "_transition_where", persist)
    handler = request("/ask", {"task": "audit", "agent": "primary"})
    handler._do_POST_guarded()
    assert handler.replies[-1][0] == 504
    for _ in range(2):
        lookup = request("/tasks/" + executed[0])
        lookup._do_GET()
    assert bridge.get_task(executed[0])["status"] == "orphaned"
    assert len(executed) == 1 and len(writes) == 3


@pytest.mark.parametrize("error", [OSError("local I/O failure"), ValueError("unexpected output")])
def test_unexpected_sync_execution_error_keeps_queryable_recovery(monkeypatch, error):
    executed = []

    def execute(agent, task_id, *args):
        executed.append(task_id)
        raise error

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    handler = request("/ask", {"task": "audit", "agent": "primary"})
    handler._do_POST_guarded()
    status, reply = handler.replies[-1]
    assert status == 500 and reply["task_id"] == executed[0]
    assert bridge.get_task(executed[0])["status"] == "orphaned"
    result_file(executed[0], "late answer")
    lookup = request("/tasks/" + executed[0])
    lookup._do_GET()
    assert lookup.replies[-1][1]["task"]["result_text"] == "late answer"


def test_routing_diagnostics_failure_does_not_lose_completed_sync_result(monkeypatch):
    original = bridge.get_agent_quota_block

    def diagnostics_unavailable(*args):
        raise sqlite3.OperationalError("database is locked")

    def execute(agent, task_id, *args):
        if agent == "primary":
            raise bridge.AgentQuotaExhaustedError(agent, "insufficient credits")
        result_file(task_id, "completed on backup")
        # Only optional post-execution routing diagnostics fail.
        monkeypatch.setattr(bridge, "get_agent_quota_block", diagnostics_unavailable)
        return "completed on backup"

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    handler = request("/ask", {"task": "audit", "agent": "primary"})
    handler._do_POST_guarded()
    status, reply = handler.replies[-1]
    monkeypatch.setattr(bridge, "get_agent_quota_block", original)
    assert status == 200
    assert bridge.get_task(reply["task_id"])["result_text"] == "completed on backup"


def test_dependency_cancellation_recovers_already_delivered_parent_result():
    parent, _ = bridge.enqueue_task("parent", 1000, "primary")
    child, _ = bridge.enqueue_task("child", 1000, "primary", depends_on=(parent,))
    bridge.claim_task(parent)
    bridge.orphan_task(parent, "wait expired")
    result_file(parent, "late parent answer")
    assert bridge.cancel_blocked_tasks() == 0
    assert bridge.get_task(parent)["status"] == "done"
    assert bridge.get_task(child)["status"] == "queued"
    assert bridge.peek_next_task()["task_id"] == child


def test_scope_warning_handles_agent_on_another_windows_drive(monkeypatch):
    import ntpath

    monkeypatch.setattr(bridge, "RESULT_DIR", "C:\\bridge\\results")
    monkeypatch.setattr(bridge, "list_agents", lambda: ([
        {"name": "other-drive", "cwd": "E:\\work"},
    ], {"ok": True}))
    # Exercise Windows path semantics on every test platform.
    monkeypatch.setattr(bridge.os.path, "abspath", ntpath.abspath)
    monkeypatch.setattr(bridge.os.path, "commonpath", ntpath.commonpath)
    assert "other-drive" in bridge.result_dir_scope_warning()


@pytest.mark.parametrize("error", [FileNotFoundError("Herdr unavailable"),
                                  subprocess.TimeoutExpired("fake-herdr", 10)])
def test_advisory_agent_discovery_failure_does_not_prevent_startup(monkeypatch, error):
    def unavailable():
        raise error

    monkeypatch.setattr(bridge, "list_agents", unavailable)
    monkeypatch.setattr(bridge, "startup_security_problems", lambda: [])
    monkeypatch.setattr(bridge, "harden_permissions", lambda: [])
    monkeypatch.setattr(bridge, "ThreadingHTTPServer", StartupServer)
    started = []
    monkeypatch.setattr(bridge, "serve_bridge", lambda server: started.append(True))
    assert bridge.main() == 0
    assert started == [True]


@pytest.mark.parametrize("reader", ["get", "list"])
def test_expiring_quota_snapshot_does_not_delete_concurrent_renewal(monkeypatch, reader):
    bridge.mark_agent_quota_blocked("primary", "old block")
    with bridge.db_session() as conn:
        conn.execute("UPDATE quota_blocks SET detected_at = ?", ("2000-01-01T00:00:00+00:00",))
    monkeypatch.setattr(bridge, "QUOTA_BLOCK_TTL_SECONDS", 60)
    original = bridge._quota_block_expired
    renew, renewed = threading.Event(), threading.Event()
    errors = []

    def writer():
        try:
            assert renew.wait(2)
            bridge.mark_agent_quota_blocked("primary", "fresh block")
        except Exception as error:
            errors.append(error)
        finally:
            renewed.set()

    def expired(block):
        if block["detail"] == "old block":
            renew.set()
            assert renewed.wait(2)
        return original(block)

    monkeypatch.setattr(bridge, "_quota_block_expired", expired)
    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        if reader == "get":
            bridge.get_agent_quota_block("primary")
        else:
            bridge.list_agent_quota_blocks()
        assert not errors
        block = bridge.get_agent_quota_block("primary")
        assert block and block["detail"] == "fresh block"
    finally:
        renew.set()
        thread.join(timeout=3)


@pytest.mark.parametrize("status", ["orphaned", "error"])
def test_pending_completion_uses_known_answer_after_state_changes_without_file(status):
    task_id = bridge.create_task("audit", 1000, "primary")
    bridge.claim_task(task_id)
    bridge.defer_sync_completion(task_id, "answer already read", "routing note")
    bridge.record_sync_failure(task_id, "audit", 1000, "primary", None, status, "original failure")
    bridge.retry_sync_completions(task_id)
    row = bridge.get_task(task_id)
    assert row["status"] == "done"
    assert row["result_text"] == "answer already read"
    assert "original failure" in row["error_text"] and "routing note" in row["error_text"]
    assert not bridge._pending_sync_completions


def test_sync_success_is_durable_when_shutdown_wins_completion_race(monkeypatch):
    def execute(agent, task_id, *args):
        result_file(task_id, "completed answer")
        bridge.orphan_task(task_id, "shutdown raced with completion")
        return "completed answer"

    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    handler = request("/ask", {"task": "audit", "agent": "primary"})

    def reply(obj, status=200):
        if status == 200:
            row = bridge.get_task(obj["task_id"])
            assert row["status"] == "done" and row["result_text"] == "completed answer"
        handler.replies.append((status, obj))

    handler.send_json = reply
    handler._do_POST_guarded()
    assert handler.replies[-1][0] == 200


class ShutdownServer:
    def __init__(self):
        self.closed = threading.Event()

    def shutdown(self):
        pass

    def server_close(self):
        self.closed.set()


def test_shutdown_drains_background_execution_and_result_settlement(monkeypatch):
    task_id = bridge.create_task("audit", 1000, "primary")
    executing, release, stopped = threading.Event(), threading.Event(), threading.Event()
    stop = threading.Event()
    server = ShutdownServer()

    def execute(**kwargs):
        executing.set()
        assert release.wait(3)
        result_file(task_id, "background answer")
        return "background answer"

    worker = threading.Thread(target=bridge.task_worker, args=(stop,), daemon=True)
    monkeypatch.setattr(bridge, "_worker_thread", worker)
    monkeypatch.setattr(bridge, "_worker_stop", stop)
    monkeypatch.setattr(bridge, "_inflight", 0)
    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    worker.start()
    shutdown = threading.Thread(target=lambda: (
        bridge.graceful_shutdown(server, grace_sec=2), stopped.set()), daemon=True)
    try:
        assert executing.wait(2)
        shutdown.start()
        assert server.closed.wait(2)
        assert not stopped.wait(0.15), "shutdown ignored the background worker"
        release.set()
        shutdown.join(timeout=3)
        worker.join(timeout=3)
        assert stopped.is_set() and not worker.is_alive()
        assert bridge.get_task(task_id)["status"] == "done"
    finally:
        release.set()
        stop.set()
        bridge._work_available.set()
        worker.join(timeout=3)
        if shutdown.ident is not None:
            shutdown.join(timeout=3)


@pytest.mark.parametrize("reminder", [False, True])
def test_shutdown_prevents_new_prompts_and_result_reminders(monkeypatch, reminder):
    dispatched = []

    def run(*args, **kwargs):
        dispatched.append(args[3])
        monkeypatch.setattr(bridge, "_shutting_down", True)
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", run)
    monkeypatch.setattr(bridge, "_terminal_read", lambda *a: ("", None))
    monkeypatch.setattr(bridge, "_read_terminal_tail", lambda *a: "")
    if not reminder:
        monkeypatch.setattr(bridge, "_shutting_down", True)
    with pytest.raises(TimeoutError, match="shutting down"):
        bridge.execute_sentinel_task("primary", "shutdown-test", "audit", 1000)
    assert len(dispatched) == int(reminder)


def test_repeated_signals_cannot_finish_shutdown_before_the_first_drain(monkeypatch):
    import signal

    handlers, calls = {}, []
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(bridge.signal, "signal", lambda sig, fn: handlers.update({sig: fn}))
    monkeypatch.setattr(bridge, "task_worker", lambda stop: None)

    def drain(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        return True

    monkeypatch.setattr(bridge, "graceful_shutdown", drain)

    class SignalsServer:
        def serve_forever(self):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            assert entered.wait(2)
            handlers[signal.SIGTERM](signal.SIGTERM, None)

    server_thread = threading.Thread(target=lambda: (
        bridge.serve_bridge(SignalsServer()), returned.set()), daemon=True)
    # serve_bridge normally runs in its own process; preserve these globals here.
    monkeypatch.setattr(bridge, "_worker_thread", None)
    monkeypatch.setattr(bridge, "_worker_stop", None)
    server_thread.start()
    try:
        assert entered.wait(2)
        assert not returned.wait(0.15), "second signal bypassed the first drain"
        assert len(calls) == 1
    finally:
        release.set()
        server_thread.join(timeout=3)
    assert returned.is_set()


def test_preclaim_failure_skips_faulty_agent_without_delaying_healthy_work(monkeypatch):
    first = bridge.create_task("fault", 1000, "primary")
    second = bridge.create_task("healthy", 1000, "backup")
    stop = threading.Event()
    original_claim = bridge.claim_task
    attempts, sleeps = [], []

    def claim(task_id):
        attempts.append(task_id)
        if len(attempts) > 5:
            stop.set()  # bound the old busy loop
        if task_id == first:
            raise sqlite3.OperationalError("pre-claim fault")
        return original_claim(task_id)

    def execute(**kwargs):
        result_file(second, "healthy answer")
        stop.set()
        return "healthy answer"

    monkeypatch.setattr(bridge, "claim_task", claim)
    monkeypatch.setattr(bridge, "execute_sentinel_task", execute)
    monkeypatch.setattr(bridge.time, "sleep", lambda seconds: sleeps.append(seconds))
    bridge.task_worker(stop)
    assert attempts == [first, second]
    assert not sleeps
    assert bridge.get_task(second)["status"] == "done"
    assert bridge.get_task(first)["status"] == "queued"


def test_auto_selection_uses_one_quota_snapshot_and_no_per_candidate_discovery(monkeypatch):
    agents = [{**a, "agent": "opencode"} for a in AGENTS]
    counts = {"discovery": 0, "snapshot": 0}

    def discover():
        counts["discovery"] += 1
        return agents, {"ok": True}

    def snapshot():
        counts["snapshot"] += 1
        return [{"agent": "w1:p1"}]

    monkeypatch.setattr(bridge, "AGENT_PRIORITY", ("primary", "backup"))
    monkeypatch.setattr(bridge, "list_agents", discover)
    monkeypatch.setattr(bridge, "list_agent_quota_blocks", snapshot)
    assert bridge.resolve_agent(None) == "backup"
    assert counts == {"discovery": 1, "snapshot": 1}


def test_invalid_default_policy_is_rejected_before_startup_changes_tasks(monkeypatch):
    task_id = bridge.create_task("unchanged", 1000, "primary")
    bridge.claim_task(task_id)
    monkeypatch.setattr(bridge, "AUTH_TOKEN", "x" * 32)
    monkeypatch.setattr(bridge, "DEFAULT_SLURM_POLICY", "invalid-policy")
    with pytest.raises(ValueError, match="slurm_policy"):
        bridge.validate_slurm_policy(None)
    monkeypatch.setattr(bridge.sys, "argv", ["bridge.py", "--check"])
    assert bridge.main() == 2
    monkeypatch.setattr(bridge.sys, "argv", ["bridge.py"])
    assert bridge.main() == 2
    assert bridge.get_task(task_id)["status"] == "running"


def test_windows_directory_scope_is_case_insensitive(monkeypatch):
    import ntpath
    monkeypatch.setattr(bridge, "RESULT_DIR", r"e:\Project\results")
    monkeypatch.setattr(bridge, "list_agents", lambda: ([{"name": "a", "cwd": r"E:\project"}], {"ok": True}))
    monkeypatch.setattr(bridge.os.path, "abspath", ntpath.abspath)
    monkeypatch.setattr(bridge.os.path, "commonpath", ntpath.commonpath)
    monkeypatch.setattr(bridge.os.path, "normcase", ntpath.normcase)
    assert bridge.result_dir_scope_warning() is None


@pytest.fixture
def smoke_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("smoke_regression", Path(__file__).parent.parent / "scripts" / "smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("wrapped", [False, True])
def test_smoke_classifies_direct_and_wrapped_timeouts_consistently(monkeypatch, smoke_module, wrapped):
    import urllib.error
    error = TimeoutError("simulated timeout")
    if wrapped:
        error = urllib.error.URLError(error)

    def request(*args, **kwargs):
        raise error

    monkeypatch.setattr(smoke_module, "request", request)
    with pytest.raises(smoke_module.ChannelDown, match="never replied"):
        smoke_module.check_channel("http://unused.invalid", None)


def test_smoke_preserves_special_characters_in_agent_query(monkeypatch, smoke_module):
    from urllib.parse import parse_qs, urlparse
    identifier = "agent name&part#tail"
    urls = []

    class ProbeComplete(Exception):
        pass

    def request(url, **kwargs):
        if url.endswith("/agents"):
            return 200, {"agents": [{"name": identifier, "agent": "opencode"}]}
        urls.append(url)
        raise ProbeComplete

    monkeypatch.setattr(smoke_module, "default_token", lambda: None)
    monkeypatch.setattr(smoke_module.sys, "argv", ["smoke.py", "--url", "http://unused.invalid"])
    monkeypatch.setattr(smoke_module, "check_channel", lambda *a: {"version": 28, "default_agent": None, "worker_alive": True})
    monkeypatch.setattr(smoke_module, "request", request)
    with pytest.raises(ProbeComplete):
        smoke_module.main()
    assert parse_qs(urlparse(urls[0]).query)["agent"] == [identifier]
    assert " " not in urls[0]


@pytest.mark.parametrize("duplicate,exact_name,wrong_success,expected", [
    (False, False, False, 0),
    (True, False, False, 0),
    (True, True, False, 0),
    (True, False, True, 1),
])
def test_smoke_checks_runtime_ambiguity_without_rejecting_healthy_deployment(
    monkeypatch, smoke_module, duplicate, exact_name, wrong_success, expected,
):
    from urllib.parse import parse_qs, urlparse
    agents = [{"name": "primary", "agent": "opencode"}]
    if duplicate:
        agents.append({"name": "opencode" if exact_name else "backup", "agent": "opencode"})
    monkeypatch.setattr(bridge, "list_agents", lambda: (agents, {"ok": True}))

    def probe(url, method="GET", body=None, **kwargs):
        parsed = urlparse(url)
        if parsed.path == "/agents":
            return 200, {"agents": agents}
        if parsed.path == "/ready":
            selector = parse_qs(parsed.query)["agent"][0]
            if wrong_success and selector == "opencode":
                return 200, {"ok": True}
            try:
                bridge.resolve_agent(selector)
                return 200, {"ok": True, "agent_status": "idle"}
            except bridge.AgentNotFoundError as error:
                return 404, {"ok": False, "reason": "agent_not_found", "error": str(error)}
        if parsed.path == "/ask":
            return 404, {"reason": "agent_not_found"}
        if parsed.path in ("/delegate", "/read"):
            return 400, {}
        if parsed.path == "/quota":
            return 200, {"blocked_agents": []}
        raise AssertionError(url)

    monkeypatch.setattr(smoke_module, "default_token", lambda: None)
    monkeypatch.setattr(smoke_module.sys, "argv", ["smoke.py", "--url", "http://unused.invalid"])
    monkeypatch.setattr(smoke_module, "check_channel", lambda *a: {"version": 28, "default_agent": None, "worker_alive": True})
    monkeypatch.setattr(smoke_module, "request", probe)
    assert smoke_module.main() == expected

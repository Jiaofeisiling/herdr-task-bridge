"""Regression cases from the repository audit; no live Herdr or deployment."""
import io
import json
import sqlite3
import threading
from pathlib import Path

import pytest

import bridge


AGENTS = [
    {"name": "primary", "pane_id": "w1:p1", "agent": "opencode", "agent_status": "idle"},
    {"name": "backup", "pane_id": "w1:p2", "agent": "claude", "agent_status": "idle"},
]


@pytest.fixture(autouse=True)
def isolated_bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "_agent_locks", {})
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

import sys
import os
import re
import sqlite3
import time

from datetime import datetime as datetime_module, timezone as datetime_module_tz, timedelta as datetime_module_delta

sys.path.insert(0, os.path.dirname(__file__))

import bridge

# Captured before the autouse fixture below replaces it, for the few
# tests whose subject *is* list_agents itself.
_REAL_LIST_AGENTS = bridge.list_agents


def compliant_agent(text="ok"):
    """A fake run_herdr that behaves the way the delegation prompt asks a
    real agent to: it finds the result path in the prompt it was handed and
    writes the answer there, instead of printing it to the terminal."""
    def fake_run_herdr(*args, **kwargs):
        if args[1] == "prompt":
            match = re.search(r"(\S*result-[0-9a-f]+\.txt)", args[3])
            if match:
                with open(match.group(1), "w", encoding="utf-8") as f:
                    f.write(text)
        return {"ok": True, "stdout": "", "stderr": ""}

    return fake_run_herdr


def test_build_delegation_prompt_includes_token_and_task():
    task_id = "11111111-2222-3333-4444-555555555555"
    prompt = bridge.build_delegation_prompt("检查磁盘", task_id)

    assert task_id.replace("-", "") in prompt
    assert "检查磁盘" in prompt





def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(bridge, "DB_PATH", str(db_path))
    bridge.init_db()
    return db_path


def test_create_and_get_task(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    task_id = bridge.create_task("check disk space", 60000, "sentinel")
    task = bridge.get_task(task_id)

    assert task["status"] == "queued"
    assert task["task"] == "check disk space"
    assert task["timeout_ms"] == 60000
    assert task["result_text"] is None
    assert task["error_text"] is None


def test_get_task_missing_returns_none(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    assert bridge.get_task("does-not-exist") is None


def test_list_tasks_orders_newest_first(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    first = bridge.create_task("first", 1000, "sentinel")
    second = bridge.create_task("second", 1000, "sentinel")

    tasks = bridge.list_tasks()

    assert [t["task_id"] for t in tasks] == [second, first]


def test_claim_task_only_succeeds_once(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    task_id = bridge.create_task("t", 1000, "sentinel")

    assert bridge.claim_task(task_id) is True
    assert bridge.claim_task(task_id) is False
    assert bridge.get_task(task_id)["status"] == "running"


def test_peek_next_task_returns_oldest_queued(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    first = bridge.create_task("first", 1000, "sentinel")
    bridge.create_task("second", 1000, "sentinel")

    assert bridge.peek_next_task()["task_id"] == first


def test_peek_next_task_returns_none_when_empty(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    assert bridge.peek_next_task() is None


def test_complete_task_sets_result(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    task_id = bridge.create_task("t", 1000, "sentinel")
    bridge.claim_task(task_id)
    bridge.complete_task(task_id, "all good")

    task = bridge.get_task(task_id)
    assert task["status"] == "done"
    assert task["result_text"] == "all good"
    assert task["finished_at"] is not None


def test_fail_task_sets_error(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    task_id = bridge.create_task("t", 1000, "sentinel")
    bridge.claim_task(task_id)
    bridge.fail_task(task_id, "boom")

    task = bridge.get_task(task_id)
    assert task["status"] == "error"
    assert task["error_text"] == "boom"


def test_init_db_orphans_stale_running_rows_on_restart(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    task_id = bridge.create_task("t", 1000, "sentinel")
    bridge.claim_task(task_id)

    # simulate a bridge restart against the same database file
    bridge.init_db()

    task = bridge.get_task(task_id)
    assert task["status"] == "orphaned"
    assert "restarted" in task["error_text"]


import json as json_module


def test_get_agent_status_idle(monkeypatch):
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": True,
        "stdout": json_module.dumps({"result": {"agent": {"agent_status": "idle"}}}),
        "stderr": "",
    })

    status, _ = bridge.get_agent_status("sentinel")

    assert status == "idle"


def test_get_agent_status_herdr_failure_returns_none(monkeypatch):
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": False, "stdout": "", "stderr": "connection refused",
    })

    status, result = bridge.get_agent_status("sentinel")

    assert status is None
    assert result["ok"] is False


def test_get_agent_status_bad_json_returns_none(monkeypatch):
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": True, "stdout": "not json", "stderr": "",
    })

    status, result = bridge.get_agent_status("sentinel")

    assert status is None
    assert result["ok"] is False
    assert "Unable to parse" in result["error"]


def test_available_states_contains_idle_and_done():
    assert bridge.AVAILABLE_STATES == {"idle", "done"}


import subprocess as subprocess_module

import pytest


def test_execute_sentinel_task_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "run_herdr", compliant_agent("一切正常"))

    result = bridge.execute_sentinel_task(
        "sentinel", "11111111-1111-1111-1111-111111111111", "do a thing", 60000
    )

    assert result == "一切正常"


def test_execute_sentinel_task_prompt_failure_raises(monkeypatch):
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": False, "stdout": "", "stderr": "herdr not found",
    })

    with pytest.raises(bridge.SentinelPromptError):
        bridge.execute_sentinel_task("sentinel", "id", "task", 60000)



def test_execute_sentinel_task_timeout_raises(monkeypatch):
    def fake_run_herdr(*args, **kwargs):
        raise subprocess_module.TimeoutExpired(cmd="herdr", timeout=1)

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    with pytest.raises(TimeoutError):
        bridge.execute_sentinel_task("sentinel", "id", "task", 60000)





def test_acquire_agent_for_delegation_succeeds_when_idle(monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))

    entered = False
    with bridge.acquire_agent_for_delegation("sentinel"):
        entered = True

    assert entered is True
    assert bridge.get_agent_lock("sentinel").locked() is False


def test_acquire_agent_for_delegation_raises_when_busy(monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("working", {"ok": True}))

    with pytest.raises(bridge.SentinelBusyError):
        with bridge.acquire_agent_for_delegation("sentinel"):
            pass

    assert bridge.get_agent_lock("sentinel").locked() is False


def test_acquire_agent_for_delegation_raises_when_locked_by_another_caller(monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))

    bridge.get_agent_lock("sentinel").acquire()
    try:
        with pytest.raises(bridge.SentinelBusyError):
            with bridge.acquire_agent_for_delegation("sentinel"):
                pass
    finally:
        bridge.get_agent_lock("sentinel").release()


def test_acquire_agent_for_delegation_raises_when_unreachable(monkeypatch):
    monkeypatch.setattr(
        bridge, "get_agent_status",
        lambda *a, **k: (None, {"ok": False, "error": "no route"}),
    )

    with pytest.raises(bridge.SentinelUnavailableError):
        with bridge.acquire_agent_for_delegation("sentinel"):
            pass

    assert bridge.get_agent_lock("sentinel").locked() is False


def test_acquire_agent_for_delegation_releases_lock_when_body_raises(monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))

    with pytest.raises(ValueError):
        with bridge.acquire_agent_for_delegation("sentinel"):
            raise ValueError("boom from caller body")
    assert bridge.get_agent_lock("sentinel").locked() is False


def test_acquire_agent_for_delegation_converts_status_query_exception(monkeypatch):
    def boom(*a, **k):
        raise subprocess_module.TimeoutExpired(cmd="herdr", timeout=10)

    monkeypatch.setattr(bridge, "get_agent_status", boom)

    with pytest.raises(bridge.SentinelUnavailableError):
        with bridge.acquire_agent_for_delegation("sentinel"):
            pass

    assert bridge.get_agent_lock("sentinel").locked() is False


import http.client
import threading as threading_module
import time as time_module


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "DB_PATH", str(tmp_path / "tasks.db"))
    bridge.init_db()

    server = bridge.ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
    port = server.server_address[1]

    thread = threading_module.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield port

    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _one_agent_running(monkeypatch):
    """A herdr with at least one agent is the baseline the bridge assumes.

    Since SENTINEL_AGENT became optional, a request that names no agent
    asks the bridge to pick one, so most endpoints now need a live agent
    list to answer at all. Stating that once here keeps every unrelated
    test -- tokens, queue depth, timeouts -- about its own subject.

    Stubbed at list_agents rather than run_herdr on purpose: tests that
    assert the bridge makes no gratuitous run_herdr calls keep working.
    """
    monkeypatch.setattr(
        bridge,
        "list_agents",
        lambda: ([{"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"}], {"ok": True}),
    )


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = json_module.loads(resp.read().decode("utf-8"))
    conn.close()
    return resp.status, body


def test_health_endpoint_does_not_touch_sentinel(live_server, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("run_herdr must not be called by /health")

    monkeypatch.setattr(bridge, "run_herdr", boom)

    status, body = _get(live_server, "/health")

    assert status == 200
    assert body["ok"] is True


def test_ready_endpoint_reports_idle(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))

    status, body = _get(live_server, "/ready")

    assert status == 200
    assert body["ready"] is True
    assert body["agent_status"] == "idle"


def test_ready_endpoint_reports_busy(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("working", {"ok": True}))

    status, body = _get(live_server, "/ready")

    assert status == 200
    assert body["ready"] is False
    assert body["agent_status"] == "working"


def test_ready_endpoint_reports_unreachable(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: (None, {"ok": False}))

    status, body = _get(live_server, "/ready")

    assert status == 503
    assert body["ready"] is False


def test_tasks_list_empty(live_server):
    status, body = _get(live_server, "/tasks")

    assert status == 200
    assert body["tasks"] == []


def test_tasks_list_returns_created_tasks(live_server):
    task_id = bridge.create_task("check disk", 1000, "sentinel")

    status, body = _get(live_server, "/tasks")

    assert status == 200
    assert body["tasks"][0]["task_id"] == task_id


def test_task_get_not_found(live_server):
    status, body = _get(live_server, "/tasks/does-not-exist")

    assert status == 404
    assert body["ok"] is False


def test_task_get_found(live_server):
    task_id = bridge.create_task("check disk", 1000, "sentinel")

    status, body = _get(live_server, f"/tasks/{task_id}")

    assert status == 200
    assert body["task"]["task_id"] == task_id


def _post(port, path, payload):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = json_module.dumps(payload).encode("utf-8")
    conn.request(
        "POST", path, body=body,
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    data = json_module.loads(resp.read().decode("utf-8"))
    conn.close()
    return resp.status, data


def test_delegate_returns_202_and_queues_task(live_server):
    status, body = _post(live_server, "/delegate", {"task": "check disk"})

    assert status == 202
    assert body["ok"] is True
    assert body["status"] == "queued"

    task = bridge.get_task(body["task_id"])
    assert task["status"] == "queued"
    assert task["task"] == "check disk"


def test_delegate_rejects_empty_task(live_server):
    status, body = _post(live_server, "/delegate", {"task": "  "})

    assert status == 400
    assert body["ok"] is False


def test_delegate_defaults_timeout_to_six_hours(live_server):
    status, body = _post(live_server, "/delegate", {"task": "check disk"})

    task = bridge.get_task(body["task_id"])
    assert task["timeout_ms"] == 21600000


def test_delegate_does_not_check_busy_state(live_server, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("/delegate must not query Sentinel status")

    monkeypatch.setattr(bridge, "get_agent_status", boom)

    status, body = _post(live_server, "/delegate", {"task": "check disk"})

    assert status == 202


def test_ask_returns_409_when_busy(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("working", {"ok": True}))

    status, body = _post(live_server, "/ask", {"task": "do something"})

    assert status == 409
    assert body["status"] == "busy"
    assert body["agent_status"] == "working"


def test_ask_returns_503_when_sentinel_unreachable(live_server, monkeypatch):
    monkeypatch.setattr(
        bridge, "get_agent_status",
        lambda *a, **k: (None, {"ok": False, "error": "no route"}),
    )

    status, body = _post(live_server, "/ask", {"task": "do something"})

    assert status == 503
    assert body["status"] == "unavailable"


def test_ask_happy_path(live_server, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))
    monkeypatch.setattr(bridge, "run_herdr", compliant_agent("总结完成"))

    status, body = _post(live_server, "/ask", {"task": "do something"})

    assert status == 200
    assert body["result"]["text"] == "总结完成"


def test_prompt_returns_prompt_result_without_extracting_one(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))

    calls = []

    def fake_run_herdr(*args, **kwargs):
        calls.append(args[1])
        return {"ok": True, "stdout": "prompt output", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    status, body = _post(live_server, "/prompt", {"task": "do something"})

    assert status == 200
    # What comes back is herdr's own output; /prompt does not go looking for
    # a result in the terminal.
    assert body["prompt"]["stdout"] == "prompt output"
    # The two reads bracket the prompt and exist only to notice a provider
    # refusal that arrived during it -- see run_prompt_only().
    assert calls == ["read", "prompt", "read"]


def test_ask_rejects_empty_task(live_server):
    status, body = _post(live_server, "/ask", {"task": "   "})

    assert status == 400


def test_ask_returns_504_on_timeout(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))

    def fake_run_herdr(*args, **kwargs):
        raise subprocess_module.TimeoutExpired(cmd="herdr", timeout=1)

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    status, body = _post(live_server, "/ask", {"task": "do something"})

    assert status == 504
    assert body["ok"] is False
    assert "task_id" in body


def test_ask_returns_502_when_marker_missing(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": True, "stdout": "始终没有 marker", "stderr": "",
    })

    status, body = _post(live_server, "/ask", {"task": "do something"})

    assert status == 502
    assert body["ok"] is False
    assert "task_id" in body


def test_ask_returns_500_on_prompt_failure(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": False, "stdout": "", "stderr": "herdr exploded",
    })

    status, body = _post(live_server, "/ask", {"task": "do something"})

    assert status == 500
    assert body["ok"] is False
    assert "task_id" in body


def test_task_worker_picks_up_and_completes_queued_task(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "DB_PATH", str(tmp_path / "tasks.db"))
    bridge.init_db()
    monkeypatch.setattr(bridge, "WORKER_POLL_SECONDS", 0.02)
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(bridge, "run_herdr", compliant_agent("worker 完成"))

    task_id = bridge.create_task("后台任务", 5000, "sentinel")

    stop_event = threading_module.Event()
    worker_thread = threading_module.Thread(
        target=bridge.task_worker, args=(stop_event,), daemon=True
    )
    worker_thread.start()

    try:
        deadline = time_module.time() + 5
        task = bridge.get_task(task_id)

        while task["status"] not in ("done", "error", "orphaned") and time_module.time() < deadline:
            time_module.sleep(0.05)
            task = bridge.get_task(task_id)

        assert task["status"] == "done"
        assert task["result_text"] == "worker 完成"
    finally:
        stop_event.set()
        worker_thread.join(timeout=2)


def test_task_worker_skips_when_sentinel_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "DB_PATH", str(tmp_path / "tasks.db"))
    bridge.init_db()
    monkeypatch.setattr(bridge, "WORKER_POLL_SECONDS", 0.02)
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("working", {"ok": True}))

    task_id = bridge.create_task("后台任务", 5000, "sentinel")

    stop_event = threading_module.Event()
    worker_thread = threading_module.Thread(
        target=bridge.task_worker, args=(stop_event,), daemon=True
    )
    worker_thread.start()

    try:
        time_module.sleep(0.3)

        task = bridge.get_task(task_id)
        assert task["status"] == "queued"
    finally:
        stop_event.set()
        worker_thread.join(timeout=2)


def test_task_worker_survives_peek_next_task_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "DB_PATH", str(tmp_path / "tasks.db"))
    bridge.init_db()
    monkeypatch.setattr(bridge, "WORKER_POLL_SECONDS", 0.02)

    calls = {"count": 0}

    def flaky_peek(*a, **k):
        calls["count"] += 1
        if calls["count"] <= 3:
            raise sqlite3.OperationalError("simulated DB hiccup")
        return None

    monkeypatch.setattr(bridge, "peek_next_task", flaky_peek)

    stop_event = threading_module.Event()
    worker_thread = threading_module.Thread(
        target=bridge.task_worker, args=(stop_event,), daemon=True
    )
    worker_thread.start()

    try:
        deadline = time_module.time() + 2
        while calls["count"] < 4 and time_module.time() < deadline:
            time_module.sleep(0.02)

        assert calls["count"] >= 4  # the worker kept calling peek_next_task
        assert worker_thread.is_alive()  # and the thread never died
    finally:
        stop_event.set()
        worker_thread.join(timeout=2)


def test_do_get_returns_500_instead_of_crashing_on_unexpected_error(live_server, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(bridge, "get_agent_status", boom)

    status, body = _get(live_server, "/ready")

    assert status == 500
    assert body["ok"] is False


def test_do_post_returns_500_instead_of_crashing_on_unexpected_error(live_server, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated unexpected failure")

    # The handler enqueues through enqueue_task (which also reports whether
    # the request was a replay); this stubs whatever it actually calls.
    monkeypatch.setattr(bridge, "enqueue_task", boom)

    status, body = _post(live_server, "/delegate", {"task": "trigger the boom"})

    assert status == 500
    assert body["ok"] is False


def test_health_reports_worker_alive_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "DB_PATH", str(tmp_path / "tasks.db"))
    bridge.init_db()

    server = bridge.ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
    port = server.server_address[1]
    server_thread = threading_module.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    stop_event = threading_module.Event()
    monkeypatch.setattr(bridge, "WORKER_POLL_SECONDS", 0.02)
    worker_thread = threading_module.Thread(
        target=bridge.task_worker, args=(stop_event,), daemon=True
    )
    worker_thread.start()
    bridge._worker_thread = worker_thread

    try:
        status, body = _get(port, "/health")
        assert status == 200
        assert body["worker_alive"] is True
    finally:
        stop_event.set()
        worker_thread.join(timeout=2)
        bridge._worker_thread = None
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def test_execute_sentinel_task_recovery_timeout_raises_timeout_error(monkeypatch):
    def fake_run_herdr(*args, **kwargs):
        if args[1] == "prompt":
            if "120000" in args:
                # this is the recovery prompt call
                raise subprocess_module.TimeoutExpired(cmd="herdr", timeout=135)
            return {"ok": True, "stdout": "", "stderr": ""}
        return {"ok": True, "stdout": "没有 marker 的输出", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    with pytest.raises(TimeoutError):
        bridge.execute_sentinel_task(
            "sentinel", "55555555-5555-5555-5555-555555555555", "task", 60000
        )



def test_orphan_task_sets_status(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    task_id = bridge.create_task("t", 1000, "sentinel")
    bridge.claim_task(task_id)
    bridge.orphan_task(task_id, "bridge timeout")

    task = bridge.get_task(task_id)
    assert task["status"] == "orphaned"
    assert task["error_text"] == "bridge timeout"


def test_task_worker_orphans_on_recovery_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "DB_PATH", str(tmp_path / "tasks.db"))
    bridge.init_db()
    monkeypatch.setattr(bridge, "WORKER_POLL_SECONDS", 0.02)
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))

    def fake_run_herdr(*args, **kwargs):
        if args[1] == "prompt":
            if "120000" in args:
                raise subprocess_module.TimeoutExpired(cmd="herdr", timeout=135)
            return {"ok": True, "stdout": "", "stderr": ""}
        return {"ok": True, "stdout": "没有 marker", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    task_id = bridge.create_task("会在恢复阶段超时的任务", 5000, "sentinel")

    stop_event = threading_module.Event()
    worker_thread = threading_module.Thread(
        target=bridge.task_worker, args=(stop_event,), daemon=True
    )
    worker_thread.start()

    try:
        deadline = time_module.time() + 5
        task = bridge.get_task(task_id)

        while task["status"] not in ("done", "error", "orphaned") and time_module.time() < deadline:
            time_module.sleep(0.05)
            task = bridge.get_task(task_id)

        assert task["status"] == "orphaned"
    finally:
        stop_event.set()
        worker_thread.join(timeout=2)


def test_check_auth_allows_everything_when_no_token_configured(monkeypatch):
    monkeypatch.setattr(bridge, "AUTH_TOKEN", "")

    handler = bridge.Handler.__new__(bridge.Handler)
    handler.headers = {}

    assert handler.check_auth() is True


def test_check_auth_rejects_missing_header_when_token_configured(monkeypatch):
    monkeypatch.setattr(bridge, "AUTH_TOKEN", "s3cret")

    handler = bridge.Handler.__new__(bridge.Handler)
    handler.headers = {}

    assert handler.check_auth() is False


def test_check_auth_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(bridge, "AUTH_TOKEN", "s3cret")

    handler = bridge.Handler.__new__(bridge.Handler)
    handler.headers = {"X-Sentinel-Token": "wrong"}

    assert handler.check_auth() is False


def test_check_auth_accepts_correct_token(monkeypatch):
    monkeypatch.setattr(bridge, "AUTH_TOKEN", "s3cret")

    handler = bridge.Handler.__new__(bridge.Handler)
    handler.headers = {"X-Sentinel-Token": "s3cret"}

    assert handler.check_auth() is True


def test_check_auth_returns_false_instead_of_raising_on_non_ascii_token(monkeypatch):
    monkeypatch.setattr(bridge, "AUTH_TOKEN", "sécret")

    handler = bridge.Handler.__new__(bridge.Handler)
    handler.headers = {"X-Sentinel-Token": "sécret"}

    assert handler.check_auth() is False


def test_health_does_not_require_auth(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "AUTH_TOKEN", "s3cret")

    status, body = _get(live_server, "/health")

    assert status == 200
    assert body["ok"] is True


def test_ready_requires_auth_when_token_configured(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "AUTH_TOKEN", "s3cret")

    status, body = _get(live_server, "/ready")

    assert status == 401
    assert body["ok"] is False


def test_delegate_requires_auth_when_token_configured(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "AUTH_TOKEN", "s3cret")

    status, body = _post(live_server, "/delegate", {"task": "should be rejected"})

    assert status == 401
    assert body["ok"] is False


def test_delegate_succeeds_with_correct_token(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "AUTH_TOKEN", "s3cret")

    conn = http.client.HTTPConnection("127.0.0.1", live_server, timeout=5)
    payload = json_module.dumps({"task": "authorized request"}).encode("utf-8")
    conn.request(
        "POST", "/delegate", body=payload,
        headers={
            "Content-Type": "application/json",
            "X-Sentinel-Token": "s3cret",
        },
    )
    resp = conn.getresponse()
    status = resp.status
    body = json_module.loads(resp.read().decode("utf-8"))
    conn.close()

    assert status == 202
    assert body["ok"] is True


def test_delegate_accepts_lowercase_token_header(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "AUTH_TOKEN", "s3cret")

    conn = http.client.HTTPConnection("127.0.0.1", live_server, timeout=5)
    payload = json_module.dumps({"task": "lowercase header test"}).encode("utf-8")
    conn.request(
        "POST", "/delegate", body=payload,
        headers={
            "Content-Type": "application/json",
            "x-sentinel-token": "s3cret",
        },
    )
    resp = conn.getresponse()
    status = resp.status
    body = json_module.loads(resp.read().decode("utf-8"))
    conn.close()

    assert status == 202
    assert body["ok"] is True


# -- timeout_ms range validation -------------------------------------------

def test_delegate_rejects_timeout_ms_below_minimum(live_server):
    status, body = _post(
        live_server, "/delegate", {"task": "check disk", "timeout_ms": 500}
    )

    assert status == 400
    assert body["ok"] is False


def test_delegate_rejects_timeout_ms_above_maximum(live_server):
    status, body = _post(
        live_server,
        "/delegate",
        {"task": "check disk", "timeout_ms": 99_999_999_999},
    )

    assert status == 400
    assert body["ok"] is False


def test_delegate_accepts_timeout_ms_at_boundaries(live_server):
    status, body = _post(
        live_server,
        "/delegate",
        {"task": "check disk", "timeout_ms": bridge.TIMEOUT_MS_MIN},
    )
    assert status == 202

    status, body = _post(
        live_server,
        "/delegate",
        {"task": "check disk", "timeout_ms": bridge.TIMEOUT_MS_MAX},
    )
    assert status == 202


def test_ask_rejects_timeout_ms_out_of_range_without_touching_sentinel(
    live_server, monkeypatch
):
    def boom(*args, **kwargs):
        raise AssertionError(
            "run_herdr must not be called when timeout_ms fails validation"
        )

    monkeypatch.setattr(bridge, "run_herdr", boom)

    status, body = _post(
        live_server, "/ask", {"task": "check disk", "timeout_ms": 0}
    )

    assert status == 400
    assert body["ok"] is False


# -- /delegate queue depth limit --------------------------------------------

def test_delegate_rejects_when_queue_is_full(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "MAX_QUEUE_DEPTH", 2)

    first = _post(live_server, "/delegate", {"task": "task 1"})
    second = _post(live_server, "/delegate", {"task": "task 2"})
    assert first[0] == 202
    assert second[0] == 202

    status, body = _post(live_server, "/delegate", {"task": "task 3"})

    assert status == 429
    assert body["ok"] is False


def test_delegate_succeeds_again_once_queue_has_room(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "MAX_QUEUE_DEPTH", 1)

    first = _post(live_server, "/delegate", {"task": "task 1"})
    assert first[0] == 202

    blocked = _post(live_server, "/delegate", {"task": "task 2"})
    assert blocked[0] == 429

    bridge.complete_task(first[1]["task_id"], "done")

    status, body = _post(live_server, "/delegate", {"task": "task 3"})
    assert status == 202


# -- non-ASCII SENTINEL_BRIDGE_TOKEN startup warning ------------------------

def test_auth_token_warning_when_not_configured(monkeypatch):
    monkeypatch.setattr(bridge, "AUTH_TOKEN", "")

    warning = bridge.auth_token_warning()

    assert warning is not None
    assert "NO AUTHENTICATION" in warning


def test_auth_token_warning_when_non_ascii(monkeypatch):
    monkeypatch.setattr(bridge, "AUTH_TOKEN", "sécret")

    warning = bridge.auth_token_warning()

    assert warning is not None
    assert "non-ASCII" in warning


def test_auth_token_warning_when_valid_ascii_token(monkeypatch):
    monkeypatch.setattr(bridge, "AUTH_TOKEN", "s3cret")

    assert bridge.auth_token_warning() is None


# -- multi-agent support ----------------------------------------------------

def test_init_db_migrates_pre_multi_agent_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(bridge, "DB_PATH", str(db_path))
    monkeypatch.setattr(bridge, "DEFAULT_AGENT", "legacy-default")

    # Build a DB with the schema from before the `agent` column existed,
    # with one row already in it -- init_db() must add the column and
    # backfill it, not just work on a fresh DB.
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            task TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            timeout_ms INTEGER NOT NULL,
            result_text TEXT,
            error_text TEXT
        )
    """)
    conn.execute(
        "INSERT INTO tasks (task_id, task, status, created_at, timeout_ms) "
        "VALUES ('pre-existing', 'old task', 'queued', '2026-01-01', 1000)"
    )
    conn.commit()
    conn.close()

    bridge.init_db()

    task = bridge.get_task("pre-existing")
    assert task["agent"] == "legacy-default"


def test_create_task_stores_agent(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    task_id = bridge.create_task("check disk", 1000, "agent-a")
    task = bridge.get_task(task_id)

    assert task["agent"] == "agent-a"


def test_get_agent_lock_returns_same_lock_for_same_name():
    assert bridge.get_agent_lock("agent-x") is bridge.get_agent_lock("agent-x")


def test_get_agent_lock_returns_different_locks_for_different_names():
    assert bridge.get_agent_lock("agent-x") is not bridge.get_agent_lock("agent-y")


def test_acquire_agent_for_delegation_does_not_block_a_different_agent(monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))

    with bridge.acquire_agent_for_delegation("agent-a"):
        # A concurrent request against a completely different agent must
        # not be blocked by agent-a's lock -- that's the entire point of
        # per-agent locks over one global AGENT_LOCK.
        entered = False
        with bridge.acquire_agent_for_delegation("agent-b"):
            entered = True

        assert entered is True

    assert bridge.get_agent_lock("agent-a").locked() is False
    assert bridge.get_agent_lock("agent-b").locked() is False


def test_agents_endpoint_returns_parsed_list(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "list_agents", _REAL_LIST_AGENTS)
    fake_agents = [
        {"name": "sentinel-opencode", "agent_status": "working", "pane_id": "w1:p3"},
        {"name": "sentinel", "agent_status": "idle", "pane_id": "w1:p9"},
    ]

    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": True,
        "stdout": json_module.dumps({"result": {"agents": fake_agents, "type": "agent_list"}}),
        "stderr": "",
    })

    status, body = _get(live_server, "/agents")

    assert status == 200
    assert body["ok"] is True
    assert body["agents"] == fake_agents


def test_agents_endpoint_returns_503_on_herdr_failure(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "list_agents", _REAL_LIST_AGENTS)
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": False, "stdout": "", "stderr": "herdr not reachable",
    })

    status, body = _get(live_server, "/agents")

    assert status == 503
    assert body["ok"] is False


def test_delegate_stores_explicit_agent(live_server, monkeypatch):
    _live(monkeypatch, [{"name": "agent-a", "agent": "opencode", "agent_status": "idle"}])
    status, body = _post(
        live_server, "/delegate", {"task": "check disk", "agent": "agent-a"}
    )

    assert status == 202
    task = bridge.get_task(body["task_id"])
    assert task["agent"] == "agent-a"


def test_delegate_defaults_agent_when_not_specified(live_server):
    status, body = _post(live_server, "/delegate", {"task": "check disk"})

    # With SENTINEL_AGENT unset the bridge picks a live agent rather than
    # storing a placeholder name that may never have existed.
    task = bridge.get_task(body["task_id"])
    assert task["agent"] == "w1:p1"


def test_ask_uses_explicit_agent(live_server, tmp_path, monkeypatch):
    _live(monkeypatch, [{"name": "agent-a", "agent": "opencode", "agent_status": "idle"}])
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))

    calls = []
    writes_result = compliant_agent("done")

    def fake_run_herdr(*args, **kwargs):
        calls.append(args)
        return writes_result(*args, **kwargs)

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    status, body = _post(
        live_server, "/ask", {"task": "do something", "agent": "agent-a"}
    )

    assert status == 200
    # every run_herdr call's third positional arg is the agent name
    assert all(call[2] == "agent-a" for call in calls)


def test_ready_endpoint_respects_agent_query_param(live_server, monkeypatch):
    _live(monkeypatch, [{"name": "agent-a", "agent": "opencode", "agent_status": "idle"}])
    seen = {}

    def fake_get_agent_status(agent_name):
        seen["agent_name"] = agent_name
        return "idle", {"ok": True}

    monkeypatch.setattr(bridge, "get_agent_status", fake_get_agent_status)

    status, body = _get(live_server, "/ready?agent=agent-a")

    assert status == 200
    assert seen["agent_name"] == "agent-a"


def test_ready_endpoint_defaults_agent_when_no_query_param(live_server, monkeypatch):
    seen = {}

    def fake_get_agent_status(agent_name):
        seen["agent_name"] = agent_name
        return "idle", {"ok": True}

    monkeypatch.setattr(bridge, "get_agent_status", fake_get_agent_status)

    _get(live_server, "/ready")

    assert seen["agent_name"] == "w1:p1"


def test_health_reports_default_agent(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "DEFAULT_AGENT", "agent-a")

    status, body = _get(live_server, "/health")

    assert status == 200
    assert body["default_agent"] == "agent-a"
    assert body["agent"] == "agent-a"
    assert body["version"] == bridge.BRIDGE_VERSION


def test_delegate_rejects_invalid_agent_names(live_server):
    # None is deliberately absent: an explicit null now means "no
    # preference", the same as omitting the field, and the bridge picks.
    for agent_name in ("", "   ", ["agent-a"]):
        status, body = _post(
            live_server,
            "/delegate",
            {"task": "check disk", "agent": agent_name},
        )

        assert status == 400
        assert body["ok"] is False
        assert "agent" in body["error"]


def test_ask_rejects_invalid_agent_without_touching_herdr(live_server, monkeypatch):
    def should_not_run(*args, **kwargs):
        raise AssertionError("invalid agent must be rejected before Herdr is queried")

    monkeypatch.setattr(bridge, "run_herdr", should_not_run)

    status, body = _post(
        live_server,
        "/ask",
        {"task": "check disk", "agent": "   "},
    )

    assert status == 400
    assert body["ok"] is False
    assert "agent" in body["error"]


def test_ready_rejects_blank_agent_query_without_touching_herdr(live_server, monkeypatch):
    def should_not_run(*args, **kwargs):
        raise AssertionError("invalid agent must be rejected before Herdr is queried")

    monkeypatch.setattr(bridge, "run_herdr", should_not_run)

    status, body = _get(live_server, "/ready?agent=%20%20")

    assert status == 400
    assert body["ok"] is False
    assert "agent" in body["error"]


def test_read_accepts_lines_query(live_server, monkeypatch):
    _live(monkeypatch, [{"name": "agent-a", "agent": "opencode", "agent_status": "idle"}])
    calls = []

    def fake_run_herdr(*args, **kwargs):
        calls.append(args)
        return {"ok": True, "stdout": "output", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    status, body = _get(live_server, "/read?agent=agent-a&lines=37")

    assert status == 200
    assert body["ok"] is True
    assert ("agent", "read", "agent-a", "--source", "recent-unwrapped", "--lines", "37") in calls

    # /read also fetches the agent's status now -- a second herdr call
    # this endpoint did not used to make. Justified rather than excused:
    # a terminal snapshot on its own was read as live activity by a real
    # caller, and /read is a diagnostic endpoint rather than something
    # polled in a loop, where the extra call would be worth avoiding.
    assert [call[1] for call in calls] == ["read", "get"]


def test_read_rejects_lines_out_of_range_without_touching_herdr(live_server, monkeypatch):
    def should_not_run(*args, **kwargs):
        raise AssertionError("invalid lines must be rejected before Herdr is queried")

    monkeypatch.setattr(bridge, "run_herdr", should_not_run)

    status, body = _get(live_server, "/read?lines=0")

    assert status == 400
    assert body["ok"] is False
    assert "lines" in body["error"]


def test_create_task_if_queue_available_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(bridge, "MAX_QUEUE_DEPTH", 1)
    bridge.init_db()

    start = threading_module.Barrier(8)
    task_ids = []
    full_errors = []

    def enqueue(index):
        start.wait()
        try:
            task_ids.append(
                bridge.create_task_if_queue_available(
                    f"task {index}", 5000, "agent-a"
                )
            )
        except bridge.QueueFullError as exc:
            full_errors.append(exc)

    threads = [
        threading_module.Thread(target=enqueue, args=(index,))
        for index in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(task_ids) == 1
    assert len(full_errors) == 7
    assert bridge.count_queued_tasks() == 1


def test_task_worker_skips_busy_agent_to_run_a_different_idle_agents_task(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(bridge, "DB_PATH", str(tmp_path / "tasks.db"))
    bridge.init_db()
    monkeypatch.setattr(bridge, "WORKER_POLL_SECONDS", 0.02)

    def fake_get_agent_status(agent_name):
        # agent-busy never becomes available; agent-idle always is.
        return ("working", {"ok": True}) if agent_name == "agent-busy" else ("idle", {"ok": True})

    monkeypatch.setattr(bridge, "get_agent_status", fake_get_agent_status)
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(bridge, "run_herdr", compliant_agent("done"))

    # The busy agent's task is queued first (oldest), the idle agent's
    # task second -- a naive "always take the oldest queued task" worker
    # would starve the second task forever behind the first.
    busy_task_id = bridge.create_task("stuck behind busy agent", 5000, "agent-busy")
    idle_task_id = bridge.create_task("should run despite being queued second", 5000, "agent-idle")

    stop_event = threading_module.Event()
    worker_thread = threading_module.Thread(
        target=bridge.task_worker, args=(stop_event,), daemon=True
    )
    worker_thread.start()

    try:
        deadline = time_module.time() + 5
        idle_task = bridge.get_task(idle_task_id)

        while idle_task["status"] == "queued" and time_module.time() < deadline:
            time_module.sleep(0.05)
            idle_task = bridge.get_task(idle_task_id)

        assert idle_task["status"] == "done"
        # the busy agent's task must still be untouched -- proves it was
        # skipped, not silently dropped or run out of order incorrectly
        assert bridge.get_task(busy_task_id)["status"] == "queued"
    finally:
        stop_event.set()
        worker_thread.join(timeout=2)


# -- file-based result exchange ---------------------------------------------
#
# The agent writes its answer to a file instead of printing it into the
# terminal for the bridge to scrape back out. Verified against a real herdr:
# agent.prompt's own response carries no reply text, and pane.read comes back
# truncated and full of TUI chrome the agent never said, so the terminal is
# the wrong channel for the result. See experiments/FINDINGS.md.


def test_result_file_path_is_under_result_dir_and_keyed_by_token(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    task_id = "11111111-2222-3333-4444-555555555555"
    path = bridge.result_file_path(task_id)

    assert path.startswith(str(tmp_path))
    assert task_id.replace("-", "") in path


def test_delegation_prompt_names_the_result_file_and_drops_the_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    task_id = "11111111-2222-3333-4444-555555555555"
    prompt = bridge.build_delegation_prompt("检查磁盘", task_id)

    assert "检查磁盘" in prompt
    assert bridge.result_file_path(task_id) in prompt
    # the whole point: no more "last line must be SENTINEL_DONE_<token>"
    assert "SENTINEL_DONE_" not in prompt


def test_read_result_file_returns_content_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    task_id = "22222222-2222-2222-2222-222222222222"
    path = bridge.result_file_path(task_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write("  磁盘使用率 42%  \n")

    assert bridge.read_result_file(task_id) == "磁盘使用率 42%"
    assert not os.path.exists(path)


def test_read_result_file_returns_none_when_agent_never_wrote_it(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    assert bridge.read_result_file("33333333-3333-3333-3333-333333333333") is None


def test_read_result_file_treats_an_empty_file_as_no_result(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    task_id = "44444444-4444-4444-4444-444444444444"
    with open(bridge.result_file_path(task_id), "w", encoding="utf-8") as f:
        f.write("   \n")

    assert bridge.read_result_file(task_id) is None


def test_execute_sentinel_task_returns_the_file_contents(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    task_id = "55555555-5555-5555-5555-555555555555"

    def fake_run_herdr(*args, **kwargs):
        # the agent "writes" its result the moment it is prompted
        with open(bridge.result_file_path(task_id), "w", encoding="utf-8") as f:
            f.write("干净的结果，没有终端噪音")
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    result = bridge.execute_sentinel_task("sentinel", task_id, "do a thing", 60000)

    assert result == "干净的结果，没有终端噪音"


def test_execute_sentinel_task_never_reads_the_terminal_on_the_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    task_id = "66666666-6666-6666-6666-666666666666"
    calls = []

    def fake_run_herdr(*args, **kwargs):
        calls.append(args[1])
        with open(bridge.result_file_path(task_id), "w", encoding="utf-8") as f:
            f.write("ok")
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    bridge.execute_sentinel_task("sentinel", task_id, "task", 60000)

    assert calls == ["prompt"]  # no "read" -- scraping is off the critical path


def test_execute_sentinel_task_reminds_once_when_the_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    task_id = "77777777-7777-7777-7777-777777777777"
    prompts = []

    def fake_run_herdr(*args, **kwargs):
        if args[1] == "prompt":
            prompts.append(args[3])
            if len(prompts) == 2:
                # the agent complies with the reminder
                with open(bridge.result_file_path(task_id), "w", encoding="utf-8") as f:
                    f.write("补写的结果")
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    result = bridge.execute_sentinel_task("sentinel", task_id, "task", 60000)

    assert result == "补写的结果"
    assert len(prompts) == 2
    # the reminder must not ask for the work to be redone
    assert "不要重新执行" in prompts[1]


def test_execute_sentinel_task_raises_when_still_missing_after_the_reminder(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": True, "stdout": "终端里最后的原始输出", "stderr": "",
    })

    with pytest.raises(bridge.SentinelResultMissingError) as exc_info:
        bridge.execute_sentinel_task(
            "sentinel", "88888888-8888-8888-8888-888888888888", "task", 60000
        )

    # a terminal tail is still attached, but only as failure diagnostics
    assert "终端里最后的原始输出" in exc_info.value.raw_output


def test_execute_sentinel_task_survives_a_failing_diagnostic_read(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    def fake_run_herdr(*args, **kwargs):
        if args[1] == "read":
            raise subprocess_module.TimeoutExpired(cmd="herdr", timeout=60)
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    # the missing result is the real error; a broken diagnostic read must not
    # mask it with a TimeoutError
    with pytest.raises(bridge.SentinelResultMissingError):
        bridge.execute_sentinel_task(
            "sentinel", "99999999-9999-9999-9999-999999999999", "task", 60000
        )


def test_delegation_prompt_does_not_explain_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    prompt = bridge.build_delegation_prompt(
        "只读探测，不要修改任何文件", "aaaaaaaa-1111-2222-3333-444444444444"
    )

    # The prompt used to carry two justifications: that terminal output is
    # never read, and that the result file is exempt from a task's own
    # "don't modify files" restriction. An A/B against a live agent removed
    # the grounds for both. Without the first, it wrote the file anyway with
    # no hesitation. Without the second, given a task that did say "don't
    # modify any files", it spotted the conflict and resolved it unaided --
    # "The result file itself is the delivery method ... (delivery file is
    # exempt)" -- reaching the same conclusion the sentence used to state,
    # then wrote the file. Neither changed behaviour, so both are gone: an
    # instruction the agent follows does not need a rationale attached.
    assert "不约束" not in prompt
    assert "终端" not in prompt
    assert "交付通道" not in prompt


def test_delegation_prompt_does_not_negotiate_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    prompt = bridge.build_delegation_prompt(
        "检查磁盘", "aaaaaaaa-1111-2222-3333-444444444444"
    )

    # RESULT_DIR used to default to /tmp -- outside the agents' cwd, so every
    # agent permission system flagged the write, and the prompt grew a
    # paragraph coaching the agent to retry past its own guardrails. The fix
    # for that was to move the directory (deployments point SENTINEL_RESULT_DIR
    # at a path under the agents' cwd), not to keep the coaching. Writing a
    # file in your own working directory is not a permission event; a prompt
    # that says otherwise invites the agent to treat it as one.
    assert "权限" not in prompt
    assert "拒绝" not in prompt
    assert "拦" not in prompt


def test_prompts_do_not_prescribe_how_to_write_the_result(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    task_id = "aaaaaaaa-1111-2222-3333-444444444444"
    prompts = [
        bridge.build_delegation_prompt("检查磁盘", task_id),
        bridge.build_result_reminder_prompt(task_id),
    ]

    # A prescribed `cat > ... <<EOF` reads as "run a shell command", which is
    # exactly the shape an auto-approval classifier stops. The agent's own
    # file-write tool is both simpler and less likely to be interrupted, so
    # neither prompt should name a mechanism at all.
    for prompt in prompts:
        assert "cat >" not in prompt
        assert "EOF" not in prompt


def test_missing_result_error_leads_with_the_likeliest_cause(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    # This is the message for a turn that ran for a while and then lost its
    # file. A fake that returns instantly would otherwise land in the
    # "ended too quickly to have run" branch, which words it differently.
    monkeypatch.setattr(bridge, "QUICK_END_SEC", 0)
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": True, "stdout": "terminal tail", "stderr": "",
    })

    with pytest.raises(bridge.SentinelResultMissingError) as exc_info:
        bridge.execute_sentinel_task(
            "sentinel", "bbbbbbbb-1111-2222-3333-444444444444", "task", 60000
        )

    message = str(exc_info.value)

    # This message used to open by naming a denied write as "the most likely
    # cause", which was never measured -- and pointed the reader away from
    # the one thing that is actually in hand. Send them to the evidence
    # first and keep the permission angle as a recurrence check; the bridge
    # cannot tell these cases apart, so it should not rank them.
    assert "Read the terminal tail below" in message
    assert str(tmp_path) in message
    assert "likely cause" not in message
    assert message.index("terminal tail") < message.index("permission")


def test_reminder_is_logged_so_the_hit_rate_is_observable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    task_id = "cccccccc-1111-2222-3333-444444444444"
    prompts = []

    def fake_run_herdr(*args, **kwargs):
        if args[1] == "prompt":
            prompts.append(args[3])
            if len(prompts) == 2:
                with open(bridge.result_file_path(task_id), "w", encoding="utf-8") as f:
                    f.write("written on the second ask")
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    bridge.execute_sentinel_task("sentinel", task_id, "task", 60000)

    # An agent whose write got denied still succeeds, but it costs a whole
    # extra prompt. That has to show up somewhere operators can count it,
    # rather than only being visible by interrogating the agent afterwards.
    assert "reminder" in capsys.readouterr().out.lower()


# -- provider quota failover -------------------------------------------------

def test_quota_error_detail_recognises_claude_and_opencode_failures():
    assert bridge.quota_error_detail(
        "Claude usage limit reached; resets in 4 hours"
    )
    assert bridge.quota_error_detail(
        "OpenCode API error 402: insufficient balance"
    )
    assert bridge.quota_error_detail("ordinary permission denied") is None


def test_run_prompt_marks_quota_error_from_herdr_output(monkeypatch):
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": False,
        "stdout": "",
        "stderr": "HTTP 429: rate limit exceeded",
    })

    with pytest.raises(bridge.AgentQuotaExhaustedError) as exc_info:
        bridge._run_herdr_prompt("sentinel-opencode", "task", 1000)

    assert exc_info.value.agent_name == "sentinel-opencode"


def test_missing_result_quota_does_not_send_recovery_prompt(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    def fake_run_herdr(*args, **kwargs):
        calls.append(args)
        if args[1] == "read":
            return {
                "ok": True,
                "stdout": "OpenCode API: insufficient credits",
                "stderr": "",
            }
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    with pytest.raises(bridge.AgentQuotaExhaustedError):
        bridge.execute_sentinel_task("sentinel-opencode", "1", "task", 1000)

    assert len([call for call in calls if call[1] == "prompt"]) == 1


def test_failover_switches_to_a_different_runtime_and_blocks_primary(
    tmp_path, monkeypatch
):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "list_agents", lambda: (
        [
            {"name": "sentinel-opencode", "agent": "opencode"},
            {"name": "sentinel-claude", "agent": "claude"},
        ],
        {"ok": True},
    ))
    monkeypatch.setattr(
        bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True})
    )
    attempted = []

    def operation(agent_name):
        attempted.append(agent_name)
        if agent_name == "sentinel-opencode":
            raise bridge.AgentQuotaExhaustedError(
                agent_name, "OpenCode API: insufficient balance"
            )
        return "completed by fallback"

    result, used_agent = bridge.run_with_quota_failover(
        "sentinel-opencode", operation
    )

    assert result == "completed by fallback"
    assert used_agent == "sentinel-claude"
    assert attempted == ["sentinel-opencode", "sentinel-claude"]
    assert bridge.get_agent_quota_block("sentinel-opencode") is not None


def test_failover_never_auto_selects_same_runtime_family(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "list_agents", lambda: (
        [
            {"name": "opencode-a", "agent": "opencode"},
            {"name": "opencode-b", "agent": "opencode"},
        ],
        {"ok": True},
    ))
    monkeypatch.setattr(
        bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True})
    )

    with pytest.raises(bridge.QuotaFailoverExhaustedError):
        bridge.run_with_quota_failover(
            "opencode-a",
            lambda agent: (_ for _ in ()).throw(
                bridge.AgentQuotaExhaustedError(agent, "credits exhausted")
            ),
        )


def test_quota_block_can_be_listed_and_cleared(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    bridge.mark_agent_quota_blocked("sentinel-claude", "5h limit reached")

    assert [row["agent"] for row in bridge.list_agent_quota_blocks()] == [
        "sentinel-claude"
    ]
    assert bridge.clear_agent_quota_blocks("sentinel-claude") == 1
    assert bridge.list_agent_quota_blocks() == []


def test_quota_endpoints_list_and_reset(live_server, monkeypatch):
    bridge.mark_agent_quota_blocked("sentinel-opencode", "credits exhausted")

    status, body = _get(live_server, "/quota")
    assert status == 200
    assert body["blocked_agents"][0]["agent"] == "sentinel-opencode"

    status, body = _post(
        live_server, "/quota/reset", {"agent": "sentinel-opencode"}
    )
    assert status == 200
    assert body["cleared"] == 1


# -- quota detection precision ----------------------------------------------
#
# A match opens a *durable* circuit in quota_blocks that only an operator can
# clear, so a false positive takes an agent out of service until a human
# notices. False negatives merely cost one wasted prompt. The pattern is
# therefore tuned to prefer misses over mistakes.


def test_quota_detection_ignores_bare_status_like_numbers():
    # 429/402 turn up constantly in ordinary HPC output -- job ids, row
    # counts, sizes. On their own they say nothing about a provider quota.
    assert bridge.quota_error_detail(
        "JOBID PARTITION NAME ST TIME NODES\n429 large train R 1:02:03 4"
    ) is None
    assert bridge.quota_error_detail("processed 402 files, 0 errors") is None


def test_quota_detection_ignores_ordinary_english_prose():
    assert bridge.quota_error_detail(
        "If the node is busy, try again in a few minutes."
    ) is None
    assert bridge.quota_error_detail(
        "Investigate the API rate limit handling in our client code"
    ) is None


def test_quota_detection_recognises_chinese_provider_errors():
    # The reference deployment's OpenCode proxy reports in Chinese. This is
    # verbatim the only real quota failure this deployment has produced, and
    # the original English-only pattern missed it entirely.
    assert bridge.quota_error_detail(
        "预扣费额度失败, 用户剩余额度: 0.289294, 需要预扣费额度: 0.311940"
    )
    assert bridge.quota_error_detail("API 返回：余额不足，请充值")


def test_quota_detection_ignores_the_delegated_task_text():
    # The terminal echoes the task back, so a task *about* rate limits must
    # not read as the agent having hit one.
    task = "排查我们客户端的 rate limit exceeded 处理逻辑"
    terminal = f"user: {task}\nagent: 好的，我先看看代码"

    assert bridge.quota_error_detail(terminal) is not None  # would false-positive
    assert bridge.quota_error_detail(terminal, ignore=task) is None


def test_missing_result_diagnostics_cover_the_reminder_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    reads = []

    def fake_run_herdr(*args, **kwargs):
        if args[1] == "read":
            reads.append(len(reads))
            return {"ok": True, "stdout": f"terminal state #{len(reads)}", "stderr": ""}
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    with pytest.raises(bridge.SentinelResultMissingError) as exc_info:
        bridge.execute_sentinel_task(
            "sentinel", "dddddddd-1111-2222-3333-444444444444", "task", 60000
        )

    # The whole question when this error fires is "what happened during the
    # reminder", so the attached tail has to be read after it, not before.
    assert "#2" in exc_info.value.raw_output


# --- stale result files -------------------------------------------------
#
# read_result_file() only removes a file it managed to read, so every
# timeout, orphaned task and delivery failure leaves one behind. That was
# survivable while RESULT_DIR defaulted under the system temp dir, which
# the OS eventually reclaims; deployments now point it at project storage,
# which nothing reclaims. Observed live: two abandoned files, one of them
# with no surviving record of which task it belonged to.


def _age_file(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_purge_stale_result_files_removes_only_the_expired_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "RESULT_RETENTION_DAYS", 7)

    fresh = tmp_path / "result-aaaa.txt"
    stale = tmp_path / "result-bbbb.txt"
    fresh.write_text("fresh", encoding="utf-8")
    stale.write_text("stale", encoding="utf-8")
    _age_file(stale, 8)

    assert bridge.purge_stale_result_files() == 1
    assert fresh.exists()
    assert not stale.exists()


def test_purge_stale_result_files_ignores_files_it_does_not_own(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "RESULT_RETENTION_DAYS", 7)

    # An operator's own notes, or another tool's state, must survive -- the
    # bridge only ever created files matching its own result-*.txt naming.
    intruder = tmp_path / "notes.txt"
    intruder.write_text("do not delete", encoding="utf-8")
    _age_file(intruder, 400)

    assert bridge.purge_stale_result_files() == 0
    assert intruder.exists()


def test_purge_stale_result_files_survives_a_missing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path / "not-created-yet"))
    monkeypatch.setattr(bridge, "RESULT_RETENTION_DAYS", 7)

    # Runs at startup, before the first task creates the directory. A
    # housekeeping step must never be the reason the bridge fails to boot.
    assert bridge.purge_stale_result_files() == 0


def test_purge_stale_result_files_is_disabled_by_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "RESULT_RETENTION_DAYS", 0)

    stale = tmp_path / "result-cccc.txt"
    stale.write_text("stale", encoding="utf-8")
    _age_file(stale, 999)

    assert bridge.purge_stale_result_files() == 0
    assert stale.exists()


# --- result dir vs agent cwd -------------------------------------------


def _agent_list(monkeypatch, agents):
    monkeypatch.setattr(bridge, "list_agents", lambda: (agents, {"ok": True}))


def test_result_dir_scope_warning_fires_when_outside_every_agent_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path / "somewhere-else"))
    _agent_list(monkeypatch, [{"name": "a", "cwd": str(tmp_path / "project")}])

    warning = bridge.result_dir_scope_warning()

    # This is the configuration that made agents' permission systems flag
    # the write, after the work was already done -- the exact "莫名的中断"
    # this check exists to make visible at boot instead of mid-task.
    assert warning is not None
    assert "SENTINEL_RESULT_DIR" in warning


def test_result_dir_scope_warning_silent_when_inside_an_agent_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path / "project" / "results"))
    _agent_list(monkeypatch, [{"name": "a", "cwd": str(tmp_path / "project")}])

    assert bridge.result_dir_scope_warning() is None


def test_result_dir_scope_warning_names_the_agents_it_is_outside_of(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path / "one" / "results"))
    _agent_list(monkeypatch, [
        {"name": "inside", "cwd": str(tmp_path / "one")},
        {"name": "outside", "cwd": str(tmp_path / "two")},
    ])

    warning = bridge.result_dir_scope_warning()

    # Partial coverage is the dangerous case: it works until routing picks
    # the other agent, so the warning has to name who is affected.
    assert warning is not None
    assert "outside" in warning
    assert "inside" not in warning.replace("outside", "")


def test_result_dir_scope_warning_stays_quiet_when_herdr_is_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "list_agents", lambda: (None, {"ok": False}))

    # herdr not running yet is normal at boot. An advisory check must not
    # invent a warning out of missing information.
    assert bridge.result_dir_scope_warning() is None


# --- agent identifier resolution ---------------------------------------
#
# herdr agent names do not survive a session rebuild, and pane ids shift
# when panes are recreated. Observed live: an operator restarted the herdr
# session and rebuilt both agent windows without renaming them, so
# SENTINEL_AGENT=sentinel-opencode stopped resolving and every call that
# did not name a pane id explicitly failed -- while /health, /agents and
# the worker all still looked perfectly healthy.


def _live(monkeypatch, agents, ok=True):
    monkeypatch.setattr(bridge, "list_agents", lambda: (agents if ok else None, {"ok": ok}))


def test_resolve_agent_prefers_an_exact_name(monkeypatch):
    _live(monkeypatch, [
        {"name": "sentinel-opencode", "agent": "opencode", "pane_id": "w1:p1"},
    ])

    assert bridge.resolve_agent("sentinel-opencode") == "sentinel-opencode"


def test_resolve_agent_accepts_a_pane_id(monkeypatch):
    _live(monkeypatch, [{"agent": "claude", "pane_id": "w1:p3"}])

    assert bridge.resolve_agent("w1:p3") == "w1:p3"


def test_resolve_agent_falls_back_to_a_unique_runtime_family(monkeypatch):
    # The repair for the observed outage: SENTINEL_AGENT="opencode" keeps
    # working across a rebuild, because the family outlives both the name
    # and the pane id.
    _live(monkeypatch, [
        {"agent": "opencode", "pane_id": "w1:p1"},
        {"agent": "claude", "pane_id": "w1:p3"},
    ])

    assert bridge.resolve_agent("opencode") == "w1:p1"


def test_resolve_agent_refuses_an_ambiguous_family(monkeypatch):
    # Two candidates means the bridge would be guessing which session gets
    # the task. Dispatching to the wrong agent is worse than failing.
    _live(monkeypatch, [
        {"agent": "opencode", "pane_id": "w1:p1"},
        {"agent": "opencode", "pane_id": "w1:p5"},
    ])

    with pytest.raises(bridge.AgentNotFoundError) as exc_info:
        bridge.resolve_agent("opencode")

    assert "w1:p1" in str(exc_info.value)
    assert "w1:p5" in str(exc_info.value)


def test_agent_not_found_error_lists_what_is_actually_live(monkeypatch):
    _live(monkeypatch, [
        {"name": "sentinel-claude", "agent": "claude", "pane_id": "w1:p3"},
    ])

    with pytest.raises(bridge.AgentNotFoundError) as exc_info:
        bridge.resolve_agent("sentinel-opencode")

    # The whole failure mode was a caller staring at "unreachable" with no
    # way to learn what it should have asked for instead.
    message = str(exc_info.value)
    assert "sentinel-opencode" in message
    assert "sentinel-claude" in message


def test_resolve_agent_passes_through_when_herdr_is_unreachable(monkeypatch):
    _live(monkeypatch, None, ok=False)

    # herdr being down is a different failure with a different fix, and it
    # is not resolve_agent's to diagnose. Hand the name on untouched and
    # let the herdr call itself report it.
    assert bridge.resolve_agent("sentinel-opencode") == "sentinel-opencode"


def test_ready_distinguishes_a_missing_agent_from_an_unreachable_herdr(monkeypatch, live_server):
    # herdr answers `agent list` fine; it is only `agent get <stale-name>`
    # that fails. That combination is exactly what a rebuilt session
    # produces, and what the old single "unreachable" reason hid.
    _live(monkeypatch, [{"name": "sentinel-claude", "agent": "claude", "pane_id": "w1:p3"}])
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: (None, {"ok": False}))

    status, body = _get(live_server, "/ready?agent=sentinel-opencode")

    # "sentinel_unreachable" sent a real caller off trying to restore a
    # channel that was never down: the bridge, herdr and both agents were
    # all healthy, and only the name had stopped resolving.
    assert status == 404
    assert body["reason"] == "agent_not_found"
    assert "sentinel-claude" in json_module.dumps(body, ensure_ascii=False)


# --- naming is optional ------------------------------------------------
#
# herdr attaches a name to an agent *session*, so it is lost whenever the
# agent process restarts -- not just when a window is rebuilt. Observed
# twice within an hour on the reference deployment, the second time
# without anyone touching the windows at all. A bridge that needs an
# agent to be named in order to work would therefore need renaming as
# routine maintenance, which is not a reasonable thing to ask.


def test_resolve_agent_picks_one_when_nothing_is_configured(monkeypatch):
    _live(monkeypatch, [{"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"}])

    assert bridge.resolve_agent(None) == "w1:p1"


def test_resolve_agent_prefers_an_available_agent_when_choosing(monkeypatch):
    _live(monkeypatch, [
        {"agent": "opencode", "pane_id": "w1:p1", "agent_status": "working"},
        {"agent": "claude", "pane_id": "w1:p3", "agent_status": "idle"},
    ])

    # "Whichever you like" still shouldn't mean "queue behind a busy one".
    assert bridge.resolve_agent(None) == "w1:p3"


def test_resolve_agent_auto_pick_is_deterministic(monkeypatch):
    # Two equally idle agents: the choice must not wander between calls,
    # or consecutive tasks land on different agents for no stated reason.
    agents = [
        {"agent": "claude", "pane_id": "w1:p3", "agent_status": "idle"},
        {"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"},
    ]
    _live(monkeypatch, agents)

    assert bridge.resolve_agent(None) == bridge.resolve_agent(None) == "w1:p1"


def test_resolve_agent_says_so_when_nothing_is_running(monkeypatch):
    _live(monkeypatch, [])

    with pytest.raises(bridge.AgentNotFoundError) as exc_info:
        bridge.resolve_agent(None)

    assert "no" in str(exc_info.value).lower()


def test_agent_not_found_error_does_not_demand_a_name(monkeypatch):
    _live(monkeypatch, [{"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"}])

    with pytest.raises(bridge.AgentNotFoundError) as exc_info:
        bridge.resolve_agent("sentinel-opencode")

    message = str(exc_info.value)

    # The old text told the reader to run `herdr agent rename`, which
    # reads as "this tool requires named agents". It does not: a pane id
    # or a runtime family addresses an agent that was never named.
    assert "rename" not in message
    assert "w1:p1" in message


def test_prompt_failure_attaches_the_terminal_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    def fake_run_herdr(*args, **kwargs):
        if args[1] == "prompt":
            return {
                "ok": False,
                "stdout": "",
                "stderr": '{"error":{"code":"agent_blocked","message":'
                          '"agent w1:p1 is blocked and requires interactive input"}}',
            }
        return {"ok": True, "stdout": "Requesting user permission for action", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    with pytest.raises(bridge.SentinelPromptError) as exc_info:
        bridge._run_herdr_prompt("w1:p1", "prompt", 1000)

    # herdr says only "blocked". *Why* -- a permission request, a plan-mode
    # gate, a confirmation dialog -- exists solely on the agent's screen,
    # so a structured status alone cannot tell an operator what to do next.
    assert "Requesting user permission" in exc_info.value.raw_output


def test_worker_records_the_terminal_tail_for_a_blocked_agent(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    # A *genuinely* blocked agent: idle when the task is claimed, then blocked
    # and staying that way. A single agent_blocked abort no longer fails a
    # task on its own -- 72% of those had in fact delivered -- so the failure
    # this test expects has to be earned by the agent actually staying blocked.
    monkeypatch.setattr(bridge, "BLOCKED_CONFIRM_SEC", 0.1)
    monkeypatch.setattr(bridge, "ABORT_POLL_SEC", 0.01)
    gets = {"n": 0}

    def fake_run_herdr(*args, **kwargs):
        if args[1] == "get":
            gets["n"] += 1
            status = "idle" if gets["n"] == 1 else "blocked"
            return {"ok": True, "stdout": json_module.dumps(
                {"result": {"agent": {"agent_status": status}}}
            ), "stderr": ""}
        if args[1] == "prompt":
            return {"ok": False, "stdout": "", "stderr": '{"error":{"code":"agent_blocked"}}'}
        return {"ok": True, "stdout": "Requesting user permission for action", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    task_id = bridge.create_task("do something", 1000, "w1:p1")
    stop = threading_module.Event()
    worker = threading_module.Thread(target=bridge.task_worker, args=(stop,), daemon=True)
    worker.start()

    for _ in range(100):
        if bridge.get_task(task_id)["status"] == "error":
            break
        time.sleep(0.05)

    stop.set()
    worker.join(timeout=5)

    # Without this the operator sees "agent_blocked" and nothing else --
    # no way to learn it was a permission prompt waiting for a human.
    assert "Requesting user permission" in bridge.get_task(task_id)["error_text"]


def test_ask_resolves_a_runtime_family_like_ready_does(live_server, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    targets = []
    writes_result = compliant_agent("done")

    def fake_run_herdr(*args, **kwargs):
        if args[1] == "get":
            # herdr only knows the pane id; the family name means nothing
            # to it, which is exactly how the outage presented.
            if args[2] != "w1:p1":
                return {"ok": False, "stdout": "", "stderr": "no such agent"}
            return {"ok": True, "stdout": json_module.dumps(
                {"result": {"agent": {"agent_status": "idle"}}}
            ), "stderr": ""}
        if args[1] == "prompt":
            targets.append(args[2])
        return writes_result(*args, **kwargs)

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    status, body = _post(
        live_server, "/ask", {"task": "do something", "agent": "opencode"}
    )

    # /ready resolved this identifier while /ask did not, because
    # resolution had been bolted onto two paths and the one that actually
    # dispatches work was not among them.
    assert status == 200
    assert targets == ["w1:p1"]


def test_ask_reports_a_missing_agent_as_such(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": False, "stdout": "", "stderr": "no such agent",
    })

    status, body = _post(
        live_server, "/ask", {"task": "do something", "agent": "sentinel-opencode"}
    )

    # "unable_to_query_sentinel" reads as a broken channel and sent a real
    # caller into a retry loop against a bridge that was working fine.
    assert status == 404
    assert body["reason"] == "agent_not_found"


# --- quota circuits expire on their own --------------------------------
#
# A quota limit is a temporary condition with a known end: the provider
# text that trips the circuit routinely states it ("You've hit your
# session limit · resets 4:20pm"). Modelling that as a latch only an
# operator can release meant a recovered agent stayed unusable until
# someone noticed -- observed live, with the agent reporting idle and
# every dispatch to it answered 429 from a circuit hours old.


def test_quota_block_expires_after_its_ttl(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "QUOTA_BLOCK_TTL_SECONDS", 3600)

    bridge.mark_agent_quota_blocked("w1:p3", "session limit")

    stale = (
        datetime_module.now(datetime_module_tz.utc)
        - datetime_module_delta(seconds=7200)
    ).isoformat()
    with bridge.db_session() as conn:
        conn.execute(
            "UPDATE quota_blocks SET detected_at = ? WHERE agent = ?",
            (stale, "w1:p3"),
        )

    assert bridge.get_agent_quota_block("w1:p3") is None


def test_quota_block_holds_inside_its_ttl(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "QUOTA_BLOCK_TTL_SECONDS", 3600)

    bridge.mark_agent_quota_blocked("w1:p3", "session limit")

    assert bridge.get_agent_quota_block("w1:p3") is not None


def test_expired_quota_blocks_disappear_from_the_listing(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "QUOTA_BLOCK_TTL_SECONDS", 3600)

    bridge.mark_agent_quota_blocked("w1:p1", "fresh")
    bridge.mark_agent_quota_blocked("w1:p3", "stale")

    stale = (
        datetime_module.now(datetime_module_tz.utc)
        - datetime_module_delta(seconds=7200)
    ).isoformat()
    with bridge.db_session() as conn:
        conn.execute(
            "UPDATE quota_blocks SET detected_at = ? WHERE agent = ?",
            (stale, "w1:p3"),
        )

    # /quota is what an operator reads to decide whether to intervene, so
    # it must not show a circuit that no longer blocks anything.
    listed = [b["agent"] for b in bridge.list_agent_quota_blocks()]
    assert listed == ["w1:p1"]


def test_ready_reports_a_quota_blocked_agent_as_not_ready(live_server, tmp_path, monkeypatch):
    _live(monkeypatch, [{"agent": "claude", "pane_id": "w1:p3", "agent_status": "idle"}])
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))
    bridge.mark_agent_quota_blocked("w1:p3", "session limit")

    status, body = _get(live_server, "/ready?agent=w1:p3")

    # herdr says idle, so the old /ready said ready -- and the dispatch
    # that followed was refused 429 by the circuit. A readiness check that
    # disagrees with what dispatch will do is worse than no check.
    assert body["ready"] is False
    assert body["reason"] == "quota_blocked"


def test_failover_candidates_work_for_unnamed_agents(monkeypatch):
    _live(monkeypatch, [
        {"agent": "claude", "pane_id": "w1:p3"},
        {"agent": "opencode", "pane_id": "w1:p1"},
    ])

    # Keyed on name, this returned nothing at all once herdr dropped the
    # names -- so a quota-exhausted agent reported "all eligible fallback
    # agents are quota-blocked" while a healthy one sat idle beside it.
    assert bridge.quota_failover_candidates("w1:p3") == ["w1:p1"]


def test_quota_detail_keeps_the_evidence_not_the_screen(tmp_path, monkeypatch):
    screen = (
        "任务：访问 /nesi/project/secret-cohort/predictions 并核验 schema\n"
        + "noise\n" * 200
        + "You've hit your session limit - resets 4:20pm"
    )

    detail = bridge.quota_error_detail(screen)

    # The detail is surfaced by /quota. Storing a trailing slab of the
    # terminal put the delegated task -- paths, dataset names -- into an
    # endpoint whose job is to report a provider error.
    assert "hit your session limit" in detail
    assert "secret-cohort" not in detail
    assert len(detail) < 400


# --- cost-ordered agent selection --------------------------------------
#
# Selection had no notion of cost: it broke ties on a stable identifier,
# which meant the cheapest agent won only by coincidence of sort order.
# A subscription's quota is already paid for whether it is used or not,
# while a metered API bills per token, so the operator's ordering is a
# real cost lever -- and one only they can state, since the bridge cannot
# see anyone's billing arrangement.


def test_auto_pick_follows_the_configured_priority(monkeypatch):
    monkeypatch.setattr(bridge, "AGENT_PRIORITY", ("claude", "opencode"))
    _live(monkeypatch, [
        {"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"},
        {"agent": "claude", "pane_id": "w1:p3", "agent_status": "idle"},
    ])

    # Without priority this returned w1:p1 purely because it sorts first.
    assert bridge.resolve_agent(None) == "w1:p3"


def test_priority_never_outranks_being_able_to_take_work(monkeypatch):
    monkeypatch.setattr(bridge, "AGENT_PRIORITY", ("claude", "opencode"))
    _live(monkeypatch, [
        {"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"},
        {"agent": "claude", "pane_id": "w1:p3", "agent_status": "working"},
    ])

    # Waiting has a cost too, and the bridge has no queue-and-wait path:
    # preferring a busy agent would just return 409 to the caller.
    assert bridge.resolve_agent(None) == "w1:p1"


def test_unlisted_agents_sort_after_listed_ones(monkeypatch):
    monkeypatch.setattr(bridge, "AGENT_PRIORITY", ("claude",))
    _live(monkeypatch, [
        {"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"},
        {"agent": "claude", "pane_id": "w1:p3", "agent_status": "idle"},
    ])

    assert bridge.resolve_agent(None) == "w1:p3"


def test_selection_is_unchanged_when_no_priority_is_configured(monkeypatch):
    monkeypatch.setattr(bridge, "AGENT_PRIORITY", ())
    _live(monkeypatch, [
        {"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"},
        {"agent": "claude", "pane_id": "w1:p3", "agent_status": "idle"},
    ])

    assert bridge.resolve_agent(None) == "w1:p1"


def test_failover_candidates_follow_the_configured_priority(monkeypatch):
    monkeypatch.setattr(bridge, "AGENT_PRIORITY", ("claude", "opencode", "gemini"))
    _live(monkeypatch, [
        {"agent": "gemini", "pane_id": "w1:p5"},
        {"agent": "opencode", "pane_id": "w1:p1"},
        {"agent": "claude", "pane_id": "w1:p3"},
    ])

    # A quota failover is exactly when cost order matters most: the
    # primary is gone and the bridge is choosing what to pay for next.
    assert bridge.quota_failover_candidates("w1:p3") == ["w1:p1", "w1:p5"]


# --- robustness against data the bridge did not write itself -----------
#
# Every severe outage in this project so far has had the same shape: one
# unexpected value in a field, and a whole path stops working. These
# cover the remaining places where the bridge trusts input it does not
# control -- herdr's JSON, and rows that may predate the current writer.


def test_quota_block_survives_a_timestamp_without_a_timezone(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "QUOTA_BLOCK_TTL_SECONDS", 3600)

    bridge.mark_agent_quota_blocked("w1:p3", "session limit")
    with bridge.db_session() as conn:
        # Parses fine, but cannot be subtracted from an aware datetime.
        # The guard around the parse did not cover the arithmetic, so this
        # raised TypeError straight out of the expiry check -- and that
        # check sits on /ready, the worker's dispatch and quota failover.
        fresh_naive = (
            datetime_module.now(datetime_module_tz.utc)
            .replace(tzinfo=None)
            .isoformat()
        )
        conn.execute(
            "UPDATE quota_blocks SET detected_at = ? WHERE agent = ?",
            (fresh_naive, "w1:p3"),
        )

    block = bridge.get_agent_quota_block("w1:p3")

    # Treated as UTC, which is what now_iso() writes, so a naive stamp is
    # aged correctly rather than crashing or being discarded outright.
    assert block is not None


def test_quota_block_with_a_naive_stamp_still_expires(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "QUOTA_BLOCK_TTL_SECONDS", 3600)

    bridge.mark_agent_quota_blocked("w1:p3", "session limit")
    stale = (
        datetime_module.now(datetime_module_tz.utc)
        - datetime_module_delta(seconds=7200)
    ).replace(tzinfo=None).isoformat()
    with bridge.db_session() as conn:
        conn.execute(
            "UPDATE quota_blocks SET detected_at = ? WHERE agent = ?",
            (stale, "w1:p3"),
        )

    assert bridge.get_agent_quota_block("w1:p3") is None


def test_auto_pick_skips_agents_with_no_usable_identifier(monkeypatch):
    _live(monkeypatch, [
        {"agent": "ghost", "agent_status": "idle"},        # no name, no pane_id
        {"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"},
    ])

    # agent_identifiers() returns [] for the first entry, and indexing it
    # raised IndexError from inside the sort key -- taking down auto-select
    # entirely, so every request that named no agent failed at once.
    assert bridge.resolve_agent(None) == "w1:p1"


def test_auto_pick_ignores_malformed_entries(monkeypatch):
    _live(monkeypatch, [
        "not-a-dict",
        {"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"},
    ])

    # quota_failover_candidates() already guarded against this; selection
    # did not, so the two disagreed about what counts as an agent.
    assert bridge.resolve_agent(None) == "w1:p1"


def test_resolve_reports_clearly_when_no_agent_is_addressable(monkeypatch):
    _live(monkeypatch, [{"agent": "ghost", "agent_status": "idle"}])

    with pytest.raises(bridge.AgentNotFoundError):
        bridge.resolve_agent(None)


# --- slurm safety gate -------------------------------------------------
#
# The bridge is not in the execution path: it sends text through `herdr
# agent prompt` and the agent decides what to run, so it can neither see
# nor block an sbatch. What it can do is state the policy in the prompt
# and make widening it a deliberate act, which is the difference between
# an agent submitting production work on its own judgement and an agent
# having been told it may. Calling that a hard block would be a lie.


def test_delegation_prompt_states_the_slurm_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    prompt = bridge.build_delegation_prompt(
        "跑一下训练", "aaaaaaaa-1111-2222-3333-444444444444"
    )

    assert "Slurm" in prompt


def test_default_policy_does_not_restrict_submission(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    prompt = bridge.build_delegation_prompt(
        "跑一下训练", "aaaaaaaa-1111-2222-3333-444444444444"
    )

    # The default used to withhold full-scale submission, which made every
    # real run cost an extra round trip -- a restriction chosen on an
    # assumed risk rather than an observed one, which is the same mistake
    # as the prompt's deleted self-justifications. Submitting is
    # reversible: scancel it and resubmit, and the only cost is queue
    # time. Not submitting is the expensive outcome.
    assert "自由提交" in prompt
    assert "未获授权" not in prompt


def test_default_policy_still_protects_what_is_irreversible(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    prompt = bridge.build_delegation_prompt(
        "跑一下训练", "aaaaaaaa-1111-2222-3333-444444444444"
    )

    # The line worth drawing is not around submitting, which is undoable,
    # but around a job destroying work that already exists.
    assert "覆盖" in prompt
    assert "checkpoint" in prompt


def test_restrictive_policies_remain_available(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    task_id = "aaaaaaaa-1111-2222-3333-444444444444"

    # Tightening stays possible; it is just no longer what happens to
    # everyone who did not ask for it.
    assert "debug" in bridge.build_delegation_prompt("x", task_id, "test_only")
    assert "--test-only" in bridge.build_delegation_prompt("x", task_id, "dry_run_only")


def test_dry_run_policy_forbids_submitting_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    prompt = bridge.build_delegation_prompt(
        "检查脚本", "aaaaaaaa-1111-2222-3333-444444444444",
        slurm_policy="dry_run_only",
    )

    assert "--test-only" in prompt
    assert "不得" in prompt


def test_no_policy_invites_an_unbounded_resubmission_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    task_id = "aaaaaaaa-1111-2222-3333-444444444444"

    # Freedom to submit is not freedom to submit repeatedly on failure:
    # that is how one bad script quietly burns an allocation. Reporting
    # the failure costs one round trip; a retry loop costs core-hours.
    for policy in ("submit", "test_only"):
        prompt = bridge.build_delegation_prompt("x", task_id, policy)
        assert "重投" in prompt


def test_every_policy_asks_for_the_job_id(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    for policy in ("dry_run_only", "test_only", "submit"):
        prompt = bridge.build_delegation_prompt(
            "x", "aaaaaaaa-1111-2222-3333-444444444444", slurm_policy=policy
        )
        # The job id is the only part of this that is mechanical: it is
        # what makes a submission auditable and cancellable afterwards.
        assert "job ID" in prompt, policy


def test_unknown_policy_is_refused_rather_than_silently_downgraded(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    # Silently falling back to the default would turn a typo in the
    # strictest setting into the loosest one the deployment allows.
    with pytest.raises(ValueError):
        bridge.validate_slurm_policy("dry-run")


def test_delegate_rejects_an_unknown_policy(live_server):
    status, body = _post(live_server, "/delegate", {
        "task": "x", "slurm_policy": "whatever",
    })

    assert status == 400
    assert "slurm_policy" in body["error"]


def test_delegate_records_the_policy_it_ran_under(live_server):
    status, body = _post(live_server, "/delegate", {
        "task": "x", "slurm_policy": "dry_run_only",
    })

    # Stored so an audit can tell what the agent was permitted to do,
    # not just what it did.
    task = bridge.get_task(body["task_id"])
    assert task["slurm_policy"] == "dry_run_only"


def test_worker_uses_the_policy_recorded_on_the_task(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    prompts = []
    writes_result = compliant_agent("done")

    def fake_run_herdr(*args, **kwargs):
        if args[1] == "get":
            return {"ok": True, "stdout": json_module.dumps(
                {"result": {"agent": {"agent_status": "idle"}}}
            ), "stderr": ""}
        if args[1] == "prompt":
            prompts.append(args[3])
        return writes_result(*args, **kwargs)

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    task_id = bridge.create_task("跑训练", 60000, "w1:p1",
                                 slurm_policy="dry_run_only")
    stop = threading_module.Event()
    worker = threading_module.Thread(target=bridge.task_worker, args=(stop,), daemon=True)
    worker.start()
    for _ in range(100):
        if bridge.get_task(task_id)["status"] == "done":
            break
        time.sleep(0.05)
    stop.set()
    worker.join(timeout=5)

    # Recording the policy but dispatching under the default would make
    # the stored value a comforting fiction: the agent would have been
    # told it could submit while the audit trail says otherwise.
    assert prompts and "--test-only" in prompts[0]


# --- terminal reads are a snapshot, not a live status ------------------
#
# A TUI does not clear itself when a task finishes, so /read routinely
# returns the leftover picture of a completed session: a finished report,
# a "new task?" hint, and whatever text was sitting unsent in the input
# box. A caller read that as two agents being stuck and told the operator
# to go and clear the windows by hand -- in a tool whose whole point is
# not having to touch the remote terminal. Both agents were idle.


def test_read_reports_the_agent_status_alongside_the_snapshot(live_server, monkeypatch):
    _live(monkeypatch, [{"agent": "claude", "pane_id": "w1:p3", "agent_status": "idle"}])
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": True, "stdout": "done 3:32 pm\n> append the addendum", "stderr": "",
    })

    status, body = _get(live_server, "/read?agent=w1:p3")

    # Without this the response is a wall of text with nothing to weigh it
    # against, and leftover input reads exactly like work in progress.
    assert status == 200
    assert body["agent_status"] == "idle"


def test_read_still_answers_when_the_status_lookup_fails(live_server, monkeypatch):
    _live(monkeypatch, [{"agent": "claude", "pane_id": "w1:p3", "agent_status": "idle"}])
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: (None, {"ok": False}))
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": True, "stdout": "terminal text", "stderr": "",
    })

    status, body = _get(live_server, "/read?agent=w1:p3")

    # /read exists to diagnose a sick agent. Refusing to return the
    # terminal because the status call also failed would withhold the
    # evidence exactly when it is most wanted.
    assert status == 200
    assert body["stdout"] == "terminal text"
    assert body["agent_status"] is None


# --- progress while a task is still running ----------------------------
#
# Between /delegate returning a task_id and the result arriving, a caller
# could see nothing at all. For a Slurm job that is most of the elapsed
# time. The terminal is not an option: it truncates, carries TUI chrome,
# and was measured showing an autocomplete suggestion that a caller read
# as live work. So progress travels the same file channel as the result,
# which has a day of production use behind it -- but appended as it
# happens, and optional, because progress is a convenience for the
# operator rather than another contract the agent must satisfy.


def test_progress_file_is_separate_from_the_result_file(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    task_id = "aaaaaaaa-1111-2222-3333-444444444444"

    assert bridge.progress_file_path(task_id) != bridge.result_file_path(task_id)


def test_reading_progress_leaves_the_file_in_place(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    task_id = "aaaaaaaa-1111-2222-3333-444444444444"

    with open(bridge.progress_file_path(task_id), "w", encoding="utf-8") as f:
        f.write("submitted job 9213894\n")

    assert "9213894" in bridge.read_progress_file(task_id)
    # Unlike the result, this is read repeatedly while the task runs.
    assert "9213894" in bridge.read_progress_file(task_id)


def test_missing_progress_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    # An agent that never reports progress has done nothing wrong.
    assert bridge.read_progress_file("aaaaaaaa-1111-2222-3333-444444444444") is None


def test_task_query_exposes_progress_while_running(tmp_path, monkeypatch, live_server):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    _fresh_db(tmp_path, monkeypatch)

    task_id = bridge.create_task("跑训练", 60000, "w1:p1")
    bridge.claim_task(task_id)
    with open(bridge.progress_file_path(task_id), "w", encoding="utf-8") as f:
        f.write("submitted job 9213894, queued\n")

    status, body = _get(live_server, f"/tasks/{task_id}")

    assert body["task"]["progress"] == "submitted job 9213894, queued"


def test_delegation_prompt_offers_the_progress_file(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    task_id = "aaaaaaaa-1111-2222-3333-444444444444"

    prompt = bridge.build_delegation_prompt("跑训练", task_id)

    assert bridge.progress_file_path(task_id) in prompt
    # Phrased as an option. Making it a requirement would turn a
    # convenience into a second thing that can fail a task.
    assert "可选" in prompt


def test_progress_file_is_removed_once_the_task_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    _fresh_db(tmp_path, monkeypatch)

    task_id = bridge.create_task("x", 60000, "w1:p1")
    path = bridge.progress_file_path(task_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write("working\n")

    bridge.complete_task(task_id, "done")

    # Otherwise every finished task leaves one behind, which is the leak
    # the result-file sweep already had to be built for.
    assert not os.path.exists(path)


def test_sweep_also_collects_stale_progress_files(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "RESULT_RETENTION_DAYS", 7)

    stale = tmp_path / "progress-bbbb.txt"
    stale.write_text("orphaned mid-task", encoding="utf-8")
    _age_file(stale, 8)

    assert bridge.purge_stale_result_files() == 1
    assert not stale.exists()


# --- a queued task explains why it is queued ---------------------------
#
# A caller watched a task sit at queued with started_at null and could
# not tell why. Nothing in the twelve fields said so, yet the bridge knew
# the whole time: the worker checks the target agent on every pass and
# skips it when it cannot take work. The agent in that case was simply
# busy with something else, which is ordinary, but indistinguishable from
# a stuck queue when nothing says it out loud.


def test_queued_task_says_the_target_agent_is_busy(tmp_path, monkeypatch, live_server):
    _fresh_db(tmp_path, monkeypatch)
    _live(monkeypatch, [{"agent": "opencode", "pane_id": "w1:p1", "agent_status": "working"}])
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("working", {"ok": True}))

    task_id = bridge.create_task("audit", 60000, "w1:p1")

    status, body = _get(live_server, f"/tasks/{task_id}")

    assert body["task"]["queued_reason"]
    assert "working" in body["task"]["queued_reason"]


def test_queued_task_says_when_the_agent_is_not_running(tmp_path, monkeypatch, live_server):
    _fresh_db(tmp_path, monkeypatch)
    _live(monkeypatch, [{"agent": "claude", "pane_id": "w1:p3", "agent_status": "idle"}])

    # Queued against an agent that has since gone away. Without this the
    # task waits forever with no hint that it never can run.
    task_id = bridge.create_task("audit", 60000, "w1:p9")

    status, body = _get(live_server, f"/tasks/{task_id}")

    reason = body["task"]["queued_reason"]
    assert "w1:p9" in reason
    assert "w1:p3" in reason


def test_queued_task_says_so_when_the_agent_is_free(tmp_path, monkeypatch, live_server):
    _fresh_db(tmp_path, monkeypatch)
    _live(monkeypatch, [{"agent": "claude", "pane_id": "w1:p3", "agent_status": "idle"}])
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))

    task_id = bridge.create_task("audit", 60000, "w1:p3")

    status, body = _get(live_server, f"/tasks/{task_id}")

    # Distinguishes "waiting its turn" from "waiting on something that
    # will never free up", which want different reactions.
    assert "worker" in body["task"]["queued_reason"].lower()


def test_only_queued_tasks_carry_a_reason(tmp_path, monkeypatch, live_server):
    _fresh_db(tmp_path, monkeypatch)
    _live(monkeypatch, [{"agent": "claude", "pane_id": "w1:p3", "agent_status": "idle"}])

    task_id = bridge.create_task("audit", 60000, "w1:p3")
    bridge.claim_task(task_id)

    status, body = _get(live_server, f"/tasks/{task_id}")

    # A running task's reason would be stale the moment it was read, and
    # asking herdr for it would cost a call per poll for nothing.
    assert body["task"]["queued_reason"] is None


def test_queued_reason_survives_herdr_being_unreachable(tmp_path, monkeypatch, live_server):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "list_agents", lambda: (None, {"ok": False}))

    task_id = bridge.create_task("audit", 60000, "w1:p1")

    status, body = _get(live_server, f"/tasks/{task_id}")

    # Explaining the queue is a convenience; it must not turn a task
    # query into a 500 when herdr happens to be unavailable.
    assert status == 200
    assert body["task"]["status"] == "queued"


# --- ready says what a false answer means ------------------------------
#
# A caller polled `ready` on one agent every forty seconds for minutes,
# reading ready=false as the system being blocked, while another agent sat
# idle the whole time. The answer was accurate: the agent was working.
# What it did not say is that working is ordinary, that it is not a fault,
# and what to do about it -- and "false" with no reason attached reads as
# an obstruction.


def test_ready_explains_a_busy_agent_and_points_at_auto_selection(live_server, monkeypatch):
    _live(monkeypatch, [{"agent": "opencode", "pane_id": "w1:p1", "agent_status": "working"}])
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("working", {"ok": True}))

    status, body = _get(live_server, "/ready?agent=w1:p1")

    assert body["ready"] is False
    # Names the cause as ordinary, and the way out: leave the agent unnamed.
    assert "not a fault" in body["hint"]
    assert "omit" in body["hint"].lower()


def test_ready_tells_a_blocked_agent_apart_from_a_busy_one(live_server, monkeypatch):
    _live(monkeypatch, [{"agent": "opencode", "pane_id": "w1:p1", "agent_status": "blocked"}])
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("blocked", {"ok": True}))

    status, body = _get(live_server, "/ready?agent=w1:p1")

    # Blocked is the one state that genuinely needs a person: the agent is
    # waiting on input in its own terminal. Saying "busy, not a fault" here
    # would hide it, so the two must not share wording.
    assert "not a fault" not in body["hint"]
    assert "read" in body["hint"]


def test_ready_gives_no_hint_when_the_agent_is_free(live_server, monkeypatch):
    _live(monkeypatch, [{"agent": "claude", "pane_id": "w1:p3", "agent_status": "idle"}])
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))

    status, body = _get(live_server, "/ready?agent=w1:p3")

    # Nothing to explain, and a field that is always populated stops being
    # read.
    assert body["ready"] is True
    assert "hint" not in body


# --- PowerShell sources must survive a non-UTF-8 console code page -----
#
# Windows PowerShell 5.1 reads a BOM-less file in the machine's ANSI code
# page, not UTF-8. On a Chinese Windows that is GBK, where a multi-byte
# lead byte consumes the byte after it -- and the byte after the last
# character of a UTF-8 sequence is often a closing quote. The quote is
# swallowed, the string never terminates, and the whole file fails to
# parse with an error pointing at a line far from the cause.
#
# CI runs an English locale and never saw it. It surfaced only on a Chinese
# machine, where sentinel.Tests.ps1 failed discovery outright. Keeping the
# sources pure ASCII is the cheap structural fix, and checking it here
# turns "fails on someone's machine" into "fails in CI".


def test_powershell_sources_are_pure_ascii():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = {}

    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in (".git", ".venv", "node_modules", ".superpowers")]
        for name in files:
            if not name.endswith((".ps1", ".psm1", ".psd1")):
                continue
            path = os.path.join(root, name)
            with open(path, "rb") as f:
                raw = f.read()
            if raw.startswith(b"\xef\xbb\xbf"):
                continue  # a BOM makes 5.1 read UTF-8 correctly
            for number, line in enumerate(raw.split(b"\n"), 1):
                if any(byte > 0x7F for byte in line):
                    offenders.setdefault(os.path.relpath(path, repo), []).append(number)

    assert not offenders, (
        "non-ASCII in BOM-less PowerShell source breaks Windows PowerShell 5.1 "
        f"under a non-UTF-8 code page: {offenders}"
    )


# --- delegate is safe to retry -----------------------------------------
#
# A client that times out waiting for /delegate cannot tell whether the
# task was queued: the request may have landed and only the reply been
# lost. Retrying then enqueues it twice, and with Slurm submission allowed
# by default that is a duplicate job. The client had just been told to
# "retry once" on exactly that timeout, so the hazard was shipped with the
# advice to walk into it.
#
# The standard fix is a caller-supplied idempotency key: the caller picks
# one per logical operation and reuses it across retries, and the server
# collapses repeats into the original task.


def _delegate(live_server, **extra):
    body = {"task": "check disk"}
    body.update(extra)
    return _post(live_server, "/delegate", body)


def test_same_key_returns_the_original_task_not_a_second_one(live_server):
    s1, first = _delegate(live_server, idempotency_key="op-1")
    s2, second = _delegate(live_server, idempotency_key="op-1")

    assert s1 == 202 and s2 == 202
    assert second["task_id"] == first["task_id"]
    assert second["replayed"] is True
    assert "replayed" not in first or first["replayed"] is False
    assert len(bridge.list_tasks()) == 1


def test_no_key_keeps_the_old_behaviour(live_server):
    _, first = _delegate(live_server)
    _, second = _delegate(live_server)

    # Two deliberate, identical submissions are legitimate without a key.
    assert first["task_id"] != second["task_id"]
    assert len(bridge.list_tasks()) == 2


def test_same_key_with_different_parameters_is_refused(live_server):
    _delegate(live_server, idempotency_key="op-1")

    status, body = _post(live_server, "/delegate", {
        "task": "a different task", "idempotency_key": "op-1",
    })

    # Silently returning the first task would hand the caller a result for
    # something it did not ask for. Reuse with changed parameters is a bug
    # on the caller's side and should be loud.
    assert status == 422
    assert "idempotency" in body["error"].lower()
    assert len(bridge.list_tasks()) == 1


def test_a_replay_is_served_even_when_the_queue_is_full(live_server, monkeypatch):
    _, first = _delegate(live_server, idempotency_key="op-1")
    monkeypatch.setattr(bridge, "MAX_QUEUE_DEPTH", 1)

    status, body = _delegate(live_server, idempotency_key="op-1")

    # A replay creates nothing, so the queue cap has no business refusing it.
    assert status == 202
    assert body["task_id"] == first["task_id"]


def test_a_replay_does_not_need_the_agent_to_still_exist(live_server, monkeypatch):
    _, first = _delegate(live_server, idempotency_key="op-1")
    monkeypatch.setattr(bridge, "list_agents", lambda: ([], {"ok": True}))

    status, body = _delegate(live_server, idempotency_key="op-1")

    # The original is already queued. Re-resolving the agent for a replay
    # would turn a harmless retry into a 404 for work that exists.
    assert status == 202
    assert body["task_id"] == first["task_id"]


def test_concurrent_requests_with_one_key_create_one_task(live_server):
    results = []

    def fire():
        results.append(_delegate(live_server, idempotency_key="race"))

    threads = [threading_module.Thread(target=fire) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()

    # The race is the whole reason the key is claimed atomically rather
    # than checked and then inserted.
    assert len({body["task_id"] for _, body in results}) == 1
    assert len(bridge.list_tasks()) == 1


@pytest.mark.parametrize("key", ["", "   ", "x" * 256, "bad\nkey", 123])
def test_malformed_keys_are_rejected(live_server, key):
    status, body = _delegate(live_server, idempotency_key=key)

    assert status == 400
    assert "idempotency_key" in body["error"]


def test_the_key_survives_a_restart(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    first = bridge.create_task_if_queue_available(
        "x", 60000, "w1:p1", None, idempotency_key="op-1", fingerprint="f"
    )

    bridge.init_db()   # what a restart does

    again = bridge.find_task_by_idempotency_key("op-1")
    # A retry that arrives after a bridge restart -- the likeliest time for
    # a timed-out request to be retried -- must still be recognised.
    assert again["task_id"] == first


# --- no herdr call waits forever ---------------------------------------
#
# run_herdr() defaulted to timeout=None and two handlers -- /status and
# /read -- passed nothing, so a herdr that hung left the request thread
# blocked indefinitely. The client is bounded now (and reports a stalled
# request distinctly), which makes this visible rather than harmless: every
# stalled request ties up a thread until herdr answers, which may be never.


def _recording_run(monkeypatch, behaviour="ok"):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(kwargs.get("timeout"))
        if behaviour == "hang":
            raise subprocess_module.TimeoutExpired(cmd, kwargs.get("timeout"))
        return subprocess_module.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    return seen


def test_run_herdr_always_passes_a_timeout(monkeypatch):
    seen = _recording_run(monkeypatch)

    bridge.run_herdr("agent", "list")   # caller named no timeout

    assert seen == [bridge.HERDR_DEFAULT_TIMEOUT_SEC]
    assert bridge.HERDR_DEFAULT_TIMEOUT_SEC > 0


def test_an_explicit_timeout_is_not_overridden(monkeypatch):
    seen = _recording_run(monkeypatch)

    bridge.run_herdr("agent", "list", timeout=7)

    assert seen == [7]


@pytest.mark.parametrize("path", ["/status?agent=w1:p1", "/read?agent=w1:p1"])
def test_status_and_read_bound_their_herdr_calls(live_server, monkeypatch, path):
    _live(monkeypatch, [{"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"}])
    seen = _recording_run(monkeypatch)

    _get(live_server, path)

    assert seen, "expected the endpoint to call herdr"
    assert all(t is not None for t in seen)


@pytest.mark.parametrize("path", ["/status?agent=w1:p1", "/read?agent=w1:p1"])
def test_a_hung_herdr_is_a_504_not_a_500(live_server, monkeypatch, path):
    _live(monkeypatch, [{"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"}])
    _recording_run(monkeypatch, behaviour="hang")

    status, body = _get(live_server, path)

    # A 500 says the bridge broke. It did not: it is up, and one thing
    # behind it is stuck. The distinction is the same one the client now
    # draws between a dead channel and a stalled request.
    assert status == 504
    assert body["reason"] == "herdr_timeout"
    assert "herdr" in body["error"]


# --- a result that arrives after the bridge stopped waiting ------------
#
# Measured on the live host: 189 result files sitting uncollected, of which
# 76 belonged to tasks the bridge had marked error, 2 to orphaned ones, and
# 111 to no task at all. None belonged to a done task -- normal collection
# deletes the file, so every one of these was a result the agent delivered
# and the bridge threw away. The 111 are synchronous asks: /ask never wrote
# a row, so when its result arrived after the timeout the caller held a
# task_id that returned 404 and the answer had nowhere to go.


def _result_for(task_id, text="the late answer"):
    with open(bridge.result_file_path(task_id), "w", encoding="utf-8") as f:
        f.write(text)


def _make_task(tmp_path, monkeypatch, status, error_text="boom"):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    task_id = bridge.create_task("audit", 60000, "w1:p1")
    with bridge.db_session() as conn:
        conn.execute(
            "UPDATE tasks SET status = ?, error_text = ? WHERE task_id = ?",
            (status, error_text, task_id),
        )
    return task_id


@pytest.mark.parametrize("status", ["error", "orphaned"])
def test_a_late_result_is_adopted(tmp_path, monkeypatch, status):
    task_id = _make_task(tmp_path, monkeypatch, status, "gave up waiting")
    _result_for(task_id)

    task = bridge.adopt_late_result(task_id)

    assert task["status"] == "done"
    assert task["result_text"] == "the late answer"
    assert task["recovered_at"]
    # History is kept rather than rewritten: what went wrong is still true.
    assert "gave up waiting" in task["error_text"]
    assert "late" in task["error_text"].lower()
    # Taken, so it is not adopted twice and does not linger.
    assert not os.path.exists(bridge.result_file_path(task_id))


@pytest.mark.parametrize("status", ["done", "queued", "running", "quota_exhausted"])
def test_only_failed_or_orphaned_tasks_adopt(tmp_path, monkeypatch, status):
    task_id = _make_task(tmp_path, monkeypatch, status)
    _result_for(task_id)

    # A running task's file is about to be read by the worker; adopting it
    # here would race the normal path. A queued one has no result to speak of.
    assert bridge.adopt_late_result(task_id) is None
    assert bridge.get_task(task_id)["status"] == status


def test_nothing_to_adopt_without_a_file_or_with_an_empty_one(tmp_path, monkeypatch):
    task_id = _make_task(tmp_path, monkeypatch, "error")

    assert bridge.adopt_late_result(task_id) is None

    _result_for(task_id, "   \n")
    # Whitespace is not an answer, and treating it as one would turn a
    # failure into a blank success.
    assert bridge.adopt_late_result(task_id) is None
    assert bridge.get_task(task_id)["status"] == "error"


def test_concurrent_adoptions_take_the_result_once(tmp_path, monkeypatch):
    task_id = _make_task(tmp_path, monkeypatch, "error")
    _result_for(task_id)
    outcomes = []

    threads = [
        threading_module.Thread(target=lambda: outcomes.append(bridge.adopt_late_result(task_id)))
        for _ in range(6)
    ]
    for t in threads: t.start()
    for t in threads: t.join()

    adopted = [o for o in outcomes if o]
    assert len(adopted) == 1
    assert bridge.get_task(task_id)["result_text"] == "the late answer"


def test_querying_a_task_adopts_its_late_result(tmp_path, monkeypatch, live_server):
    task_id = _make_task(tmp_path, monkeypatch, "orphaned")
    _result_for(task_id)

    status, body = _get(live_server, f"/tasks/{task_id}")

    assert body["task"]["status"] == "done"
    assert body["task"]["result_text"] == "the late answer"


def test_listing_tasks_adopts_late_results_too(tmp_path, monkeypatch, live_server):
    task_id = _make_task(tmp_path, monkeypatch, "error")
    _result_for(task_id)

    status, body = _get(live_server, "/tasks")

    assert [t["status"] for t in body["tasks"] if t["task_id"] == task_id] == ["done"]


def test_startup_adopts_the_backlog(tmp_path, monkeypatch):
    first = _make_task(tmp_path, monkeypatch, "error")
    second = bridge.create_task("other", 60000, "w1:p1")
    with bridge.db_session() as conn:
        conn.execute("UPDATE tasks SET status = 'orphaned' WHERE task_id = ?", (second,))
    _result_for(first)
    _result_for(second, "second answer")

    assert bridge.adopt_all_late_results() == 2
    assert bridge.get_task(second)["result_text"] == "second answer"


# --- synchronous ask leaves a row when a late result is possible -------


def _ask_failing_with(live_server, monkeypatch, error):
    monkeypatch.setattr(
        bridge, "run_with_quota_failover",
        lambda *a, **k: (_ for _ in ()).throw(error),
    )
    return _post(live_server, "/ask", {"task": "audit", "agent": "w1:p1", "timeout_ms": 5000})


def test_an_ask_that_times_out_leaves_a_recoverable_task(tmp_path, monkeypatch, live_server):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    _live(monkeypatch, [{"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"}])

    status, body = _ask_failing_with(live_server, monkeypatch, TimeoutError("gave up"))

    assert status == 504
    task_id = body["task_id"]
    # The caller holds this id. It used to 404, which is where the answer
    # went to die.
    assert bridge.get_task(task_id)["status"] == "orphaned"
    assert "recover" in body["hint"].lower()

    _result_for(task_id)
    _, later = _get(live_server, f"/tasks/{task_id}")
    assert later["task"]["status"] == "done"
    assert later["task"]["result_text"] == "the late answer"


def test_an_ask_with_no_result_after_a_reminder_is_recoverable_too(tmp_path, monkeypatch, live_server):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    _live(monkeypatch, [{"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"}])

    status, body = _ask_failing_with(
        live_server, monkeypatch, bridge.SentinelResultMissingError("never wrote it", raw_output="tail")
    )

    assert status == 502
    assert bridge.get_task(body["task_id"])["status"] == "error"


def test_a_successful_ask_still_writes_no_row(tmp_path, monkeypatch, live_server):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    _live(monkeypatch, [{"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"}])
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))
    monkeypatch.setattr(bridge, "run_herdr", compliant_agent("done"))

    status, body = _post(live_server, "/ask", {"task": "audit", "agent": "w1:p1", "timeout_ms": 5000})

    # The success path is untouched: no new write, no new place to go wrong.
    assert status == 200
    assert bridge.get_task(body["task_id"]) is None


def test_a_refused_ask_writes_no_row(tmp_path, monkeypatch, live_server):
    _live(monkeypatch, [{"agent": "opencode", "pane_id": "w1:p1", "agent_status": "working"}])
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("working", {"ok": True}))

    status, body = _post(live_server, "/ask", {"task": "audit", "agent": "w1:p1", "timeout_ms": 5000})

    # Nothing ran, so there is nothing that could arrive late.
    assert status == 409
    assert bridge.list_tasks() == []


# --- /health must not say ok while the worker is dead ------------------
#
# A bridge whose worker thread has died still accepts /delegate and returns
# a task_id, and the task never runs. /health used to report that state as
# {"ok": true, "worker_alive": false} -- a response that contradicts
# itself, and one every check that looks only at ok would pass.


class _DeadThread:
    def is_alive(self):
        return False


class _LiveThread:
    def is_alive(self):
        return True


def test_health_is_503_when_the_worker_is_dead(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "_worker_thread", _DeadThread())

    status, body = _get(live_server, "/health")

    assert status == 503
    assert body["ok"] is False
    assert body["reason"] == "worker_dead"
    assert body["worker_alive"] is False
    # Names the consequence, since the queue accepting work is what makes
    # this state look fine from outside.
    assert "never run" in body["error"]


def test_health_is_unchanged_with_a_live_worker(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "_worker_thread", _LiveThread())

    status, body = _get(live_server, "/health")

    assert status == 200
    assert body["ok"] is True
    assert body["worker_alive"] is True


def test_health_is_unchanged_when_no_worker_was_started(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "_worker_thread", None)

    status, body = _get(live_server, "/health")

    # Not started is not dead: the test harness and any embedding that
    # drives the queue itself must not be reported unhealthy for it.
    assert status == 200
    assert body["worker_alive"] is None


# --- orderly shutdown --------------------------------------------------
#
# bridge-restart stops the bridge with a signal, and the default action for
# one is to die on the spot: no log line, in-flight requests cut off, and a
# running task left to be marked orphaned later with a message that says
# nothing about why. The listener also stayed bound until the process was
# gone, which matters when a new one is started two seconds after.


class _FakeServer:
    def __init__(self):
        self.calls = []

    def shutdown(self):
        self.calls.append("shutdown")

    def server_close(self):
        self.calls.append("close")


@pytest.fixture
def _fresh_shutdown(monkeypatch):
    monkeypatch.setattr(bridge, "_shutting_down", False)
    monkeypatch.setattr(bridge, "_inflight", 0)


def test_requests_in_flight_are_counted(_fresh_shutdown):
    assert bridge._inflight == 0

    with bridge.track_inflight():
        assert bridge._inflight == 1
        with bridge.track_inflight():
            assert bridge._inflight == 2

    assert bridge._inflight == 0


def test_the_in_flight_count_is_released_when_a_handler_raises(_fresh_shutdown):
    with pytest.raises(RuntimeError):
        with bridge.track_inflight():
            raise RuntimeError("handler blew up")

    # A leaked count would make every later shutdown wait out its full
    # grace period for a request that ended long ago.
    assert bridge._inflight == 0


def test_the_listener_closes_before_draining(tmp_path, monkeypatch, _fresh_shutdown):
    _fresh_db(tmp_path, monkeypatch)
    server = _FakeServer()
    monkeypatch.setattr(bridge, "_inflight", 1)

    worker = threading_module.Thread(
        target=lambda: bridge.graceful_shutdown(server, grace_sec=5, signame="SIGTERM")
    )
    worker.start()
    time.sleep(0.3)

    # Still waiting on the in-flight request, yet the port is already free.
    # Closing last would leave it bound while a new process, started a couple
    # of seconds later, tried to take it.
    assert server.calls == ["shutdown", "close"]
    assert worker.is_alive()

    monkeypatch.setattr(bridge, "_inflight", 0)
    worker.join(timeout=5)
    assert not worker.is_alive()


def test_draining_gives_up_after_the_grace_period(tmp_path, monkeypatch, _fresh_shutdown):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "_inflight", 3)

    started = time.monotonic()
    drained = bridge.graceful_shutdown(_FakeServer(), grace_sec=0.4, signame="SIGTERM")

    # A synchronous ask can run for minutes; waiting for it would turn
    # every restart into a hang.
    assert drained is False
    assert time.monotonic() - started < 3


def test_a_running_task_is_orphaned_with_the_real_reason(tmp_path, monkeypatch, _fresh_shutdown):
    _fresh_db(tmp_path, monkeypatch)
    running = bridge.create_task("audit", 60000, "w1:p1")
    queued = bridge.create_task("later", 60000, "w1:p1")
    bridge.claim_task(running)

    bridge.graceful_shutdown(_FakeServer(), grace_sec=0, signame="SIGTERM")

    task = bridge.get_task(running)
    assert task["status"] == "orphaned"
    assert "SIGTERM" in task["error_text"]
    # Says what happens next instead of leaving the reader to wonder.
    assert "recover" in task["error_text"].lower()
    # A task that never started has nothing to be orphaned about.
    assert bridge.get_task(queued)["status"] == "queued"


def test_a_second_signal_does_nothing(tmp_path, monkeypatch, _fresh_shutdown):
    _fresh_db(tmp_path, monkeypatch)
    server = _FakeServer()

    bridge.graceful_shutdown(server, grace_sec=0, signame="SIGHUP")
    bridge.graceful_shutdown(server, grace_sec=0, signame="SIGTERM")

    # bridge-restart sends two in quick succession (the screen session being
    # quit, then the kill), so the second must not repeat the first.
    assert server.calls == ["shutdown", "close"]


# --- the real thing: a signal to a real process ------------------------
#
# The tests above drive graceful_shutdown() directly. What they cannot cover
# is the wiring: that the handler is registered, that it does not deadlock by
# calling server.shutdown() on the thread that is inside serve_forever(), and
# that the process actually exits. This starts a real bridge and signals it.
#
# POSIX only: Windows cannot deliver SIGTERM or SIGHUP to another process, so
# on a Windows machine this is skipped and CI (ubuntu) is what runs it.


def _wait_until_up(port, proc, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError("bridge exited early:\n" + proc.stdout.read())
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/health")
            conn.getresponse().read()
            conn.close()
            return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("bridge never came up")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot deliver POSIX signals")
@pytest.mark.parametrize("sig_name", ["SIGTERM", "SIGHUP"])
def test_a_real_signal_stops_the_bridge_in_an_orderly_way(tmp_path, sig_name):
    import signal as signal_module
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    db = tmp_path / "tasks.db"
    env = dict(
        os.environ,
        SENTINEL_BRIDGE_PORT=str(port),
        SENTINEL_DB=str(db),
        SENTINEL_RESULT_DIR=str(tmp_path / "results"),
        SENTINEL_SHUTDOWN_GRACE_SEC="1",
        HERDR_BIN="/bin/false",
    )
    proc = subprocess_module.Popen(
        [sys.executable, "-u", "bridge.py"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=env,
        stdout=subprocess_module.PIPE,
        stderr=subprocess_module.STDOUT,
        text=True,
    )

    try:
        _wait_until_up(port, proc)

        # A task the bridge believes is running, planted once it is up so the
        # startup orphaning does not already claim it.
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO tasks (task_id, task, agent, status, created_at, timeout_ms)"
            " VALUES ('running-1', 'audit', 'w1:p1', 'running', 'now', 60000)"
        )
        conn.commit()
        conn.close()

        proc.send_signal(getattr(signal_module, sig_name))
        exit_code = proc.wait(timeout=15)
        output = proc.stdout.read()
    finally:
        if proc.poll() is None:
            proc.kill()

    # It exits, rather than dying on the spot or hanging on its own handler.
    assert exit_code == 0, output
    assert f"[shutdown] {sig_name} received" in output

    # A replacement process can take the port straight away. SO_REUSEADDR
    # because the server sets it (HTTPServer.allow_reuse_address) and a
    # just-closed port keeps TIME_WAIT connections from the health polling;
    # without it this fails with "Address already in use" on Linux, which is
    # exactly how it first failed in CI -- a flaw in this test, not the bridge.
    #
    # Note what this does NOT show: the process has exited, so the OS would
    # release the port regardless. That the listener closes *before* draining
    # begins is what test_the_listener_closes_before_draining covers.
    with socket.socket() as again:
        again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        again.bind(("127.0.0.1", port))

    conn = sqlite3.connect(db)
    status, error_text = conn.execute(
        "SELECT status, error_text FROM tasks WHERE task_id = 'running-1'"
    ).fetchone()
    conn.close()

    assert status == "orphaned"
    assert sig_name in error_text


# --- herdr aborting its wait is not the prompt failing -----------------
#
# The bridge sends `herdr agent prompt --wait`, which delivers the prompt and
# then waits for the agent to finish. If herdr sees the agent become
# "blocked" during that wait it aborts with agent_blocked -- and it does so
# for a transient blocked as readily as for a real one. The bridge read that
# as "the prompt failed", marked the task error, and told the caller the
# agent "requires interactive input".
#
# Measured on the live host: of 129 failed tasks, 96 were agent_blocked, and
# 69 of those 96 (72%) had in fact delivered a result. One of three sampled
# failures showed herdr reporting `blocked`, then `working` one second later;
# a real approval prompt does not clear itself in a second. The agent was
# doing the work. A caller then repeated herdr's wording as "blocked by an
# interactive menu" -- three times, each wrong.
#
# So an abort is not a verdict. The bridge observes what the agent does next
# and only asserts "blocked" once it has stayed blocked.


_ABORT_STDERR = json_module.dumps({"error": {
    "code": "agent_blocked",
    "message": "agent w1:p1 is blocked and requires interactive input",
}})


def _abort_world(monkeypatch, tmp_path, statuses, task_id, result_after=None):
    """herdr aborts the wait; the agent's status then follows `statuses`.

    result_after: write the result file once get_agent_status has been
    called that many times, as an agent finishing partway through.
    """
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "ABORT_POLL_SEC", 0.01)
    monkeypatch.setattr(bridge, "NOT_STARTED_GRACE_SEC", 0.2)
    monkeypatch.setattr(bridge, "BLOCKED_CONFIRM_SEC", 0.2)

    calls = {"n": 0}

    def fake_status(agent, *a, **k):
        index = calls["n"]
        calls["n"] += 1
        if result_after is not None and calls["n"] >= result_after:
            _result_for(task_id, "the finished answer")
        return statuses[min(index, len(statuses) - 1)], {"ok": True}

    monkeypatch.setattr(bridge, "get_agent_status", fake_status)

    def fake_run(*args, **kwargs):
        if args[1] == "prompt":
            return {"ok": False, "stdout": "", "stderr": _ABORT_STDERR}
        if args[1] == "read":
            return {"ok": True, "stdout": "terminal tail", "stderr": ""}
        return {"ok": True, "stdout": "{}", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run)


TASK = "cccccccc-1111-2222-3333-444444444444"


def test_a_transient_blocked_does_not_fail_a_task_that_goes_on_to_finish(tmp_path, monkeypatch):
    # blocked -> working -> working, and the result turns up: the exact
    # shape of the live failures.
    _abort_world(monkeypatch, tmp_path, ["blocked", "working", "working", "working"],
                 TASK, result_after=4)

    result = bridge.execute_sentinel_task("w1:p1", TASK, "audit", 60000)

    assert result == "the finished answer"


def test_a_transient_blocked_followed_by_the_agent_finishing_is_fine(tmp_path, monkeypatch):
    # The turn ended and the file is there by the time status says done.
    _abort_world(monkeypatch, tmp_path, ["blocked", "working", "done"], TASK, result_after=3)

    assert bridge.execute_sentinel_task("w1:p1", TASK, "audit", 60000) == "the finished answer"


def test_it_only_says_blocked_once_the_agent_has_stayed_blocked(tmp_path, monkeypatch):
    _abort_world(monkeypatch, tmp_path, ["blocked"], TASK)

    with pytest.raises(bridge.SentinelPromptError) as exc_info:
        bridge.execute_sentinel_task("w1:p1", TASK, "audit", 60000)

    error = exc_info.value
    # The one case where the claim is earned: it did not clear.
    assert error.reason == "blocked_confirmed"
    assert "stayed blocked" in str(error)
    assert "interactive" in str(error)


def test_an_agent_that_never_starts_is_not_called_blocked(tmp_path, monkeypatch):
    _abort_world(monkeypatch, tmp_path, ["done"], TASK)

    with pytest.raises(bridge.SentinelPromptError) as exc_info:
        bridge.execute_sentinel_task("w1:p1", TASK, "audit", 60000)

    error = exc_info.value
    # herdr aborted, the agent showed no activity, and no result appeared.
    # Whether the prompt was delivered is genuinely unknown, and saying
    # "blocked" here would be the very misreport this exists to stop.
    assert error.reason == "delivery_unknown"
    assert "unknown whether" in str(error)
    assert "interactive" not in str(error)


def test_waiting_after_an_abort_stops_at_the_tasks_own_deadline(tmp_path, monkeypatch):
    _abort_world(monkeypatch, tmp_path, ["working"], TASK)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        bridge.execute_sentinel_task("w1:p1", TASK, "audit", 300)   # 0.3 s budget

    # A prompt that never completes must not hold the worker forever.
    assert time.monotonic() - started < 3


def test_status_lookups_that_fail_do_not_end_the_wait(tmp_path, monkeypatch):
    _abort_world(monkeypatch, tmp_path, ["working"], TASK, result_after=5)
    real = bridge.get_agent_status
    count = {"n": 0}

    def flaky(agent, *a, **k):
        count["n"] += 1
        if count["n"] in (1, 2):
            raise RuntimeError("herdr hiccup")
        return real(agent, *a, **k)

    monkeypatch.setattr(bridge, "get_agent_status", flaky)

    # Observing the agent is a convenience; a failed look is not a verdict.
    assert bridge.execute_sentinel_task("w1:p1", TASK, "audit", 60000) == "the finished answer"


def test_a_herdr_timeout_is_a_timeout_not_a_prompt_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": False, "stdout": "",
        "stderr": json_module.dumps({"error": {"code": "timeout", "message": "wait timed out"}}),
    })

    # herdr's own wait expired: the prompt WAS delivered and the agent is
    # still going. That is the same event as the bridge's subprocess timeout,
    # and the same recovery applies -- a 504, a row, a late result adopted --
    # where it used to surface as a 500 "prompt command failed".
    with pytest.raises(TimeoutError):
        bridge.execute_sentinel_task("w1:p1", TASK, "audit", 60000)


def test_the_reminder_prompt_gets_the_same_treatment(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "ABORT_POLL_SEC", 0.01)
    monkeypatch.setattr(bridge, "NOT_STARTED_GRACE_SEC", 0.2)
    monkeypatch.setattr(bridge, "BLOCKED_CONFIRM_SEC", 0.2)

    prompts = []

    def fake_run(*args, **kwargs):
        if args[1] == "prompt":
            prompts.append(args[3])
            if len(prompts) == 1:
                return {"ok": True, "stdout": "", "stderr": ""}     # task prompt: fine, no file
            return {"ok": False, "stdout": "", "stderr": _ABORT_STDERR}   # reminder: aborted
        return {"ok": True, "stdout": "terminal tail", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run)
    statuses = iter(["blocked", "working", "working"])

    def fake_status(agent, *a, **k):
        value = next(statuses, "working")
        if value == "working" and len(prompts) == 2:
            _result_for(TASK, "written after the reminder")
        return value, {"ok": True}

    monkeypatch.setattr(bridge, "get_agent_status", fake_status)

    assert bridge.execute_sentinel_task("w1:p1", TASK, "audit", 60000) == "written after the reminder"


def test_a_worker_task_survives_a_transient_blocked(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    task_id = bridge.create_task("audit", 60000, "w1:p1")
    _abort_world(monkeypatch, tmp_path, ["idle", "blocked", "working", "working"],
                 task_id, result_after=5)

    stop = threading_module.Event()
    worker = threading_module.Thread(target=bridge.task_worker, args=(stop,), daemon=True)
    worker.start()
    for _ in range(200):
        if bridge.get_task(task_id)["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    stop.set()
    worker.join(timeout=5)

    # Before: error, "agent_blocked", with a good result arriving a moment
    # later to be thrown away.
    assert bridge.get_task(task_id)["status"] == "done"
    assert bridge.get_task(task_id)["result_text"] == "the finished answer"


def test_ask_reports_a_confirmed_block_and_records_it_for_recovery(tmp_path, monkeypatch, live_server):
    _live(monkeypatch, [{"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"}])
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))
    error = bridge.SentinelPromptError("it stayed blocked", raw_output="tail", reason="blocked_confirmed")
    monkeypatch.setattr(
        bridge, "run_with_quota_failover",
        lambda *a, **k: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    status, body = _post(live_server, "/ask", {"task": "audit", "agent": "w1:p1", "timeout_ms": 5000})

    assert status == 500
    # The caller is told *which* failure this is, instead of having to infer
    # it from herdr's wording.
    assert body["reason"] == "blocked_confirmed"
    assert bridge.get_task(body["task_id"])["status"] == "error"


# --- the blocked hint must not claim more than herdr can support -------


def test_the_blocked_hint_does_not_claim_a_certainty_herdr_cannot_give():
    hint = bridge.ready_hint("blocked")

    # herdr's `blocked` flickers while an agent is busy (observed: blocked,
    # then working a second later). The hint used to say the agent "will not
    # free itself", which is false often enough to have sent a caller off
    # reporting a menu that was not there.
    assert "will not free itself" not in hint
    assert "again" in hint.lower()
    assert "read" in hint


# --- a provider can refuse an agent for a reason that is not quota -------
#
# Observed live: an OpenCode session had grown to 379k tokens, past the 185k
# per-request cap its OpenRouter plan allows, so every prompt was refused
# within a second. herdr's status went working -> done exactly as it does for
# a success -- the only evidence was text in the agent's own terminal -- and
# the bridge did not recognise that text. So:
#
#   * the task failed as "never wrote its result file ... check permissions",
#     which sent the caller looking at the wrong thing;
#   * a `prompt` came back ok:true, and the caller waited on a compaction that
#     had been refused;
#   * `ready` kept saying the agent was ready, and the circuit that exists for
#     exactly "this agent's provider says no" never opened.

SIZE_LIMIT = (
    "Prompt tokens limit exceeded: 334110 > 185528. To increase, visit "
    "https://openrouter.ai/settings/credits and upgrade to a paid account"
)

# As the bridge actually received it: the agent's TUI is two columns, so the
# error is interleaved with the sidebar's text.
SIZE_LIMIT_TAIL = (
    "  ┃                                                              Context\n"
    "  ┃  Prompt tokens limit exceeded: 334110 > 185528. To increase,  379,392 tokens\n"
    "  ┃  visit https://openrouter.ai/settings/credits and upgrade     36% used\n"
    "  ┃                                                              $0.35 spent\n"
)


def _two_agents(monkeypatch):
    _live(monkeypatch, [
        {"agent": "opencode", "pane_id": "w1:p1", "agent_status": "idle"},
        {"agent": "claude", "pane_id": "w1:p3", "agent_status": "idle"},
    ])
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))


def _age_block(agent, seconds):
    stale = (
        datetime_module.now(datetime_module_tz.utc)
        - datetime_module_delta(seconds=seconds)
    ).isoformat()
    with bridge.db_session() as conn:
        conn.execute(
            "UPDATE quota_blocks SET detected_at = ? WHERE agent = ?", (stale, agent)
        )


def test_a_provider_size_limit_is_recognised_as_a_refusal():
    assert bridge.quota_error_detail(SIZE_LIMIT) is not None
    # ... including through the interleaving.
    assert bridge.quota_error_detail(SIZE_LIMIT_TAIL) is not None


def test_a_size_limit_is_told_apart_from_a_quota():
    # The remedy differs: a quota waits for a reset; a size limit is cured by
    # compacting or restarting the agent's session, and waiting never helps.
    assert bridge.provider_failure_kind(bridge.quota_error_detail(SIZE_LIMIT)) == "context_limit"
    assert bridge.provider_failure_kind("You've hit your session limit - resets 4:20pm") == "quota"


def test_talk_about_token_limits_is_not_a_provider_refusal():
    # A false positive opens a circuit that takes an agent out of service.
    assert bridge.quota_error_detail("batching is limited by the model's prompt token limit") is None

    task = "explain why 'Prompt tokens limit exceeded' errors appear"
    assert bridge.quota_error_detail(task + "\nok", ignore=task) is None


def test_a_size_limit_is_reported_as_one_not_as_a_missing_file(tmp_path, monkeypatch):
    prompts = []
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    def fake(*args, **kwargs):
        if args[1] == "prompt":
            prompts.append(args)
        if args[1] == "read":
            return {"ok": True, "stdout": SIZE_LIMIT_TAIL, "stderr": ""}
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake)

    with pytest.raises(bridge.AgentQuotaExhaustedError) as exc_info:
        bridge.execute_sentinel_task("w1:p1", "1", "audit", 1000)

    error = exc_info.value
    assert error.kind == "context_limit"
    assert "size limit" in str(error)
    # Not "quota exhausted": that sends a caller off to check credit.
    assert "quota exhausted" not in str(error).lower()
    # And not a second prompt into a session that refuses everything.
    assert len(prompts) == 1


def test_a_size_limit_fails_over_and_the_task_says_where_it_ran(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _two_agents(monkeypatch)

    def fake_execute(agent_name, task_id, task, timeout_ms, slurm_policy=None):
        if agent_name == "w1:p1":
            raise bridge.AgentQuotaExhaustedError(agent_name, SIZE_LIMIT)
        return "answered by the other agent"

    monkeypatch.setattr(bridge, "execute_sentinel_task", fake_execute)
    task_id = bridge.create_task("audit", 60000, "w1:p1")

    stop = threading_module.Event()
    worker = threading_module.Thread(target=bridge.task_worker, args=(stop,), daemon=True)
    worker.start()
    for _ in range(200):
        if bridge.get_task(task_id)["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    stop.set()
    worker.join(timeout=5)

    row = bridge.get_task(task_id)
    assert row["status"] == "done"
    assert row["result_text"] == "answered by the other agent"
    # The row used to say only `w1:p1`, so a result from a different agent
    # read as the first agent's work.
    assert "w1:p3" in row["error_text"]
    assert "w1:p1" in row["error_text"]
    assert bridge.get_agent_quota_block("w1:p1") is not None


def test_a_size_limit_circuit_clears_sooner_than_a_quota_one(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "QUOTA_BLOCK_TTL_SECONDS", 3600)
    monkeypatch.setattr(bridge, "CONTEXT_BLOCK_TTL_SECONDS", 600)
    bridge.mark_agent_quota_blocked("w1:p1", SIZE_LIMIT)
    bridge.mark_agent_quota_blocked("w1:p3", "session limit")
    _age_block("w1:p1", 1200)
    _age_block("w1:p3", 1200)

    # Someone fixes a bloated session in seconds. A circuit that lingered for
    # the quota's hour would keep a recovered agent out of service -- the
    # failure the quota TTL was added for.
    assert bridge.get_agent_quota_block("w1:p1") is None
    assert bridge.get_agent_quota_block("w1:p3") is not None


def test_ready_and_quota_explain_a_size_limit_block(live_server, monkeypatch):
    _two_agents(monkeypatch)
    bridge.mark_agent_quota_blocked("w1:p1", SIZE_LIMIT)

    status, body = _get(live_server, "/ready?agent=w1:p1")
    assert body["ready"] is False
    assert body["reason"] == "quota_blocked"          # unchanged for existing clients
    assert body["kind"] == "context_limit"
    hint = body["hint"].lower()
    # Says what to do, and what not to: a retry fails identically.
    assert "compact" in hint and "restart" in hint

    status, body = _get(live_server, "/quota")
    assert body["blocked_agents"][0]["kind"] == "context_limit"


def test_a_turn_that_ends_in_seconds_with_no_result_says_nothing_was_done(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    # Wording no pattern knows: the signature is the timing, not the text.
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": True,
        "stdout": "gateway refused the request (E4711)" if a[1] == "read" else "",
        "stderr": "",
    })

    with pytest.raises(bridge.SentinelResultMissingError) as exc_info:
        bridge.execute_sentinel_task("w1:p1", "1", "audit", 1000)

    error = exc_info.value
    assert error.reason == "ended_quickly"
    assert "never started" in str(error)
    # The permission advice is the wrong lead when no work happened at all.
    assert "permission" not in str(error)
    assert "gateway refused" in error.raw_output


def test_a_slow_turn_with_no_result_keeps_the_original_advice(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "QUICK_END_SEC", 0)
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": True, "stdout": "agent chatter" if a[1] == "read" else "", "stderr": "",
    })

    with pytest.raises(bridge.SentinelResultMissingError) as exc_info:
        bridge.execute_sentinel_task("w1:p1", "1", "audit", 1000)

    # Work that ran for a while and then lost its file really can be a
    # permissions problem; only a turn too short to have done anything is not.
    assert exc_info.value.reason is None
    assert "permission system" in str(exc_info.value)


def _prompt_world(monkeypatch, tmp_path, before, after, targets=None):
    """A herdr whose terminal reads `before` first, then `after`."""
    _two_agents(monkeypatch)
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    reads = {"n": 0}

    def fake(*args, **kwargs):
        if args[1] == "read":
            reads["n"] += 1
            return {"ok": True, "stdout": before if reads["n"] == 1 else after, "stderr": ""}
        if args[1] == "prompt" and targets is not None:
            targets.append(args[2])
        return {"ok": True, "stdout": "{}", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake)


def test_prompt_reports_a_refusal_that_appeared_while_it_ran(live_server, tmp_path, monkeypatch):
    _prompt_world(monkeypatch, tmp_path, "an idle prompt\n", SIZE_LIMIT_TAIL)

    status, body = _post(live_server, "/prompt", {"task": "do it", "agent": "w1:p1", "timeout_ms": 5000})

    # herdr called this a success -- done, exactly as for a real one -- and
    # the bridge relayed ok:true, so the caller waited on work that had been
    # refused within a second.
    assert body["ok"] is False
    assert body["reason"] == "provider_rejected"
    assert body["kind"] == "context_limit"
    assert "334110" in body["error"]
    assert bridge.get_agent_quota_block("w1:p1") is not None


def test_prompt_does_not_blame_an_error_that_was_already_on_screen(live_server, tmp_path, monkeypatch):
    # An old refusal still in view, and a prompt that went fine: the same
    # evidence both before and after is not news about *this* prompt, and
    # reading it as one would trip a circuit on a healthy agent.
    _prompt_world(monkeypatch, tmp_path, SIZE_LIMIT_TAIL, SIZE_LIMIT_TAIL)

    status, body = _post(live_server, "/prompt", {"task": "do it", "agent": "w1:p1", "timeout_ms": 5000})

    assert body["ok"] is True
    assert bridge.get_agent_quota_block("w1:p1") is None


def test_a_pinned_prompt_goes_to_the_agent_it_names_even_past_an_open_circuit(
    live_server, tmp_path, monkeypatch
):
    targets = []
    _prompt_world(monkeypatch, tmp_path, "idle\n", "idle\n", targets)
    bridge.mark_agent_quota_blocked("w1:p1", SIZE_LIMIT)

    status, body = _post(live_server, "/prompt", {"task": "do it", "agent": "w1:p1", "timeout_ms": 5000})

    # Failover is for work any agent can do. A prompt aimed at one agent --
    # the way to act on a session that needs compacting -- means nothing
    # elsewhere, and quietly sending it to the other agent acts on the wrong
    # one.
    assert body["ok"] is True
    assert body["agent"] == "w1:p1"
    assert targets == ["w1:p1"]


def test_the_evidence_is_the_providers_column_not_its_neighbours():
    # An agent's TUI is two columns. Taken as a flat window, the evidence
    # around a hit spilled into whatever sat beside it -- measured on a real
    # capture, a path from the delegated task. The detail is served by /ready
    # and /quota and quoted in task notes, so what is in it matters.
    two_columns = (
        "  ┃  想让远端看到进展（拿到 job ID、卡在┃  Prompt tokens limit exceeded: 334110 > 185528. To  ┃    Context\n"
        "  ┃  /srv/projects/secret-cohort/sent    ┃  increase, visit https://openrouter.ai/settings/   ┃    379,392 tokens\n"
    )

    detail = bridge.quota_error_detail(two_columns)

    assert "334110 > 185528" in detail
    assert "secret-cohort" not in detail
    assert "379,392" not in detail
    assert "想让远端" not in detail


def test_evidence_in_a_single_column_still_keeps_its_context():
    # No separators, no change: a provider message that wraps over lines
    # keeps the line that carries the reset time.
    detail = bridge.quota_error_detail("Usage limit reached\nresets 4:20pm\n")

    assert "Usage limit reached" in detail
    assert "resets 4:20pm" in detail


# --- reading an agent that is working ------------------------------------
#
# herdr will not read the *history* of a working agent: "cannot read 120 lines
# while w1:p3 is working: its alternate-screen history can only be captured by
# scrolling while idle. Wait and retry, or use --source visible". The bridge's
# default is more lines than any screen holds, so every read of a working
# agent failed -- 33 times in one night, each answered by the same refusal
# after the caller followed herdr's advice, because the bridge could not
# honour it: the source was hard-coded. The one time a caller most wants to
# see an agent's terminal is while it is working.

NOT_IDLE = json_module.dumps({"error": {
    "code": "agent_not_idle",
    "message": "cannot read 120 lines while w1:p3 is working: its alternate-screen "
               "history can only be captured by scrolling while idle. Wait and "
               "retry, or use --source visible",
}, "id": "cli:agent:read"})


def _read_world(monkeypatch, history_refused=True):
    """herdr refusing to read history (not the visible screen) when asked to."""
    reads = []

    def fake(*args, **kwargs):
        if args[1] == "read":
            source = args[args.index("--source") + 1]
            reads.append((source, "--lines" in args))
            if source != "visible" and history_refused:
                return {"ok": False, "returncode": 1, "stdout": "", "stderr": NOT_IDLE}
            return {"ok": True, "returncode": 0, "stdout": f"screen via {source}", "stderr": ""}
        return {"ok": True, "returncode": 0, "stdout": "{}", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake)
    _two_agents(monkeypatch)
    return reads


def test_reading_a_working_agent_falls_back_to_the_visible_screen(live_server, monkeypatch):
    reads = _read_world(monkeypatch)

    status, body = _get(live_server, "/read?agent=w1:p3")

    assert status == 200 and body["ok"] is True
    assert body["stdout"] == "screen via visible"
    assert body["source"] == "visible"
    # Says plainly that this is not what was asked for, and why.
    assert "working" in body["note"] and "visible" in body["note"]
    # The visible screen is asked for without a line count: it is the screen.
    assert reads == [("recent-unwrapped", True), ("visible", False)]


def test_read_honours_an_explicit_source(live_server, monkeypatch):
    reads = _read_world(monkeypatch)

    status, body = _get(live_server, "/read?agent=w1:p3&source=visible&lines=300")

    assert status == 200
    assert body["source"] == "visible"
    assert "note" not in body
    assert reads == [("visible", False)]


def test_read_rejects_a_source_herdr_does_not_offer(live_server, monkeypatch):
    reads = _read_world(monkeypatch)

    status, body = _get(live_server, "/read?agent=w1:p3&source=bogus")

    assert status == 400
    assert "visible" in body["error"]
    assert reads == []


def test_an_explicit_history_read_is_not_quietly_swapped_for_the_screen(live_server, monkeypatch):
    reads = _read_world(monkeypatch)

    status, body = _get(live_server, "/read?agent=w1:p3&source=recent-unwrapped")

    # The caller named the source, so it gets what it asked for -- here, the
    # refusal -- plus the way forward, instead of a different answer.
    assert status == 500 and body["ok"] is False
    assert "source=visible" in body["hint"]
    assert reads == [("recent-unwrapped", True)]


def test_an_idle_agent_is_read_exactly_as_before(live_server, monkeypatch):
    reads = _read_world(monkeypatch, history_refused=False)

    status, body = _get(live_server, "/read?agent=w1:p3&lines=40")

    assert status == 200
    assert body["source"] == "recent-unwrapped"
    assert "note" not in body
    assert reads == [("recent-unwrapped", True)]


def test_failure_diagnostics_use_the_visible_screen_when_history_is_refused(monkeypatch):
    _read_world(monkeypatch)

    # Evidence is wanted exactly when something has gone wrong, and that is
    # often while the agent is still busy or stuck on a prompt. "(terminal
    # read failed ...)" is no evidence at all; the screen is.
    assert bridge._read_terminal_tail("w1:p3", 120) == "screen via visible"


# --- a herdr call that is killed has not reported a failed prompt --------
#
# bridge-restart quits the screen session, which hangs up the whole process
# group -- including the `herdr agent prompt` the worker is waiting on. That
# call then ends with no output at all, and the bridge reported it as "Herdr
# prompt command failed:" followed by nothing, marking the task `error`.
# Seen on a real deploy. `error` means it failed, so retrying is safe;
# whether the prompt had landed was never known. An agent that had just been
# handed a Slurm submission would be handed it again.


def test_a_herdr_call_that_dies_without_a_word_is_not_a_failed_prompt(monkeypatch):
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": False, "returncode": -1, "stdout": "", "stderr": "",
    })

    # Not SentinelPromptError ("it failed"): TimeoutError, "the bridge lost
    # track and the agent may still be working".
    with pytest.raises(TimeoutError):
        bridge._run_herdr_prompt("w1:p3", "task", 1000)


def test_a_failure_that_explains_itself_is_still_a_failure(monkeypatch):
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": False, "returncode": 1, "stdout": "",
        "stderr": "error: unrecognised option",
    })

    with pytest.raises(bridge.SentinelPromptError):
        bridge._run_herdr_prompt("w1:p3", "task", 1000)


def test_a_task_whose_herdr_call_is_killed_ends_orphaned_not_error(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _two_agents(monkeypatch)
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: (
        {"ok": False, "returncode": -1, "stdout": "", "stderr": ""}
        if a[1] == "prompt" else {"ok": True, "returncode": 0, "stdout": "tail", "stderr": ""}
    ))
    task_id = bridge.create_task("audit", 60000, "w1:p3")

    stop = threading_module.Event()
    worker = threading_module.Thread(target=bridge.task_worker, args=(stop,), daemon=True)
    worker.start()
    for _ in range(200):
        if bridge.get_task(task_id)["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    stop.set()
    worker.join(timeout=5)

    row = bridge.get_task(task_id)
    assert row["status"] == "orphaned"
    assert "may still" in row["error_text"]


def test_an_error_never_overwrites_an_orphaned_task(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    task_id = bridge.create_task("audit", 60000, "w1:p3")
    bridge.orphan_task(task_id, "stopped while running")

    # `orphaned` says "may have run; do not retry". Whichever of the shutdown
    # and the worker gets there second must not downgrade that to `error`.
    bridge.fail_task(task_id, "late failure")

    assert bridge.get_task(task_id)["status"] == "orphaned"


# --- /prompt: herdr's wait aborting is not the prompt failing ------------
#
# Applied to tasks in v23; /prompt called the raw prompt and still answered a
# transient `blocked` with a 500 saying the agent "requires interactive
# input".


def test_prompt_survives_a_transient_blocked(live_server, tmp_path, monkeypatch):
    _two_agents(monkeypatch)
    _abort_world(monkeypatch, tmp_path, ["idle", "blocked", "working", "done"], TASK)

    status, body = _post(live_server, "/prompt", {"task": "do it", "agent": "w1:p3", "timeout_ms": 5000})

    assert status == 200 and body["ok"] is True


def test_prompt_that_stays_blocked_says_so(live_server, tmp_path, monkeypatch):
    _two_agents(monkeypatch)
    _abort_world(monkeypatch, tmp_path, ["idle", "blocked"], TASK)

    status, body = _post(live_server, "/prompt", {"task": "do it", "agent": "w1:p3", "timeout_ms": 5000})

    assert body["ok"] is False
    assert body["reason"] == "blocked_confirmed"


def test_prompt_that_never_started_is_not_called_blocked(live_server, tmp_path, monkeypatch):
    _two_agents(monkeypatch)
    _abort_world(monkeypatch, tmp_path, ["idle", "done"], TASK)

    status, body = _post(live_server, "/prompt", {"task": "do it", "agent": "w1:p3", "timeout_ms": 5000})

    assert body["ok"] is False
    assert body["reason"] == "delivery_unknown"
    assert "interactive" not in body["error"]

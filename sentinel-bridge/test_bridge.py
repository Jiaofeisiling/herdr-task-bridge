import sys
import os
import re
import sqlite3

sys.path.insert(0, os.path.dirname(__file__))

import bridge


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


def test_prompt_returns_prompt_result_without_reading(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda *a, **k: ("idle", {"ok": True}))

    calls = []

    def fake_run_herdr(*args, **kwargs):
        calls.append(args[1])
        return {"ok": True, "stdout": "prompt output", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    status, body = _post(live_server, "/prompt", {"task": "do something"})

    assert status == 200
    assert body["prompt"]["stdout"] == "prompt output"
    assert calls == ["prompt"]  # /prompt never calls "read"


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

    monkeypatch.setattr(bridge, "create_task_if_queue_available", boom)

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
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": False, "stdout": "", "stderr": "herdr not reachable",
    })

    status, body = _get(live_server, "/agents")

    assert status == 503
    assert body["ok"] is False


def test_delegate_stores_explicit_agent(live_server):
    status, body = _post(
        live_server, "/delegate", {"task": "check disk", "agent": "agent-a"}
    )

    assert status == 202
    task = bridge.get_task(body["task_id"])
    assert task["agent"] == "agent-a"


def test_delegate_defaults_agent_when_not_specified(live_server):
    status, body = _post(live_server, "/delegate", {"task": "check disk"})

    task = bridge.get_task(body["task_id"])
    assert task["agent"] == bridge.DEFAULT_AGENT


def test_ask_uses_explicit_agent(live_server, tmp_path, monkeypatch):
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

    assert seen["agent_name"] == bridge.DEFAULT_AGENT


def test_health_reports_default_agent(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "DEFAULT_AGENT", "agent-a")

    status, body = _get(live_server, "/health")

    assert status == 200
    assert body["default_agent"] == "agent-a"
    assert body["agent"] == "agent-a"
    assert body["version"] == bridge.BRIDGE_VERSION


def test_delegate_rejects_invalid_agent_names(live_server):
    for agent_name in (None, "", "   ", ["agent-a"]):
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
    calls = []

    def fake_run_herdr(*args, **kwargs):
        calls.append(args)
        return {"ok": True, "stdout": "output", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    status, body = _get(live_server, "/read?agent=agent-a&lines=37")

    assert status == 200
    assert body["ok"] is True
    assert calls == [("agent", "read", "agent-a", "--source", "recent-unwrapped", "--lines", "37")]


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


def test_delegation_prompt_exempts_the_result_file_from_read_only_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))

    prompt = bridge.build_delegation_prompt(
        "只读探测，不要修改任何文件", "aaaaaaaa-1111-2222-3333-444444444444"
    )

    # Without this carve-out the prompt contradicts its own task text: the
    # task says "modify nothing", the contract demands a file write. A real
    # agent flagged the conflict in its own answer before this was added.
    assert "不包括" in prompt or "例外" in prompt


def test_missing_result_error_points_at_write_permission(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": True, "stdout": "terminal tail", "stderr": "",
    })

    with pytest.raises(bridge.SentinelResultMissingError) as exc_info:
        bridge.execute_sentinel_task(
            "sentinel", "bbbbbbbb-1111-2222-3333-444444444444", "task", 60000
        )

    # the most likely cause by far -- agents run under permission systems
    # that can deny writes outside an allowlist, and the denial lands after
    # the work is already done
    assert "权限" in str(exc_info.value) or "permission" in str(exc_info.value).lower()
    assert str(tmp_path) in str(exc_info.value)

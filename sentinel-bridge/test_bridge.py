import sys
import os
import sqlite3

sys.path.insert(0, os.path.dirname(__file__))

import bridge


def test_build_delegation_prompt_includes_token_and_task():
    task_id = "11111111-2222-3333-4444-555555555555"
    prompt = bridge.build_delegation_prompt("检查磁盘", task_id)

    assert task_id.replace("-", "") in prompt
    assert "检查磁盘" in prompt
    assert "SENTINEL_DONE_" in prompt


def test_extract_task_response_finds_marker_after_assistant_marker():
    task_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    token = task_id.replace("-", "")
    raw = f"some earlier noise\n● 这是最终总结\nSENTINEL_DONE_{token}"

    result = bridge.extract_task_response(raw, task_id)

    assert result == "这是最终总结"


def test_extract_task_response_returns_none_when_marker_missing():
    task_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    raw = "Claude finished but forgot to print anything useful"

    result = bridge.extract_task_response(raw, task_id)

    assert result is None


def test_extract_task_response_falls_back_without_assistant_marker():
    task_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    token = task_id.replace("-", "")
    raw = f"raw terminal text with no bullet marker\nSENTINEL_DONE_{token}"

    result = bridge.extract_task_response(raw, task_id)

    assert result == "raw terminal text with no bullet marker"


def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(bridge, "DB_PATH", str(db_path))
    bridge.init_db()
    return db_path


def test_create_and_get_task(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    task_id = bridge.create_task("check disk space", 60000)
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

    first = bridge.create_task("first", 1000)
    second = bridge.create_task("second", 1000)

    tasks = bridge.list_tasks()

    assert [t["task_id"] for t in tasks] == [second, first]


def test_claim_task_only_succeeds_once(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    task_id = bridge.create_task("t", 1000)

    assert bridge.claim_task(task_id) is True
    assert bridge.claim_task(task_id) is False
    assert bridge.get_task(task_id)["status"] == "running"


def test_peek_next_task_returns_oldest_queued(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    first = bridge.create_task("first", 1000)
    bridge.create_task("second", 1000)

    assert bridge.peek_next_task()["task_id"] == first


def test_peek_next_task_returns_none_when_empty(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    assert bridge.peek_next_task() is None


def test_complete_task_sets_result(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    task_id = bridge.create_task("t", 1000)
    bridge.claim_task(task_id)
    bridge.complete_task(task_id, "all good")

    task = bridge.get_task(task_id)
    assert task["status"] == "done"
    assert task["result_text"] == "all good"
    assert task["finished_at"] is not None


def test_fail_task_sets_error(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    task_id = bridge.create_task("t", 1000)
    bridge.claim_task(task_id)
    bridge.fail_task(task_id, "boom")

    task = bridge.get_task(task_id)
    assert task["status"] == "error"
    assert task["error_text"] == "boom"


def test_init_db_orphans_stale_running_rows_on_restart(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    task_id = bridge.create_task("t", 1000)
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

    status, _ = bridge.get_agent_status()

    assert status == "idle"


def test_get_agent_status_herdr_failure_returns_none(monkeypatch):
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": False, "stdout": "", "stderr": "connection refused",
    })

    status, result = bridge.get_agent_status()

    assert status is None
    assert result["ok"] is False


def test_get_agent_status_bad_json_returns_none(monkeypatch):
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": True, "stdout": "not json", "stderr": "",
    })

    status, result = bridge.get_agent_status()

    assert status is None
    assert result["ok"] is False
    assert "Unable to parse" in result["error"]


def test_available_states_contains_idle_and_done():
    assert bridge.AVAILABLE_STATES == {"idle", "done"}


import subprocess as subprocess_module

import pytest


def test_execute_sentinel_task_happy_path(monkeypatch):
    task_id = "11111111-1111-1111-1111-111111111111"
    token = task_id.replace("-", "")
    marker_output = f"● 一切正常\nSENTINEL_DONE_{token}"

    def fake_run_herdr(*args, **kwargs):
        if args[1] == "prompt":
            return {"ok": True, "stdout": "", "stderr": ""}
        return {"ok": True, "stdout": marker_output, "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    result = bridge.execute_sentinel_task(task_id, "do a thing", 60000)

    assert result == "一切正常"


def test_execute_sentinel_task_prompt_failure_raises(monkeypatch):
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": False, "stdout": "", "stderr": "herdr not found",
    })

    with pytest.raises(bridge.SentinelPromptError):
        bridge.execute_sentinel_task("id", "task", 60000)


def test_execute_sentinel_task_read_failure_raises(monkeypatch):
    def fake_run_herdr(*args, **kwargs):
        if args[1] == "prompt":
            return {"ok": True, "stdout": "", "stderr": ""}
        return {"ok": False, "stdout": "", "stderr": "read broke"}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    with pytest.raises(bridge.SentinelReadError):
        bridge.execute_sentinel_task("id", "task", 60000)


def test_execute_sentinel_task_timeout_raises(monkeypatch):
    def fake_run_herdr(*args, **kwargs):
        raise subprocess_module.TimeoutExpired(cmd="herdr", timeout=1)

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    with pytest.raises(TimeoutError):
        bridge.execute_sentinel_task("id", "task", 60000)


def test_execute_sentinel_task_recovers_missing_marker(monkeypatch):
    task_id = "22222222-2222-2222-2222-222222222222"
    token = task_id.replace("-", "")
    state = {"reads": 0}

    def fake_run_herdr(*args, **kwargs):
        if args[1] == "prompt":
            return {"ok": True, "stdout": "", "stderr": ""}

        state["reads"] += 1

        if state["reads"] == 1:
            return {"ok": True, "stdout": "没有 marker 的输出", "stderr": ""}

        return {
            "ok": True,
            "stdout": f"● 补发总结\nSENTINEL_DONE_{token}",
            "stderr": "",
        }

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    result = bridge.execute_sentinel_task(task_id, "task", 60000)

    assert result == "补发总结"


def test_execute_sentinel_task_marker_missing_after_recovery_raises(monkeypatch):
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": True, "stdout": "始终没有 marker", "stderr": "",
    })

    with pytest.raises(bridge.SentinelMarkerMissingError):
        bridge.execute_sentinel_task(
            "33333333-3333-3333-3333-333333333333", "task", 60000
        )


def test_execute_sentinel_task_raises_marker_missing_when_recovery_prompt_fails(monkeypatch):
    task_id = "44444444-4444-4444-4444-444444444444"
    state = {"prompt_calls": 0, "read_calls": 0}

    def fake_run_herdr(*args, **kwargs):
        if args[1] == "prompt":
            state["prompt_calls"] += 1
            if state["prompt_calls"] == 1:
                # First prompt (delegation) succeeds
                return {"ok": True, "stdout": "", "stderr": ""}
            else:
                # Second prompt (recovery) fails
                return {"ok": False, "stdout": "", "stderr": "recovery failed"}

        if args[1] == "read":
            state["read_calls"] += 1
            # All reads return no marker
            return {"ok": True, "stdout": "没有 marker 的输出", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    with pytest.raises(bridge.SentinelMarkerMissingError):
        bridge.execute_sentinel_task(task_id, "task", 60000)

    # Verify that we got exactly 2 prompt calls (initial + recovery) and 1 read call (no second read after failed recovery)
    assert state["prompt_calls"] == 2
    assert state["read_calls"] == 1


def test_acquire_agent_for_delegation_succeeds_when_idle(monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda: ("idle", {"ok": True}))

    entered = False
    with bridge.acquire_agent_for_delegation():
        entered = True

    assert entered is True
    assert bridge.AGENT_LOCK.locked() is False


def test_acquire_agent_for_delegation_raises_when_busy(monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda: ("working", {"ok": True}))

    with pytest.raises(bridge.SentinelBusyError):
        with bridge.acquire_agent_for_delegation():
            pass

    assert bridge.AGENT_LOCK.locked() is False


def test_acquire_agent_for_delegation_raises_when_locked_by_another_caller(monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda: ("idle", {"ok": True}))

    bridge.AGENT_LOCK.acquire()
    try:
        with pytest.raises(bridge.SentinelBusyError):
            with bridge.acquire_agent_for_delegation():
                pass
    finally:
        bridge.AGENT_LOCK.release()


def test_acquire_agent_for_delegation_raises_when_unreachable(monkeypatch):
    monkeypatch.setattr(
        bridge, "get_agent_status",
        lambda: (None, {"ok": False, "error": "no route"}),
    )

    with pytest.raises(bridge.SentinelUnavailableError):
        with bridge.acquire_agent_for_delegation():
            pass

    assert bridge.AGENT_LOCK.locked() is False


def test_acquire_agent_for_delegation_releases_lock_when_body_raises(monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda: ("idle", {"ok": True}))

    with pytest.raises(ValueError):
        with bridge.acquire_agent_for_delegation():
            raise ValueError("boom from caller body")
    assert bridge.AGENT_LOCK.locked() is False


def test_acquire_agent_for_delegation_converts_status_query_exception(monkeypatch):
    def boom():
        raise subprocess_module.TimeoutExpired(cmd="herdr", timeout=10)

    monkeypatch.setattr(bridge, "get_agent_status", boom)

    with pytest.raises(bridge.SentinelUnavailableError):
        with bridge.acquire_agent_for_delegation():
            pass

    assert bridge.AGENT_LOCK.locked() is False


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
    monkeypatch.setattr(bridge, "get_agent_status", lambda: ("idle", {"ok": True}))

    status, body = _get(live_server, "/ready")

    assert status == 200
    assert body["ready"] is True
    assert body["agent_status"] == "idle"


def test_ready_endpoint_reports_busy(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda: ("working", {"ok": True}))

    status, body = _get(live_server, "/ready")

    assert status == 200
    assert body["ready"] is False
    assert body["agent_status"] == "working"


def test_ready_endpoint_reports_unreachable(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda: (None, {"ok": False}))

    status, body = _get(live_server, "/ready")

    assert status == 503
    assert body["ready"] is False


def test_tasks_list_empty(live_server):
    status, body = _get(live_server, "/tasks")

    assert status == 200
    assert body["tasks"] == []


def test_tasks_list_returns_created_tasks(live_server):
    task_id = bridge.create_task("check disk", 1000)

    status, body = _get(live_server, "/tasks")

    assert status == 200
    assert body["tasks"][0]["task_id"] == task_id


def test_task_get_not_found(live_server):
    status, body = _get(live_server, "/tasks/does-not-exist")

    assert status == 404
    assert body["ok"] is False


def test_task_get_found(live_server):
    task_id = bridge.create_task("check disk", 1000)

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
    def boom():
        raise AssertionError("/delegate must not query Sentinel status")

    monkeypatch.setattr(bridge, "get_agent_status", boom)

    status, body = _post(live_server, "/delegate", {"task": "check disk"})

    assert status == 202


def test_ask_returns_409_when_busy(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda: ("working", {"ok": True}))

    status, body = _post(live_server, "/ask", {"task": "do something"})

    assert status == 409
    assert body["status"] == "busy"
    assert body["agent_status"] == "working"


def test_ask_returns_503_when_sentinel_unreachable(live_server, monkeypatch):
    monkeypatch.setattr(
        bridge, "get_agent_status",
        lambda: (None, {"ok": False, "error": "no route"}),
    )

    status, body = _post(live_server, "/ask", {"task": "do something"})

    assert status == 503
    assert body["status"] == "unavailable"


def test_ask_happy_path(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda: ("idle", {"ok": True}))

    def fake_run_herdr(*args, **kwargs):
        if args[1] == "prompt":
            return {"ok": True, "stdout": "", "stderr": ""}
        return {"ok": True, "stdout": "raw", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)
    monkeypatch.setattr(bridge, "extract_task_response", lambda raw, task_id: "总结完成")

    status, body = _post(live_server, "/ask", {"task": "do something"})

    assert status == 200
    assert body["result"]["text"] == "总结完成"


def test_prompt_returns_prompt_result_without_reading(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda: ("idle", {"ok": True}))

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
    monkeypatch.setattr(bridge, "get_agent_status", lambda: ("idle", {"ok": True}))

    def fake_run_herdr(*args, **kwargs):
        raise subprocess_module.TimeoutExpired(cmd="herdr", timeout=1)

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    status, body = _post(live_server, "/ask", {"task": "do something"})

    assert status == 504
    assert body["ok"] is False
    assert "task_id" in body


def test_ask_returns_502_when_marker_missing(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda: ("idle", {"ok": True}))
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": True, "stdout": "始终没有 marker", "stderr": "",
    })

    status, body = _post(live_server, "/ask", {"task": "do something"})

    assert status == 502
    assert body["ok"] is False
    assert "task_id" in body


def test_ask_returns_500_on_prompt_failure(live_server, monkeypatch):
    monkeypatch.setattr(bridge, "get_agent_status", lambda: ("idle", {"ok": True}))
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
    monkeypatch.setattr(bridge, "get_agent_status", lambda: ("idle", {"ok": True}))

    def fake_run_herdr(*args, **kwargs):
        if args[1] == "prompt":
            return {"ok": True, "stdout": "", "stderr": ""}
        return {"ok": True, "stdout": "raw", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)
    monkeypatch.setattr(bridge, "extract_task_response", lambda raw, task_id: "worker 完成")

    task_id = bridge.create_task("后台任务", 5000)

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
    monkeypatch.setattr(bridge, "get_agent_status", lambda: ("working", {"ok": True}))

    task_id = bridge.create_task("后台任务", 5000)

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

    def flaky_peek():
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
    def boom():
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(bridge, "get_agent_status", boom)

    status, body = _get(live_server, "/ready")

    assert status == 500
    assert body["ok"] is False


def test_do_post_returns_500_instead_of_crashing_on_unexpected_error(live_server, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(bridge, "create_task", boom)

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
            "55555555-5555-5555-5555-555555555555", "task", 60000
        )


def test_execute_sentinel_task_marker_missing_includes_raw_output(monkeypatch):
    monkeypatch.setattr(bridge, "run_herdr", lambda *a, **k: {
        "ok": True, "stdout": "这是终端里最后的原始输出", "stderr": "",
    })

    with pytest.raises(bridge.SentinelMarkerMissingError) as exc_info:
        bridge.execute_sentinel_task(
            "66666666-6666-6666-6666-666666666666", "task", 60000
        )

    assert "这是终端里最后的原始输出" in exc_info.value.raw_output


def test_orphan_task_sets_status(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    task_id = bridge.create_task("t", 1000)
    bridge.claim_task(task_id)
    bridge.orphan_task(task_id, "bridge timeout")

    task = bridge.get_task(task_id)
    assert task["status"] == "orphaned"
    assert task["error_text"] == "bridge timeout"


def test_task_worker_orphans_on_recovery_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "DB_PATH", str(tmp_path / "tasks.db"))
    bridge.init_db()
    monkeypatch.setattr(bridge, "WORKER_POLL_SECONDS", 0.02)
    monkeypatch.setattr(bridge, "get_agent_status", lambda: ("idle", {"ok": True}))

    def fake_run_herdr(*args, **kwargs):
        if args[1] == "prompt":
            if "120000" in args:
                raise subprocess_module.TimeoutExpired(cmd="herdr", timeout=135)
            return {"ok": True, "stdout": "", "stderr": ""}
        return {"ok": True, "stdout": "没有 marker", "stderr": ""}

    monkeypatch.setattr(bridge, "run_herdr", fake_run_herdr)

    task_id = bridge.create_task("会在恢复阶段超时的任务", 5000)

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

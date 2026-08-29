import sys
import os

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


def test_acquire_agent_for_delegation_converts_status_query_exception(monkeypatch):
    def boom():
        raise subprocess_module.TimeoutExpired(cmd="herdr", timeout=10)

    monkeypatch.setattr(bridge, "get_agent_status", boom)

    with pytest.raises(bridge.SentinelUnavailableError):
        with bridge.acquire_agent_for_delegation():
            pass

    assert bridge.AGENT_LOCK.locked() is False

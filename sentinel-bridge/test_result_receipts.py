"""Task-local result recovery; no live agents or research execution."""
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

import bridge


@pytest.fixture
def result_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RESULT_DIR", str(tmp_path))
    return tmp_path


def test_result_reminder_does_not_assert_completion_or_request_reexecution(result_dir):
    prompt = bridge.build_result_reminder_prompt("current-task")
    assert "已经做完" not in prompt
    assert "不确定" in prompt
    assert "current-task" in prompt
    assert "不要重新执行" in prompt
    assert "其他任务" in prompt
    assert "仅允许写入" in prompt


@pytest.mark.parametrize("receipt_id", ["another-task", "", None, 1, True])
def test_explicit_wrong_task_receipt_is_not_accepted_or_deleted(result_dir, receipt_id):
    path = Path(bridge.result_file_path("current-task"))
    raw = json.dumps({"task_id": receipt_id, "summary": "old task output"})
    path.write_text(raw, encoding="utf-8")
    assert bridge.read_result_file("current-task") is None
    assert path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("receipt", [
    {"task_id": "current-task", "summary": "current output"},
    {"summary": "legacy structured output"},
    "legacy text output",
])
def test_matching_and_legacy_results_remain_compatible(result_dir, receipt):
    raw = json.dumps(receipt) if isinstance(receipt, dict) else receipt
    path = Path(bridge.result_file_path("current-task"))
    path.write_text(raw, encoding="utf-8")
    assert bridge.read_result_file("current-task", cleanup=False) == raw
    assert path.exists()


def test_normalized_uuid_in_receipt_matches_hyphenated_bridge_id(result_dir):
    task_id = "12345678-1234-5678-1234-567812345678"
    raw = json.dumps({"task_id": task_id.replace("-", ""), "summary": "current"})
    Path(bridge.result_file_path(task_id)).write_text(raw, encoding="utf-8")
    assert bridge.read_result_file(task_id) == raw


@pytest.mark.parametrize("receipt_id,accepted", [("current-task", True), ("other-task", False)])
def test_fenced_json_cannot_bypass_receipt_identity(result_dir, receipt_id, accepted):
    raw = '```json\n' + json.dumps({"task_id": receipt_id, "summary": "output"}) + '\n```'
    path = Path(bridge.result_file_path("current-task"))
    path.write_text(raw, encoding="utf-8")
    assert bridge.read_result_file("current-task", cleanup=False) == (raw if accepted else None)
    assert path.exists()


def test_wrong_receipt_only_triggers_result_recovery_not_task_replay(result_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(bridge, "_terminal_read", lambda *a: ("", None))
    monkeypatch.setattr(bridge, "_read_terminal_tail", lambda *a: "")

    def deliver(agent, prompt, timeout_ms, task_id):
        calls.append((agent, prompt))
        payload = {"task_id": "unrelated-task" if len(calls) == 1 else task_id,
                   "summary": "saved output"}
        Path(bridge.result_file_path(task_id)).write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(bridge, "deliver_prompt", deliver)
    result = bridge.execute_sentinel_task("primary", "current-task", "perform once", 1000)
    assert json.loads(result)["task_id"] == "current-task"
    assert len(calls) == 2
    assert calls[1][1] == bridge.build_result_reminder_prompt("current-task")
    assert "perform once" not in calls[1][1]


def test_uncertain_connection_failure_does_not_switch_agent_or_send_reminder(result_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(bridge, "get_agent_quota_block", lambda *a: None)
    monkeypatch.setattr(bridge, "_terminal_read", lambda *a: ("", None))

    def deliver(agent, *args):
        calls.append(agent)
        raise TimeoutError("connection lost; dispatch may already have succeeded")

    monkeypatch.setattr(bridge, "deliver_prompt", deliver)
    with pytest.raises(TimeoutError):
        bridge.run_with_quota_failover("primary", lambda agent:
            bridge.execute_sentinel_task(agent, "current-task", "perform once", 1000),
            primary_locked=True)
    assert calls == ["primary"]


@pytest.mark.parametrize("reminder", [False, True])
def test_shutdown_between_admission_and_process_creation_does_not_send_prompt(
    result_dir, monkeypatch, reminder,
):
    admitted, release = threading.Event(), threading.Event()
    monkeypatch.setattr(bridge, "_shutting_down", False)
    errors, started = [], []
    real_remaining = bridge.remaining_ms

    def budget(value):
        admitted.set()
        assert release.wait(3)
        return real_remaining(value)

    monkeypatch.setattr(bridge, "remaining_ms", budget)
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **k:
                        (started.append(1) or subprocess.CompletedProcess(a, 0, "ok", "")))
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *a, **k: pytest.fail("late prompt process"))

    def send():
        try:
            bridge._run_herdr_prompt("primary", bridge.build_result_reminder_prompt("current-task")
                                    if reminder else "original task", 1000)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    try:
        assert admitted.wait(2)
        with bridge._shutdown_guard:
            bridge._shutting_down = True
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert not started
        assert len(errors) == 1 and isinstance(errors[0], TimeoutError)
    finally:
        release.set()
        thread.join(timeout=3)


def test_prompt_creation_holds_shutdown_guard_but_waiting_for_result_does_not(monkeypatch):
    waiting, release = threading.Event(), threading.Event()
    monkeypatch.setattr(bridge, "_shutting_down", False)
    outcomes, errors = [], []

    class Process:
        returncode = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def communicate(self, timeout=None):
            waiting.set()
            assert release.wait(3)
            return "answer", ""

    def create(*args, **kwargs):
        assert bridge._shutdown_guard.locked()
        return Process()

    monkeypatch.setattr(bridge.subprocess, "Popen", create)

    def send():
        try:
            outcomes.append(bridge.run_herdr("agent", "prompt", "primary", "task"))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    try:
        assert waiting.wait(2)
        acquired = bridge._shutdown_guard.acquire(timeout=0.5)
        assert acquired, "long-running prompt blocked shutdown"
        try:
            bridge._shutting_down = True
        finally:
            bridge._shutdown_guard.release()
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive() and not errors
        assert outcomes[0]["ok"] and outcomes[0]["stdout"] == "answer"
    finally:
        release.set()
        thread.join(timeout=3)


def test_prompt_driver_timeout_is_reaped_without_restarting_or_resending(monkeypatch):
    monkeypatch.setattr(bridge, "_shutting_down", False)
    calls = []

    class Process:
        returncode = -9

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            if len(calls) == 2:
                raise subprocess.TimeoutExpired("fake herdr", timeout,
                                                output="partial output", stderr="timeout detail")
            return "partial output", "timeout detail"

        def kill(self):
            calls.append(("kill", None))

        def wait(self):
            calls.append(("wait", None))

    def create(*args, **kwargs):
        calls.append(("create", None))
        return Process()

    monkeypatch.setattr(bridge.subprocess, "Popen", create)
    with pytest.raises(subprocess.TimeoutExpired) as error:
        bridge.run_herdr("agent", "prompt", "primary", "task", timeout=0.01)
    assert [name for name, _ in calls] == ["create", "communicate", "kill",
                                          "communicate" if os.name == "nt" else "wait"]
    assert error.value.stdout == "partial output"

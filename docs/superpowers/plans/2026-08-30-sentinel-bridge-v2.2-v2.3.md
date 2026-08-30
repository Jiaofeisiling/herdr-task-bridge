# Sentinel Bridge v2.2 + v2.3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the NeSI Sentinel bridge so it never blocks on a busy Claude Sentinel session, recovers from missed completion markers, and gains an async `/delegate` task queue backed by SQLite for long-running (Slurm/benchmark) work — while keeping `/health` always instantly responsive.

**Architecture:** `bridge.py` is a single-file `http.server.ThreadingHTTPServer` running on the remote NeSI-reachable host, driving a persistent Claude Code session ("Sentinel") via the `herdr` CLI. A single process-wide `AGENT_LOCK` mutually excludes all paths that actually talk to Sentinel (`/ask`, `/prompt`, and the new background task worker) so at most one delegation runs at a time. `/delegate` never touches the lock — it only writes a row to a local SQLite `tasks.db` and returns immediately; a daemon worker thread polls the queue and drains it through the same execution pipeline used by `/ask`. `sentinel.ps1` is the Windows-side CLI wrapper; it gains a `Invoke-SentinelApi` helper so PowerShell 5.1 (which throws on non-2xx HTTP responses) can read the JSON body of error responses instead of crashing with a raw .NET exception.

**Tech Stack:** Python 3 stdlib only (`http.server`, `sqlite3`, `threading`, `subprocess`) on the remote side — no new pip dependencies are added to `bridge.py` itself. `pytest` is a local-only dev dependency used to test the logic before deployment. PowerShell 5.1 on Windows.

## Global Constraints

- No new runtime dependencies in `bridge.py` (stdlib only) — matches the user's explicit "don't introduce Redis/Temporal/etc." constraint for v2.3.
- `pytest` is installed locally only, for developing/testing the working copy before it is deployed to the remote host; it must NOT be required on the remote NeSI-reachable host to run `bridge.py`.
- All local pytest runs use the project virtualenv at `C:\Tools\sentinel-bridge\.venv` (its interpreter: `C:\Tools\sentinel-bridge\.venv\Scripts\python.exe`, or `/c/Tools/sentinel-bridge/.venv/Scripts/python.exe` from Git Bash) — never the bare `/e/ProgramData/miniforge3/python`, which cannot `pip install` anything as a non-admin user. `.venv/` is git-ignored.
- All new/changed HTTP responses stay `ensure_ascii=False`, UTF-8 encoded JSON (already the existing pattern in `send_json`) — Chinese text in `result.text` must round-trip without mangling.
- `/health` must never call `run_herdr`, `get_agent_status`, or touch the SQLite DB — it only proves the bridge process itself is alive. This is deliberate: it is the tool that lets you tell "bridge process dead" apart from "bridge alive but Sentinel busy".
- `/delegate` never checks Sentinel's busy state and never touches `AGENT_LOCK` — it only enqueues. Busy/idle gating happens later, when the worker (or `/ask`) actually tries to run the task.
- Exactly one `threading.Lock()` (`AGENT_LOCK`) guards every code path that calls `run_herdr("agent", "prompt", ...)` against Sentinel: `/ask`, `/prompt`, and `task_worker`. There is no second, differently-named lock — this was an explicit inconsistency in the original v2.2/v2.3 sketches (`DELEGATE_LOCK` vs `AGENT_LOCK`) that this plan resolves by unifying on `AGENT_LOCK`.
- Local working copy lives at `C:\Tools\sentinel-bridge\bridge.py` / `C:\Tools\sentinel-bridge\test_bridge.py` on Windows. The deployed file lives at `~/sentinel-bridge/bridge.py` on the remote host. Task 9 is a manual copy-and-restart step — there is no automated deploy pipeline yet.
- `E:\Claude` is an unrelated project (MT5/trading strategy work) — nothing in this plan touches it.

## File Structure

- `C:\Tools\sentinel-bridge\bridge.py` — the bridge server. Modified in place across Tasks 1–8. Responsibilities added, in order: SQLite task-queue persistence, Sentinel busy-state query, prompt/read/extract/recovery pipeline with typed exceptions, lock-guarded synchronous delegation (`/ask`, `/prompt`), async delegation (`/delegate`, `/tasks`, `/tasks/<id>`), background worker thread.
- `C:\Tools\sentinel-bridge\test_bridge.py` — pytest suite covering everything in `bridge.py` that doesn't require a real `herdr`/Sentinel (all `run_herdr` calls are monkeypatched). Grows alongside `bridge.py` task by task.
- `C:\Tools\sentinel.ps1` — Windows CLI wrapper. Rewritten in Task 12 to add `Invoke-SentinelApi` (PS 5.1-safe error-body extraction) and the new `ready`/`delegate`/`task`/`tasks` commands.

---

### Task 1: Baseline characterization tests

**Files:**
- Modify: `C:\Tools\sentinel-bridge\bridge.py` (already seeded with the exact current remote v2.1 content — no code changes in this task)
- Create: `C:\Tools\sentinel-bridge\test_bridge.py`

**Interfaces:**
- Consumes: `build_delegation_prompt(task, task_id)`, `extract_task_response(raw, task_id)` — both already exist in the seeded file.
- Produces: nothing new; this task only locks in existing behavior with tests before any refactor touches it.

- [ ] **Step 1: Confirm the project virtualenv has pytest installed**

`C:\Tools\sentinel-bridge\.venv` already exists (created once against the base `/e/ProgramData/miniforge3/python` interpreter — that conda installation refuses `pip install` for non-admin users, including `--user`, since it has `site.ENABLE_USER_SITE = False`, so every task in this plan uses this project-local venv instead). Confirm pytest is present; install it if this is a fresh checkout without the venv:

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pip install pytest
```

Expected: `Requirement already satisfied` (or a fresh install if the venv didn't exist yet).

- [ ] **Step 2: Write characterization tests for the existing marker-extraction logic**

Create `C:\Tools\sentinel-bridge\test_bridge.py`:

```python
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import bridge


def test_build_delegation_prompt_includes_token_and_task():
    prompt = bridge.build_delegation_prompt("检查磁盘", "11111111-2222-3333-4444-555555555555")
    assert "11112222333344445555555555555" not in prompt  # sanity: token strips only dashes
    assert "1111111122223333444455555555" not in prompt
    assert "112223334445555555555555" not in prompt
    token = "11111111-2222-3333-4444-555555555555".replace("-", "")
    assert token in prompt
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
```

- [ ] **Step 3: Run the tests and confirm they pass against the untouched baseline**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: 4 passed. (Fix the first characterization test's redundant assert lines if they read oddly — keep only the two meaningful assertions: `token in prompt` and `"检查磁盘" in prompt` and `"SENTINEL_DONE_" in prompt`; drop the three `not in` sanity lines, they were scratch reasoning, not real assertions.)

- [ ] **Step 4: Clean up Step 2's test before moving on**

Edit `test_bridge.py`, replace the first test with:

```python
def test_build_delegation_prompt_includes_token_and_task():
    task_id = "11111111-2222-3333-4444-555555555555"
    prompt = bridge.build_delegation_prompt("检查磁盘", task_id)

    assert task_id.replace("-", "") in prompt
    assert "检查磁盘" in prompt
    assert "SENTINEL_DONE_" in prompt
```

Re-run Step 3's command. Expected: 4 passed.

---

### Task 2: SQLite task-queue persistence

**Files:**
- Modify: `C:\Tools\sentinel-bridge\bridge.py`
- Modify: `C:\Tools\sentinel-bridge\test_bridge.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `DB_PATH` (module-level str), `now_iso()`, `db_connect()`, `db_session()` (contextmanager), `init_db()`, `create_task(task, timeout_ms) -> task_id`, `get_task(task_id) -> dict | None`, `list_tasks(limit=20) -> list[dict]`, `peek_next_task() -> dict | None`, `claim_task(task_id) -> bool`, `complete_task(task_id, result_text)`, `fail_task(task_id, error_text)`, `orphan_task(task_id, error_text)`. Task rows have columns: `task_id, task, status, created_at, started_at, finished_at, timeout_ms, result_text, error_text`. `status` is one of `queued`, `running`, `done`, `error`, `orphaned`.

- [ ] **Step 1: Write the failing tests**

Append to `test_bridge.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: the 9 new tests FAIL with `AttributeError: module 'bridge' has no attribute 'create_task'` (or similar).

- [ ] **Step 3: Implement the SQLite layer**

In `bridge.py`, add these imports at the top (alongside the existing ones):

```python
import contextlib
import sqlite3
import time

from datetime import datetime, timezone
```

Add this configuration + persistence block right after the existing `SENTINEL = ...` line:

```python
DB_PATH = os.environ.get(
    "SENTINEL_DB",
    os.path.expanduser("~/sentinel-bridge/tasks.db"),
)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def db_session():
    # sqlite3.Connection used as `with conn:` only commits/rolls back —
    # it does NOT close the connection, so close it explicitly here.
    conn = db_connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    with db_session() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
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

        # 如果 bridge 在一个任务执行期间崩溃，
        # 千万不要自动重新运行它，否则可能重复执行危险操作。
        conn.execute("""
            UPDATE tasks
            SET
                status = 'orphaned',
                finished_at = ?,
                error_text = COALESCE(
                    error_text,
                    'Bridge restarted while this task was running. Sentinel may have partially or fully executed it.'
                )
            WHERE status = 'running'
        """, (now_iso(),))


def create_task(task, timeout_ms):
    task_id = str(uuid.uuid4())

    with db_session() as conn:
        conn.execute("""
            INSERT INTO tasks (
                task_id, task, status, created_at, timeout_ms
            )
            VALUES (?, ?, 'queued', ?, ?)
        """, (task_id, task, now_iso(), timeout_ms))

    return task_id


def get_task(task_id):
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()

    return dict(row) if row else None


def list_tasks(limit=20):
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    return [dict(row) for row in rows]


def peek_next_task():
    with db_session() as conn:
        row = conn.execute("""
            SELECT * FROM tasks
            WHERE status = 'queued'
            ORDER BY created_at ASC
            LIMIT 1
        """).fetchone()

    return dict(row) if row else None


def claim_task(task_id):
    with db_session() as conn:
        cursor = conn.execute("""
            UPDATE tasks
            SET status = 'running', started_at = ?
            WHERE task_id = ? AND status = 'queued'
        """, (now_iso(), task_id))

        return cursor.rowcount == 1


def complete_task(task_id, result_text):
    with db_session() as conn:
        conn.execute("""
            UPDATE tasks
            SET status = 'done', result_text = ?, finished_at = ?
            WHERE task_id = ?
        """, (result_text, now_iso(), task_id))


def fail_task(task_id, error_text):
    with db_session() as conn:
        conn.execute("""
            UPDATE tasks
            SET status = 'error', error_text = ?, finished_at = ?
            WHERE task_id = ?
        """, (error_text, now_iso(), task_id))


def orphan_task(task_id, error_text):
    with db_session() as conn:
        conn.execute("""
            UPDATE tasks
            SET status = 'orphaned', error_text = ?, finished_at = ?
            WHERE task_id = ?
        """, (error_text, now_iso(), task_id))
```

Note: `uuid` is already imported at the top of the existing file (used by `do_POST`), so no new import needed for it here.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: 13 passed.

---

### Task 3: Sentinel busy-state query

**Files:**
- Modify: `C:\Tools\sentinel-bridge\bridge.py`
- Modify: `C:\Tools\sentinel-bridge\test_bridge.py`

**Interfaces:**
- Consumes: `run_herdr(*args, timeout=None) -> dict` (existing).
- Produces: `AGENT_LOCK` (module-level `threading.Lock()`), `AVAILABLE_STATES` (module-level `set`, `{"idle", "done"}`), `get_agent_status() -> tuple[str | None, dict]`.

- [ ] **Step 1: Write the failing tests**

Append to `test_bridge.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: the 4 new tests FAIL with `AttributeError: module 'bridge' has no attribute 'get_agent_status'`.

- [ ] **Step 3: Implement**

Add `import threading` to the top imports. Add this block after the SQLite functions from Task 2:

```python
AGENT_LOCK = threading.Lock()

AVAILABLE_STATES = {
    "idle",
    "done",
}


def get_agent_status():
    result = run_herdr(
        "agent",
        "get",
        SENTINEL,
        timeout=10,
    )

    if not result["ok"]:
        return None, result

    try:
        payload = json.loads(result["stdout"])

        agent = (
            payload
            .get("result", {})
            .get("agent", {})
        )

        return agent.get("agent_status"), result

    except Exception as e:
        return None, {
            "ok": False,
            "error": f"Unable to parse Herdr status: {e}",
            "raw": result["stdout"],
        }
```

`json` is already imported at the top of the existing file — no new import needed for it.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: 17 passed.

---

### Task 4: Recovery prompt + typed execution pipeline

**Files:**
- Modify: `C:\Tools\sentinel-bridge\bridge.py`
- Modify: `C:\Tools\sentinel-bridge\test_bridge.py`

**Interfaces:**
- Consumes: `run_herdr`, `build_delegation_prompt`, `extract_task_response` (all existing).
- Produces: exception classes `SentinelPromptError(RuntimeError)`, `SentinelReadError(RuntimeError)`, `SentinelMarkerMissingError(RuntimeError)`; functions `build_recovery_prompt(task_id) -> str`, `execute_sentinel_task(task_id, task, timeout_ms, read_lines=500) -> str` (returns the extracted response text, or raises `TimeoutError` / `SentinelPromptError` / `SentinelReadError` / `SentinelMarkerMissingError`).

- [ ] **Step 1: Write the failing tests**

Append to `test_bridge.py`:

```python
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

    # Exactly 2 prompt calls (initial + recovery) and 1 read call
    # (no second read after the recovery prompt itself failed).
    assert state["prompt_calls"] == 2
    assert state["read_calls"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: the 7 new tests FAIL with `AttributeError: module 'bridge' has no attribute 'execute_sentinel_task'`.

- [ ] **Step 3: Implement**

Add this block after the `get_agent_status` function:

```python
class SentinelPromptError(RuntimeError):
    pass


class SentinelReadError(RuntimeError):
    pass


class SentinelMarkerMissingError(RuntimeError):
    pass


def build_recovery_prompt(task_id):
    token = task_id.replace("-", "")

    return f"""
你刚才已经完成了来自 Windows Codex 的任务。

不要重新执行任务。
不要再次运行命令。
不要修改文件。

请仅根据你刚才已经完成的工作：
1. 简洁总结最终结果；
2. 列出重要操作；
3. 如有 Slurm job，给出 job ID；
4. 如有修改文件，列出路径；
5. 如被阻塞，说明原因。

Task token:
{token}

最后一行必须由字面前缀 SENTINEL_DONE_
紧接 Task token 组成，中间不得有空格。

最后一行后不要输出其他内容。
""".strip()


def _run_herdr_prompt(delegated_prompt, timeout_ms):
    try:
        result = run_herdr(
            "agent",
            "prompt",
            SENTINEL,
            delegated_prompt,
            "--wait",
            "--timeout",
            str(timeout_ms),
            timeout=(timeout_ms / 1000) + 15,
        )

    except subprocess.TimeoutExpired:
        raise TimeoutError(
            "Bridge stopped waiting for Sentinel. "
            "Sentinel may still be executing the task."
        )

    if not result["ok"]:
        raise SentinelPromptError(
            "Herdr prompt command failed: " + result.get("stderr", "")
        )

    return result


def run_prompt_only(task_id, task, timeout_ms):
    delegated_prompt = build_delegation_prompt(task, task_id)
    return _run_herdr_prompt(delegated_prompt, timeout_ms)


def execute_sentinel_task(task_id, task, timeout_ms, read_lines=500):
    delegated_prompt = build_delegation_prompt(task, task_id)

    _run_herdr_prompt(delegated_prompt, timeout_ms)

    read_result = run_herdr(
        "agent",
        "read",
        SENTINEL,
        "--source",
        "recent-unwrapped",
        "--lines",
        str(read_lines),
    )

    if not read_result["ok"]:
        raise SentinelReadError(
            "Unable to read Sentinel output: " + read_result.get("stderr", "")
        )

    response = extract_task_response(read_result["stdout"], task_id)

    if response is None:
        recovery_prompt = build_recovery_prompt(task_id)

        recovery_result = run_herdr(
            "agent",
            "prompt",
            SENTINEL,
            recovery_prompt,
            "--wait",
            "--timeout",
            "120000",
            timeout=135,
        )

        if recovery_result["ok"]:
            read_result = run_herdr(
                "agent",
                "read",
                SENTINEL,
                "--source",
                "recent-unwrapped",
                "--lines",
                str(read_lines),
            )

            response = extract_task_response(read_result["stdout"], task_id)

    if response is None:
        raise SentinelMarkerMissingError(
            "Sentinel finished but no completion marker was found, "
            "including after recovery."
        )

    return response
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: 24 passed.

---

### Task 5: Lock-guarded delegation entry point

**Files:**
- Modify: `C:\Tools\sentinel-bridge\bridge.py`
- Modify: `C:\Tools\sentinel-bridge\test_bridge.py`

**Interfaces:**
- Consumes: `AGENT_LOCK`, `get_agent_status`, `AVAILABLE_STATES` (Task 3).
- Produces: `SentinelBusyError(RuntimeError)` (has `.agent_status` attribute), `SentinelUnavailableError(RuntimeError)`, `acquire_agent_for_delegation()` (contextmanager — raises one of the two errors instead of yielding if Sentinel isn't available; always releases `AGENT_LOCK` on the way out if it was acquired).

- [ ] **Step 1: Write the failing tests**

Append to `test_bridge.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: the 6 new tests FAIL with `AttributeError: module 'bridge' has no attribute 'acquire_agent_for_delegation'`.

- [ ] **Step 3: Implement**

Add after the `execute_sentinel_task` function:

```python
class SentinelBusyError(RuntimeError):
    def __init__(self, agent_status):
        self.agent_status = agent_status
        super().__init__(f"Sentinel busy: {agent_status}")


class SentinelUnavailableError(RuntimeError):
    pass


@contextlib.contextmanager
def acquire_agent_for_delegation():
    if not AGENT_LOCK.acquire(blocking=False):
        raise SentinelBusyError("locked")

    try:
        try:
            agent_status, status_result = get_agent_status()
        except Exception as e:
            # get_agent_status() only returns (None, ...) for herdr/JSON
            # failures it can see coming — a hung herdr process still
            # raises subprocess.TimeoutExpired out of run_herdr(). Catch
            # that (and anything else unexpected) here so callers only
            # ever see SentinelBusyError/SentinelUnavailableError, never
            # a raw exception escaping this contextmanager.
            raise SentinelUnavailableError(
                f"unable to query Sentinel status: {e}"
            )

        if agent_status is None:
            raise SentinelUnavailableError(
                status_result.get(
                    "error", "unable to query Sentinel status"
                )
            )

        if agent_status not in AVAILABLE_STATES:
            raise SentinelBusyError(agent_status)

        yield
    finally:
        AGENT_LOCK.release()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: 30 passed.

---

### Task 6: `do_GET` — `/health`, `/status`, `/ready`, `/read`, `/tasks`, `/tasks/<id>`

**Files:**
- Modify: `C:\Tools\sentinel-bridge\bridge.py`
- Modify: `C:\Tools\sentinel-bridge\test_bridge.py`

**Interfaces:**
- Consumes: `get_agent_status`, `AVAILABLE_STATES`, `list_tasks`, `get_task`, `run_herdr`, `Handler`, `ThreadingHTTPServer` (all existing/from earlier tasks).
- Produces: rewritten `Handler.do_GET`. No new module-level names.

- [ ] **Step 1: Write the failing tests**

Append to `test_bridge.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: the new `/ready`, `/tasks`, `/tasks/<id>` tests FAIL (404 "not found" instead of the expected responses); `/health` test passes already (no change needed there yet).

- [ ] **Step 3: Implement**

Add `from urllib.parse import urlparse` to the top imports. Replace the entire `do_GET` method with:

```python
    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            self.send_json({
                "ok": True,
                "service": "nesi-sentinel-bridge",
                "version": 3,
                "agent": SENTINEL,
            })
            return

        if path == "/status":
            result = run_herdr(
                "agent",
                "get",
                SENTINEL,
            )

            self.send_json(
                result,
                200 if result["ok"] else 500,
            )
            return

        if path == "/ready":
            agent_status, status_result = get_agent_status()

            if agent_status is None:
                self.send_json(
                    {
                        "ok": False,
                        "ready": False,
                        "reason": "sentinel_unreachable",
                    },
                    503,
                )
                return

            self.send_json({
                "ok": True,
                "ready": agent_status in AVAILABLE_STATES,
                "agent_status": agent_status,
            })
            return

        if path == "/read":
            result = run_herdr(
                "agent",
                "read",
                SENTINEL,
                "--source",
                "recent-unwrapped",
                "--lines",
                "120",
            )

            self.send_json(
                result,
                200 if result["ok"] else 500,
            )
            return

        if path == "/tasks":
            self.send_json({
                "ok": True,
                "tasks": list_tasks(),
            })
            return

        if path.startswith("/tasks/"):
            task_id = path[len("/tasks/"):]
            task = get_task(task_id)

            if task is None:
                self.send_json(
                    {"ok": False, "error": "task not found"},
                    404,
                )
                return

            self.send_json({
                "ok": True,
                "task": task,
            })
            return

        self.send_json(
            {"error": "not found"},
            404,
        )
```

Note two intentional small fixes versus the original: `/read` is now matched with an exact `path == "/read"` (via the parsed path) instead of the original loose `self.path.startswith("/read")`, which would have also matched unrelated paths like `/readXYZ`; and `/health`'s reported `"version"` is bumped from `2` to `3` to reflect this change set.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: 38 passed.

---

### Task 7: `do_POST` — lock-guarded `/ask`, `/prompt`, plus `/delegate`

**Files:**
- Modify: `C:\Tools\sentinel-bridge\bridge.py`
- Modify: `C:\Tools\sentinel-bridge\test_bridge.py`

**Interfaces:**
- Consumes: `acquire_agent_for_delegation`, `run_prompt_only`, `execute_sentinel_task`, `SentinelBusyError`, `SentinelUnavailableError`, `SentinelPromptError`, `SentinelReadError`, `SentinelMarkerMissingError`, `create_task` (all existing/from earlier tasks).
- Produces: rewritten `Handler.do_POST`. No new module-level names.

- [ ] **Step 1: Write the failing tests**

Append to `test_bridge.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: the `/delegate` tests fail with 404 (route doesn't exist yet); the `/ask` busy/unavailable tests fail because there's no lock/status check yet; `test_ask_happy_path` currently passes by coincidence with the old code (leave it, it'll still pass after the rewrite) — check its actual result carefully, it may already pass since old `/ask` doesn't check busy state.

- [ ] **Step 3: Implement**

Replace the entire `do_POST` method with:

```python
    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/delegate":
            try:
                body = self.read_json()

                task = body["task"].strip()

                timeout_ms = int(
                    body.get(
                        "timeout_ms",
                        21600000,  # 6 hours
                    )
                )

                if not task:
                    raise ValueError("task cannot be empty")

            except Exception as e:
                self.send_json(
                    {
                        "ok": False,
                        "error": f"invalid request: {e}",
                    },
                    400,
                )
                return

            task_id = create_task(task, timeout_ms)

            self.send_json(
                {
                    "ok": True,
                    "task_id": task_id,
                    "status": "queued",
                },
                202,
            )
            return

        if path not in ("/prompt", "/ask"):
            self.send_json(
                {"error": "not found"},
                404,
            )
            return

        try:
            body = self.read_json()

            task = body["task"].strip()

            timeout_ms = int(
                body.get("timeout_ms", 120000)
            )

            read_lines = int(
                body.get("lines", 500)
            )

            if not task:
                raise ValueError("task cannot be empty")

        except Exception as e:
            self.send_json(
                {
                    "ok": False,
                    "error": f"invalid request: {e}",
                },
                400,
            )
            return

        task_id = str(uuid.uuid4())

        try:
            with acquire_agent_for_delegation():
                if path == "/prompt":
                    prompt_result = run_prompt_only(task_id, task, timeout_ms)

                    self.send_json({
                        "ok": True,
                        "task_id": task_id,
                        "prompt": prompt_result,
                    })
                    return

                result_text = execute_sentinel_task(
                    task_id, task, timeout_ms, read_lines
                )

                self.send_json({
                    "ok": True,
                    "task_id": task_id,
                    "result": {"text": result_text},
                })
                return

        except SentinelBusyError as e:
            self.send_json(
                {
                    "ok": False,
                    "status": "busy",
                    "agent_status": e.agent_status,
                },
                409,
            )
            return

        except SentinelUnavailableError as e:
            self.send_json(
                {
                    "ok": False,
                    "status": "unavailable",
                    "reason": "unable_to_query_sentinel",
                    "detail": str(e),
                },
                503,
            )
            return

        except TimeoutError as e:
            self.send_json(
                {
                    "ok": False,
                    "task_id": task_id,
                    "error": str(e),
                },
                504,
            )
            return

        except SentinelMarkerMissingError as e:
            self.send_json(
                {
                    "ok": False,
                    "task_id": task_id,
                    "error": str(e),
                },
                502,
            )
            return

        except (SentinelPromptError, SentinelReadError) as e:
            self.send_json(
                {
                    "ok": False,
                    "task_id": task_id,
                    "error": str(e),
                },
                500,
            )
            return
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: 50 passed.

---

### Task 8: Background worker thread

**Files:**
- Modify: `C:\Tools\sentinel-bridge\bridge.py`
- Modify: `C:\Tools\sentinel-bridge\test_bridge.py`

**Interfaces:**
- Consumes: `acquire_agent_for_delegation`, `SentinelBusyError`, `SentinelUnavailableError` (Task 5), `peek_next_task`, `claim_task`, `execute_sentinel_task`, `complete_task`, `fail_task`, `orphan_task` (all existing).
- Produces: `WORKER_POLL_SECONDS` (module-level float, default `2`), `task_worker(stop_event=None)` (loops until `stop_event.is_set()`, or forever if `stop_event` is `None` — the parameter exists purely so tests can cleanly stop the background thread they start; production `__main__` calls it with no argument, so its behavior there is unchanged), rewritten `if __name__ == "__main__":` block that starts it.

- [ ] **Step 1: Write the failing test**

Append to `test_bridge.py`:

```python
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
        # Stop the worker before this test's monkeypatches unwind — the
        # thread reads module globals (DB_PATH, run_herdr, ...) on every
        # loop iteration, so a leaked thread would keep polling with the
        # real defaults after teardown, including a real subprocess call.
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: both FAIL with `AttributeError: module 'bridge' has no attribute 'task_worker'`.

- [ ] **Step 3: Implement**

Add after `acquire_agent_for_delegation`:

```python
WORKER_POLL_SECONDS = 2


def task_worker(stop_event=None):
    print("Task worker started.")

    while stop_event is None or not stop_event.is_set():
        task_row = peek_next_task()

        if task_row is None:
            time.sleep(WORKER_POLL_SECONDS)
            continue

        # 复用 /ask 用的同一把锁 + 同一套 busy 判断，
        # 避免维护两份重复的加锁逻辑。
        try:
            with acquire_agent_for_delegation():
                task_id = task_row["task_id"]

                if not claim_task(task_id):
                    # 任务已被别的地方 claim（正常情况下不会发生，
                    # 因为只有这一个 worker），直接进入下一轮。
                    continue

                print(f"[task {task_id}] running")

                try:
                    result = execute_sentinel_task(
                        task_id=task_id,
                        task=task_row["task"],
                        timeout_ms=task_row["timeout_ms"],
                    )

                    complete_task(task_id, result)
                    print(f"[task {task_id}] done")

                except TimeoutError as e:
                    # Claude 有可能还在继续工作，
                    # 所以不能简单标 error。
                    orphan_task(task_id, str(e))
                    print(f"[task {task_id}] orphaned")

                except Exception as e:
                    fail_task(task_id, str(e))
                    print(f"[task {task_id}] error: {e}")

        except (SentinelBusyError, SentinelUnavailableError):
            # Sentinel 正忙或查不到状态，这一轮先不碰它，稍后再试。
            time.sleep(WORKER_POLL_SECONDS)
            continue
```

Replace the `if __name__ == "__main__":` block at the bottom of the file with:

```python
if __name__ == "__main__":
    init_db()

    worker = threading.Thread(
        target=task_worker,
        daemon=True,
        name="sentinel-task-worker",
    )
    worker.start()

    print("Sentinel Bridge v3")
    print(f"Agent: {SENTINEL}")
    print(f"Database: {DB_PATH}")
    print(f"Listening: http://{HOST}:{PORT}")

    server = ThreadingHTTPServer(
        (HOST, PORT),
        Handler,
    )

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        print("\nStopping.")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: 52 passed.

- [ ] **Step 5: Run the full suite one more time to confirm nothing earlier regressed**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: 52 passed, 0 failed.

---

### Task 9: Deploy to the remote host

**Files:**
- Reference only (no local file changes): `~/sentinel-bridge/bridge.py` on the remote NeSI-reachable host.

**Interfaces:**
- Consumes: the finished `C:\Tools\sentinel-bridge\bridge.py` from Task 8.
- Produces: nothing new — this is an operational step.

- [ ] **Step 1: Copy the file to the remote host**

Use whatever channel you already use to reach that host through the VS Code Remote-SSH connection (its integrated terminal, its file explorer drag-and-drop, or `scp`/`rsync` if you have a direct SSH path outside VS Code). Overwrite `~/sentinel-bridge/bridge.py` with the contents of `C:\Tools\sentinel-bridge\bridge.py`.

- [ ] **Step 2: Restart the bridge process on the remote host**

This depends on however you're currently keeping `bridge.py` running there (a `tmux`/`screen` session, `nohup`, a systemd user unit, etc.) — that process manager was never described in this conversation, so stop and restart it the same way you started it originally. After restart you should see in its logs:

```text
Task worker started.
Sentinel Bridge v3
Agent: sentinel
Database: /home/.../sentinel-bridge/tasks.db
Listening: http://127.0.0.1:8765
```

- [ ] **Step 3: Verify the port forward still lands on the new process**

From Windows:

```bash
curl -sS -m 10 http://127.0.0.1:8765/health
```

Expected: `{"ok": true, "service": "nesi-sentinel-bridge", "version": 3, "agent": "sentinel"}` — the `"version": 3` confirms the new code is actually running, not a stale process still bound to the port.

---

### Task 10: `sentinel.ps1` — force UTF-8 console encoding

**Files:**
- Modify: `C:\Tools\sentinel.ps1`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing new — this only affects how the process's own console streams are encoded.

- [ ] **Step 1: Add the encoding block**

Add immediately after the closing `)` of the `param(...)` block at the top of `C:\Tools\sentinel.ps1`:

```powershell
$Utf8 = New-Object System.Text.UTF8Encoding($false)

[Console]::InputEncoding  = $Utf8
[Console]::OutputEncoding = $Utf8
$OutputEncoding           = $Utf8
```

- [ ] **Step 2: Verify manually**

```bash
powershell -NoProfile -Command "C:\Tools\sentinel.ps1 health"
```

Expected: valid JSON, no mangled characters (this endpoint has no Chinese text to check encoding with, but confirms the script still runs after the edit — the actual UTF-8 proof comes in Task 13 with a Chinese-bearing response).

---

### Task 11: `sentinel.ps1` — `Invoke-SentinelApi` helper (PowerShell 5.1-safe error bodies)

**Files:**
- Modify: `C:\Tools\sentinel.ps1`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Invoke-SentinelApi -Uri <string> [-Method <string>] [-Body <string>]` — behaves like `Invoke-RestMethod` on success; on a non-2xx response (which PS 5.1's `Invoke-RestMethod` normally throws a `System.Net.WebException` for), it instead reads and JSON-parses the response body and returns it just like a success, so callers can do the same `$result.ok` checks regardless of HTTP status code.

**Why this is needed:** this machine runs PowerShell `5.1.26100.9278` (checked directly — `$PSVersionTable.PSVersion`), which has no `-SkipHttpErrorCheck` (that flag is PowerShell 7+ only). Every new endpoint in this plan uses non-2xx status codes on purpose (`404` task-not-found, `409` busy, `503` unavailable, `502` marker-missing, `504` timeout) — without this helper, `sentinel.ps1` would crash with a raw .NET stack trace on exactly the failure cases this whole hardening effort is meant to report cleanly.

- [ ] **Step 1: Add the helper function**

Add after the existing `Join-TaskText` function in `C:\Tools\sentinel.ps1`:

```powershell
function Invoke-SentinelApi {
    param(
        [Parameter(Mandatory=$true)][string]$Uri,
        [string]$Method = "Get",
        [string]$Body = $null
    )

    try {
        if ($Body) {
            return Invoke-RestMethod -Uri $Uri -Method $Method `
                -ContentType "application/json; charset=utf-8" -Body $Body
        }

        return Invoke-RestMethod -Uri $Uri -Method $Method
    }
    catch [System.Net.WebException] {
        $errResponse = $_.Exception.Response

        if ($null -eq $errResponse) {
            throw
        }

        # In Windows PowerShell 5.1, Invoke-RestMethod has already
        # consumed the response stream by the time control reaches this
        # catch block -- re-reading $errResponse.GetResponseStream() here
        # reliably returns an empty string (confirmed against a live 409:
        # the whole function returned nothing, silently). PowerShell
        # already captured the body for us in $_.ErrorDetails.Message,
        # so use that instead of re-reading the (already-drained) stream.
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            return $_.ErrorDetails.Message | ConvertFrom-Json
        }

        # Fallback for the rare case ErrorDetails wasn't populated.
        $stream = $errResponse.GetResponseStream()
        $reader = New-Object System.IO.StreamReader($stream)
        $rawBody = $reader.ReadToEnd()
        $reader.Close()

        return $rawBody | ConvertFrom-Json
    }
}
```

- [ ] **Step 2: Retrofit the existing `health`/`status`/`read` branches to use it**

In the `switch ($Command)` block, replace:

```powershell
    "health" {
        $result = Invoke-RestMethod "$BaseUrl/health"
        $result | ConvertTo-Json -Depth 10
    }
```

with:

```powershell
    "health" {
        $result = Invoke-SentinelApi -Uri "$BaseUrl/health"
        $result | ConvertTo-Json -Depth 10
    }
```

Replace:

```powershell
    "status" {
        $result = Invoke-RestMethod "$BaseUrl/status"

        if (-not $result.ok) {
            Write-Error $result.stderr
            exit 1
        }

        $result.stdout
    }
```

with:

```powershell
    "status" {
        $result = Invoke-SentinelApi -Uri "$BaseUrl/status"

        if (-not $result.ok) {
            Write-Error $result.stderr
            exit 1
        }

        $result.stdout
    }
```

Replace:

```powershell
    "read" {
        $result = Invoke-RestMethod "$BaseUrl/read"

        if (-not $result.ok) {
            Write-Error $result.stderr
            exit 1
        }

        $result.stdout
    }
```

with:

```powershell
    "read" {
        $result = Invoke-SentinelApi -Uri "$BaseUrl/read"

        if (-not $result.ok) {
            Write-Error $result.stderr
            exit 1
        }

        $result.stdout
    }
```

Replace the `prompt` branch's call:

```powershell
        $result = Invoke-RestMethod `
            -Uri "$BaseUrl/prompt" `
            -Method Post `
            -ContentType "application/json; charset=utf-8" `
            -Body $body

        if (-not $result.ok) {
            Write-Error $result.stderr
            exit 1
        }

        $result.stdout
```

with:

```powershell
        $result = Invoke-SentinelApi -Uri "$BaseUrl/prompt" -Method Post -Body $body
        $result | ConvertTo-Json -Depth 20
```

(This also drops the old exit-1-on-failure behavior for `prompt`, matching the "always show the full JSON" fix already applied to `ask` earlier in this project — the whole point of the new status-coded errors is to see them, not swallow them behind `Write-Error`.)

Replace the `ask` branch's call — it currently reads:

```powershell
        $result = Invoke-RestMethod `
            -Uri "$BaseUrl/ask" `
            -Method Post `
            -ContentType "application/json; charset=utf-8" `
            -Body $body

        if (-not $result.ok) {
            Write-Error "Sentinel task failed."
            $result | ConvertTo-Json -Depth 10
            exit 1
        }

        $result | ConvertTo-Json -Depth 20
```

with:

```powershell
        $result = Invoke-SentinelApi -Uri "$BaseUrl/ask" -Method Post -Body $body
        $result | ConvertTo-Json -Depth 20
```

- [ ] **Step 3: Verify manually**

```bash
powershell -NoProfile -Command "C:\Tools\sentinel.ps1 health"
```

```bash
powershell -NoProfile -Command "C:\Tools\sentinel.ps1 status"
```

Expected: both still return clean output as before (this task only changes internal plumbing, not observable behavior for the success path — the real payoff shows up in Task 13 when a `409`/`503` no longer crashes the script).

---

### Task 12: `sentinel.ps1` — add `ready`, `delegate`, `task`, `tasks` commands

**Files:**
- Modify: `C:\Tools\sentinel.ps1`

**Interfaces:**
- Consumes: `Invoke-SentinelApi` (Task 11).
- Produces: four new CLI subcommands.

- [ ] **Step 1: Extend the `ValidateSet`**

Replace:

```powershell
    [ValidateSet("ask", "prompt", "status", "read", "health")]
```

with:

```powershell
    [ValidateSet(
        "ask",
        "prompt",
        "delegate",
        "task",
        "tasks",
        "status",
        "ready",
        "read",
        "health"
    )]
```

- [ ] **Step 2: Add the `ready` branch**

Add right after the `"health"` branch:

```powershell
    "ready" {
        $result = Invoke-SentinelApi -Uri "$BaseUrl/ready"
        $result | ConvertTo-Json -Depth 10
    }
```

- [ ] **Step 3: Add the `tasks` and `task` branches**

Add after the `"read"` branch:

```powershell
    "tasks" {
        $result = Invoke-SentinelApi -Uri "$BaseUrl/tasks"
        $result | ConvertTo-Json -Depth 20
    }

    "task" {
        $taskId = Join-TaskText

        if (-not $taskId) {
            Write-Error "Task id cannot be empty."
            exit 1
        }

        $result = Invoke-SentinelApi -Uri "$BaseUrl/tasks/$taskId"
        $result | ConvertTo-Json -Depth 20
    }
```

- [ ] **Step 4: Add the `delegate` branch**

Add after the `ask` branch. Note it deliberately omits `timeout_ms` from the request body unless `-TimeoutMs` was explicitly passed on the command line, so the server's own 6-hour default (for long Slurm/benchmark work) applies instead of this script's 2-minute default:

```powershell
    "delegate" {
        $task = Join-TaskText

        if (-not $task) {
            Write-Error "Task cannot be empty."
            exit 1
        }

        $payload = @{ task = $task }

        if ($PSBoundParameters.ContainsKey("TimeoutMs")) {
            $payload["timeout_ms"] = $TimeoutMs
        }

        $body = $payload | ConvertTo-Json -Compress

        $result = Invoke-SentinelApi -Uri "$BaseUrl/delegate" -Method Post -Body $body
        $result | ConvertTo-Json -Depth 20
    }
```

- [ ] **Step 5: Verify manually against the deployed remote bridge**

```bash
powershell -NoProfile -Command "C:\Tools\sentinel.ps1 ready"
```

Expected: `{"ok": true, "ready": true or false, "agent_status": "idle" | "working" | ...}`.

```bash
powershell -NoProfile -Command "C:\Tools\sentinel.ps1 tasks"
```

Expected: `{"ok": true, "tasks": []}` (or whatever tasks already exist from Task 9's verification).

---

### Task 13: End-to-end verification against the real deployed bridge

**Files:** none — this is a manual verification pass, not a code change.

- [ ] **Step 1: Confirm `/health` stays instant even while a task is running**

```bash
powershell -NoProfile -Command "C:\Tools\sentinel.ps1 delegate '检查当前 NeSI 项目的 Git 状态和当前 Slurm 队列。不要修改任何文件。给 Windows Codex 一个简洁总结。'"
```

Expected: an immediate response, well under a second:

```json
{
  "ok": true,
  "task_id": "....",
  "status": "queued"
}
```

Save the `task_id` from the output, then immediately:

```bash
powershell -NoProfile -Command "C:\Tools\sentinel.ps1 health"
```

Expected: instant `{"ok": true, ...}` regardless of whether the delegated task has started running yet.

- [ ] **Step 2: Poll the task until it's done**

```bash
powershell -NoProfile -Command "C:\Tools\sentinel.ps1 task <task_id_from_step_1>"
```

Expected progression across repeated calls: `"status": "queued"` → `"status": "running"` → `"status": "done"` with a populated `"result_text"` field. The Chinese text in `result_text` must render correctly (no `�`) — this is the Task 10 UTF-8 fix being proven end-to-end.

- [ ] **Step 3: Confirm busy protection on the synchronous path**

While the task from Step 1 is still `"running"` (or start a fresh `delegate` call and immediately race it), run:

```bash
powershell -NoProfile -Command "C:\Tools\sentinel.ps1 ask '这是一个不同的任务，请立即执行'"
```

Expected: `409` busy response instead of the request hanging or silently interleaving with the running task:

```json
{
  "ok": false,
  "status": "busy",
  "agent_status": "working"
}
```

- [ ] **Step 4: Confirm `ready` reflects the same state**

```bash
powershell -NoProfile -Command "C:\Tools\sentinel.ps1 ready"
```

Expected while busy: `{"ok": true, "ready": false, "agent_status": "working"}`. After the task finishes: `{"ok": true, "ready": true, "agent_status": "idle"}`.

---

## Addendum: fixes from the final whole-branch review

Tasks 1-13 are complete, individually reviewed, and live-verified. A final whole-branch review (`.superpowers/sdd/review-976ab5c..30209b7.diff`) then found a cluster of Important issues sharing one root cause — unexpected exceptions in paths outside the already-covered `acquire_agent_for_delegation()` chokepoint escape uncaught, producing either a silently-dead worker thread (with `/health` still reporting healthy) or a raw connection reset on the client (which `Invoke-SentinelApi` cannot recover a body from, since `Response` is `null` for a connection-level failure, not an HTTP-level one). Tasks 14-16 close that gap. Tasks 17-18 are two additional fixes the user explicitly approved after the review: non-zero exit codes on API failure, and a shared-secret auth header (the latter is a new, small security feature, out of the original v2.2/v2.3 scope, requested because `/delegate`'s fire-and-forget shape makes the pre-existing lack of auth easier to exploit than `/ask` was).

### Task 14: Worker thread resilience, `/health` worker visibility, and handler-level catch-alls

**Files:**
- Modify: `C:\Tools\sentinel-bridge\bridge.py`
- Modify: `C:\Tools\sentinel-bridge\test_bridge.py`

**Interfaces:**
- Consumes: `task_worker`, `Handler.do_GET`, `Handler.do_POST` (all existing, being modified in place).
- Produces: module-level `_worker_thread` (starts `None`, set in `__main__`); `do_GET`/`do_POST` renamed to `_do_GET`/`_do_POST` with new thin `do_GET`/`do_POST` wrappers around them.

- [ ] **Step 1: Write the failing tests**

Append to `test_bridge.py`:

```python
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
```

Add `import sqlite3` to the top of `test_bridge.py` if it isn't already imported directly (it currently isn't — only `bridge.sqlite3` is used indirectly; add a plain `import sqlite3` near the other stdlib imports at the top of the file).

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: `test_task_worker_survives_peek_next_task_exception` FAILS (the current worker dies on the first `peek_next_task()` exception, so `calls["count"]` never reaches 4 and/or the thread is not alive). The two 500-handling tests FAIL with 404 or an unhandled exception propagating through the test client. `test_health_reports_worker_alive_flag` FAILS with a `KeyError`/`AssertionError` since `/health` doesn't have a `worker_alive` field yet.

- [ ] **Step 3: Implement**

Replace `task_worker` entirely:

```python
def task_worker(stop_event=None):
    print("Task worker started.")

    while stop_event is None or not stop_event.is_set():
        try:
            task_row = peek_next_task()

            if task_row is None:
                time.sleep(WORKER_POLL_SECONDS)
                continue

            # 复用 /ask 用的同一把锁 + 同一套 busy 判断，
            # 避免维护两份重复的加锁逻辑。
            try:
                with acquire_agent_for_delegation():
                    task_id = task_row["task_id"]

                    if not claim_task(task_id):
                        # 任务已被别的地方 claim（正常情况下不会发生，
                        # 因为只有这一个 worker），直接进入下一轮。
                        continue

                    print(f"[task {task_id}] running")

                    try:
                        result = execute_sentinel_task(
                            task_id=task_id,
                            task=task_row["task"],
                            timeout_ms=task_row["timeout_ms"],
                        )

                    except TimeoutError as e:
                        # Claude 有可能还在继续工作，
                        # 所以不能简单标 error。
                        orphan_task(task_id, str(e))
                        print(f"[task {task_id}] orphaned")

                    except Exception as e:
                        fail_task(task_id, str(e))
                        print(f"[task {task_id}] error: {e}")

                    else:
                        # 只有 execute_sentinel_task 真正成功才走到这里；
                        # 如果 complete_task 自己失败，不应该被上面那段
                        # "except Exception" 错误地标成任务失败。
                        complete_task(task_id, result)
                        print(f"[task {task_id}] done")

            except (SentinelBusyError, SentinelUnavailableError):
                # Sentinel 正忙或查不到状态，这一轮先不碰它，稍后再试。
                time.sleep(WORKER_POLL_SECONDS)
                continue

        except Exception as e:
            # Anything else -- a DB hiccup in peek_next_task/claim_task,
            # or even orphan_task/fail_task/complete_task itself failing --
            # must not kill this thread. A dead worker is invisible: /health
            # keeps reporting healthy and /delegate keeps accepting work
            # that will now never run. Log it loudly and keep polling.
            print(f"[worker] unexpected error, will retry: {e}")
            time.sleep(WORKER_POLL_SECONDS)
```

Rename the existing `do_GET` method to `_do_GET` (keep its entire body exactly as-is, just the `def` line changes), and add this thin wrapper in its place:

```python
    def do_GET(self):
        try:
            self._do_GET()
        except Exception as e:
            self.send_json(
                {"ok": False, "error": f"internal error: {e}"},
                500,
            )
```

Do the same for `do_POST` → `_do_POST`, with this wrapper:

```python
    def do_POST(self):
        try:
            self._do_POST()
        except Exception as e:
            self.send_json(
                {"ok": False, "error": f"internal error: {e}"},
                500,
            )
```

(Every branch inside `_do_GET`/`_do_POST` already calls `send_json` followed immediately by `return`, so there is no existing path where a response is partially sent and then an exception is raised afterward in the same request — the wrapper's own `send_json` call is safe to assume it's the first and only one for any request that reaches it.)

Add the `/health` worker-visibility field. First, add a module-level variable right after `WORKER_POLL_SECONDS = 2`:

```python
_worker_thread = None
```

Then update `/health`'s response inside `_do_GET`:

```python
        if path == "/health":
            self.send_json({
                "ok": True,
                "service": "nesi-sentinel-bridge",
                "version": 3,
                "agent": SENTINEL,
                "worker_alive": (
                    _worker_thread.is_alive() if _worker_thread else None
                ),
            })
            return
```

Finally, update `if __name__ == "__main__":` to record the worker thread in that module-level variable:

```python
if __name__ == "__main__":
    init_db()

    worker = threading.Thread(
        target=task_worker,
        daemon=True,
        name="sentinel-task-worker",
    )
    worker.start()
    _worker_thread = worker

    print("Sentinel Bridge v3")
    print(f"Agent: {SENTINEL}")
    print(f"Database: {DB_PATH}")
    print(f"Listening: http://{HOST}:{PORT}")

    server = ThreadingHTTPServer(
        (HOST, PORT),
        Handler,
    )

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        print("\nStopping.")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: 56 passed (52 + 4 new).

- [ ] **Step 5: Commit**

```bash
cd /c/Tools && git add sentinel-bridge/bridge.py sentinel-bridge/test_bridge.py && git commit -m "Task 14: worker thread resilience, /health worker visibility, handler catch-alls"
```

---

### Task 15: `execute_sentinel_task` hardening — recovery timeout classification, read timeouts, restored diagnostics

**Files:**
- Modify: `C:\Tools\sentinel-bridge\bridge.py`
- Modify: `C:\Tools\sentinel-bridge\test_bridge.py`

**Interfaces:**
- Consumes: `execute_sentinel_task`, `SentinelMarkerMissingError`, `run_herdr`, `orphan_task` (all existing).
- Produces: `SentinelMarkerMissingError` gains a `.raw_output` attribute (constructor signature becomes `SentinelMarkerMissingError(message, raw_output="")`); `execute_sentinel_task`'s two `run_herdr("agent", "read", ...)` calls gain `timeout=60`; the recovery prompt call gains its own `subprocess.TimeoutExpired` handling.

**Design note (deviation from the review's literal suggestion):** the final review suggested routing the recovery prompt through `_run_herdr_prompt` to get its `TimeoutExpired → TimeoutError` conversion "for free." That function also raises `SentinelPromptError` when `result["ok"]` is `False` — but the recovery path's existing, already-tested contract (`test_execute_sentinel_task_raises_marker_missing_when_recovery_prompt_fails`, Task 4) is that a *failed* recovery attempt (as opposed to a *timed-out* one) is swallowed and falls through to `SentinelMarkerMissingError`, not raised as `SentinelPromptError`. Routing through `_run_herdr_prompt` unmodified would silently break that established behavior. This task fixes only the specific defect named in the review (an uncaught `subprocess.TimeoutExpired` during recovery) by adding a scoped `try/except subprocess.TimeoutExpired` around just the recovery call, leaving its `ok: False` handling untouched.

- [ ] **Step 1: Write the failing tests**

Append to `test_bridge.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: all 4 new tests FAIL — the recovery timeout currently propagates as a raw `subprocess.TimeoutExpired` (not `TimeoutError`, so `pytest.raises(TimeoutError)` doesn't match it and the worker test never reaches `orphaned`), and `SentinelMarkerMissingError` has no `raw_output` attribute yet.

- [ ] **Step 3: Implement**

Replace the `SentinelMarkerMissingError` class definition with:

```python
class SentinelMarkerMissingError(RuntimeError):
    def __init__(self, message, raw_output=""):
        self.raw_output = raw_output
        super().__init__(message)
```

Replace the body of `execute_sentinel_task` from the first `read_result = run_herdr(...)` call onward with:

```python
    read_result = run_herdr(
        "agent",
        "read",
        SENTINEL,
        "--source",
        "recent-unwrapped",
        "--lines",
        str(read_lines),
        timeout=60,
    )

    if not read_result["ok"]:
        raise SentinelReadError(
            "Unable to read Sentinel output: " + read_result.get("stderr", "")
        )

    response = extract_task_response(read_result["stdout"], task_id)

    if response is None:
        recovery_prompt = build_recovery_prompt(task_id)

        try:
            recovery_result = run_herdr(
                "agent",
                "prompt",
                SENTINEL,
                recovery_prompt,
                "--wait",
                "--timeout",
                "120000",
                timeout=135,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError(
                "Bridge stopped waiting for Sentinel during recovery. "
                "Sentinel may still be executing the task."
            )

        if recovery_result["ok"]:
            read_result = run_herdr(
                "agent",
                "read",
                SENTINEL,
                "--source",
                "recent-unwrapped",
                "--lines",
                str(read_lines),
                timeout=60,
            )

            response = extract_task_response(read_result["stdout"], task_id)

    if response is None:
        raise SentinelMarkerMissingError(
            "Sentinel finished but no completion marker was found, "
            "including after recovery.",
            raw_output=read_result["stdout"][-4000:],
        )

    return response
```

Update `do_POST`'s `except SentinelMarkerMissingError` branch to surface the diagnostic:

```python
        except SentinelMarkerMissingError as e:
            self.send_json(
                {
                    "ok": False,
                    "task_id": task_id,
                    "error": str(e),
                    "raw_output": e.raw_output,
                },
                502,
            )
            return
```

Update `task_worker`'s `except Exception as e:` block (inside the inner `try` around `execute_sentinel_task`) to also surface `.raw_output` when present, so the diagnostic isn't lost for tasks that fail through the async path too. Find:

```python
                    except Exception as e:
                        fail_task(task_id, str(e))
                        print(f"[task {task_id}] error: {e}")
```

Replace with:

```python
                    except Exception as e:
                        detail = str(e)

                        if isinstance(e, SentinelMarkerMissingError) and e.raw_output:
                            detail += (
                                "\n\nRaw Sentinel output (last 4000 chars):\n"
                                + e.raw_output
                            )

                        fail_task(task_id, detail)
                        print(f"[task {task_id}] error: {e}")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: 60 passed (56 + 4 new).

- [ ] **Step 5: Commit**

```bash
cd /c/Tools && git add sentinel-bridge/bridge.py sentinel-bridge/test_bridge.py && git commit -m "Task 15: fix recovery timeout misclassification, add read timeouts, restore raw_output diagnostics"
```

---

### Task 16: Minor fixes — socket leak, DB_PATH edge case

**Files:**
- Modify: `C:\Tools\sentinel-bridge\bridge.py`
- Modify: `C:\Tools\sentinel-bridge\test_bridge.py`

**Interfaces:** none new — purely internal robustness fixes.

- [ ] **Step 1: Fix the `live_server` fixture's socket leak**

In `test_bridge.py`, find the `live_server` fixture's teardown:

```python
    server.shutdown()
    thread.join(timeout=5)
```

Replace with:

```python
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
```

- [ ] **Step 2: Fix `init_db`'s bare-filename `DB_PATH` edge case**

In `bridge.py`, find in `init_db()`:

```python
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
```

Replace with:

```python
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
```

- [ ] **Step 3: Run the full suite to confirm nothing regressed**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: 60 passed, 0 failed (same count as after Task 15 — this task changes no test count, only fixes internal robustness).

- [ ] **Step 4: Commit**

```bash
cd /c/Tools && git add sentinel-bridge/bridge.py sentinel-bridge/test_bridge.py && git commit -m "Task 16: close live_server test sockets, handle bare-filename DB_PATH"
```

---

### Task 17: `sentinel.ps1` — exit non-zero on API failure for `ask`/`prompt`/`delegate`

**Files:**
- Modify: `C:\Tools\sentinel.ps1`

**User decision (recorded 2026-08-30):** the final review flagged that `ask`/`prompt`/`delegate` always exit 0 even when the API reports `"ok": false`, which was a deliberate Task 11 decision ("don't swallow the error JSON behind `Write-Error`") but has the side effect that no automation can use `$LASTEXITCODE` to detect failure. The user chose to keep printing the full JSON body *and* exit 1 on failure — both, not a tradeoff.

- [ ] **Step 1: Add the exit check to `ask`**

Find:

```powershell
        $result = Invoke-SentinelApi -Uri "$BaseUrl/ask" -Method Post -Body $body
        $result | ConvertTo-Json -Depth 20
```

Replace with:

```powershell
        $result = Invoke-SentinelApi -Uri "$BaseUrl/ask" -Method Post -Body $body
        $result | ConvertTo-Json -Depth 20

        if (-not $result.ok) {
            exit 1
        }
```

- [ ] **Step 2: Add the exit check to `prompt`**

Find:

```powershell
        $result = Invoke-SentinelApi -Uri "$BaseUrl/prompt" -Method Post -Body $body
        $result | ConvertTo-Json -Depth 20
```

Replace with:

```powershell
        $result = Invoke-SentinelApi -Uri "$BaseUrl/prompt" -Method Post -Body $body
        $result | ConvertTo-Json -Depth 20

        if (-not $result.ok) {
            exit 1
        }
```

- [ ] **Step 3: Add the exit check to `delegate`**

Find:

```powershell
        $result = Invoke-SentinelApi -Uri "$BaseUrl/delegate" -Method Post -Body $body
        $result | ConvertTo-Json -Depth 20
```

Replace with:

```powershell
        $result = Invoke-SentinelApi -Uri "$BaseUrl/delegate" -Method Post -Body $body
        $result | ConvertTo-Json -Depth 20

        if (-not $result.ok) {
            exit 1
        }
```

- [ ] **Step 4: Verify manually against the live remote bridge**

```bash
powershell -NoProfile -Command "C:\Tools\sentinel.ps1 delegate '   ' ; Write-Output \"EXIT:$LASTEXITCODE\""
```

Expected: the `Write-Error \"Task cannot be empty.\"; exit 1` path fires first (empty task text is rejected client-side before any HTTP call), so `EXIT:1`.

```bash
powershell -NoProfile -Command "C:\Tools\sentinel.ps1 task does-not-exist-12345 ; Write-Output \"EXIT:$LASTEXITCODE\""
```

Note: `task` itself isn't in scope for this change (only `ask`/`prompt`/`delegate` per the user's decision) — expect `EXIT:0` here since `task`'s branch has no exit-code logic added. Use this only to confirm you *didn't* accidentally touch the `task` branch.

```bash
powershell -NoProfile -Command "C:\Tools\sentinel.ps1 delegate 'quick real check' ; Write-Output \"EXIT:$LASTEXITCODE\""
```

Expected: `"ok": true` in the printed JSON, and `EXIT:0`.

- [ ] **Step 5: Commit**

```bash
cd /c/Tools && git add sentinel.ps1 && git commit -m "Task 17: exit 1 on ok:false for ask/prompt/delegate, per user decision"
```

---

### Task 18: Shared-secret authentication (`SENTINEL_BRIDGE_TOKEN`)

**Files:**
- Modify: `C:\Tools\sentinel-bridge\bridge.py`
- Modify: `C:\Tools\sentinel-bridge\test_bridge.py`
- Modify: `C:\Tools\sentinel.ps1`

**User decision (recorded 2026-08-30):** the final review flagged that `/delegate` (and the pre-existing `/ask`) have no authentication or origin check at all — any local process, or a malicious webpage issuing a `no-cors` POST from a browser on the same machine, can submit tasks that execute arbitrary commands on the HPC-connected host, and `/delegate`'s fire-and-forget response makes this easier to exploit unnoticed than `/ask` (no need to read the response). This was out of the original v2.2/v2.3 scope; the user asked for a simple shared-secret header now rather than deferring it.

**Design:** a `SENTINEL_BRIDGE_TOKEN` environment variable, read by both sides. If unset on the server, the bridge runs with a loud startup warning and *no* enforcement (so an already-deployed bridge doesn't silently start rejecting requests the moment this code ships — the operator has to opt in by setting the variable and restarting). If set, every request except `GET /health` must carry a matching `X-Sentinel-Token` header, checked with a constant-time comparison; a missing or wrong token gets `401`. `sentinel.ps1` reads the same variable from its own environment and attaches the header automatically when set.

**Interfaces:**
- Consumes: `Handler` (existing).
- Produces: `AUTH_TOKEN` (module-level str, from `os.environ.get("SENTINEL_BRIDGE_TOKEN", "")`), `Handler.check_auth() -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `test_bridge.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: the 4 `check_auth` tests FAIL with `AttributeError: module 'bridge' has no attribute 'AUTH_TOKEN'`; the 4 live-server auth tests FAIL because every route currently succeeds regardless of headers.

- [ ] **Step 3: Implement the Python side**

Add near the top of `bridge.py`, after the `SENTINEL = ...` line:

```python
AUTH_TOKEN = os.environ.get("SENTINEL_BRIDGE_TOKEN", "")
```

Add `import hmac` to the top imports.

Add a `check_auth` method to `Handler`, right after `read_json`:

```python
    def check_auth(self):
        if not AUTH_TOKEN:
            return True

        provided = self.headers.get("X-Sentinel-Token", "")
        return hmac.compare_digest(provided, AUTH_TOKEN)
```

Add the enforcement at the top of `_do_GET` and `_do_POST`, right after computing `path`:

```python
        if path != "/health" and not self.check_auth():
            self.send_json({"ok": False, "error": "unauthorized"}, 401)
            return
```

(`_do_POST` has no `/health` route at all, so this same line works unchanged there too — it just never matches the `path != "/health"` exemption for any POST path, which is correct since no POST route should be exempt.)

Add a startup warning in `if __name__ == "__main__":`, right after the existing `print(f"Listening: ...")` line:

```python
    if not AUTH_TOKEN:
        print(
            "WARNING: SENTINEL_BRIDGE_TOKEN is not set. This bridge is "
            "running with NO AUTHENTICATION -- anyone who can reach "
            f"http://{HOST}:{PORT} can execute arbitrary commands via "
            "Sentinel. Set SENTINEL_BRIDGE_TOKEN to a random secret and "
            "restart to require it."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/c/Tools/sentinel-bridge" && /c/Tools/sentinel-bridge/.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Expected: 68 passed (60 + 8 new).

- [ ] **Step 5: Implement the PowerShell side**

In `C:\Tools\sentinel.ps1`, replace `Invoke-SentinelApi`'s body to attach the token header when present. Find:

```powershell
    try {
        if ($Body) {
            return Invoke-RestMethod -Uri $Uri -Method $Method `
                -ContentType "application/json; charset=utf-8" -Body $Body
        }

        return Invoke-RestMethod -Uri $Uri -Method $Method
    }
```

Replace with:

```powershell
    $headers = @{}

    if ($env:SENTINEL_BRIDGE_TOKEN) {
        $headers["X-Sentinel-Token"] = $env:SENTINEL_BRIDGE_TOKEN
    }

    try {
        if ($Body) {
            return Invoke-RestMethod -Uri $Uri -Method $Method `
                -ContentType "application/json; charset=utf-8" -Body $Body -Headers $headers
        }

        return Invoke-RestMethod -Uri $Uri -Method $Method -Headers $headers
    }
```

- [ ] **Step 6: Verify manually — confirm the deployed bridge is unaffected until the operator opts in**

The remote bridge process is currently running without `SENTINEL_BRIDGE_TOKEN` set, so after this code deploys, it must keep working exactly as before with no token configured on either side:

```bash
powershell -NoProfile -Command "C:\Tools\sentinel.ps1 health"
```

Expected: unchanged, `"ok": true`.

- [ ] **Step 7: Commit**

```bash
cd /c/Tools && git add sentinel-bridge/bridge.py sentinel-bridge/test_bridge.py sentinel.ps1 && git commit -m "Task 18: add optional SENTINEL_BRIDGE_TOKEN shared-secret auth"
```

**Note for the user, not a task step:** this ships opt-in — the currently-running remote bridge process has no token and will keep accepting unauthenticated requests until you (1) set `SENTINEL_BRIDGE_TOKEN` to a random secret in the environment the remote `bridge.py` process runs under, (2) restart that process, and (3) set the same value for `$env:SENTINEL_BRIDGE_TOKEN` in whatever Windows environment/session runs `sentinel.ps1`. Nothing in this plan does that for you, since it requires a secret value only you should generate and hold.

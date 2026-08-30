import contextlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


HOST = "127.0.0.1"
PORT = int(os.environ.get("SENTINEL_BRIDGE_PORT", "8765"))

HERDR = os.environ.get("HERDR_BIN", "herdr")
SENTINEL = os.environ.get("SENTINEL_AGENT", "sentinel")

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


class SentinelPromptError(RuntimeError):
    pass


class SentinelReadError(RuntimeError):
    pass


class SentinelMarkerMissingError(RuntimeError):
    def __init__(self, message, raw_output=""):
        self.raw_output = raw_output
        super().__init__(message)


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


WORKER_POLL_SECONDS = 2

_worker_thread = None


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
                        detail = str(e)

                        if isinstance(e, SentinelMarkerMissingError) and e.raw_output:
                            detail += (
                                "\n\nRaw Sentinel output (last 4000 chars):\n"
                                + e.raw_output
                            )

                        fail_task(task_id, detail)
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


def run_herdr(*args, timeout=None):
    result = subprocess.run(
        [HERDR, *args],
        text=True,
        capture_output=True,
        timeout=timeout,
    )

    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def build_delegation_prompt(task, task_id):
    token = task_id.replace("-", "")

    return f"""
你正在接受来自 Windows Codex 的远程委派任务。

Task token:
{token}

任务：
{task}

请正常完成任务。

完成后：
1. 简洁总结结果。
2. 如果执行了重要操作，请说明。
3. 如果涉及 Slurm job，请给出 job ID。
4. 如果修改了文件，请列出文件。
5. 如果被阻塞，请明确说明原因和 Windows Codex 下一步需要做什么。

最后一行必须是：
字面前缀 SENTINEL_DONE_ 后立即连接上面的 Task token，中间不得有空格。

最后一行之后不要输出其他内容。
""".strip()

def extract_task_response(raw, task_id):
    token = task_id.replace("-", "")
    marker = f"SENTINEL_DONE_{token}"

    pos = raw.rfind(marker)

    if pos == -1:
        return None

    before = raw[:pos]

    # Claude Code 的最终回答通常以 "● " 开始。
    assistant_pos = before.rfind("\n● ")

    if assistant_pos == -1:
        assistant_pos = before.rfind("● ")

    if assistant_pos != -1:
        response = before[assistant_pos + 2:].strip()
    else:
        # fallback：至少返回 marker 前面的最近文本
        response = before[-4000:].strip()

    return response


class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # 保留最基本日志即可。
        print(
            f"{self.client_address[0]} "
            f"{self.command} "
            f"{self.path}"
        )

    def send_json(self, obj, status=200):
        data = json.dumps(
            obj,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )
        self.send_header(
            "Content-Length",
            str(len(data))
        )
        self.end_headers()

        self.wfile.write(data)

    def read_json(self):
        length = int(
            self.headers.get("Content-Length", "0")
        )

        raw = self.rfile.read(length)

        if not raw:
            return {}

        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        try:
            self._do_GET()
        except Exception as e:
            self.send_json(
                {"ok": False, "error": f"internal error: {e}"},
                500,
            )

    def _do_GET(self):
        path = urlparse(self.path).path

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

    def do_POST(self):
        try:
            self._do_POST()
        except Exception as e:
            self.send_json(
                {"ok": False, "error": f"internal error: {e}"},
                500,
            )

    def _do_POST(self):
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
                    "raw_output": e.raw_output,
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

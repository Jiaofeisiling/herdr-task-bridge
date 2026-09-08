import contextlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


HOST = "127.0.0.1"
PORT = int(os.environ.get("SENTINEL_BRIDGE_PORT", "8765"))
BRIDGE_VERSION = 7

HERDR = os.environ.get("HERDR_BIN", "herdr")

# Fallback when a request doesn't name an agent explicitly. Not "the"
# agent any more -- herdr can host several concurrent agent sessions on
# one host (see /agents, and the `agent` field on /ask, /prompt,
# /delegate), this is just what a caller gets if it doesn't pick one.
DEFAULT_AGENT = os.environ.get("SENTINEL_AGENT", "sentinel")

AUTH_TOKEN = os.environ.get("SENTINEL_BRIDGE_TOKEN", "")

DB_PATH = os.environ.get(
    "SENTINEL_DB",
    os.path.expanduser("~/sentinel-bridge/tasks.db"),
)

# Where the agent drops its answer. The bridge runs on the same host as the
# agents it drives, so a plain file is the cheapest possible channel -- and a
# far better one than the terminal: `pane.read` comes back truncated and
# carries TUI chrome the agent never said (see experiments/FINDINGS.md), while
# a file has no rendering, no wrapping and no truncation.
RESULT_DIR = os.environ.get(
    "SENTINEL_RESULT_DIR",
    os.path.join(tempfile.gettempdir(), "sentinel-bridge-results"),
)

# How long an uncollected result file survives before the startup sweep
# takes it. Only failed deliveries ever reach this age -- a collected file
# is deleted the moment it is read. 0 disables the sweep.
RESULT_RETENTION_DAYS = int(os.environ.get("SENTINEL_RESULT_RETENTION_DAYS", "7"))

# Budget for the one reminder sent when the agent finished but never wrote
# the file. Short on purpose: it only has to write a file it already knows
# the contents of, not redo any work.
RESULT_REMINDER_TIMEOUT_MS = 120_000

# Bounds for the client-supplied timeout_ms on /ask, /prompt, /delegate.
# Below the min there's no realistic chance Sentinel responds in time;
# above the max a client typo (extra zero, seconds passed as ms) could
# otherwise tie up the queue/lock far longer than any real task needs.
TIMEOUT_MS_MIN = 1000  # 1 second
TIMEOUT_MS_MAX = 21_600_000  # 6 hours -- matches /delegate's own default
READ_LINES_DEFAULT = 120
READ_LINES_MIN = 1
READ_LINES_MAX = 5000
AGENT_NAME_MAX_LENGTH = 200

# Cap on how many tasks may sit in 'queued' state at once. Without this,
# a burst of /delegate calls (or a buggy client retry loop) could grow
# tasks.db and the backlog without bound, with no way for a caller to
# tell "queued, will run eventually" apart from "the queue is effectively
# stuck". Override via env var for deployments that need a different limit.
MAX_QUEUE_DEPTH = int(os.environ.get("SENTINEL_MAX_QUEUE_DEPTH", "50"))

# A quota/balance failure belongs to the model provider, not Herdr. Keep a
# durable circuit per agent so the queue immediately uses a different runtime
# instead of repeatedly spending requests on an account that cannot answer.
# Operators clear a circuit explicitly after the provider reset/recharge.
QUOTA_FAILOVER_AGENTS = tuple(
    name.strip()
    for name in os.environ.get("SENTINEL_QUOTA_FAILOVER_AGENTS", "").split(",")
    if name.strip()
)

# Deliberately biased towards missing a real quota failure rather than
# inventing one. A match opens a durable circuit that only an operator can
# clear, so a false positive removes an agent from service until a human
# notices; a false negative just costs one wasted prompt.
#
# What the earlier, looser version got wrong, measured against real text:
#   - a bare `\b(429|402)\b` matched ordinary HPC output (Slurm job ids,
#     row counts, file sizes)
#   - `try again (in|after)`, `billing`, and a bare `rate[ -]?limit` matched
#     everyday English, including tasks *about* rate limiting
#   - being English-only, it missed the single real quota failure this
#     deployment has produced, because the provider proxy reports in Chinese
QUOTA_ERROR_PATTERN = re.compile(
    "|".join([
        # HTTP statuses, only where something actually marks them as one
        r"\b429\b[^\n]{0,24}too many requests",
        r"too many requests[^\n]{0,24}\b429\b",
        r"\b402\b[^\n]{0,24}payment required",
        r"payment required",
        r"(?:http|https|status|code|error)[^\n]{0,12}\b(?:429|402)\b",
        # provider quota / credit wording
        r"insufficient\s+(?:credits?|balance|funds|quota)",
        r"(?:credits?|balance|quota)\s+(?:exceeded|exhausted|insufficient|depleted|too low)",
        r"quota\s+(?:exceeded|exhausted|reached)",
        r"out of credits?",
        r"rate[ -]?limit(?:ed)?\s+(?:exceeded|reached)",
        # Anthropic-style usage windows, in either word order
        r"(?:usage|weekly|daily|5[ -]?hour|1[ -]?week)\s+limit\s+(?:reached|exceeded|exhausted)",
        r"(?:reached|exceeded)[^\n]{0,24}(?:usage|weekly|daily)\s+limit",
        # Chinese -- the reference deployment's proxy reports in Chinese
        r"额度不足", r"余额不足", r"余额不够", r"预扣费额度失败", r"欠费",
        r"配额(?:不足|已?用尽|超限|耗尽)",
        r"额度(?:已?用尽|超限|耗尽)",
    ]),
    re.IGNORECASE,
)


def validate_timeout_ms(timeout_ms):
    if not (TIMEOUT_MS_MIN <= timeout_ms <= TIMEOUT_MS_MAX):
        raise ValueError(
            f"timeout_ms must be between {TIMEOUT_MS_MIN} and "
            f"{TIMEOUT_MS_MAX}, got {timeout_ms}"
        )

    return timeout_ms


def validate_read_lines(read_lines):
    if not (READ_LINES_MIN <= read_lines <= READ_LINES_MAX):
        raise ValueError(
            f"lines must be between {READ_LINES_MIN} and "
            f"{READ_LINES_MAX}, got {read_lines}"
        )

    return read_lines


def validate_agent_name(agent_name):
    if not isinstance(agent_name, str):
        raise ValueError("agent must be a string")

    agent_name = agent_name.strip()

    if not agent_name:
        raise ValueError("agent cannot be empty")

    if len(agent_name) > AGENT_NAME_MAX_LENGTH:
        raise ValueError(
            f"agent cannot exceed {AGENT_NAME_MAX_LENGTH} characters"
        )

    if any(ord(char) < 32 or ord(char) == 127 for char in agent_name):
        raise ValueError("agent cannot contain control characters")

    return agent_name


def auth_token_warning():
    if not AUTH_TOKEN:
        return (
            "WARNING: SENTINEL_BRIDGE_TOKEN is not set. This bridge is "
            "running with NO AUTHENTICATION -- anyone who can reach "
            f"http://{HOST}:{PORT} can execute arbitrary commands via "
            "Sentinel. Set SENTINEL_BRIDGE_TOKEN to a random secret and "
            "restart to require it."
        )

    if not AUTH_TOKEN.isascii():
        return (
            "WARNING: SENTINEL_BRIDGE_TOKEN contains non-ASCII characters. "
            "hmac.compare_digest() cannot compare non-ASCII strings, so "
            "check_auth() fails closed and EVERY authenticated request "
            "will get 401 until this is fixed. Use an ASCII-only token."
        )

    return None


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
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

    with db_session() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                agent TEXT NOT NULL,

                status TEXT NOT NULL,

                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,

                timeout_ms INTEGER NOT NULL,

                result_text TEXT,
                error_text TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS quota_blocks (
                agent TEXT PRIMARY KEY,
                detected_at TEXT NOT NULL,
                detail TEXT NOT NULL
            )
        """)

        # Migrate DBs from before multi-agent support -- every row that
        # already exists was necessarily created against DEFAULT_AGENT,
        # since there was no other choice at the time.
        existing_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(tasks)")
        }

        if "agent" not in existing_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN agent TEXT")
            conn.execute(
                "UPDATE tasks SET agent = ? WHERE agent IS NULL",
                (DEFAULT_AGENT,),
            )

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


class QueueFullError(RuntimeError):
    def __init__(self, queued, maximum):
        self.queued = queued
        self.maximum = maximum
        super().__init__(
            f"queue full: {queued}/{maximum} tasks already queued"
        )


def _insert_task(conn, task_id, task, timeout_ms, agent_name):
    conn.execute("""
        INSERT INTO tasks (
            task_id, task, agent, status, created_at, timeout_ms
        )
        VALUES (?, ?, ?, 'queued', ?, ?)
    """, (task_id, task, agent_name, now_iso(), timeout_ms))


def create_task(task, timeout_ms, agent_name):
    task_id = str(uuid.uuid4())

    with db_session() as conn:
        _insert_task(conn, task_id, task, timeout_ms, agent_name)

    return task_id


def create_task_if_queue_available(task, timeout_ms, agent_name):
    """Atomically enforce the queue cap and enqueue one task.

    ThreadingHTTPServer can process several /delegate requests concurrently.
    BEGIN IMMEDIATE serializes the count-and-insert section so two callers
    cannot both observe the same free queue slot and overfill the queue.
    """
    task_id = str(uuid.uuid4())

    with db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status = 'queued'"
        ).fetchone()
        queued = row["n"]

        if queued >= MAX_QUEUE_DEPTH:
            raise QueueFullError(queued, MAX_QUEUE_DEPTH)

        _insert_task(conn, task_id, task, timeout_ms, agent_name)

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


def count_queued_tasks():
    with db_session() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status = 'queued'"
        ).fetchone()

    return row["n"]


def peek_next_task(exclude_agents=()):
    # exclude_agents lets the worker skip past tasks whose agent it
    # already found busy earlier in the same pass, instead of getting
    # stuck retrying the oldest queued task while a different, already-
    # idle agent's tasks sit behind it untouched.
    exclude_agents = list(exclude_agents)
    placeholders = ", ".join("?" for _ in exclude_agents)
    where_agent = f"AND agent NOT IN ({placeholders})" if exclude_agents else ""

    with db_session() as conn:
        row = conn.execute(f"""
            SELECT * FROM tasks
            WHERE status = 'queued' {where_agent}
            ORDER BY created_at ASC
            LIMIT 1
        """, exclude_agents).fetchone()

    return dict(row) if row else None


def claim_task(task_id):
    with db_session() as conn:
        cursor = conn.execute("""
            UPDATE tasks
            SET status = 'running', started_at = ?
            WHERE task_id = ? AND status = 'queued'
        """, (now_iso(), task_id))

        return cursor.rowcount == 1


def requeue_task(task_id):
    """Return a claimed task to the queue when every fallback is busy."""
    with db_session() as conn:
        cursor = conn.execute("""
            UPDATE tasks
            SET status = 'queued', started_at = NULL, finished_at = NULL
            WHERE task_id = ? AND status = 'running'
        """, (task_id,))

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


def quota_exhausted_task(task_id, error_text):
    with db_session() as conn:
        conn.execute("""
            UPDATE tasks
            SET status = 'quota_exhausted', error_text = ?, finished_at = ?
            WHERE task_id = ?
        """, (error_text, now_iso(), task_id))


def mark_agent_quota_blocked(agent_name, detail):
    with db_session() as conn:
        conn.execute("""
            INSERT INTO quota_blocks (agent, detected_at, detail)
            VALUES (?, ?, ?)
            ON CONFLICT(agent) DO UPDATE SET
                detected_at = excluded.detected_at,
                detail = excluded.detail
        """, (agent_name, now_iso(), detail))


def get_agent_quota_block(agent_name):
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM quota_blocks WHERE agent = ?", (agent_name,)
        ).fetchone()

    return dict(row) if row else None


def list_agent_quota_blocks():
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM quota_blocks ORDER BY detected_at DESC"
        ).fetchall()

    return [dict(row) for row in rows]


def clear_agent_quota_blocks(agent_name=None):
    with db_session() as conn:
        if agent_name is None:
            cursor = conn.execute("DELETE FROM quota_blocks")
        else:
            cursor = conn.execute(
                "DELETE FROM quota_blocks WHERE agent = ?", (agent_name,)
            )

    return cursor.rowcount


# One lock per agent name, not one global lock -- a request against
# "sentinel-opencode" must not block on (or be blocked by) a concurrent
# request against a completely different agent like "sentinel". Locks are
# created lazily and never removed; a handful of long-lived Lock objects
# for the agents this bridge ever sees is not worth cleaning up.
_agent_locks = {}
_agent_locks_guard = threading.Lock()

AVAILABLE_STATES = {
    "idle",
    "done",
}


def get_agent_lock(agent_name):
    with _agent_locks_guard:
        lock = _agent_locks.get(agent_name)
        if lock is None:
            lock = threading.Lock()
            _agent_locks[agent_name] = lock
        return lock


def get_agent_status(agent_name):
    result = run_herdr(
        "agent",
        "get",
        agent_name,
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


def list_agents():
    result = run_herdr("agent", "list", timeout=10)

    if not result["ok"]:
        return None, result

    try:
        payload = json.loads(result["stdout"])
        agents = payload.get("result", {}).get("agents", [])
        return agents, result

    except Exception as e:
        return None, {
            "ok": False,
            "error": f"Unable to parse Herdr agent list: {e}",
            "raw": result["stdout"],
        }


class SentinelPromptError(RuntimeError):
    pass


class AgentQuotaExhaustedError(SentinelPromptError):
    def __init__(self, agent_name, detail, raw_output=""):
        self.agent_name = agent_name
        self.detail = detail
        self.raw_output = raw_output
        super().__init__(f"Agent quota exhausted ({agent_name}): {detail}")


class QuotaFailoverExhaustedError(AgentQuotaExhaustedError):
    def __init__(self, quota_errors):
        self.quota_errors = quota_errors
        detail = "; ".join(
            f"{error.agent_name}: {error.detail}" for error in quota_errors
        )
        super().__init__(
            ", ".join(error.agent_name for error in quota_errors),
            "all eligible fallback agents are quota-blocked or quota-exhausted; "
            + detail,
        )


class SentinelResultMissingError(RuntimeError):
    def __init__(self, message, raw_output=""):
        self.raw_output = raw_output
        super().__init__(message)


def result_file_path(task_id):
    token = task_id.replace("-", "")
    return os.path.join(RESULT_DIR, f"result-{token}.txt")


def purge_stale_result_files():
    """Delete result files nobody came back for. Returns how many went.

    read_result_file() removes a file only once it has read it, so every
    timeout, orphaned task and failed delivery leaves one behind. Under the
    system temp dir the OS eventually reclaimed those; deployments now point
    RESULT_DIR at project storage, which reclaims nothing, so the bridge has
    to sweep up after itself. Startup is enough -- the leak is slow, and a
    restart is the one moment when no task can be mid-flight.
    """
    if RESULT_RETENTION_DAYS <= 0:
        return 0

    cutoff = time.time() - RESULT_RETENTION_DAYS * 86400
    removed = 0

    try:
        names = os.listdir(RESULT_DIR)
    except OSError:
        # Not created yet (first boot), or unreadable. Housekeeping must
        # never be the reason the bridge fails to start.
        return 0

    for name in names:
        # Only files this bridge created. An operator's own notes, or
        # anything else sharing the directory, is not ours to delete.
        if not (name.startswith("result-") and name.endswith(".txt")):
            continue

        path = os.path.join(RESULT_DIR, name)

        try:
            if os.path.getmtime(path) >= cutoff:
                continue
            os.remove(path)
            removed += 1
        except OSError:
            # Raced with a live read, or not ours to remove. Either way the
            # next restart gets another go.
            continue

    return removed


def result_dir_scope_warning():
    """Warn when RESULT_DIR sits outside an agent's own working directory.

    That configuration is what made agents' permission systems flag the
    result write -- and the denial lands *after* the task has run, so it
    surfaces as an unexplained mid-task stall. Checking it at boot turns a
    puzzling runtime failure into a line in the startup log. Advisory only:
    the bridge cannot see an agent's allowlist, just this one common cause.
    """
    agents, _ = list_agents()

    if not agents:
        # herdr not up yet is normal at boot. Don't invent a warning out of
        # missing information.
        return None

    result_dir = os.path.abspath(RESULT_DIR)
    outside = []

    for agent in agents:
        cwd = agent.get("cwd")
        if not cwd:
            continue

        if os.path.commonpath([result_dir, os.path.abspath(cwd)]) != os.path.abspath(cwd):
            outside.append(agent.get("name") or agent.get("pane_id") or "?")

    if not outside:
        return None

    return (
        f"WARNING: {RESULT_DIR} is outside the working directory of: "
        f"{', '.join(outside)}. Those agents' permission systems may treat "
        "writing the result file as an external write and block it -- after "
        "the task has already run, so it looks like an unexplained stall. "
        "Point SENTINEL_RESULT_DIR at a directory under their cwd."
    )


def read_result_file(task_id, cleanup=True):
    """Return the agent's answer, or None if it never wrote one."""
    path = result_file_path(task_id)

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read().strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        print(f"[result] unreadable result file {path}: {e}")
        return None

    if cleanup:
        try:
            os.remove(path)
        except OSError:
            # A leftover file is untidy, not a failure -- the content is
            # already in hand and the name is task-unique either way.
            pass

    return content or None


def quota_error_detail(text, ignore=None):
    """Return a compact provider error when text looks like a quota failure.

    `ignore` is text the agent merely echoed rather than produced -- the
    delegated task itself, which the terminal always contains. Without this,
    a task *about* rate limits reads as the agent having hit one.
    """
    if not text:
        return None

    haystack = text
    if ignore:
        haystack = haystack.replace(ignore, " ")

    if not QUOTA_ERROR_PATTERN.search(haystack):
        return None

    compact = " ".join(text.strip().split())
    return compact[-1000:] or "provider reported a quota or balance failure"


def _agent_runtime_family(agent):
    """Map Herdr's runtime field to a provider family for safe auto-failover."""
    raw = str(agent.get("agent", "")).strip().lower()
    name = str(agent.get("name", "")).strip().lower()

    for value in (raw, name):
        if "opencode" in value:
            return "opencode"
        if "claude" in value:
            return "claude"
        if "codex" in value:
            return "codex"

    return raw or None


def quota_failover_candidates(primary_agent):
    """Return alternate agents in a different runtime family.

    `SENTINEL_QUOTA_FAILOVER_AGENTS` is an explicit ordered allowlist. Without
    it, use Herdr discovery, but never silently switch to another session of
    the same provider family because it may share the same exhausted account.
    """
    agents, list_result = list_agents()
    if agents is None:
        raise SentinelUnavailableError(
            list_result.get("error", "unable to discover fallback agents")
        )

    by_name = {
        agent.get("name"): agent
        for agent in agents
        if isinstance(agent, dict) and agent.get("name")
    }
    primary_family = _agent_runtime_family(by_name.get(primary_agent, {}))

    if QUOTA_FAILOVER_AGENTS:
        names = QUOTA_FAILOVER_AGENTS
    else:
        names = tuple(by_name)

    candidates = []
    for name in names:
        if name == primary_agent or name not in by_name:
            continue

        # A configured allowlist is an operator's explicit decision to use
        # these sessions. Discovery mode is stricter and insists on a
        # different runtime family before it can switch automatically.
        if not QUOTA_FAILOVER_AGENTS:
            family = _agent_runtime_family(by_name[name])
            if not primary_family or not family or family == primary_family:
                continue

        candidates.append(name)

    return candidates


def build_result_reminder_prompt(task_id):
    return f"""
你刚才那条委派任务已经做完了，但结果没有写进约定的文件。

不要重新执行任务，不要再运行任何命令，不要改动任何文件。

只需要把你刚才已经得到的结果写入：
{result_file_path(task_id)}
""".strip()


def _run_herdr_prompt(agent_name, delegated_prompt, timeout_ms):
    try:
        result = run_herdr(
            "agent",
            "prompt",
            agent_name,
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

    output = "\n".join((result.get("stderr", ""), result.get("stdout", "")))
    detail = quota_error_detail(output)
    if detail:
        raise AgentQuotaExhaustedError(agent_name, detail, raw_output=output)

    if not result["ok"]:
        raise SentinelPromptError(
            "Herdr prompt command failed: " + result.get("stderr", "")
        )

    return result


def run_prompt_only(agent_name, task_id, task, timeout_ms):
    delegated_prompt = build_delegation_prompt(task, task_id)
    return _run_herdr_prompt(agent_name, delegated_prompt, timeout_ms)


def _read_terminal_tail(agent_name, read_lines):
    """Terminal text, for failure diagnostics only. Never on the happy path:
    a broken/slow read here must not mask the error being diagnosed."""
    try:
        result = run_herdr(
            "agent",
            "read",
            agent_name,
            "--source",
            "recent-unwrapped",
            "--lines",
            str(read_lines),
            timeout=60,
        )
    except Exception as e:
        return f"(unable to read terminal for diagnostics: {e})"

    if not result["ok"]:
        return "(terminal read failed: " + result.get("stderr", "") + ")"

    return result["stdout"][-4000:]


def execute_sentinel_task(agent_name, task_id, task, timeout_ms, read_lines=500):
    os.makedirs(RESULT_DIR, exist_ok=True)

    _run_herdr_prompt(
        agent_name, build_delegation_prompt(task, task_id), timeout_ms
    )

    response = read_result_file(task_id)

    if response is None:
        terminal_tail = _read_terminal_tail(agent_name, read_lines)

        # `ignore=task`: the terminal echoes the delegated task back, so
        # without this a task about rate limits would read as the agent
        # having hit one -- and that would durably circuit-break the agent.
        detail = quota_error_detail(terminal_tail, ignore=task)
        if detail:
            raise AgentQuotaExhaustedError(
                agent_name, detail, raw_output=terminal_tail
            )

        # The agent's turn ended without a result file. Ask once for just the
        # file -- it already did the work, so this is far cheaper than the old
        # "restate everything" recovery, and it cannot re-run anything.
        #
        # Logged because it costs a whole extra prompt, and because the hit
        # rate is the only signal the bridge has about how often results go
        # missing at all. It fired when RESULT_DIR sat in /tmp, outside the
        # agents' cwd, where an agent's permission system blocked the write
        # (observed once, and only the reminder got that result out). If it
        # starts recurring, check RESULT_DIR has not moved back outside.
        print(
            f"[task {task_id}] no result file after the task prompt, "
            f"sending reminder (agent={agent_name})"
        )

        _run_herdr_prompt(
            agent_name,
            build_result_reminder_prompt(task_id),
            RESULT_REMINDER_TIMEOUT_MS,
        )

        response = read_result_file(task_id)

    if response is None:
        raise SentinelResultMissingError(
            "Sentinel finished but never wrote its result file "
            f"({result_file_path(task_id)}), including after a reminder. "
            "Read the terminal tail below before re-running anything: the "
            "work itself may have succeeded and only the delivery was "
            f"missed. If this recurs, check that {RESULT_DIR} is writable by "
            "the agent and inside what its own permission system allows "
            "(OpenCode's external_directory rules, Claude Code's auto-mode "
            "classifier) -- pointing SENTINEL_RESULT_DIR at a directory "
            "under the agent's cwd rules that class out.",
            # Re-read rather than reusing the pre-reminder snapshot: when this
            # error fires, what happened *during* the reminder is the whole
            # question, and that snapshot predates it.
            raw_output=_read_terminal_tail(agent_name, read_lines),
        )

    return response


class SentinelBusyError(RuntimeError):
    def __init__(self, agent_status):
        self.agent_status = agent_status
        super().__init__(f"Sentinel busy: {agent_status}")


class SentinelUnavailableError(RuntimeError):
    pass


def _remember_quota_error(error):
    mark_agent_quota_blocked(error.agent_name, error.detail)


def _run_quota_fallbacks(primary_agent, operation, first_error):
    """Try eligible alternate providers after the primary is quota-blocked."""
    quota_errors = [first_error]
    unavailable = []

    for agent_name in quota_failover_candidates(primary_agent):
        existing_block = get_agent_quota_block(agent_name)
        if existing_block:
            quota_errors.append(AgentQuotaExhaustedError(
                agent_name,
                "quota circuit is open since " + existing_block["detected_at"],
            ))
            continue

        try:
            with acquire_agent_for_delegation(agent_name):
                return operation(agent_name), agent_name
        except AgentQuotaExhaustedError as error:
            _remember_quota_error(error)
            quota_errors.append(error)
        except (SentinelBusyError, SentinelUnavailableError) as error:
            unavailable.append(f"{agent_name}: {error}")

    if unavailable:
        raise SentinelBusyError(
            "quota fallback unavailable; " + "; ".join(unavailable)
        )

    raise QuotaFailoverExhaustedError(quota_errors)


def run_with_quota_failover(primary_agent, operation, primary_locked=False):
    """Run once on the requested agent, then actively switch providers.

    The caller receives both the operation result and the actual agent name.
    A persisted quota circuit avoids hitting a known-exhausted account again.
    """
    existing_block = get_agent_quota_block(primary_agent)
    if existing_block:
        return _run_quota_fallbacks(
            primary_agent,
            operation,
            AgentQuotaExhaustedError(
                primary_agent,
                "quota circuit is open since " + existing_block["detected_at"],
            ),
        )

    try:
        if primary_locked:
            return operation(primary_agent), primary_agent

        with acquire_agent_for_delegation(primary_agent):
            return operation(primary_agent), primary_agent
    except AgentQuotaExhaustedError as error:
        _remember_quota_error(error)
        return _run_quota_fallbacks(primary_agent, operation, error)


@contextlib.contextmanager
def acquire_agent_for_delegation(agent_name):
    lock = get_agent_lock(agent_name)

    if not lock.acquire(blocking=False):
        raise SentinelBusyError("locked")

    try:
        try:
            agent_status, status_result = get_agent_status(agent_name)
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
        lock.release()


WORKER_POLL_SECONDS = 2

_worker_thread = None


def task_worker(stop_event=None):
    print("Task worker started.")

    # Agents found busy earlier in the current pass over the queue --
    # reset once the pass finds nothing left to try (empty queue, or
    # every remaining queued task's agent is in this set already). This
    # is what stops one busy agent's oldest task from starving a
    # different, already-idle agent's tasks sitting behind it.
    busy_this_pass = set()

    while stop_event is None or not stop_event.is_set():
        try:
            task_row = peek_next_task(exclude_agents=busy_this_pass)

            if task_row is None:
                busy_this_pass.clear()
                time.sleep(WORKER_POLL_SECONDS)
                continue

            # 复用 /ask 用的同一把锁 + 同一套 busy 判断，
            # 避免维护两份重复的加锁逻辑。
            agent_name = task_row["agent"]
            task_id = task_row["task_id"]
            claimed = False

            try:
                operation = lambda target: execute_sentinel_task(
                    agent_name=target,
                    task_id=task_id,
                    task=task_row["task"],
                    timeout_ms=task_row["timeout_ms"],
                )

                if get_agent_quota_block(agent_name):
                    # The primary was already proved unavailable for quota
                    # reasons. Claim once, then actively use a fallback
                    # without sending another prompt to that account.
                    if not claim_task(task_id):
                        continue
                    claimed = True
                    result, used_agent = run_with_quota_failover(
                        agent_name, operation
                    )
                else:
                    with acquire_agent_for_delegation(agent_name):
                        if not claim_task(task_id):
                            continue
                        claimed = True
                        result, used_agent = run_with_quota_failover(
                            agent_name, operation, primary_locked=True
                        )

                complete_task(task_id, result)
                print(f"[task {task_id}] done (agent={used_agent})")

            except TimeoutError as e:
                # The agent may still be executing, so it cannot safely be
                # retried on another provider.
                if claimed:
                    orphan_task(task_id, str(e))
                    print(f"[task {task_id}] orphaned")

            except QuotaFailoverExhaustedError as e:
                detail = str(e)
                if e.raw_output:
                    detail += "\n\nRaw provider output:\n" + e.raw_output
                if claimed:
                    quota_exhausted_task(task_id, detail)
                    print(f"[task {task_id}] quota exhausted: {e}")

            except (SentinelBusyError, SentinelUnavailableError):
                if claimed:
                    # A quota fallback exists but is temporarily occupied or
                    # unreachable. Preserve the task and retry later; do not
                    # misreport this as either a completed fallback or a
                    # permanent quota failure.
                    requeue_task(task_id)

                # This agent's busy/unreachable -- remember that for the
                # rest of this pass and immediately look for a task
                # against a different agent, instead of sleeping and
                # retrying the same oldest-but-busy task.
                busy_this_pass.add(agent_name)
                continue

            except Exception as e:
                if claimed:
                    detail = str(e)
                    if isinstance(e, SentinelResultMissingError) and e.raw_output:
                        detail += (
                            "\n\nRaw Sentinel output (last 4000 chars):\n"
                            + e.raw_output
                        )
                    fail_task(task_id, detail)
                    print(f"[task {task_id}] error: {e}")

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
    path = result_file_path(task_id)

    return f"""
以下是一条远程委派的任务。

===== 任务开始 =====
{task}
===== 任务结束 =====

做完后把结果写入：
{path}

写清楚做了什么、结论是什么、有没有卡住或改动了什么。文件会被整份取走，别写凭据。
""".strip()

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

    def query_agent_name(self):
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        values = query.get("agent")
        return validate_agent_name(values[0] if values else DEFAULT_AGENT)

    def query_read_lines(self):
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        values = query.get("lines")
        value = values[0] if values else READ_LINES_DEFAULT
        return validate_read_lines(int(value))

    def read_json(self):
        length = int(
            self.headers.get("Content-Length", "0")
        )

        raw = self.rfile.read(length)

        if not raw:
            return {}

        return json.loads(raw.decode("utf-8"))

    def check_auth(self):
        if not AUTH_TOKEN:
            return True

        provided = self.headers.get("X-Sentinel-Token", "")

        try:
            return hmac.compare_digest(provided, AUTH_TOKEN)
        except TypeError:
            # e.g. a non-ASCII token/header value -- treat as a failed
            # comparison (401), not an internal error (500).
            return False

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

        if not (self.command == "GET" and path == "/health") and not self.check_auth():
            self.send_json({"ok": False, "error": "unauthorized"}, 401)
            return

        if path == "/health":
            self.send_json({
                "ok": True,
                "service": "nesi-sentinel-bridge",
                "version": BRIDGE_VERSION,
                # Keep the v3 field for one compatibility cycle. New clients
                # should prefer default_agent, which better describes v4.
                "agent": DEFAULT_AGENT,
                "default_agent": DEFAULT_AGENT,
                "worker_alive": (
                    _worker_thread.is_alive() if _worker_thread else None
                ),
            })
            return

        if path == "/agents":
            agents, list_result = list_agents()

            if agents is None:
                self.send_json(
                    {
                        "ok": False,
                        "error": list_result.get(
                            "error", "unable to list agents"
                        ),
                    },
                    503,
                )
                return

            self.send_json({
                "ok": True,
                "agents": agents,
            })
            return

        if path == "/quota":
            self.send_json({
                "ok": True,
                "blocked_agents": list_agent_quota_blocks(),
            })
            return

        if path in ("/status", "/ready", "/read"):
            try:
                agent_name = self.query_agent_name()
                read_lines = (
                    self.query_read_lines() if path == "/read" else None
                )
            except (TypeError, ValueError) as e:
                self.send_json(
                    {"ok": False, "error": f"invalid request: {e}"},
                    400,
                )
                return

        if path == "/status":
            result = run_herdr(
                "agent",
                "get",
                agent_name,
            )

            self.send_json(
                result,
                200 if result["ok"] else 500,
            )
            return

        if path == "/ready":
            agent_status, status_result = get_agent_status(
                agent_name
            )

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
                agent_name,
                "--source",
                "recent-unwrapped",
                "--lines",
                str(read_lines),
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

        if not (self.command == "GET" and path == "/health") and not self.check_auth():
            self.send_json({"ok": False, "error": "unauthorized"}, 401)
            return

        if path == "/delegate":
            try:
                body = self.read_json()

                task = body["task"].strip()
                agent_name = validate_agent_name(
                    body.get("agent", DEFAULT_AGENT)
                )

                timeout_ms = validate_timeout_ms(int(
                    body.get(
                        "timeout_ms",
                        21600000,  # 6 hours
                    )
                ))

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

            try:
                task_id = create_task_if_queue_available(
                    task, timeout_ms, agent_name
                )
            except QueueFullError as e:
                self.send_json(
                    {
                        "ok": False,
                        "error": str(e),
                    },
                    429,
                )
                return

            self.send_json(
                {
                    "ok": True,
                    "task_id": task_id,
                    "status": "queued",
                },
                202,
            )
            return

        if path == "/quota/reset":
            try:
                body = self.read_json()
                agent_value = body.get("agent")
                agent_name = (
                    validate_agent_name(agent_value)
                    if agent_value is not None else None
                )
            except Exception as e:
                self.send_json(
                    {"ok": False, "error": f"invalid request: {e}"}, 400
                )
                return

            cleared = clear_agent_quota_blocks(agent_name)
            self.send_json({
                "ok": True,
                "agent": agent_name,
                "cleared": cleared,
            })
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
            agent_name = validate_agent_name(
                body.get("agent", DEFAULT_AGENT)
            )

            timeout_ms = validate_timeout_ms(int(
                body.get("timeout_ms", 120000)
            ))

            read_lines = validate_read_lines(int(
                body.get("lines", 500)
            ))

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
            if path == "/prompt":
                prompt_result, used_agent = run_with_quota_failover(
                    agent_name,
                    lambda target: run_prompt_only(
                        target, task_id, task, timeout_ms
                    ),
                )

                self.send_json({
                    "ok": True,
                    "task_id": task_id,
                    "agent": used_agent,
                    "prompt": prompt_result,
                })
                return

            result_text, used_agent = run_with_quota_failover(
                agent_name,
                lambda target: execute_sentinel_task(
                    target, task_id, task, timeout_ms, read_lines
                ),
            )

            self.send_json({
                "ok": True,
                "task_id": task_id,
                "agent": used_agent,
                "result": {"text": result_text},
            })
            return

        except QuotaFailoverExhaustedError as e:
            self.send_json(
                {
                    "ok": False,
                    "status": "quota_exhausted",
                    "task_id": task_id,
                    "agents": [
                        error.agent_name for error in e.quota_errors
                    ],
                    "error": str(e),
                },
                429,
            )
            return

        except SentinelBusyError as e:
            self.send_json(
                {
                    "ok": False,
                    "status": "busy",
                    "agent": agent_name,
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
                    "agent": agent_name,
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

        except SentinelResultMissingError as e:
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

        except SentinelPromptError as e:
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

    print(f"Sentinel Bridge v{BRIDGE_VERSION}")
    print(f"Default agent: {DEFAULT_AGENT}")
    print(f"Database: {DB_PATH}")
    print(f"Listening: http://{HOST}:{PORT}")

    swept = purge_stale_result_files()
    if swept:
        print(f"Swept {swept} result file(s) older than {RESULT_RETENTION_DAYS}d")

    for startup_warning in (auth_token_warning(), result_dir_scope_warning()):
        if startup_warning:
            print(startup_warning)

    server = ThreadingHTTPServer(
        (HOST, PORT),
        Handler,
    )

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        print("\nStopping.")

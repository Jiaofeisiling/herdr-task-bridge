import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote


HOST = "127.0.0.1"
PORT = int(os.environ.get("SENTINEL_BRIDGE_PORT", "8765"))
BRIDGE_VERSION = 28

HERDR = os.environ.get("HERDR_BIN", "herdr")

# After herdr aborts its wait with agent_blocked, how the bridge decides what
# actually happened. herdr's `blocked` flickers while an agent is busy -- one
# measured failure showed it blocked, then working a second later -- so the
# abort is not taken as a verdict; the agent is watched instead.
#
# BLOCKED_CONFIRM_SEC: how long it must *stay* blocked before the bridge says
#   so. Long enough that a flicker does not qualify, short enough that a real
#   approval prompt is not left waiting out the task's whole timeout.
# NOT_STARTED_GRACE_SEC: how long to wait for any sign of activity before
#   concluding the prompt was never taken up.
BLOCKED_CONFIRM_SEC = float(os.environ.get("SENTINEL_BLOCKED_CONFIRM_SEC", "20"))
NOT_STARTED_GRACE_SEC = float(os.environ.get("SENTINEL_NOT_STARTED_GRACE_SEC", "8"))
ABORT_POLL_SEC = 2.0

# How long an orderly shutdown waits for requests already in flight. Short on
# purpose: the listener is closed first, so nothing new arrives, and a
# synchronous ask can run for minutes -- waiting for one would turn every
# restart into a hang.
SHUTDOWN_GRACE_SEC = float(os.environ.get("SENTINEL_SHUTDOWN_GRACE_SEC", "5"))

# Upper bound on any single herdr call that did not name its own. The
# default used to be None -- wait forever -- so a herdr that hung left the
# calling request thread blocked indefinitely. Callers with a legitimate
# reason to wait longer (a prompt that runs as long as the task allows) pass
# their own timeout; this only catches the ones that forgot.
HERDR_DEFAULT_TIMEOUT_SEC = int(os.environ.get("SENTINEL_HERDR_TIMEOUT_SEC", "30"))

# Fallback when a request doesn't name an agent explicitly. Not "the"
# agent any more -- herdr can host several concurrent agent sessions on
# one host (see /agents, and the `agent` field on /ask, /prompt,
# /delegate), this is just what a caller gets if it doesn't pick one.
# Unset means "pick an available agent", not a literal agent called
# "sentinel". Pinning a deployment to one name was the old default and it
# ages badly: herdr drops an agent's name when the agent process restarts,
# so the configured name stops resolving and every request that took the
# default fails at once. Leave this unset unless a deployment genuinely
# needs one specific agent.
DEFAULT_AGENT = os.environ.get("SENTINEL_AGENT") or None

AUTH_TOKEN = os.environ.get("SENTINEL_BRIDGE_TOKEN", "")

# Without a token anyone who can reach the port can have the agents run commands,
# and on a shared login node that is every user on the machine. So the bridge
# does not start that way unless asked to, by name.
ALLOW_NO_AUTH = os.environ.get("SENTINEL_ALLOW_NO_AUTH") == "1"
MIN_TOKEN_LENGTH = 16

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
# is deleted after persistence or a successful synchronous response.
# 0 disables the sweep.
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

# Bound asynchronous execution independently of the HTTP request threads.
WORKER_CONCURRENCY = os.environ.get("SENTINEL_WORKER_CONCURRENCY", "4")


def validate_worker_concurrency(value):
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if type(value) is not int or not 1 <= value <= 32:
        raise ValueError("SENTINEL_WORKER_CONCURRENCY must be an integer from 1 to 32")
    return value

# A quota/balance failure belongs to the model provider, not Herdr. Open a
# circuit per agent so the queue immediately uses a different runtime instead
# of repeatedly spending requests on an account that cannot answer.
#
# The circuit expires on its own because the condition does: providers state
# a reset time in the very text that trips it ("resets 4:20pm"). This was
# originally a latch only an operator could release, which left a recovered
# agent unusable until somebody noticed -- observed live, with the agent
# reporting idle and every dispatch refused by a circuit hours old.
#
# The default is short because the costs are asymmetric. Expiring too early
# wastes one prompt and re-opens the circuit; expiring too late takes an
# agent out of service for no reason. quota-reset still clears one early.
QUOTA_BLOCK_TTL_SECONDS = int(
    os.environ.get("SENTINEL_QUOTA_BLOCK_TTL_SECONDS", "3600")
)

# A provider can also refuse an agent for a reason that is not credit: its
# session has grown past the per-request size its model or provider accepts. From here
# it looks the same -- herdr reports the agent done, and the refusal is only
# text in its terminal -- and the right response is the same: stop sending it
# work and use another agent. It clears sooner than a quota, though, because
# the cure is a person compacting or restarting the session, which takes
# minutes rather than waiting out a reset. A circuit that lingered for the
# quota's hour would keep a recovered agent out of service, which is the
# failure the TTL above exists to prevent.
CONTEXT_BLOCK_TTL_SECONDS = int(
    os.environ.get("SENTINEL_CONTEXT_BLOCK_TTL_SECONDS", "600")
)

# Timing selects a concise missing-result diagnostic, never a verdict about
# whether work ran: small operations can have effects within a second.
QUICK_END_SEC = float(os.environ.get("SENTINEL_QUICK_END_SEC", "15"))

QUOTA_FAILOVER_AGENTS = tuple(
    name.strip()
    for name in os.environ.get("SENTINEL_QUOTA_FAILOVER_AGENTS", "").split(",")
    if name.strip()
)

# Ordered cost preference, cheapest first, matched against an agent's name
# or its runtime family. Selection otherwise breaks ties on a stable
# identifier, which makes the cheapest agent win only by coincidence of
# sort order.
#
# The bridge cannot work this order out for itself: whether a backend is
# a prepaid subscription or metered per token is a billing arrangement,
# not something herdr reports. A subscription's quota is already paid for
# whether it is used or not, so it usually belongs first.
#
# Deliberately not time-aware. Metered providers do vary price by hour --
# DeepSeek bills peak 01:00-04:00 and 06:00-10:00 UTC on weekdays at
# double its off-peak rate as of 2026-09 -- but a schedule baked in here
# would go stale silently, and it changes no decision while only one
# metered backend exists: a subscription outranks it at every hour.
AGENT_PRIORITY = tuple(
    name.strip().lower()
    for name in os.environ.get("SENTINEL_AGENT_PRIORITY", "").split(",")
    if name.strip()
)

# What a delegated task is permitted to do with Slurm, stated in the
# prompt so the agent knows before it acts.
#
# This is a declared policy, not an enforced one, and the distinction
# matters: the bridge is not in the execution path. It sends text through
# `herdr agent prompt` and the agent decides what to run, so nothing here
# can see or stop an sbatch. What it changes is that submitting
# production work becomes something the agent was told it may do, rather
# than something it decided on its own -- and that widening the policy is
# a deliberate act by the caller, visible in the request and recorded
# against the task.
#
# The default does not restrict submission, and that is a correction. It
# previously withheld full-scale runs, costing an extra round trip on
# every real job -- a restriction chosen on an assumed risk rather than an
# observed one, which is the same mistake as the self-justifying sentences
# this prompt has already had cut out of it.
#
# Submitting is reversible: scancel it and resubmit, and the only cost is
# queue time. Not submitting is the expensive outcome, and every genuine
# blockage on this deployment has come from a guardrail firing where none
# was needed, not from an agent doing something rash.
#
# What is genuinely irreversible is not the submission but a job
# destroying work that already exists, so that is where the line is drawn
# in every policy. Unbounded resubmission is the other real hazard -- it
# is how one bad script quietly burns an allocation -- so reporting a
# failure rather than retrying is asked for throughout.
SLURM_POLICIES = {
    "dry_run_only": (
        "Slurm：本任务只允许静态检查和 `sbatch --test-only`，"
        "**不得真正提交任何作业**。"
    ),
    "test_only": (
        "Slurm：只提交 debug/短时限作业，不要完整规模。失败别反复重投，"
        "写清原因。不要覆盖或删除已有的 checkpoint、结果和数据集。"
    ),
    "submit": (
        "Slurm：**自由提交**，不限规模，不必先请示——写错了 scancel 重来即可。"
        "失败别反复重投，写清原因。**不要覆盖或删除已有的 checkpoint、"
        "结果和数据集**。"
    ),
}

DEFAULT_SLURM_POLICY = os.environ.get("SENTINEL_SLURM_POLICY", "submit")


def validate_slurm_policy(policy):
    if policy is None:
        policy = DEFAULT_SLURM_POLICY

    if policy not in SLURM_POLICIES:
        # Never fall back to the default on an unrecognised value: a typo
        # in the strictest setting would silently become the loosest one
        # the deployment allows, which is the opposite of what the caller
        # was reaching for.
        raise ValueError(
            f"slurm_policy must be one of {', '.join(sorted(SLURM_POLICIES))}, "
            f"got {policy!r}"
        )

    return policy

# What may become of a queued task whose agent no longer exists. `none` is the
# default and fails it, since sending work to the wrong agent is worse than not
# sending it; `any` is the caller saying it does not care which agent runs it.
REROUTE_POLICIES = ("none", "any")


def validate_reroute(policy):
    if policy is None:
        return "none"

    if policy not in REROUTE_POLICIES:
        # Never a quiet fallback to the default, for the same reason as
        # validate_slurm_policy: a typo must not change what was asked for.
        raise ValueError(
            f"reroute must be one of {', '.join(REROUTE_POLICIES)}, "
            f"got {policy!r}"
        )

    return policy


# Wordings of "this request is over a size its model or provider accepts". Kept apart
# from the quota wordings because the remedy differs: a quota waits for a
# reset, a size limit is cured by shrinking the session, and waiting never
# helps. Only wording actually seen is listed -- a guess here would open a
# circuit on a healthy agent.
CONTEXT_LIMIT_WORDING = [
    # OpenRouter, from a live refusal: "Prompt tokens limit exceeded: 334110 >
    # 185528. To increase, visit .../settings/credits and upgrade ...".
    r"prompt\s+tokens\s+limit\s+exceeded",
]
CONTEXT_LIMIT_PATTERN = re.compile("|".join(CONTEXT_LIMIT_WORDING), re.IGNORECASE)

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
        # "You've hit your session limit - resets 4:20pm". The live circuit
        # that stranded an agent only tripped because the same screen also
        # said "Usage limit reached"; this wording on its own was invisible.
        # "hit your" is required so the bare word "session" cannot match.
        r"hit your (?:session|usage|weekly|daily|5[ -]?hour) limit",
        *CONTEXT_LIMIT_WORDING,
        # Chinese -- the reference deployment's proxy reports in Chinese
        r"额度不足", r"余额不足", r"余额不够", r"预扣费额度失败", r"欠费",
        r"配额(?:不足|已?用尽|超限|耗尽)",
        r"额度(?:已?用尽|超限|耗尽)",
    ]),
    re.IGNORECASE,
)


def provider_failure_kind(detail):
    """Which kind of provider refusal `detail` describes.

    Derived from the stored text rather than kept in its own column, so a
    circuit written before this existed classifies the same way.
    """
    if detail and CONTEXT_LIMIT_PATTERN.search(detail):
        return "context_limit"

    return "quota"


# How long a queued task may wait for an agent that is not among herdr's
# running agents before the bridge gives up on it. herdr drops an agent's name
# whenever its process restarts, so a task queued for a name can end up waiting
# for something that no longer exists -- two did, for three weeks. Not
# immediately, because an agent that is restarting is missing from the list
# for a moment, and failing then would lose work that was about to be possible.
STALE_TARGET_SEC = int(os.environ.get("SENTINEL_STALE_TARGET_SEC", "600"))

TASK_STATUSES = (
    "queued", "running", "done", "error", "orphaned", "quota_exhausted",
    "cancelled",
)

# The whole life of a task, in one place. Every status change goes through
# transition(), which refuses anything not listed here.
TASK_TRANSITIONS = {
    # claimed by the worker; abandoned unsent; cancelled before it started
    "queued": ("running", "error", "cancelled"),
    "running": ("queued", "done", "error", "orphaned", "quota_exhausted"),
    # Not final: a result that turns up late adopts them (adopt_late_result).
    "error": ("done",),
    "orphaned": ("done",),
    "done": (),
    "quota_exhausted": (),
    "cancelled": (),
}

# A task waiting on one of these can never be satisfied. `orphaned` is in the
# list although a late result could still make it `done`: the dependent has not
# been sent anywhere, so cancelling it is safe, and running it on an unknown
# outcome is not.
FAILED_DEPENDENCY_STATUSES = ("error", "orphaned", "quota_exhausted", "cancelled")

MAX_DEPENDENCIES = 20
WORKFLOW_ID_MAX_LENGTH = 100

# What a transition may write alongside the status. A whitelist because the
# names are spliced into the SQL.
_TRANSITION_COLUMNS = frozenset({
    "started_at", "finished_at", "result_text", "result_json", "error_text",
    "recovered_at",
})
TASKS_LIMIT_DEFAULT = 20
TASKS_LIMIT_MAX = 200

# herdr takes the prompt as ONE command-line argument, and Linux caps a single
# argument at 131072 bytes (MAX_ARG_STRLEN) -- measured on the host: 131000
# bytes is delivered, 131072 fails with OSError [Errno 7]. The default leaves
# room under that; the cap is on bytes, not characters, so a Chinese character
# counts three.
MAX_PROMPT_BYTES = int(os.environ.get("SENTINEL_MAX_PROMPT_BYTES", "120000"))

# The bridge listens on loopback, which on a shared login node every user can
# reach. A request body is capped well above the largest legitimate one (a
# prompt of MAX_PROMPT_BYTES plus its JSON), and a connection that does not
# finish its request is dropped, since each one holds a thread.
MAX_BODY_BYTES = int(os.environ.get("SENTINEL_MAX_BODY_BYTES", str(1024 * 1024)))
REQUEST_TIMEOUT_SEC = int(os.environ.get("SENTINEL_REQUEST_TIMEOUT_SEC", "30"))


class TaskTooLargeError(ValueError):
    reason = "task_too_large"


def validate_task_size(task, slurm_policy=None):
    # Measured on what is actually sent: the envelope, the result path and the
    # policy line travel in the same argument as the task.
    size = len(
        build_delegation_prompt(
            task, "00000000-0000-0000-0000-000000000000", slurm_policy
        ).encode("utf-8")
    )

    if size > MAX_PROMPT_BYTES:
        raise TaskTooLargeError(
            f"task is too large to deliver: with the delegation envelope the "
            f"prompt is {size} bytes (UTF-8; a Chinese character is 3) and the "
            f"limit is {MAX_PROMPT_BYTES}. herdr takes the prompt as one "
            "command-line argument, which Linux caps at 131072 bytes. Put the "
            "material in a file on the host and have the task refer to it by "
            "path."
        )

    return task


def invalid_request_payload(error):
    payload = {"ok": False, "error": f"invalid request: {error}"}

    reason = getattr(error, "reason", None)
    if reason:
        payload["reason"] = reason

    return payload


def validate_timeout_ms(timeout_ms):
    if not (TIMEOUT_MS_MIN <= timeout_ms <= TIMEOUT_MS_MAX):
        raise ValueError(
            f"timeout_ms must be between {TIMEOUT_MS_MIN} and "
            f"{TIMEOUT_MS_MAX}, got {timeout_ms}"
        )

    return timeout_ms


# Which part of an agent's terminal to read. herdr offers more, but these are
# the two a caller needs: the scrollback (what `lines` counts) and the screen.
READ_SOURCES = ("recent-unwrapped", "visible")
DEFAULT_READ_SOURCE = "recent-unwrapped"


def validate_read_source(source):
    if source not in READ_SOURCES:
        raise ValueError(
            f"source must be one of {', '.join(READ_SOURCES)}, got {source!r}"
        )

    return source


def validate_read_lines(read_lines):
    if not (READ_LINES_MIN <= read_lines <= READ_LINES_MAX):
        raise ValueError(
            f"lines must be between {READ_LINES_MIN} and "
            f"{READ_LINES_MAX}, got {read_lines}"
        )

    return read_lines


def validate_agent_name(agent_name):
    if agent_name is None:
        # "No preference" is a legitimate request, not a malformed one:
        # resolve_agent() turns it into whichever agent can take work.
        return None

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


class IdempotencyConflictError(RuntimeError):
    pass


def _validate_label(value, name, max_length):
    """A caller-chosen string: present, bounded, and free of control characters."""
    if value is None:
        return None

    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")

    value = value.strip()

    if not value:
        raise ValueError(f"{name} cannot be empty")

    if len(value) > max_length:
        raise ValueError(f"{name} cannot exceed {max_length} characters")

    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{name} cannot contain control characters")

    return value


def validate_idempotency_key(key):
    """A caller-chosen key naming one logical operation across its retries."""
    return _validate_label(key, "idempotency_key", 255)


def validate_workflow_id(workflow_id):
    """A caller-chosen label grouping the tasks of one piece of work."""
    return _validate_label(workflow_id, "workflow_id", WORKFLOW_ID_MAX_LENGTH)


def validate_depends_on(depends_on):
    """The task ids a new task waits on, de-duplicated, in the order given."""
    if depends_on is None:
        return ()

    if not isinstance(depends_on, list):
        raise ValueError("depends_on must be a list of task ids")

    for task_id in depends_on:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("depends_on must contain only non-empty task ids")

    unique = tuple(dict.fromkeys(t.strip() for t in depends_on))

    if len(unique) > MAX_DEPENDENCIES:
        raise ValueError(
            f"depends_on cannot name more than {MAX_DEPENDENCIES} tasks"
        )

    return unique


def request_fingerprint(task, agent, slurm_policy, timeout_ms, reroute="none",
                        workflow_id=None, depends_on=()):
    """What makes two requests 'the same' for idempotency purposes.

    Built from what the caller *asked for*, not what the bridge resolved it
    to: the agent as named (or left unnamed), and the policy as given. A
    retry has to match even if the live agent set changed in between, or a
    harmless retry would be refused as a different request.

    The options added since are part of it only when set, and as named keys so
    that one cannot pose as another, so a key stored before they existed still
    matches its own retries.
    """
    parts = [task, agent, slurm_policy, timeout_ms]

    extras = {}
    if reroute != "none":
        extras["reroute"] = reroute
    if workflow_id:
        extras["workflow_id"] = workflow_id
    if depends_on:
        extras["depends_on"] = sorted(depends_on)
    if extras:
        parts.append(extras)

    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def startup_security_problems():
    """Reasons the bridge should not start with these settings.

    Kept apart from auth_token_warning(), which only warns: these stop it.
    """
    problems = []
    try:
        validate_worker_concurrency(WORKER_CONCURRENCY)
    except ValueError as error:
        problems.append(str(error))
    if DEFAULT_SLURM_POLICY not in SLURM_POLICIES:
        problems.append(
            f"SENTINEL_SLURM_POLICY={DEFAULT_SLURM_POLICY!r} is invalid. "
            f"Must be one of: {', '.join(sorted(SLURM_POLICIES))}."
        )

    if not AUTH_TOKEN:
        if not ALLOW_NO_AUTH:
            problems.append(
                "SENTINEL_BRIDGE_TOKEN is not set, so anyone who can reach "
                f"http://{HOST}:{PORT} could have the agents run commands. Set it "
                "to a random secret (remote/bridge-set-token.sh makes one), or "
                "set SENTINEL_ALLOW_NO_AUTH=1 to run open knowingly."
            )
    elif len(AUTH_TOKEN) < MIN_TOKEN_LENGTH:
        # Saying open is fine is not saying a guessable secret is fine.
        problems.append(
            f"SENTINEL_BRIDGE_TOKEN is too short to resist guessing: use at "
            f"least {MIN_TOKEN_LENGTH} characters."
        )

    return problems


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# Files the bridge writes hold every task and result in the clear. On a shared
# login node a default umask of 022 leaves them readable by every other user,
# which makes the access token beside the point: they can read the data off the
# disk instead of asking for it. A deployment whose agents run as a different
# user, and so need to share files, sets SENTINEL_UMASK (octal) to say so.
POSIX = os.name == "posix"
PRIVATE_UMASK = int(os.environ.get("SENTINEL_UMASK", "077"), 8)


def _mode(path):
    """Permission bits of an existing path, or None."""
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def harden_permissions():
    """Make what the bridge writes private to its user; return warnings.

    New files and directories are covered by the umask. The database is the
    bridge's own file, so an existing one that is too open is tightened. A
    directory is only reported, with the command to run: the operator can point
    these anywhere, and tightening one that is shared with something else is
    how a security setting becomes an outage.
    """
    if not POSIX:
        return []

    os.umask(PRIVATE_UMASK)

    if PRIVATE_UMASK & 0o077 != 0o077:
        return []          # deliberately shared; not ours to second-guess

    warnings = []

    mode = _mode(DB_PATH)
    if mode is not None and mode & 0o077:
        try:
            os.chmod(DB_PATH, 0o600)
        except OSError as e:
            warnings.append(f"could not make {DB_PATH} private: {e}")

    for label, path in (
        ("the database directory", os.path.dirname(DB_PATH) or "."),
        ("SENTINEL_RESULT_DIR", RESULT_DIR),
    ):
        mode = _mode(path)
        if mode is not None and mode & 0o077:
            warnings.append(
                f"{label} {path} is accessible to other users (mode {mode:03o}); "
                f"tighten it with: chmod 700 {path}"
            )

    return warnings


_execution_clock = threading.local()


@contextlib.contextmanager
def execution_budget(timeout_ms):
    previous = getattr(_execution_clock, "deadline", None)
    deadline = time.monotonic() + timeout_ms / 1000
    _execution_clock.deadline = min(previous, deadline) if previous is not None else deadline
    try:
        yield
    finally:
        _execution_clock.deadline = previous


def remaining_seconds(maximum):
    deadline = getattr(_execution_clock, "deadline", None)
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Bridge execution deadline expired; the agent may still be executing")
    return min(maximum, remaining)


def remaining_ms(maximum):
    return max(1, int(remaining_seconds(maximum / 1000) * 1000))


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


class InstanceAlreadyRunningError(RuntimeError):
    pass


@contextlib.contextmanager
def bridge_instance():
    """Own this database for the entire process lifetime, including recovery.

    Keep the lock file after release: unlinking a locked inode lets another
    process create a different lock at the same path. Kernel locks disappear
    on process death, so no PID guessing or stale-file removal is needed.
    """
    lock_path = os.path.realpath(DB_PATH) + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise InstanceAlreadyRunningError("A bridge already owns this database") from error
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise InstanceAlreadyRunningError("A bridge already owns this database") from error
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
                result_json TEXT,
                error_text TEXT,

                slurm_policy TEXT,

                idempotency_key TEXT,
                request_fingerprint TEXT,

                recovered_at TEXT,

                reroute TEXT,
                rerouted_from TEXT,

                workflow_id TEXT
            )
        """)

        # Every status change, written in the transaction that makes it. `seq`
        # only ever grows, so a reader that remembers the last one it saw can
        # ask for exactly what came after.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                workflow_id TEXT,
                at TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_events_task "
            "ON task_events(task_id)"
        )
        # Events for one workflow, in order. Without it that query walks every
        # event after the cursor and throws away the other workflows' rows.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_events_workflow "
            "ON task_events(workflow_id, seq)"
        )

        # Who asked for what, and every refusal of the token. No token, query
        # string or request body is ever stored here.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                status INTEGER NOT NULL,
                client TEXT,
                task_id TEXT
            )
        """)

        # Which tasks a task waits on. A separate table because the scheduler
        # has to ask "is everything this waits on done?" in SQL.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_deps (
                task_id TEXT NOT NULL,
                depends_on TEXT NOT NULL,
                PRIMARY KEY (task_id, depends_on)
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

        if "recovered_at" not in existing_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN recovered_at TEXT")

        # NULL reads as "none": rows from before the option were never
        # re-pointed and still are not.
        if "reroute" not in existing_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN reroute TEXT")
            conn.execute("ALTER TABLE tasks ADD COLUMN rerouted_from TEXT")

        if "workflow_id" not in existing_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN workflow_id TEXT")

        if "executed_agent" not in existing_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN executed_agent TEXT")

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_workflow_id
            ON tasks(workflow_id) WHERE workflow_id IS NOT NULL
        """)

        # tasks is never pruned and each row carries its prompt and result, so
        # a query that reads every row only gets slower. Measured on 20,000
        # tasks: the worker's poll (every two seconds) and the queue-cap count
        # that /delegate makes while holding the write lock took 65 ms each, and
        # listing the newest twenty 360 ms. These make them lookups.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_status_created "
            "ON tasks(status, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at)"
        )

        # Rows from before structured results stay NULL here: they were
        # never claimed to be structured, so they read back as plain text.
        if "result_json" not in existing_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN result_json TEXT")

        if "idempotency_key" not in existing_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN idempotency_key TEXT")
            conn.execute("ALTER TABLE tasks ADD COLUMN request_fingerprint TEXT")

        # Partial, so the many tasks created without a key do not collide on
        # NULL. This is the backstop; the claim itself is made atomically
        # inside the enqueue transaction.
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency_key
            ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL
        """)

        if "slurm_policy" not in existing_cols:
            # Rows created before the gate existed ran with no policy at
            # all. Recording that honestly beats back-filling a default
            # they were never actually told about.
            conn.execute("ALTER TABLE tasks ADD COLUMN slurm_policy TEXT")

        if "agent" not in existing_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN agent TEXT")
            conn.execute(
                "UPDATE tasks SET agent = ? WHERE agent IS NULL",
                (DEFAULT_AGENT,),
            )

        # 如果 bridge 在一个任务执行期间崩溃，
        # 千万不要自动重新运行它，否则可能重复执行危险操作。
        _transition_where(conn, "orphaned", "running", {
            "finished_at": now_iso(),
            "error_text": (
                "Bridge restarted while this task was running. Sentinel may "
                "have partially or fully executed it."
            ),
        })


class DependencyError(RuntimeError):
    """A new task names a dependency that cannot be waited on.

    `reason` is `unknown_dependency` (no such task) or `dependency_failed`
    (it has already ended in a state it will not recover from).
    """

    def __init__(self, reason, task_ids):
        self.reason = reason
        self.task_ids = list(task_ids)
        super().__init__(
            "unknown task(s) named in depends_on: " + ", ".join(self.task_ids)
            if reason == "unknown_dependency" else
            "depends_on names task(s) that have already failed, so this "
            "could never run: " + ", ".join(self.task_ids)
        )


def _check_dependencies(conn, depends_on):
    """Refuse a dependency that does not exist or has already failed."""
    if not depends_on:
        return

    marks = ", ".join("?" for _ in depends_on)
    found = {
        row["task_id"]: row["status"] for row in conn.execute(
            f"SELECT task_id, status FROM tasks WHERE task_id IN ({marks})",
            list(depends_on),
        )
    }

    unknown = [t for t in depends_on if t not in found]
    if unknown:
        raise DependencyError("unknown_dependency", unknown)

    failed = [
        t for t in depends_on if found[t] in FAILED_DEPENDENCY_STATUSES
    ]
    if failed:
        raise DependencyError("dependency_failed", failed)


def list_dependencies(task_id):
    """The task ids this task waits on, in the order they were given."""
    with db_session() as conn:
        return [
            row["depends_on"] for row in conn.execute(
                "SELECT depends_on FROM task_deps WHERE task_id = ? "
                "ORDER BY rowid",
                (task_id,),
            )
        ]


def dependencies_of(task_ids):
    """{task_id: [what it waits on]} for several tasks, in one query."""
    found = {task_id: [] for task_id in task_ids}

    if not found:
        return found

    marks = ", ".join("?" for _ in found)
    with db_session() as conn:
        for row in conn.execute(
            f"SELECT task_id, depends_on FROM task_deps "
            f"WHERE task_id IN ({marks}) ORDER BY rowid",
            list(found),
        ):
            found[row["task_id"]].append(row["depends_on"])

    return found


def unmet_dependencies(task_id):
    """(task_id, status) of everything this task waits on that is not done."""
    with db_session() as conn:
        return [
            (row["task_id"], row["status"]) for row in conn.execute("""
                SELECT u.task_id AS task_id, u.status AS status
                FROM task_deps d JOIN tasks u ON u.task_id = d.depends_on
                WHERE d.task_id = ? AND u.status != 'done'
                ORDER BY d.rowid
            """, (task_id,))
        ]


def list_workflow_tasks(workflow_id):
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE workflow_id = ? "
            "ORDER BY created_at ASC, rowid ASC",
            (workflow_id,),
        ).fetchall()

    return [dict(row) for row in rows]


def workflow_state(counts):
    """Where a workflow stands, from its tasks' statuses.

    `in_progress` while anything can still move, then `done` only if every
    task is, and otherwise `failed`: it stopped short of done.
    """
    if counts.get("queued") or counts.get("running"):
        return "in_progress"

    if set(counts) == {"done"}:
        return "done"

    # Stopped on purpose, nothing having gone wrong.
    if set(counts) == {"cancelled"}:
        return "cancelled"

    return "failed"


class QueueFullError(RuntimeError):
    def __init__(self, queued, maximum):
        self.queued = queued
        self.maximum = maximum
        super().__init__(
            f"queue full: {queued}/{maximum} tasks already queued"
        )


def _insert_task(conn, task_id, task, timeout_ms, agent_name, slurm_policy=None,
                 idempotency_key=None, fingerprint=None, reroute=None,
                 workflow_id=None):
    conn.execute("""
        INSERT INTO tasks (
            task_id, task, agent, status, created_at, timeout_ms, slurm_policy,
            idempotency_key, request_fingerprint, reroute, workflow_id
        )
        VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
    """, (
        task_id, task, agent_name, now_iso(), timeout_ms,
        validate_slurm_policy(slurm_policy),
        idempotency_key, fingerprint, validate_reroute(reroute), workflow_id,
    ))

    _record_event(conn, task_id, workflow_id, None, "queued")


# What the agent itself says about the work, as opposed to `status`, which says
# whether the bridge delivered the result. A task can be `done` here and
# `failed` there: one `done` must never stand in for both.
RESULT_OUTCOMES = ("success", "partial", "failed", "blocked")
_RESULT_LIST_FIELDS = ("job_ids", "artifacts", "warnings", "changed_files")

# Agents wrap JSON in a markdown fence far more often than not.
_FENCED_BLOCK = re.compile(r"^```[A-Za-z]*[ \t]*\n(.*?)\n?```$", re.DOTALL)


def parse_structured_result(text):
    """Read an agent's result as a JSON object, if that is what it wrote.

    Returns (data, problem). Prose is not a problem: (None, None). Something
    that tried to be an object and was not -- cut off, a field of the wrong
    type -- is (None, why), so the caller can say so; it is never an error,
    because the work was delivered and only the format slipped.
    """
    body = (text or "").strip()

    fenced = _FENCED_BLOCK.match(body)
    if fenced:
        body = fenced.group(1).strip()

    if not body.startswith("{"):
        return None, None

    try:
        raw = json.loads(body)
    except ValueError as e:
        return None, f"could not parse as JSON ({e})"

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None, "'summary' is required and must be a non-empty string"

    status = raw.get("status")
    if status is not None and status not in RESULT_OUTCOMES:
        return None, (
            f"'status' must be one of {', '.join(RESULT_OUTCOMES)}, "
            f"got {status!r}"
        )

    data = {"summary": summary, "status": status}

    for field in _RESULT_LIST_FIELDS:
        value = raw.get(field, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return None, f"'{field}' must be a list of strings"
        data[field] = value

    metrics = raw.get("metrics", {})
    if not isinstance(metrics, dict) or not all(
        isinstance(v, (str, int, float)) for v in metrics.values()
    ):
        return None, "'metrics' must be an object of numbers and strings"

    # json.loads reads NaN and Infinity, and the API would write them back out
    # as bare tokens that are not JSON, which a strict client rejects along
    # with everything else in the reply.
    if any(isinstance(v, float) and not math.isfinite(v) for v in metrics.values()):
        return None, "'metrics' values must be finite numbers"
    data["metrics"] = metrics

    return data, None


def _result_columns(result_text):
    """The columns that record a result, plus a note if it fell short.

    The text is always kept as delivered; the structured form is read out of
    it, never put in its place.
    """
    data, problem = parse_structured_result(result_text)

    columns = {
        "result_text": result_text,
        "result_json": json.dumps(data, ensure_ascii=False) if data else None,
    }
    note = (
        f"Result kept as plain text, not read as structured JSON: {problem}."
        if problem else None
    )

    return columns, note


def task_view(row):
    """A task row as the API serves it.

    `result_json` is storage; callers get `result_data` and `result_format`
    instead. A row without it is text, whatever its text looks like: it was
    never claimed to be anything else.
    """
    view = dict(row)
    raw = view.pop("result_json", None)

    view["result_data"] = json.loads(raw) if raw else None

    if raw:
        view["result_format"] = "json"
    elif view.get("result_text") is not None:
        view["result_format"] = "text"
    else:
        view["result_format"] = None

    return view


# Task states in which a delivered result can still turn up. A queued or
# running task is on the normal path -- the worker is about to read its file --
# and quota_exhausted never reached an agent that could have written one.
LATE_RESULT_STATUSES = ("error", "orphaned")


def adopt_late_result(task_id, result_text=None, note=None):
    """Take a result that arrived after the bridge had stopped waiting.

    A caller that already read the answer can supply it directly, including
    when database retries have outlived the original recovery file.

    Measured on the live host: 189 result files sitting uncollected, of which
    76 belonged to tasks marked error and 2 to orphaned ones. Every one was a
    result the agent delivered and the bridge discarded, because nothing ever
    looked at a result file once a task was no longer being waited on.

    The task becomes done, with the original error text kept and a note added
    rather than the history rewritten: what went wrong is still true, it
    just was not the end of the story. The UPDATE is conditional on the task
    still being failed, so concurrent callers adopt it once.
    """
    task = get_task(task_id)

    if task is None or task["status"] not in LATE_RESULT_STATUSES:
        return None

    # Left in place until it is safely in the database: deleting first would
    # lose the answer if the write failed.
    content = result_text if result_text is not None else read_result_file(task_id, cleanup=False)

    if content is None:
        return None

    recovery_note = (
        "Late delivery: the result arrived after the bridge had stopped "
        f"waiting, and was recovered at {now_iso()}."
    )

    columns, format_note = _result_columns(content)

    adopted = transition(
        task_id, "done", LATE_RESULT_STATUSES,
        recovered_at=now_iso(),
        error_text="\n".join(
            n for n in (task["error_text"], recovery_note, note, format_note) if n
        ),
        **columns,
    )

    if not adopted:
        return None   # another caller got there first

    try:
        os.remove(result_file_path(task_id))
    except OSError:
        pass

    discard_progress_file(task_id)
    return get_task(task_id)


def adopt_all_late_results():
    """Sweep the backlog, at startup. Returns how many were adopted."""
    with db_session() as conn:
        ids = [
            row["task_id"] for row in conn.execute(
                "SELECT task_id FROM tasks WHERE status IN ('error', 'orphaned')"
            )
        ]

    return sum(
        1 for task_id in ids
        if os.path.exists(result_file_path(task_id)) and adopt_late_result(task_id)
    )


def record_sync_failure(task_id, task, timeout_ms, agent_name, slurm_policy,
                        status, error_text, _defer=True):
    """Settle a failed synchronous ask, retaining its original execution target.

    Also accepts failures predating begin_sync_attempt (for example an
    interruption in the dispatch wrapper). A database error must not mask the
    original execution error.
    """
    try:
        now = now_iso()
        with db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (task_id,),
            ).fetchone()
            if existing:
                return bool(_transition_where(conn, status, "running", {
                    "finished_at": now, "error_text": error_text,
                }, task_id=task_id))
            conn.execute("""
                INSERT INTO tasks (
                    task_id, task, agent, status, created_at, started_at,
                    finished_at, timeout_ms, error_text, slurm_policy
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task_id, task, agent_name, status, now, now, now, timeout_ms,
                error_text, validate_slurm_policy(slurm_policy),
            ))
            _record_event(conn, task_id, None, None, status)
        return True
    except Exception as e:
        print(f"[ask {task_id}] could not record the failure: {e}")
        if _defer:
            with _pending_sync_guard:
                _pending_sync_failures[(os.path.realpath(DB_PATH), task_id)] = (
                    task_id, task, timeout_ms, agent_name, slurm_policy, status, error_text,
                )
            _work_available.set()
            return True  # recovery remains queryable; only settlement will be retried
        return False


SYNC_RECOVERY_HINT = (
    "The agent may still finish. If it delivers a result after this response "
    "it is recovered automatically: query this task_id later "
    "(GET /tasks/<id>, or `sentinel.ps1 task <id>`)."
)


def begin_sync_attempt(task_id, task, timeout_ms, requested_agent, target, slurm_policy):
    """Persist ownership before a synchronous prompt can reach an agent."""
    with _shutdown_guard:
        if _shutting_down:
            raise SentinelUnavailableError("Bridge is shutting down")
        with db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (task_id,),
            ).fetchone()
            if row is None:
                _insert_task(conn, task_id, task, timeout_ms, requested_agent, slurm_policy)
                _transition_where(conn, "running", "queued", {
                    "started_at": now_iso(),
                }, task_id=task_id)
            elif row["status"] != "running":
                raise SentinelUnavailableError("Task is no longer running; it was not resent")
            conn.execute("UPDATE tasks SET executed_agent = ? WHERE task_id = ?", (target, task_id))


def find_task_by_idempotency_key(key):
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE idempotency_key = ?", (key,)
        ).fetchone()

    return dict(row) if row else None


def check_replay(existing, fingerprint):
    """Return the original task for a matching retry, or refuse a mismatch.

    Returning the first task for a request that differs would hand the
    caller a result for something it did not ask for, so reuse of a key with
    changed parameters is refused loudly: it is a bug on the caller's side.
    """
    if fingerprint is not None and existing["request_fingerprint"] != fingerprint:
        raise IdempotencyConflictError(
            "this idempotency key was already used for a different request "
            "(task, agent, slurm_policy or timeout differ). Use a new key "
            "for a new operation."
        )

    return existing


def create_task(task, timeout_ms, agent_name, slurm_policy=None, reroute=None):
    task_id = str(uuid.uuid4())

    with db_session() as conn:
        _insert_task(
            conn, task_id, task, timeout_ms, agent_name, slurm_policy,
            reroute=reroute,
        )

    return task_id


def create_task_if_queue_available(task, timeout_ms, agent_name, slurm_policy=None,
                                  idempotency_key=None, fingerprint=None):
    return enqueue_task(
        task, timeout_ms, agent_name, slurm_policy, idempotency_key, fingerprint
    )[0]


def enqueue_task(task, timeout_ms, agent_name, slurm_policy=None,
                 idempotency_key=None, fingerprint=None, reroute=None,
                 workflow_id=None, depends_on=()):
    """Atomically enforce the queue cap and enqueue one task.

    Returns (task_id, replayed).

    ThreadingHTTPServer can process several /delegate requests concurrently.
    BEGIN IMMEDIATE serializes the count-and-insert section so two callers
    cannot both observe the same free queue slot and overfill the queue --
    and, for the same reason, cannot both find a key unclaimed and each
    insert a task for it. The key is checked inside this transaction rather
    than before it, which is the whole point: check-then-insert is the race
    that makes an idempotency key worthless under concurrent retries.
    """
    task_id = str(uuid.uuid4())

    with db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")

        # Before the queue cap: a replay creates nothing, so a full queue
        # has no business refusing it.
        if idempotency_key:
            row = conn.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row:
                return check_replay(dict(row), fingerprint)["task_id"], True

        # A mistake in the request, so it is reported ahead of a full queue.
        _check_dependencies(conn, depends_on)

        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status = 'queued'"
        ).fetchone()
        queued = row["n"]

        if queued >= MAX_QUEUE_DEPTH:
            raise QueueFullError(queued, MAX_QUEUE_DEPTH)

        _insert_task(
            conn, task_id, task, timeout_ms, agent_name, slurm_policy,
            idempotency_key, fingerprint, reroute, workflow_id,
        )

        conn.executemany(
            "INSERT INTO task_deps (task_id, depends_on) VALUES (?, ?)",
            [(task_id, dependency) for dependency in depends_on],
        )

    # After the commit, so what the worker wakes up to look for is there.
    _work_available.set()
    return task_id, False


def get_task(task_id):
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()

    return dict(row) if row else None


def list_tasks(limit=TASKS_LIMIT_DEFAULT, status=None):
    where = "WHERE status = ?" if status else ""
    params = (status, limit) if status else (limit,)

    with db_session() as conn:
        rows = conn.execute(
            f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()

    return [dict(row) for row in rows]


def validate_tasks_query(query):
    """(limit, status) from a parsed query string; ValueError if either is bad."""
    limit_values = query.get("limit")
    status_values = query.get("status")

    limit = TASKS_LIMIT_DEFAULT
    if limit_values:
        limit = int(limit_values[0])
        if not (1 <= limit <= TASKS_LIMIT_MAX):
            raise ValueError(
                f"limit must be between 1 and {TASKS_LIMIT_MAX}, got {limit}"
            )

    status = None
    if status_values:
        status = status_values[0]
        if status not in TASK_STATUSES:
            raise ValueError(
                f"status must be one of {', '.join(TASK_STATUSES)}, got {status!r}"
            )

    return limit, status


def _transition_where(conn, to, expect, columns, task_id=None):
    """Move every task in the `expect` state(s) to `to`; return how many moved.

    The one place that writes a status. `expect` makes it a compare-and-set:
    a task that is no longer in that state is left alone, which is how two
    paths racing to finish the same task cannot both win. A move the table
    does not allow is a bug in the caller rather than a lost race, and raises.
    """
    sources = (expect,) if isinstance(expect, str) else tuple(expect)

    for source in sources:
        if to not in TASK_TRANSITIONS.get(source, ()):
            raise ValueError(f"illegal task transition: {source} -> {to}")

    unknown = set(columns) - _TRANSITION_COLUMNS
    if unknown:
        raise ValueError(
            f"a transition cannot set: {', '.join(sorted(unknown))}"
        )

    assignments = ", ".join(f"{name} = ?" for name in ("status", *columns))
    where = f"status IN ({', '.join('?' for _ in sources)})"
    match = list(sources)

    if task_id is not None:
        where += " AND task_id = ?"
        match.append(task_id)

    # Take the write lock before looking, so the rows seen are the rows moved
    # and each event can name the status it really replaced.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")

    moving = conn.execute(
        f"SELECT task_id, workflow_id, status FROM tasks WHERE {where}", match
    ).fetchall()

    if not moving:
        return 0

    conn.execute(
        f"UPDATE tasks SET {assignments} WHERE {where}",
        [to, *columns.values(), *match],
    )

    for row in moving:
        _record_event(conn, row["task_id"], row["workflow_id"], row["status"], to)

    return len(moving)


def _record_event(conn, task_id, workflow_id, from_status, to_status):
    conn.execute(
        "INSERT INTO task_events "
        "(task_id, workflow_id, at, from_status, to_status) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, workflow_id, now_iso(), from_status, to_status),
    )


EVENTS_LIMIT_DEFAULT = 100
EVENTS_LIMIT_MAX = 500
EVENTS_WAIT_MAX_SEC = 30
EVENTS_POLL_SEC = 0.25
EVENTS_CURSOR_MAX = 2**63 - 1


def list_events(after=0, limit=EVENTS_LIMIT_DEFAULT, task_id=None,
                workflow_id=None):
    """Events with a `seq` greater than `after`, oldest first."""
    clauses, params = ["seq > ?"], [after]

    if task_id is not None:
        clauses.append("task_id = ?")
        params.append(task_id)

    if workflow_id is not None:
        clauses.append("workflow_id = ?")
        params.append(workflow_id)

    with db_session() as conn:
        rows = conn.execute(
            f"SELECT * FROM task_events WHERE {' AND '.join(clauses)} "
            "ORDER BY seq ASC LIMIT ?",
            [*params, limit],
        ).fetchall()

    return [dict(row) for row in rows]


AUDIT_RETAIN_ROWS = int(os.environ.get("SENTINEL_AUDIT_RETAIN_ROWS", "50000"))


def clean_client_label(value):
    """A client's self-declared name, made safe to store, or None.

    It is only a label the caller chose: with one shared token nothing proves it.
    """
    if not value:
        return None

    cleaned = "".join(c for c in value if c.isascii() and c.isprintable())[:64].strip()
    return cleaned or None


def record_audit(method, path, status, client=None, task_id=None):
    """Append one row, and drop the oldest beyond AUDIT_RETAIN_ROWS."""
    with db_session() as conn:
        conn.execute(
            "INSERT INTO audit_log (at, method, path, status, client, task_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now_iso(), method, path, status, client, task_id),
        )
        conn.execute(
            "DELETE FROM audit_log WHERE seq <= "
            "(SELECT MAX(seq) FROM audit_log) - ?",
            (AUDIT_RETAIN_ROWS,),
        )


# tasks is never pruned unless the operator asks: each row carries its prompt and
# result, so the file only grows, but deleting history is theirs to decide.
# 0 keeps everything, as before.
TASK_RETENTION_DAYS = max(0, int(os.environ.get("SENTINEL_TASK_RETENTION_DAYS", "0")))
PRUNE_INTERVAL_SEC = 24 * 3600
PRUNE_BATCH = 500
# Between batches, outside any transaction. Without it the next batch takes the
# write lock again the instant the last released it, and a writer that backed off
# to wait (a /delegate) can be kept out for the whole prune: measured at 1.7 s.
PRUNE_PAUSE_SEC = 0.05
_FINAL_STATUSES = ("done", "error", "orphaned", "quota_exhausted", "cancelled")


def prune_old_tasks():
    """Delete finished tasks older than SENTINEL_TASK_RETENTION_DAYS. Returns how many.

    Only a task that is in a final state and finished long enough ago, and that
    no task still waiting depends on: gone, it would read as satisfied to the
    join that decides whether a dependent may start. Its events and dependency
    rows go with it. The audit trail has its own limit and is not touched.

    A stamp that cannot be read is left alone. The age is read as a time, not
    compared as text, because stamps carry different UTC offsets.
    """
    if TASK_RETENTION_DAYS <= 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=TASK_RETENTION_DAYS)
    # A text comparison picks the candidates, with a day's margin for the offsets
    # stamps are written with; the parsed time below is what decides.
    margin = (cutoff + timedelta(days=1)).isoformat()
    final = ", ".join("?" for _ in _FINAL_STATUSES)

    pruned, last_rowid = 0, 0

    while True:
        with db_session() as conn:
            # Serialised with enqueue_task, so a task cannot be queued against
            # one of these between the check and the delete.
            conn.execute("BEGIN IMMEDIATE")

            rows = conn.execute(f"""
                SELECT rowid AS rid, task_id, finished_at FROM tasks
                WHERE rowid > ? AND status IN ({final}) AND created_at < ?
                AND NOT EXISTS (
                    SELECT 1 FROM task_deps d
                    JOIN tasks waiting ON waiting.task_id = d.task_id
                    WHERE d.depends_on = tasks.task_id
                    AND waiting.status NOT IN ({final})
                )
                ORDER BY rowid LIMIT ?
            """, [last_rowid, *_FINAL_STATUSES, margin, *_FINAL_STATUSES, PRUNE_BATCH]).fetchall()

            if not rows:
                return pruned

            last_rowid = rows[-1]["rid"]
            old = []
            for row in rows:
                age = _age_seconds(row["finished_at"])
                if age is not None and age > TASK_RETENTION_DAYS * 86400:
                    old.append(row["task_id"])

            if old:
                marks = ", ".join("?" for _ in old)
                conn.execute(f"DELETE FROM task_events WHERE task_id IN ({marks})", old)
                conn.execute(f"DELETE FROM task_deps WHERE task_id IN ({marks})", old)
                conn.execute(f"DELETE FROM task_deps WHERE depends_on IN ({marks})", old)
                conn.execute(f"DELETE FROM tasks WHERE task_id IN ({marks})", old)
                pruned += len(old)

        if len(rows) < PRUNE_BATCH:
            return pruned

        time.sleep(PRUNE_PAUSE_SEC)


def list_audit(after=0, limit=EVENTS_LIMIT_DEFAULT):
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE seq > ? ORDER BY seq ASC LIMIT ?",
            (after, limit),
        ).fetchall()

    return [dict(row) for row in rows]


def validate_events_query(query):
    """(after, limit, wait, task_id, workflow_id) from a parsed query string."""
    def first(name):
        values = query.get(name)
        return values[0] if values else None

    after = int(first("after") or 0)
    # The upper bound is SQLite's largest integer; beyond it the query raises
    # rather than finds nothing.
    if not (0 <= after <= EVENTS_CURSOR_MAX):
        raise ValueError(
            f"after must be between 0 and {EVENTS_CURSOR_MAX}, got {after}"
        )

    limit = EVENTS_LIMIT_DEFAULT
    if first("limit") is not None:
        limit = int(first("limit"))
        if not (1 <= limit <= EVENTS_LIMIT_MAX):
            raise ValueError(
                f"limit must be between 1 and {EVENTS_LIMIT_MAX}, got {limit}"
            )

    wait = 0.0
    if first("wait") is not None:
        wait = float(first("wait"))
        if not (0 <= wait <= EVENTS_WAIT_MAX_SEC):
            raise ValueError(
                f"wait must be between 0 and {EVENTS_WAIT_MAX_SEC} seconds, "
                f"got {wait:g}"
            )

    return after, limit, wait, first("task_id"), first("workflow_id")


def transition(task_id, to, expect, **columns):
    """Atomically move one task from `expect` to `to`. True if it moved."""
    with db_session() as conn:
        return _transition_where(conn, to, expect, columns, task_id) == 1


def abandon_task(task_id, error_text):
    """Fail a task that is still queued. Never touches one that has started."""
    return transition(
        task_id, "error", "queued",
        error_text=error_text, finished_at=now_iso(),
    )


def cancel_task(task_id, reason="Cancelled before it started."):
    """Cancel a task that is still queued. True if it was cancelled.

    Nothing that has been sent to an agent can be called back: the bridge has
    no way to stop one mid-task, so a started task is left alone.
    """
    return transition(
        task_id, "cancelled", "queued",
        error_text=f"{reason} It was never sent to any agent, so "
                   "resubmitting is safe.",
        finished_at=now_iso(),
    )


def cancel_blocked_tasks():
    """Cancel queued tasks that wait on something that has failed.

    Repeats until nothing more moves, so a failure reaches every descendant.
    Returns how many were cancelled.
    """
    cancelled = 0
    marks = ", ".join("?" for _ in FAILED_DEPENDENCY_STATUSES)

    while True:
        with db_session() as conn:
            # Most deployments never declare a dependency, and this runs on
            # every worker pass: don't scan the tasks table for work that
            # cannot exist.
            if conn.execute("SELECT 1 FROM task_deps LIMIT 1").fetchone() is None:
                return cancelled

            recoverable = conn.execute("""
                SELECT DISTINCT u.task_id FROM tasks u
                JOIN task_deps d ON d.depends_on = u.task_id
                JOIN tasks t ON t.task_id = d.task_id
                WHERE t.status = 'queued' AND u.status IN ('error', 'orphaned')
            """).fetchall()

        # Recovery uses its own transaction. Do it before deciding which
        # descendants to cancel, then re-read under the write lock so a
        # concurrent adoption cannot be followed by a stale cancellation.
        for row in recoverable:
            adopt_late_result(row["task_id"])

        with db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")

            blocked = conn.execute(f"""
                SELECT t.task_id AS task_id, u.task_id AS upstream,
                       u.status AS upstream_status
                FROM tasks t
                JOIN task_deps d ON d.task_id = t.task_id
                JOIN tasks u ON u.task_id = d.depends_on
                WHERE t.status = 'queued' AND u.status IN ({marks})
                GROUP BY t.task_id
            """, FAILED_DEPENDENCY_STATUSES).fetchall()

            moved = sum(
                _transition_where(
                    conn, "cancelled", "queued",
                    {
                        "error_text": (
                            f"Cancelled before it started: it waits on "
                            f"{row['upstream']}, which ended "
                            f"{row['upstream_status']}. It was never sent to "
                            "any agent, so resubmitting is safe."
                        ),
                        "finished_at": now_iso(),
                    },
                    task_id=row["task_id"],
                )
                for row in blocked
            )

        if not moved:
            return cancelled

        cancelled += moved


def reroute_task(task_id, old_agent, new_agent):
    """Point a queued task at a different agent. True if it moved.

    Only while it is still queued: a task that has started was sent to its
    agent, and is no longer the bridge's to redirect. The agent it was meant
    for is kept in `rerouted_from`. Not a status change, so it stays off the
    transition table.
    """
    with db_session() as conn:
        cursor = conn.execute("""
            UPDATE tasks SET agent = ?, rerouted_from = ?
            WHERE task_id = ? AND status = 'queued' AND agent = ?
        """, (new_agent, old_agent, task_id, old_agent))

        return cursor.rowcount == 1


def _age_seconds(iso_stamp):
    try:
        stamp = datetime.fromisoformat(iso_stamp)
    except (TypeError, ValueError):
        return None

    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)

    return (datetime.now(timezone.utc) - stamp).total_seconds()


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

    # A task is eligible only once everything it waits on is done. One that
    # is not simply is not a candidate, so it never stands in front of a task
    # that can run.
    with db_session() as conn:
        row = conn.execute(f"""
            SELECT * FROM tasks
            WHERE status = 'queued' {where_agent}
            AND NOT EXISTS (
                SELECT 1 FROM task_deps d
                JOIN tasks upstream ON upstream.task_id = d.depends_on
                WHERE d.task_id = tasks.task_id AND upstream.status != 'done'
            )
            ORDER BY created_at ASC
            LIMIT 1
        """, exclude_agents).fetchone()

    return dict(row) if row else None


def claim_task(task_id):
    with _shutdown_guard:
        if _shutting_down:
            return False
        return transition(task_id, "running", "queued", started_at=now_iso())


def requeue_task(task_id):
    """Return a claimed task to the queue when every fallback is busy."""
    return transition(
        task_id, "queued", "running", started_at=None, finished_at=None
    )


def complete_task(task_id, result_text, note=None):
    """Finish a running task. False if it was no longer running.

    A task that is not running has already been settled some other way -- a
    shutdown orphaned it, say, while the worker was still waiting on the agent.
    Completing it anyway would overwrite that, and deleting the result file
    would lose the only copy: it stays where adopt_late_result will find it.
    """
    columns, format_note = _result_columns(result_text)
    columns["finished_at"] = now_iso()

    # Notes ride in error_text, as a late delivery's does: a finished task
    # that still has something the reader should know.
    notes = "\n".join(n for n in (note, format_note) if n)
    if notes:
        columns["error_text"] = notes

    if not transition(task_id, "done", "running", **columns):
        return False

    # The file is the recovery copy until the transaction commits.
    discard_result_file(task_id)
    discard_progress_file(task_id)
    return True


def routing_note(requested, used):
    """Say so when a task ran on a different agent than the one it asked for.

    The row records the agent that was requested, so without this a result
    from the fallback reads as the first agent's work.
    """
    if used == requested:
        return None

    try:
        block = get_agent_quota_block(requested)
    except (sqlite3.Error, OSError):
        # Optional diagnostic enrichment must not replace a delivered result
        # with an error, or prevent its persistence from being retried.
        block = None
    why = ""
    if block:
        why = f" ({block['kind'].replace('_', ' ')}: {block['detail']})"

    return (
        f"Ran on {used}, not the requested {requested}, "
        f"whose provider refused it{why}."
    )


def settle_completed_task(task_id, result, note, used_agent):
    if complete_task(task_id, result, note=note):
        print(f"[task {task_id}] done (agent={used_agent})")
    else:
        print(f"[task {task_id}] finished on {used_agent} but is no longer running; "
              "its result file is left for late adoption")


_pending_sync_completions = {}
_pending_sync_failures = {}
_pending_sync_guard = threading.Lock()


def defer_sync_completion(task_id, result, note):
    with _pending_sync_guard:
        _pending_sync_completions[(os.path.realpath(DB_PATH), task_id)] = (result, note)
    _work_available.set()


def persist_sync_completion(task_id, result, note):
    """Commit a known answer even when shutdown won the running-state race."""
    if complete_task(task_id, result, note=note):
        return True
    # The answer has already been read. A recovery must not require the
    # original file to remain accessible while its database writes retry.
    if adopt_late_result(task_id, result_text=result, note=note):
        return True
    row = get_task(task_id)
    return bool(row and row["status"] == "done")


def retry_sync_completions(task_id=None):
    """Retry synchronous result/failure persistence, never task execution."""
    database = os.path.realpath(DB_PATH)
    with _pending_sync_guard:
        failures = [(key, value) for key, value in _pending_sync_failures.items()
                    if key[0] == database and (task_id is None or key[1] == task_id)]
    for key, args in failures:
        if not record_sync_failure(*args, _defer=False):
            row = get_task(key[1])
            if not row or row["status"] == "running":
                continue
        with _pending_sync_guard:
            if _pending_sync_failures.get(key) == args:
                _pending_sync_failures.pop(key, None)

    with _pending_sync_guard:
        pending = [(key, value) for key, value in _pending_sync_completions.items()
                   if key[0] == database and (task_id is None or key[1] == task_id)]
    for key, (result, note) in pending:
        try:
            if not persist_sync_completion(key[1], result, note):
                continue
        except (sqlite3.Error, OSError) as error:
            print(f"[ask {key[1]}] completion still pending: {error}")
            continue
        with _pending_sync_guard:
            _pending_sync_completions.pop(key, None)


def fail_task(task_id, error_text):
    discard_progress_file(task_id)

    # Only a running task can fail. A shutdown that has already marked it
    # orphaned -- "may have run; do not retry" -- must not be overwritten by
    # the worker's own report of the same event: `error` says retrying is
    # safe, and which of the two arrived last was a race.
    return transition(
        task_id, "error", "running",
        error_text=error_text, finished_at=now_iso(),
    )


def orphan_task(task_id, error_text):
    discard_progress_file(task_id)

    return transition(
        task_id, "orphaned", "running",
        error_text=error_text, finished_at=now_iso(),
    )


def quota_exhausted_task(task_id, error_text):
    return transition(
        task_id, "quota_exhausted", "running",
        error_text=error_text, finished_at=now_iso(),
    )


def mark_agent_quota_blocked(agent_name, detail):
    agent_name = live_agent_aliases(agent_name)[0]
    with db_session() as conn:
        conn.execute("""
            INSERT INTO quota_blocks (agent, detected_at, detail)
            VALUES (?, ?, ?)
            ON CONFLICT(agent) DO UPDATE SET
                detected_at = excluded.detected_at,
                detail = excluded.detail
        """, (agent_name, now_iso(), detail))


def _quota_block_ttl(block):
    if provider_failure_kind(block.get("detail")) == "context_limit":
        return CONTEXT_BLOCK_TTL_SECONDS

    return QUOTA_BLOCK_TTL_SECONDS


def _quota_block_expired(block):
    ttl = _quota_block_ttl(block)

    if ttl <= 0:
        return False

    try:
        detected = datetime.fromisoformat(block["detected_at"])
    except (TypeError, ValueError):
        # An unreadable timestamp cannot be aged, and a circuit that can
        # never expire is the failure this TTL exists to prevent. Treat it
        # as expired so the agent comes back into service.
        return True

    if detected.tzinfo is None:
        # fromisoformat() accepts a stamp with no offset, but subtracting
        # one from an aware datetime raises TypeError -- which used to
        # escape this function entirely, taking /ready, the worker's
        # dispatch and quota failover down with it. now_iso() writes UTC,
        # so reading a bare stamp as UTC ages it correctly instead of
        # crashing or discarding a circuit that is still valid.
        detected = detected.replace(tzinfo=timezone.utc)

    age = (datetime.now(timezone.utc) - detected).total_seconds()
    return age >= ttl


def get_agent_quota_block(agent_name):
    aliases = live_agent_aliases(agent_name)
    placeholders = ", ".join("?" for _ in aliases)
    with db_session() as conn:
        rows = conn.execute(
            f"SELECT * FROM quota_blocks WHERE agent IN ({placeholders}) "
            "ORDER BY detected_at DESC", aliases,
        ).fetchall()
        active = None
        for row in rows:
            block = dict(row)
            if _quota_block_expired(block):
                # A concurrent refusal may have renewed this row after SELECT.
                # Delete only the exact snapshot whose expiration we checked.
                conn.execute(
                    "DELETE FROM quota_blocks WHERE agent = ? AND detected_at = ? AND detail = ?",
                    (block["agent"], block["detected_at"], block["detail"]),
                )
            elif active is None:
                block["kind"] = provider_failure_kind(block.get("detail"))
                active = block
    return active


def list_agent_quota_blocks():
    try:
        with db_session() as conn:
            rows = conn.execute(
                "SELECT * FROM quota_blocks ORDER BY detected_at DESC"
            ).fetchall()
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return []

    live = []
    for row in rows:
        block = dict(row)
        if _quota_block_expired(block):
            # Expiring a snapshot must not clear a concurrently renewed row,
            # or a different active alias.
            try:
                with db_session() as conn:
                    conn.execute(
                        "DELETE FROM quota_blocks WHERE agent = ? AND detected_at = ? AND detail = ?",
                        (block["agent"], block["detected_at"], block["detail"]),
                    )
            except (sqlite3.OperationalError, sqlite3.DatabaseError):
                pass
            continue
        block["kind"] = provider_failure_kind(block.get("detail"))
        live.append(block)

    return live


def clear_agent_quota_blocks(agent_name=None):
    aliases = None
    if agent_name is not None:
        try:
            agent_name = resolve_agent(agent_name)
        except AgentNotFoundError:
            # An offline concrete identifier can still own a persisted circuit.
            pass
        aliases = live_agent_aliases(agent_name)
    with db_session() as conn:
        if agent_name is None:
            cursor = conn.execute("DELETE FROM quota_blocks")
        else:
            cursor = conn.execute(
                "DELETE FROM quota_blocks WHERE agent IN ("
                + ", ".join("?" for _ in aliases) + ")", aliases,
            )

    return cursor.rowcount


# One lock per agent name, not one global lock -- a request against
# "sentinel-opencode" must not block on (or be blocked by) a concurrent
# request against a completely different agent like "sentinel". Locks are
# created lazily and never removed; a handful of long-lived Lock objects
# for the agents this bridge ever sees is not worth cleaning up.
_agent_locks = {}
_queued_agent_locks = {}
_agent_locks_guard = threading.Lock()
_agent_locks_changed = threading.Condition(_agent_locks_guard)


class AgentLock:
    """A lock whose identity can merge without invalidating existing holders."""
    def __init__(self):
        self.parent = self
        self.holders = 0

    def _root(self):
        node = self
        while node.parent is not node:
            node = node.parent
        return node

    def acquire(self, blocking=True, timeout=-1):
        deadline = None if timeout < 0 else time.monotonic() + timeout
        with _agent_locks_changed:
            while self._root().holders:
                if not blocking:
                    return False
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                _agent_locks_changed.wait(remaining)
            self._root().holders += 1
            return True

    def release(self):
        with _agent_locks_changed:
            root = self._root()
            if not root.holders:
                raise RuntimeError("release unlocked agent lock")
            root.holders -= 1
            _agent_locks_changed.notify_all()

    def locked(self):
        with _agent_locks_guard:
            return bool(self._root().holders)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()

AVAILABLE_STATES = {
    "idle",
    "done",
}


def get_agent_lock(agent_name):
    return _get_alias_lock(agent_name, _agent_locks)


def get_queued_agent_lock(agent_name):
    # Hold the requested agent's queue slot through fallback and settlement.
    # Actual dispatch still uses get_agent_lock, shared with synchronous calls.
    return _get_alias_lock(agent_name, _queued_agent_locks)


def _get_alias_lock(agent_name, registry):
    # Include old queued rows, which can still carry a pane id alias.
    aliases = live_agent_aliases(agent_name)
    with _agent_locks_guard:
        known = list(dict.fromkeys(
            registry[alias]._root() for alias in aliases if alias in registry
        ))
        lock = known[0] if known else AgentLock()
        for other in known[1:]:
            lock.holders += other.holders
            other.parent = lock
        # Remember both addresses so a temporary discovery outage, or a
        # name disappearing, cannot give a known pane a second lock.
        for alias in aliases:
            registry[alias] = lock
        return lock


class AgentNotFoundError(RuntimeError):
    pass


def agent_or_auto(agent_name):
    """Settle on a real target before anything downstream uses the name.

    This used to resolve lazily -- pass a concrete identifier straight
    through, and only consult herdr once something had failed -- to save a
    subprocess on the happy path. That optimisation cost more than it
    saved: resolution ended up on the paths that *report* status and
    absent from the one that *dispatches work*, so a runtime family name
    answered /ready and then failed /ask with "unable to query Sentinel
    status". Resolving once, here, is what makes the identifier the same
    everywhere it matters: the lock key, the prompt target, and the agent
    recorded against the task.
    """
    return resolve_agent(agent_name)


def agent_priority_rank(agent):
    """Position in the operator's cost order; unlisted agents sort last."""
    if not AGENT_PRIORITY:
        return 0

    haystack = [
        str(value).lower()
        for value in (*agent_identifiers(agent), agent.get("agent"))
        if value
    ]

    for rank, wanted in enumerate(AGENT_PRIORITY):
        if any(wanted in value for value in haystack):
            return rank

    return len(AGENT_PRIORITY)


def agent_identifiers(agent):
    """Every string herdr will accept as a target for this agent."""
    return [v for v in (agent.get("name"), agent.get("pane_id")) if v]


def auto_pick_key(agent, blocked_names=()):
    """Sort key for choosing an agent when the caller named none.

    Being able to start now outranks being cheap, cost order decides among the
    rest, and a stable identifier breaks the remaining ties so consecutive
    defaults don't wander between agents for no visible reason.
    """
    if isinstance(blocked_names, set):
        names = blocked_names
    elif isinstance(blocked_names, dict):
        names = set(blocked_names.keys())
    elif blocked_names:
        first = next(iter(blocked_names), None)
        if isinstance(first, dict):
            names = {b["agent"] for b in blocked_names if isinstance(b, dict) and "agent" in b}
        else:
            names = set(blocked_names)
    else:
        names = ()

    ids = agent_identifiers(agent)
    primary_id = ids[0] if ids else ""

    return (
        any(identifier in names for identifier in ids),
        not bool(ids),
        agent.get("agent_status") not in AVAILABLE_STATES,
        agent_priority_rank(agent),
        primary_id,
    )


def live_agent_aliases(identifier):
    """Canonical address first; keep offline circuits/rows addressable too."""
    try:
        agents, _ = list_agents()
    except Exception:
        agents = None
    for agent in agents or ():
        if isinstance(agent, dict):
            aliases = agent_identifiers(agent)
            if identifier in aliases:
                return aliases
    return [identifier]


def describe_live_agents(agents):
    return ", ".join(
        f"{a.get('name') or a.get('pane_id')} ({a.get('agent')})"
        for a in agents
    ) or "none"


def resolve_agent(identifier):
    """Map a caller's identifier onto something herdr can address today.

    A herdr agent's name does not survive a session rebuild and its
    pane_id shifts when panes are recreated. Observed live: an operator
    restarted the herdr session and rebuilt both agent windows without
    renaming them, so a configured SENTINEL_AGENT stopped resolving and
    every call that did not hard-code a pane id failed -- while /health,
    /agents and the worker all still reported healthy.

    Resolution is deliberately ordered from most to least specific, and
    stops rather than guesses when a choice would be arbitrary: sending a
    task to the wrong agent is worse than refusing to send it.
    """
    try:
        agents, _ = list_agents()
    except Exception:
        # Missing binary, timeout, anything: same conclusion as an empty
        # list below.
        agents = None

    if not identifier:
        # Nothing configured and nothing asked for. Naming an agent is
        # optional -- herdr drops a name whenever the agent process
        # restarts, so a bridge that required one would make renaming a
        # routine chore -- and "any of them" is a perfectly good answer
        # when the caller expressed no preference.
        if not agents:
            raise AgentNotFoundError(
                "No herdr agents are running, so there is nothing to "
                "delegate to. Start one and try again."
            )

        # Being able to start now outranks being cheap: there is no
        # queue-and-wait path here, so preferring a busy agent would just
        # return 409 to the caller. Cost order decides among the agents
        # that can actually take the work, and a stable key breaks the
        # remaining ties so consecutive defaults don't wander between
        # agents for no reason the caller can see.
        # Only entries herdr gave an address to are candidates. An entry
        # with neither a name nor a pane id cannot be prompted, and
        # indexing its empty identifier list used to raise IndexError from
        # inside the sort key -- which took auto-select down completely,
        # failing every request that named no agent. The same filter is
        # already applied in quota_failover_candidates(); applying it in
        # only one of the two left them disagreeing about what an agent is.
        addressable = [
            agent for agent in agents
            if isinstance(agent, dict) and agent_identifiers(agent)
        ]

        if not addressable:
            raise AgentNotFoundError(
                f"{len(agents)} herdr agent(s) are running, but none has a "
                "name or a pane id to address it by, so none can be given "
                "work. Check `herdr agent list`."
            )

        try:
            blocked_names = {block["agent"] for block in list_agent_quota_blocks()}
        except Exception:
            blocked_names = set()
        return agent_identifiers(min(
            addressable, key=lambda agent: auto_pick_key(agent, blocked_names),
        ))[0]

    if not agents:
        # herdr being down is a different failure with a different fix,
        # and diagnosing it is not this function's job. Pass the name
        # through and let the herdr call itself report what went wrong.
        return identifier

    for agent in agents:
        if identifier in agent_identifiers(agent):
            return agent_identifiers(agent)[0]

    # Nothing matched exactly. A runtime family ("opencode", "claude")
    # outlives both the name and the pane id, so it is the one identifier
    # a deployment can configure and still have working after a rebuild.
    family = [a for a in agents if a.get("agent") == identifier]

    if len(family) == 1:
        return agent_identifiers(family[0])[0]

    if len(family) > 1:
        raise AgentNotFoundError(
            f"'{identifier}' matches {len(family)} live agents "
            f"({', '.join(agent_identifiers(a)[0] for a in family)}). "
            "Name the one you mean -- the bridge will not pick for you."
        )

    raise AgentNotFoundError(
        f"No live herdr agent matches '{identifier}'. Live agents: "
        f"{describe_live_agents(agents)}. Naming an agent is optional and "
        "a name does not survive the agent process restarting -- address "
        "one by pane id, by runtime family (opencode, claude), or leave "
        "the agent unset to let the bridge pick an available one."
    )


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
    def __init__(self, message, raw_output="", reason=None):
        self.raw_output = raw_output
        # Which failure this is, when the bridge has established it, so a
        # caller is told rather than left to infer it from herdr's wording.
        self.reason = reason
        super().__init__(message)


class PromptWaitAborted(SentinelPromptError):
    """herdr stopped waiting for the agent. The prompt may well have landed.

    A subclass of SentinelPromptError so anything that handled the old
    failure still does.
    """

    def __init__(self, message, raw_output="", code=None):
        super().__init__(message, raw_output=raw_output)
        self.code = code


class AgentQuotaExhaustedError(SentinelPromptError):
    """The agent's provider refused it. `kind` says why: a quota, or a size
    limit its session has outgrown."""

    def __init__(self, agent_name, detail, raw_output=""):
        self.agent_name = agent_name
        self.detail = detail
        self.kind = provider_failure_kind(detail)

        if self.kind == "context_limit":
            message = (
                f"Agent {agent_name} cannot take work: its provider refused "
                f"the request as over a size limit ({detail}). Its session "
                "has outgrown what its model or provider accepts, so sending it "
                "anything fails the same way until the session is compacted "
                "or restarted in its own terminal."
            )
        else:
            message = f"Agent quota exhausted ({agent_name}): {detail}"

        # raw_output goes through the base class: setting it here and then
        # calling super().__init__() without it reset it to "".
        super().__init__(message, raw_output=raw_output, reason="provider_rejected")


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
    def __init__(self, message, raw_output="", reason=None):
        self.raw_output = raw_output
        # "ended_quickly" records elapsed time, not whether work executed.
        self.reason = reason
        super().__init__(message)


def result_file_path(task_id):
    token = task_id.replace("-", "")
    return os.path.join(RESULT_DIR, f"result-{token}.txt")


def progress_file_path(task_id):
    token = task_id.replace("-", "")
    return os.path.join(RESULT_DIR, f"progress-{token}.txt")


def read_progress_file(task_id):
    """Whatever the agent has reported so far, or None.

    Separate from the result and never removed on read: this is polled
    repeatedly while the task runs, whereas a result is collected once.
    A missing file means the agent had nothing to say, not that anything
    failed -- progress is a convenience for the operator rather than a
    second contract the agent has to satisfy.
    """
    try:
        with open(progress_file_path(task_id), encoding="utf-8", errors="replace") as f:
            content = f.read().strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        print(f"[progress] unreadable progress file for {task_id}: {e}")
        return None

    return content or None


def circuit_hint(kind):
    if kind != "context_limit":
        return None

    if CONTEXT_BLOCK_TTL_SECONDS > 0:
        lifts = (
            f"It lifts by itself after {CONTEXT_BLOCK_TTL_SECONDS // 60} min, "
            "or with `quota-reset`."
        )
    else:
        lifts = "It lifts with `quota-reset`."

    return (
        "The provider refused this agent's last request as over a size "
        "limit: its session has outgrown what its model or provider accepts, so "
        "sending it anything fails the same way. It has to be compacted or "
        "restarted in its own terminal -- a `prompt` sent through the bridge "
        "is wrapped in a delegation envelope, so `/compact` sent that way is "
        "not a command. While this holds, tasks addressed to it are run on "
        f"another agent instead. {lifts}"
    )


def ready_hint(agent_status):
    """What a not-ready answer means and what to do about it, or None.

    A caller polled ready on one agent every forty seconds for minutes,
    reading ready=false as the system being blocked, while another agent sat
    idle throughout. The answer was accurate. What it did not say is that
    "working" is ordinary, and "false" with no reason attached reads as an
    obstruction.

    blocked is worded differently on purpose: it is the one state that
    really does need a person -- the agent is waiting on input in its own
    terminal -- and describing it as ordinary busyness would hide that.
    """
    if agent_status in AVAILABLE_STATES:
        return None

    if agent_status == "blocked":
        # Worded as a report, not a diagnosis. herdr's `blocked` flickers
        # while an agent is busy (observed: blocked, then working a second
        # later), and the previous wording -- "will not free itself" -- sent a
        # caller off reporting an interactive menu that was not there.
        return (
            "herdr reports this agent as blocked, which usually means a "
            "permission prompt or approval -- but the report can flicker "
            "while an agent is busy. Check again in a few seconds before "
            "acting on it; `read` shows what is on its screen."
        )

    if agent_status == "working":
        return (
            "This agent is busy with other work -- ordinary, not a fault. "
            "Omit the agent to let the bridge pick one that is free, or wait. "
            "Polling this one repeatedly gains nothing."
        )

    return None


def _reroute_target(live):
    """The live agent an opted-in task should go to, or None.

    The best one that can be addressed and whose provider is not blocked --
    a task moved to an agent that would refuse it has gained nothing.
    """
    try:
        blocked_names = {block["agent"] for block in list_agent_quota_blocks()}
    except Exception:
        blocked_names = set()

    eligible = [
        a for a in live
        if agent_identifiers(a)
        and not any(identifier in blocked_names for identifier in agent_identifiers(a))
    ]

    if not eligible:
        return None

    return agent_identifiers(min(
        eligible, key=lambda a: auto_pick_key(a, blocked_names),
    ))[0]


def settle_unreachable_task(task_row):
    """Deal with a queued task whose target agent no longer exists.

    Returns "rerouted" or "abandoned", or None if the task was left alone.

    Conservative on every side: only after STALE_TARGET_SEC, only when herdr
    actually answered and listed no such agent (a herdr that cannot be asked
    proves nothing), and never for an agent that exists but is busy -- waiting
    for a free agent is the job, however long it takes.

    By default it does not re-point the task at some other agent. resolve_agent()
    holds that sending work to the wrong agent is worse than not sending it, and
    the caller can choose. What it does do is say so, and say that nothing was
    sent. A task submitted with reroute="any" is the caller choosing: it has
    not started, so moving it cannot run anything twice.
    """
    age = _age_seconds(task_row["created_at"])

    if age is None or age < STALE_TARGET_SEC:
        return None

    try:
        agents, _ = list_agents()
    except Exception:
        return None

    if agents is None:
        return None

    live = [a for a in agents if isinstance(a, dict)]
    agent_name = task_row["agent"]

    if any(agent_name in agent_identifiers(a) for a in live):
        return None

    if task_row.get("reroute") == "any":
        target = _reroute_target(live)

        if target and reroute_task(task_row["task_id"], agent_name, target):
            return "rerouted"

    if abandon_task(
        task_row["task_id"],
        f"The agent this task was queued for ('{agent_name}') no longer "
        f"exists -- herdr now lists {describe_live_agents(live)} -- so "
        "nothing could ever pick it up. It was never sent to any agent, so "
        "resubmitting is safe. (A herdr agent's name does not survive its "
        "process restarting; omit the agent and the bridge will choose one.)",
    ):
        return "abandoned"

    return None


def explain_queued(agent_name):
    """Why a queued task has not started yet, in one line.

    The worker already establishes this on every pass -- it checks the
    target agent and skips the task when that agent cannot take work --
    but it never surfaced anywhere. A caller watching `queued` with
    started_at null had twelve fields and not one of them said the agent
    was simply busy with something else, which is ordinary and looks
    identical to a stuck queue.

    Advisory only: this is a convenience, so a herdr that cannot be
    reached returns None rather than turning a task query into a 500.
    """
    try:
        agents, _ = list_agents()
    except Exception:
        return None

    if agents is None:
        return None

    live = [a for a in agents if isinstance(a, dict)]
    match = next(
        (a for a in live if agent_name in agent_identifiers(a)),
        None,
    )

    if match is None:
        return (
            f"'{agent_name}' is not among the running agents "
            f"({describe_live_agents(live)}), so nothing will pick this up. "
            "Delegate again without naming an agent to let the bridge choose."
        )

    try:
        status, _ = get_agent_status(agent_name)
    except Exception:
        status = None

    status = status or match.get("agent_status")

    if status not in AVAILABLE_STATES:
        return (
            f"agent {agent_name} is {status}, so the worker is skipping this "
            "task until it frees up. Nothing is wrong -- it is someone "
            "else's turn."
        )

    return (
        f"agent {agent_name} is {status}; waiting for the worker to reach "
        "this task."
    )


def bounded_queue_explanation(agent_name):
    with execution_budget(1000):
        return explain_queued(agent_name)


def discard_progress_file(task_id):
    try:
        os.remove(progress_file_path(task_id))
    except OSError:
        # Never created, or already gone. The startup sweep is the
        # backstop for whatever this misses.
        pass


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
        # Progress files are swept too: a task killed mid-flight leaves
        # one behind exactly as a failed delivery leaves a result.
        if not (name.startswith(("result-", "progress-")) and name.endswith(".txt")):
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
    try:
        agents, _ = list_agents()
    except (OSError, subprocess.TimeoutExpired):
        # Discovery is advisory here. The HTTP service must still start so
        # operators can inspect it while Herdr is unavailable or recovering.
        return None

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

        try:
            inside = os.path.normcase(os.path.commonpath(
                [result_dir, os.path.abspath(cwd)]
            )) == os.path.normcase(os.path.abspath(cwd))
        except ValueError:
            # Windows paths on different drives share no common path.
            inside = False
        if not inside:
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


def discard_result_file(task_id):
    try:
        os.remove(result_file_path(task_id))
    except OSError:
        pass


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

    fenced = _FENCED_BLOCK.match(content)
    body = fenced.group(1).strip() if fenced else content
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        payload = None  # Legacy text remains an accepted result format.
    if isinstance(payload, dict) and "task_id" in payload:
        receipt_id = payload["task_id"]
        matches = isinstance(receipt_id, str) and receipt_id == task_id
        if isinstance(receipt_id, str) and not matches:
            try:
                matches = uuid.UUID(receipt_id) == uuid.UUID(task_id)
            except ValueError:
                pass
        if not matches:
            print(f"[result] task id mismatch for {task_id}; retaining the file for diagnosis")
            return None

    if cleanup:
        discard_result_file(task_id)

    return content or None


def quota_error_detail(text, ignore=None):
    """Return a compact provider error when text looks like a quota failure.

    `ignore` is text the agent merely echoed rather than produced -- the
    delegated task itself, which the terminal always contains. Without this,
    a task *about* rate limits reads as the agent having hit one.
    """
    haystack, matches = _provider_error_hits(text, ignore)

    if not matches:
        return None

    # Keep the matched evidence and a little context around it, not a
    # trailing slab of the terminal. /quota surfaces this detail, and the
    # terminal always contains the delegated task -- paths, dataset names
    # -- which has no business in an endpoint that reports provider errors.
    compact = _evidence_window(haystack, matches[0])

    return compact or "provider reported a quota or balance failure"


# What a TUI draws between its columns.
COLUMN_SEPARATORS = "┃│║"


def _evidence_window(haystack, match):
    """The matched text and a little context, kept to its own column.

    An agent's terminal is two columns -- the conversation beside a sidebar --
    so a flat window around a hit spills into whatever sits next to it. On a
    real capture that was a path from the delegated task. The text is served
    by /ready and /quota and quoted in task notes, so it is cut at the
    separators. With none present, as in plain output, nothing changes.
    """
    start = max(0, match.start() - 80)
    end = match.end() + 80

    left = max(haystack.rfind(c, start, match.start()) for c in COLUMN_SEPARATORS)
    if left != -1:
        start = left + 1

    rights = [
        i for i in (haystack.find(c, match.end(), end) for c in COLUMN_SEPARATORS)
        if i != -1
    ]
    if rights:
        end = min(rights)

    return " ".join(haystack[start:end].split())


def _provider_error_hits(text, ignore=None):
    if not text:
        return "", []

    haystack = text
    if ignore:
        haystack = haystack.replace(ignore, " ")

    return haystack, list(QUOTA_ERROR_PATTERN.finditer(haystack))


def fresh_provider_refusal(before, after, ignore=None):
    """A provider refusal in `after` that was not already in `before`.

    A terminal keeps old errors in view, so finding one after a prompt proves
    nothing -- it may be last hour's. Only refusal wording and explicit size
    figures identify an error; adjoining task output or sidebar text cannot
    prove a new refusal. Counts preserve an identical appended error.
    """
    def keys(text):
        haystack, matches = _provider_error_hits(text, ignore)
        found = []
        for match in matches:
            wording = " ".join(match.group().lower().split())
            figures = None
            if CONTEXT_LIMIT_PATTERN.fullmatch(match.group()):
                suffix = haystack[match.end():].split("\n", 1)[0]
                numeric = re.match(r"\s*:\s*(\d+)\s*>\s*(\d+)", suffix)
                if numeric:
                    figures = numeric.groups()
            found.append(((wording, figures), match))
        return found, haystack

    previous, _ = keys(before)
    seen = Counter(key for key, _ in previous)
    current, haystack = keys(after)

    for key, match in current:
        compatible = key if seen[key] else next((
            old for old, count in seen.items()
            if count and old[0] == key[0] and (old[1] is None or key[1] is None)
        ), None)
        if compatible is None:
            return _evidence_window(haystack, match)
        seen[compatible] -= 1

    return None


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

    # Keyed on whatever herdr will actually accept as a target, not on
    # name: herdr drops an agent's name when the agent process restarts,
    # and keying on it meant an all-unnamed host produced no candidates at
    # all -- so a quota-exhausted agent reported "all eligible fallback
    # agents are quota-blocked" while a healthy one sat idle beside it.
    by_id = {}
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        for identifier in agent_identifiers(agent):
            by_id.setdefault(identifier, agent)

    primary_family = _agent_runtime_family(by_id.get(primary_agent, {}))

    if QUOTA_FAILOVER_AGENTS:
        names = QUOTA_FAILOVER_AGENTS
    else:
        # One entry per agent, not one per identifier, or an agent with
        # both a name and a pane id would be offered as two candidates.
        seen = set()
        names = []
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            identifiers = agent_identifiers(agent)
            if identifiers and id(agent) not in seen:
                seen.add(id(agent))
                names.append(identifiers[0])

    candidates = []
    primary_aliases = agent_identifiers(by_id.get(primary_agent, {}))
    for name in names:
        if name == primary_agent or name in primary_aliases or name not in by_id:
            continue

        # A configured allowlist is an operator's explicit decision to use
        # these sessions. Discovery mode is stricter and insists on a
        # different runtime family before it can switch automatically.
        if not QUOTA_FAILOVER_AGENTS:
            family = _agent_runtime_family(by_id[name])
            if not primary_family or not family or family == primary_family:
                continue

        canonical = agent_identifiers(by_id[name])[0]
        if canonical not in candidates:
            candidates.append(canonical)

    if QUOTA_FAILOVER_AGENTS:
        # An explicit allowlist is already an ordered operator decision.
        return candidates

    # A quota failover is when cost order matters most: the primary is
    # gone and the bridge is choosing what to pay for next.
    return sorted(candidates, key=lambda name: agent_priority_rank(by_id[name]))


def build_result_reminder_prompt(task_id):
    return f"""
当前委派任务编号：{task_id}
本轮调用已返回，但桥尚未收到与此任务对应的有效结果，执行状态仍不确定。

不要重新执行任务，不要继续检查、计算或提交作业。仅允许写入下述结果文件，
可使用完成此写入必需的文件工具或命令，不得修改其他文件。
只报告此任务已确认的结果；不要把其他任务、旧轮次或其他执行端的回执写到这里。
若任务未执行、连接失败或无法确认，请如实记录失败或不确定，不要声称已经完成。
如使用 JSON，请填写 task_id 为 {json.dumps(task_id)}。

把已有结果写入：
{result_file_path(task_id)}
""".strip()


def _run_herdr_prompt(agent_name, delegated_prompt, timeout_ms):
    with _shutdown_guard:
        if _shutting_down:
            # This may be a reminder after actual work, so use the uncertain
            # execution path rather than a busy error that could requeue it.
            raise TimeoutError("Bridge is shutting down; no further prompt was sent. "
                               "The agent may still be executing the earlier task.")
    timeout_ms = remaining_ms(timeout_ms)
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
        code = herdr_error_code(result)

        if code == "timeout":
            # herdr's own wait expired: the prompt was delivered and the
            # agent is still going. The same event as the subprocess timeout
            # above, and handled the same way -- it used to surface as a 500
            # "prompt command failed", telling the caller it had not landed.
            raise TimeoutError(
                "Bridge stopped waiting for Sentinel. "
                "Sentinel may still be executing the task."
            )

        if code == "agent_blocked":
            # NOT a delivery failure. `--wait` delivers the prompt and then
            # waits, and herdr aborts the wait on a transient blocked just as
            # readily as on a real one. Measured: 69 of 96 tasks that failed
            # this way had gone on to deliver a result.
            raise PromptWaitAborted(
                "Herdr prompt command failed: " + result.get("stderr", ""),
                raw_output=_read_terminal_tail(agent_name, READ_LINES_DEFAULT),
                code=code,
            )

        if not (result.get("stderr", "").strip() or result.get("stdout", "").strip()):
            # No error, no output, just a non-zero exit: the call was killed
            # (bridge-restart hangs up the whole process group, `herdr agent
            # prompt` included) rather than refused. Nothing here says the
            # prompt did not land, and "failed" -- which means safe to retry --
            # is exactly the claim that cannot be made. Reported as a lost
            # track instead, which is what the worker records as orphaned.
            raise TimeoutError(
                "The herdr prompt call ended without any output -- it was "
                "probably killed, as a bridge restart does -- so it is "
                "unknown whether the prompt reached the agent. Sentinel may "
                "still be executing the task."
            )

        # The caller resolved this identity, acquired its lock, and recorded
        # the execution target. Retargeting here would bypass the new agent's
        # lock and durable ownership. An unknown transport failure also says
        # nothing about whether the first prompt ran. Never resend here.

        # herdr reports a refusal as a code -- "agent_blocked" -- and
        # nothing more. Why the agent is blocked (a permission request, a
        # read-only plan mode, a confirmation dialog) lives only on its
        # screen, so a structured status alone leaves an operator with no
        # idea what to do next. This is the case that keeps terminal
        # reading worth having even though results no longer come from it.
        raise SentinelPromptError(
            "Herdr prompt command failed: " + result.get("stderr", ""),
            raw_output=_read_terminal_tail(agent_name, READ_LINES_DEFAULT),
        )

    return result


def herdr_error_code(result):
    """The `error.code` from a failed herdr call, or None."""
    for stream in (result.get("stderr", ""), result.get("stdout", "")):
        try:
            payload = json.loads(stream)
        except (TypeError, ValueError):
            continue

        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and error.get("code"):
            return error["code"]

    return None


def settle_after_abort(agent_name, task_id, aborted, deadline):
    """herdr aborted its wait; work out what actually happened.

    The abort tells the bridge almost nothing: it fires for a transient
    `blocked` exactly as for a real one. So rather than take it as a verdict,
    watch the agent and let what it does decide:

      result file appears        -> it delivered; carry on
      working, then idle/done    -> its turn ended; carry on (the caller
                                    reads the file, or asks once for it)
      still blocked after a while -> only now say so: it really is waiting
                                    on interactive input
      idle/done and never started -> the prompt was not taken up, or at
                                    least nothing shows that it was
      deadline                   -> TimeoutError, as for any task

    A failed status lookup is not a verdict either; it just means look again.
    Returns normally whenever it is safe to go on and read the result.
    """
    started = time.monotonic()
    saw_working = False
    blocked_since = None

    while True:
        if os.path.exists(result_file_path(task_id)):
            return

        now = time.monotonic()

        if now >= deadline:
            raise TimeoutError(
                "Bridge stopped waiting for Sentinel. "
                "Sentinel may still be executing the task."
            )

        try:
            status, _ = get_agent_status(agent_name)
        except Exception:
            status = None

        if status == "working":
            saw_working = True
            blocked_since = None

        elif status == "blocked":
            if blocked_since is None:
                blocked_since = now
            elif now - blocked_since >= BLOCKED_CONFIRM_SEC:
                raise SentinelPromptError(
                    f"The agent stayed blocked for {BLOCKED_CONFIRM_SEC:g}s "
                    "after herdr aborted its wait, so it really is waiting "
                    "on interactive input in its own terminal -- a "
                    "permission prompt or an approval. The terminal tail, if "
                    "herdr would give one, is attached.",
                    raw_output=_read_terminal_tail(agent_name, READ_LINES_DEFAULT),
                    reason="blocked_confirmed",
                )

        elif status in AVAILABLE_STATES:
            blocked_since = None

            if saw_working:
                return

            if now - started >= NOT_STARTED_GRACE_SEC:
                raise SentinelPromptError(
                    "herdr aborted its wait with agent_blocked, but afterwards "
                    "the agent showed no activity and no result appeared "
                    f"within {NOT_STARTED_GRACE_SEC:g}s. It is unknown whether "
                    "the prompt was delivered, so do not retry blindly: check "
                    "the agent first (ready, then read).",
                    raw_output=aborted.raw_output,
                    reason="delivery_unknown",
                )

        time.sleep(ABORT_POLL_SEC)


def deliver_prompt(agent_name, delegated_prompt, timeout_ms, task_id):
    """Send a prompt and wait for the agent, tolerating a spurious abort."""
    deadline = time.monotonic() + timeout_ms / 1000

    try:
        return _run_herdr_prompt(agent_name, delegated_prompt, timeout_ms)
    except PromptWaitAborted as aborted:
        settle_after_abort(agent_name, task_id, aborted, deadline)

        return {
            "ok": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "note": (
                "herdr aborted its wait on a transient `blocked`; the agent "
                "was then seen to carry on."
            ),
        }


def run_prompt_only(agent_name, task_id, task, timeout_ms, slurm_policy=None):
    with execution_budget(timeout_ms):
        return _run_prompt_only(agent_name, task_id, task, timeout_ms, slurm_policy)


def _run_prompt_only(agent_name, task_id, task, timeout_ms, slurm_policy=None):
    delegated_prompt = build_delegation_prompt(task, task_id, slurm_policy)

    # herdr reports an agent whose provider refused the prompt as done, just
    # as for one that finished -- so a prompt "succeeds" in about a second and
    # the caller waits for work that was never started. The refusal is only
    # text in the terminal, so look: before and after, because a terminal
    # keeps old errors in view and an old one proves nothing about this prompt.
    # If the "before" cannot be read there is nothing to compare against, and
    # accusing the agent of an error that may be hours old is worse than not
    # checking.
    before, _ = _terminal_read(agent_name, READ_LINES_DEFAULT)

    result = deliver_prompt(agent_name, delegated_prompt, remaining_ms(timeout_ms), task_id)

    if before is not None:
        after, _ = _terminal_read(agent_name, READ_LINES_DEFAULT)
        detail = fresh_provider_refusal(before, after, ignore=task)

        if detail:
            raise AgentQuotaExhaustedError(agent_name, detail, raw_output=after[-4000:])

    return result


WORKING_READ_NOTE = (
    "The agent is working, and herdr reads history only while an agent is "
    "idle (a request for more lines than the screen holds has to scroll back), "
    "so this is the visible screen only -- not the lines asked for. Ask again "
    "when it is idle for more."
)


def _read_once(agent_name, source, read_lines, timeout):
    args = ["agent", "read", agent_name, "--source", source]

    # `visible` is the screen itself. herdr clamps a line count to the
    # screen's height rather than refusing it, so leaving the count off asks
    # for what is wanted instead of for a number that has no meaning here.
    if source != "visible":
        args += ["--lines", str(read_lines)]

    return run_herdr(*args, timeout=timeout)


def read_terminal(agent_name, read_lines, source=None, timeout=10):
    """Read an agent's terminal. Returns (result, source_used, note).

    herdr refuses to read the history of a working agent once more lines are
    asked for than the screen holds -- measured: fine at the screen's height,
    `agent_not_idle` one line past it, and `visible` never refused. The
    default request is well past any screen, so without this every read of a
    working agent failed, at the one time a caller most wants to look.

    When the caller did not name a source, a refused history read falls back
    to the visible screen and says so. When it did, it gets what it asked for.
    """
    chosen = source or DEFAULT_READ_SOURCE
    result = _read_once(agent_name, chosen, read_lines, timeout)

    if (
        result["ok"]
        or source is not None
        or herdr_error_code(result) != "agent_not_idle"
    ):
        return result, chosen, None

    screen = _read_once(agent_name, "visible", read_lines, timeout)

    if not screen["ok"]:
        return result, chosen, None

    return screen, "visible", WORKING_READ_NOTE


def _terminal_read(agent_name, read_lines, timeout=60):
    """(text, None) if the terminal could be read, else (None, why)."""
    try:
        result, _, _ = read_terminal(agent_name, read_lines, timeout=timeout)
    except Exception as e:
        return None, f"unable to read terminal for diagnostics: {e}"

    if not result["ok"]:
        return None, "terminal read failed: " + result.get("stderr", "")

    return result["stdout"], None


def _read_terminal_tail(agent_name, read_lines):
    """Terminal text, for failure diagnostics only. Never on the happy path:
    a broken/slow read here must not mask the error being diagnosed."""
    text, why = _terminal_read(agent_name, read_lines)

    if why:
        return f"({why})"

    return text[-4000:]


def execute_sentinel_task(agent_name, task_id, task, timeout_ms, read_lines=500,
                          slurm_policy=None):
    with execution_budget(timeout_ms):
        return _execute_sentinel_task(agent_name, task_id, task, timeout_ms, read_lines, slurm_policy)


def _execute_sentinel_task(agent_name, task_id, task, timeout_ms, read_lines=500,
                           slurm_policy=None):
    os.makedirs(RESULT_DIR, exist_ok=True)

    # Old terminal errors are not evidence that this request was refused.
    before, _ = _terminal_read(agent_name, read_lines, 10)
    turn_began = time.monotonic()
    deliver_prompt(
        agent_name,
        build_delegation_prompt(task, task_id, slurm_policy),
        remaining_ms(timeout_ms),
        task_id,
    )
    turn_sec = time.monotonic() - turn_began

    response = read_result_file(task_id, cleanup=False)

    if response is None:
        terminal_tail = _read_terminal_tail(agent_name, read_lines)

        # Without a before snapshot, delivery remains uncertain: missing
        # output must not send already-executed work to another agent.
        detail = (
            fresh_provider_refusal(before, terminal_tail, ignore=task)
            if before is not None else None
        )
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

        try:
            deliver_prompt(
                agent_name,
                build_result_reminder_prompt(task_id),
                remaining_ms(RESULT_REMINDER_TIMEOUT_MS),
                task_id,
            )
        except AgentQuotaExhaustedError as error:
            # The original task may already have run. A refusal of its
            # result-only reminder cannot justify replaying that task.
            raise SentinelResultMissingError(
                "The result-only reminder was refused by the provider. "
                "The original task may already have executed; do not retry "
                "it on another agent without checking its effects.",
                raw_output=error.raw_output,
                reason="result_delivery_refused",
            ) from error

        response = read_result_file(task_id, cleanup=False)

    if response is None and turn_sec < QUICK_END_SEC:
        # A fast turn can still have completed side effects before losing its file.
        raise SentinelResultMissingError(
            f"Sentinel's turn ended after {turn_sec:.0f}s without a result "
            "file. It may already have executed the task and failed only "
            "to deliver the answer. Check its terminal and actual effects "
            "before retrying; elapsed time does not establish whether work ran.",
            raw_output=_read_terminal_tail(agent_name, read_lines),
            reason="ended_quickly",
        )

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


def run_with_quota_failover(primary_agent, operation, primary_locked=False,
                            failover=True):
    """Run once on the requested agent, then actively switch providers.

    The caller receives both the operation result and the actual agent name.
    A persisted quota circuit avoids hitting a known-exhausted account again.

    `failover=False` is for a request that only means something on the agent
    it names: it neither consults the circuit nor moves elsewhere, though a
    refusal it meets is still remembered.
    """
    existing_block = get_agent_quota_block(primary_agent) if failover else None
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

        if not failover:
            raise

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

# Set when a task is queued, so an idle worker starts it at once instead of at
# its next poll. The poll stays as the fallback for what no one announces: an
# agent becoming free, a circuit expiring.
_work_available = threading.Event()

_worker_thread = None
_worker_stop = None
_worker_drain_deadline = None
_worker_maintenance_guard = threading.Lock()
_worker_last_prune = {}


class TaskWorkerPool(threading.Thread):
    """A bounded worker group whose thread lives until every member exits."""
    def __init__(self, stop_event, max_workers=None):
        super().__init__(daemon=True, name="sentinel-task-workers")
        self.stop_event = stop_event
        count = validate_worker_concurrency(
            WORKER_CONCURRENCY if max_workers is None else max_workers)
        self.workers = [threading.Thread(
            target=self._run_worker, daemon=True,
            name=f"sentinel-task-worker-{index + 1}",
        ) for index in range(count)]
        self._failed = threading.Event()

    def _run_worker(self):
        try:
            task_worker(self.stop_event)
        finally:
            if not self.stop_event.is_set() and not _shutting_down:
                self._failed.set()
                self.stop_event.set()
                _work_available.set()

    def run(self):
        started = []
        try:
            for worker in self.workers:
                worker.start()
                started.append(worker)
            for worker in started:
                worker.join()
        finally:
            self.stop_event.set()
            _work_available.set()
            for worker in started:
                worker.join()

    def healthy(self):
        return (self.is_alive() and not self._failed.is_set()
                and all(worker.is_alive() for worker in self.workers))


def worker_is_healthy():
    if _worker_thread is None:
        return None
    if isinstance(_worker_thread, TaskWorkerPool):
        return _worker_thread.healthy()
    return _worker_thread.is_alive()


def _retry_worker_settlement(stop_event):
    if _shutting_down or (stop_event is not None and stop_event.is_set()):
        deadline = _worker_drain_deadline if _shutting_down else None
        remaining = ((deadline or 0) - time.monotonic())
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    else:
        time.sleep(WORKER_POLL_SECONDS)
    return True


def _prune_worker_history():
    if not TASK_RETENTION_DAYS or not _worker_maintenance_guard.acquire(False):
        return
    try:
        key = os.path.realpath(DB_PATH)
        now = time.monotonic()
        if now - _worker_last_prune.get(key, float("-inf")) < PRUNE_INTERVAL_SEC:
            return
        pruned = prune_old_tasks()
        _worker_last_prune[key] = now
        if pruned:
            print(f"[prune] removed {pruned} finished task(s) older than {TASK_RETENTION_DAYS} day(s)")
    finally:
        _worker_maintenance_guard.release()


def task_worker(stop_event=None):
    print("Task worker started.")

    # Agents found busy earlier in the current pass over the queue --
    # reset once the pass finds nothing left to try (empty queue, or
    # every remaining queued task's agent is in this set already). This
    # is what stops one busy agent's oldest task from starving a
    # different, already-idle agent's tasks sitting behind it.
    busy_this_pass = set()
    settlement = None
    queue_slot = None

    try:
        while True:
            if settlement is not None:
                finish, args = settlement
                try:
                    finish(*args)
                    settlement = None
                    if queue_slot is not None:
                        queue_slot.release()
                        queue_slot = None
                    if finish is not requeue_task:
                        _work_available.set()
                except Exception as error:
                    print(f"[worker] task state was not persisted; retrying only persistence: {error}")
                    if not _retry_worker_settlement(stop_event):
                        return
                    continue

            if _shutting_down or (stop_event is not None and stop_event.is_set()):
                return
            try:
                if _worker_maintenance_guard.acquire(False):
                    try:
                        retry_sync_completions()
                        cancel_blocked_tasks()
                    finally:
                        _worker_maintenance_guard.release()
                task_row = peek_next_task(exclude_agents=busy_this_pass)

                if task_row is None:
                    busy_this_pass.clear()

                    _prune_worker_history()

                    # Cleared before the next look, never after it: a task queued
                    # in between sets it again, so it is found rather than slept
                    # through.
                    _work_available.wait(WORKER_POLL_SECONDS)
                    _work_available.clear()
                    continue

                # 复用 /ask 用的同一把锁 + 同一套 busy 判断，
                # 避免维护两份重复的加锁逻辑。
                agent_name = task_row["agent"]
                task_id = task_row["task_id"]
                claimed = False

                try:
                    slot = get_queued_agent_lock(agent_name)
                    if not slot.acquire(blocking=False):
                        busy_this_pass.add(agent_name)
                        continue
                    queue_slot = slot
                    def operation(target):
                        with db_session() as conn:
                            changed = conn.execute(
                                "UPDATE tasks SET executed_agent = ? "
                                "WHERE task_id = ? AND status = 'running'", (target, task_id),
                            ).rowcount
                        if not changed:
                            raise SentinelUnavailableError("Task is no longer running")
                        return execute_sentinel_task(
                            agent_name=target, task_id=task_id, task=task_row["task"],
                            timeout_ms=task_row["timeout_ms"],
                            slurm_policy=task_row["slurm_policy"],
                        )

                    if get_agent_quota_block(agent_name):
                        # The primary was already proved unavailable for quota
                        # reasons. Claim once, then actively use a fallback
                        # without sending another prompt to that account.
                        if not claim_task(task_id):
                            continue
                        claimed = True
                        with execution_budget(task_row["timeout_ms"]):
                            result, used_agent = run_with_quota_failover(agent_name, operation)
                    else:
                        with acquire_agent_for_delegation(agent_name):
                            if not claim_task(task_id):
                                continue
                            claimed = True
                            with execution_budget(task_row["timeout_ms"]):
                                result, used_agent = run_with_quota_failover(
                                    agent_name, operation, primary_locked=True,
                                )

                    settlement = (settle_completed_task, (
                        task_id, result, routing_note(agent_name, used_agent), used_agent,
                    ))

                except TimeoutError as e:
                    # The agent may still be executing, so it cannot safely be
                    # retried on another provider.
                    if claimed:
                        settlement = (orphan_task, (task_id, str(e)))
                        print(f"[task {task_id}] orphaned")
                    else:
                        print(f"[task {task_id}] pre-claim timeout on {agent_name!r}: {e}")
                        busy_this_pass.add(agent_name)
                        continue

                except QuotaFailoverExhaustedError as e:
                    detail = str(e)
                    if e.raw_output:
                        detail += "\n\nRaw provider output:\n" + e.raw_output
                    if claimed:
                        settlement = (quota_exhausted_task, (task_id, detail))
                        print(f"[task {task_id}] quota exhausted: {e}")
                    else:
                        print(f"[task {task_id}] pre-claim quota exhaustion on {agent_name!r}: {e}")
                        busy_this_pass.add(agent_name)
                        continue

                except (SentinelBusyError, SentinelUnavailableError) as unavailable:
                    settled = (
                        settle_unreachable_task(task_row)
                        if isinstance(unavailable, SentinelUnavailableError)
                        and not claimed
                        else None
                    )
                    if settled:
                        print(
                            f"[task {task_id}] {settled}: its agent "
                            f"({agent_name}) no longer exists"
                        )
                        continue

                    if claimed:
                        # A quota fallback exists but is temporarily occupied or
                        # unreachable. Preserve the task and retry later; do not
                        # misreport this as either a completed fallback or a
                        # permanent quota failure.
                        settlement = (requeue_task, (task_id,))

                    # This agent's busy/unreachable -- remember that for the
                    # rest of this pass and immediately look for a task
                    # against a different agent, instead of sleeping and
                    # retrying the same oldest-but-busy task.
                    busy_this_pass.add(agent_name)
                    continue

                except Exception as e:
                    if claimed:
                        detail = str(e)
                        # Any failure carrying a terminal tail gets it attached,
                        # not just a missing result: a refused prompt says only
                        # "agent_blocked", and the reason is on the screen.
                        raw_output = getattr(e, "raw_output", "")
                        if raw_output:
                            detail += (
                                "\n\nRaw Sentinel output (last 4000 chars):\n"
                                + raw_output
                            )
                        settlement = (fail_task, (task_id, detail))
                        print(f"[task {task_id}] error: {e}")
                    else:
                        print(f"[task {task_id}] pre-claim error on {agent_name!r}: {e}")
                        busy_this_pass.add(agent_name)
                        continue

                finally:
                    if settlement is None and queue_slot is not None:
                        queue_slot.release()
                        queue_slot = None

            except Exception as e:
                # Anything else -- a DB hiccup in peek_next_task/claim_task,
                # or even orphan_task/fail_task/complete_task itself failing --
                # must not kill this thread. A dead worker is invisible: /health
                # keeps reporting healthy and /delegate keeps accepting work
                # that will now never run. Log it loudly and keep polling.
                print(f"[worker] unexpected error, will retry: {e}")
                time.sleep(WORKER_POLL_SECONDS)
    finally:
        if queue_slot is not None:
            queue_slot.release()


def run_herdr(*args, timeout=None):
    timeout = remaining_seconds(timeout if timeout is not None else HERDR_DEFAULT_TIMEOUT_SEC)
    if args[:2] == ("agent", "prompt"):
        # Make shutdown exclusion atomic with creation, not with the potentially
        # hours-long --wait. Waiting and killing a timed-out driver hold no guard.
        with _shutdown_guard:
            if _shutting_down:
                raise TimeoutError("Bridge is shutting down; no further prompt was sent. "
                                   "The earlier task may still be executing.")
            process = subprocess.Popen(
                [HERDR, *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        with process:
            try:
                stdout, stderr = process.communicate(timeout=remaining_seconds(timeout))
            except subprocess.TimeoutExpired as error:
                process.kill()
                if os.name == "nt":
                    error.stdout, error.stderr = process.communicate()
                else:
                    # Descendants may still hold the pipes. Reap only the
                    # killed driver, as subprocess.run does on POSIX.
                    process.wait()
                raise
            except BaseException:
                process.kill()
                process.wait()
                raise
        return {"ok": process.returncode == 0, "returncode": process.returncode,
                "stdout": stdout, "stderr": stderr}
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


def build_delegation_prompt(task, task_id, slurm_policy=None):
    path = result_file_path(task_id)
    progress = progress_file_path(task_id)
    policy = SLURM_POLICIES[validate_slurm_policy(slurm_policy)]

    # The policy line is always present, even for tasks that have nothing
    # to do with Slurm. A gate the caller has to remember to attach is not
    # a gate, and this is a safety control rather than a rationale -- the
    # same reason the credentials warning stayed when the prompt's
    # self-justifying sentences were cut.
    return f"""
以下是一条远程委派的任务。
任务编号：{task_id}。结果只对应此编号，不要复用其他任务的回执。

===== 任务开始 =====
{task}
===== 任务结束 =====

{policy}动了作业就把 job ID 写进结果。

想让远端看到进展（拿到 job ID、卡在哪步）可追加一行到此，**可选**：
{progress}

做完后把结果写入：
{path}

推荐写成一个 JSON 对象，调用方能直接读到状态和 job ID（写纯文本也行，同样会被收下）：
{{"task_id": {json.dumps(task_id)}, "status": "success|partial|failed|blocked", "summary": "做了什么、结论是什么、有没有卡住", "job_ids": [], "artifacts": [], "metrics": {{}}, "warnings": [], "changed_files": []}}
只有 summary 必填；改动了什么写进 changed_files。文件会被整份取走，别写凭据。
""".strip()

_inflight = 0
_inflight_lock = threading.Lock()
_shutting_down = False
_shutdown_guard = threading.Lock()


@contextlib.contextmanager
def track_inflight():
    """Count requests being handled, so a shutdown can tell when it is quiet.

    The decrement is in a finally: a leaked count would make every later
    shutdown wait out its whole grace period for a request long gone.
    """
    global _inflight

    with _inflight_lock:
        _inflight += 1

    try:
        yield
    finally:
        with _inflight_lock:
            _inflight -= 1


def wait_for_inflight(grace_sec):
    """Drain HTTP handlers and the stopped worker, including its settlement."""
    deadline = time.monotonic() + grace_sec

    while True:
        with _inflight_lock:
            worker_alive = (
                _worker_thread is not None and _worker_thread.is_alive()
                and _worker_thread is not threading.current_thread()
            )
            if _inflight == 0 and not worker_alive:
                return True

        if time.monotonic() >= deadline:
            return False

        time.sleep(0.05)


def orphan_running_tasks(reason):
    with db_session() as conn:
        return _transition_where(
            conn, "orphaned", "running",
            {"finished_at": now_iso(), "error_text": reason},
        )


def graceful_shutdown(server, grace_sec=None, signame="signal"):
    """Stop in an order that does not leave anything worse behind.

    The default action for these signals is to die on the spot: no log line,
    in-flight requests cut off, a running task left to be marked orphaned
    later with a message that says nothing about why, and the listener bound
    until the process is gone -- which matters when a new one is started two
    seconds after, as bridge-restart does.

    So: close the listener first, which frees the port at once and stops new
    work arriving; then give requests and the background worker a bounded
    time to persist their outcomes; then orphan anything still running. Returns
    whether the drain finished within the grace period.

    A second call is a no-op. bridge-restart sends two signals in quick
    succession (the screen session is quit, then the process is killed), and
    the second must not repeat what the first did.
    """
    global _shutting_down, _worker_drain_deadline

    with _shutdown_guard:
        if _shutting_down:
            return True
        grace = SHUTDOWN_GRACE_SEC if grace_sec is None else grace_sec
        _worker_drain_deadline = time.monotonic() + grace
        _shutting_down = True

    if _worker_stop is not None:
        _worker_stop.set()
    _work_available.set()

    print(f"[shutdown] {signame} received; closing the listener")
    server.shutdown()
    server.server_close()

    drained = wait_for_inflight(grace)

    orphaned = orphan_running_tasks(
        f"Bridge was stopped ({signame}) while this task was running. The "
        "agent may still finish it; if it delivers a result, that result is "
        "recovered automatically."
    )

    print(
        f"[shutdown] {'drained' if drained else 'gave up draining'} in-flight "
        f"requests; {orphaned} running task(s) marked orphaned"
    )

    return drained


class Handler(BaseHTTPRequestHandler):

    # Applies to reading the request and writing the reply, not to the time the
    # bridge spends working in between, so a slow /ask is unaffected.
    timeout = REQUEST_TIMEOUT_SEC

    def log_message(self, format, *args):
        # 保留最基本日志即可。
        print(
            f"{self.client_address[0]} "
            f"{self.command} "
            f"{self.path}"
        )

    def send_json(self, obj, status=200):
        # Recorded before the reply goes out, so that a caller who has been told
        # something happened can rely on the trail saying so, and a bridge killed
        # between the two cannot have done it unrecorded.
        task_id = obj.get("task_id") if isinstance(obj, dict) else None
        self._audit(status, task_id if isinstance(task_id, str) else None)

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
        return agent_or_auto(
            validate_agent_name(values[0] if values else DEFAULT_AGENT)
        )

    def query_read_source(self):
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        values = query.get("source")

        # None, not the default: whether the caller named a source decides
        # whether a refused read may fall back to the screen.
        return validate_read_source(values[0]) if values else None

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
        with track_inflight():
            self._do_GET_guarded()

    def _audit(self, status, task_id):
        """Record this request if it is the kind worth a record.

        Anything that is not a read, and every refusal of the token. Polling
        reads are left out; they would bury the rest.
        """
        if getattr(self, "_audited", False):
            return

        self._audited = True

        if self.command == "GET" and status != 401:
            return

        try:
            record_audit(
                self.command,
                urlparse(self.path).path[:200],
                status,
                clean_client_label(self.headers.get("X-Sentinel-Client")),
                task_id,
            )
        except Exception as e:
            # A trail that cannot be written must not take the request with it.
            print(f"[audit] could not record a {self.command} request: {e}")

    def _do_GET_guarded(self):
        try:
            self._do_GET()
        except subprocess.TimeoutExpired as e:
            # Not a 500: the bridge did not break. It is up, and one call
            # behind it is stuck -- the same distinction the client draws
            # between a dead channel and a stalled request.
            self.send_json(
                {
                    "ok": False,
                    "reason": "herdr_timeout",
                    "error": (
                        f"herdr did not answer within {e.timeout:g}s. The "
                        "bridge itself is up; the herdr call behind this "
                        "endpoint is slow or hung on the host."
                    ),
                },
                504,
            )
        except AgentNotFoundError as e:
            # Not a server fault: the caller named something that is not
            # running. Say which, so they can correct it in one step.
            self.send_json(
                {"ok": False, "reason": "agent_not_found", "error": str(e)},
                404,
            )
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
            worker_alive = worker_is_healthy()

            payload = {
                "ok": True,
                "service": "nesi-sentinel-bridge",
                "version": BRIDGE_VERSION,
                # Keep the v3 field for one compatibility cycle. New clients
                # should prefer default_agent, which better describes v4.
                "agent": DEFAULT_AGENT,
                "default_agent": DEFAULT_AGENT,
                "worker_alive": worker_alive,
            }

            if worker_alive is False:
                # A bridge whose worker has died still accepts /delegate and
                # hands back a task_id, so from outside it looks perfectly
                # well. This used to answer ok:true next to worker_alive:false
                # -- a response that contradicts itself, and one that every
                # check looking only at ok would pass.
                payload["ok"] = False
                payload["reason"] = "worker_dead"
                payload["error"] = (
                    "The task worker thread has died. The bridge still accepts "
                    "requests, but queued tasks will never run. Restart it."
                )
                self.send_json(payload, 503)
                return

            self.send_json(payload)
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
                read_source = (
                    self.query_read_source() if path == "/read" else None
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
                timeout=10,
            )

            self.send_json(
                result,
                200 if result["ok"] else 500,
            )
            return

        if path == "/ready":
            # A readiness check that disagrees with what dispatch will do
            # is worse than no check. herdr happily reports a quota-blocked
            # agent as idle, so /ready used to answer ready:true and the
            # dispatch that followed was refused 429 by the circuit.
            if get_agent_lock(agent_name).locked():
                self.send_json({"ok": True, "ready": False, "reason": "locked"})
                return
            quota_block = get_agent_quota_block(agent_name)

            if quota_block:
                payload = {
                    "ok": True,
                    "ready": False,
                    "reason": "quota_blocked",
                    "kind": quota_block["kind"],
                    "detected_at": quota_block["detected_at"],
                    "detail": quota_block["detail"],
                }

                hint = circuit_hint(quota_block["kind"])
                if hint:
                    payload["hint"] = hint

                self.send_json(payload)
                return

            agent_status, status_result = get_agent_status(
                agent_name
            )

            if agent_status is None:
                # Only now is it worth asking herdr what is actually live.
                # The old code reported every failure here as
                # "sentinel_unreachable", which sent a real caller off
                # trying to restore a channel that was never down: the
                # bridge, herdr and both agents were healthy, and only a
                # rebuilt session's lost name had stopped resolving.
                try:
                    target = resolve_agent(agent_name)
                except AgentNotFoundError as e:
                    self.send_json(
                        {
                            "ok": False,
                            "ready": False,
                            "reason": "agent_not_found",
                            "error": str(e),
                        },
                        404,
                    )
                    return

                if target != agent_name:
                    agent_status, status_result = get_agent_status(target)

            if agent_status is None:
                self.send_json(
                    {
                        "ok": False,
                        "ready": False,
                        "reason": "herdr_unreachable",
                    },
                    503,
                )
                return

            payload = {
                "ok": True,
                "ready": agent_status in AVAILABLE_STATES,
                "agent_status": agent_status,
            }

            hint = ready_hint(agent_status)
            if hint:
                payload["hint"] = hint

            self.send_json(payload)
            return

        if path == "/read":
            result, used_source, note = read_terminal(
                agent_name, read_lines, source=read_source, timeout=10
            )

            # A TUI does not clear itself when a task finishes, so this
            # is routinely the leftover picture of a completed session:
            # a finished report, a "new task?" hint, and a line of
            # predicted input after the prompt that nobody ever typed.
            # Returned on its own it reads exactly like work in progress,
            # and a caller acted on that -- reporting two idle agents as
            # stuck, on the strength of an autocomplete suggestion, and
            # telling the operator to clear the windows by hand. The
            # status is what settles it, so it travels with the snapshot
            # rather than being a second call the caller must think to
            # make.
            try:
                agent_status, _ = get_agent_status(agent_name)
            except Exception:
                # /read exists to diagnose a sick agent; withholding the
                # terminal because the status call also failed would hide
                # the evidence exactly when it is most wanted.
                agent_status = None

            result = dict(result)
            result["agent"] = agent_name
            result["agent_status"] = agent_status
            result["source"] = used_source

            if note:
                result["note"] = note
            elif (
                not result["ok"]
                and read_source is not None
                and herdr_error_code(result) == "agent_not_idle"
            ):
                result["hint"] = (
                    "This agent is working, and herdr reads history only "
                    "while an agent is idle. Ask for source=visible to see "
                    "its screen now."
                )

            self.send_json(
                result,
                200 if result["ok"] else 500,
            )
            return

        if path == "/tasks":
            try:
                limit, status_filter = validate_tasks_query(
                    parse_qs(urlparse(self.path).query, keep_blank_values=True)
                )
            except (TypeError, ValueError) as e:
                self.send_json(
                    {"ok": False, "error": f"invalid request: {e}"}, 400
                )
                return

            retry_sync_completions()
            # Recover before filtering/paging, so the response matches its filter.
            adopt_all_late_results()
            tasks = [
                task_view(t)
                for t in list_tasks(limit, status_filter)
            ]

            self.send_json({
                "ok": True,
                "tasks": tasks,
            })
            return

        if path == "/audit":
            try:
                after, limit, _, _, _ = validate_events_query({
                    k: v for k, v in parse_qs(
                        urlparse(self.path).query, keep_blank_values=True
                    ).items() if k in ("after", "limit")
                })
            except (TypeError, ValueError) as e:
                self.send_json(
                    {"ok": False, "error": f"invalid request: {e}"}, 400
                )
                return

            entries = list_audit(after, limit)
            self.send_json({
                "ok": True,
                "entries": entries,
                "next": entries[-1]["seq"] if entries else after,
            })
            return

        if path == "/events":
            try:
                after, limit, wait, task_id, workflow_id = validate_events_query(
                    parse_qs(urlparse(self.path).query, keep_blank_values=True)
                )
            except (TypeError, ValueError) as e:
                self.send_json(
                    {"ok": False, "error": f"invalid request: {e}"}, 400
                )
                return

            # A long poll: hold the request until something happens or the
            # wait runs out, so a watcher is told at once without asking in a
            # tight loop.
            deadline = time.monotonic() + wait
            while True:
                events = list_events(after, limit, task_id, workflow_id)

                if events or _shutting_down or time.monotonic() >= deadline:
                    break

                time.sleep(EVENTS_POLL_SEC)

            self.send_json({
                "ok": True,
                "events": events,
                "next": events[-1]["seq"] if events else after,
            })
            return

        if path.startswith("/workflows/"):
            retry_sync_completions()
            workflow_id = unquote(path[len("/workflows/"):])
            rows = list_workflow_tasks(workflow_id)

            if not rows:
                self.send_json(
                    {"ok": False, "error": "workflow not found"}, 404
                )
                return

            waits_on = dependencies_of([row["task_id"] for row in rows])

            tasks = []
            for row in rows:
                # A late result may have arrived since anyone last looked.
                if row["status"] in LATE_RESULT_STATUSES:
                    row = adopt_late_result(row["task_id"]) or row

                view = task_view(row)
                view["depends_on"] = waits_on[row["task_id"]]
                tasks.append(view)

            counts = {}
            for view in tasks:
                counts[view["status"]] = counts.get(view["status"], 0) + 1

            self.send_json({
                "ok": True,
                "workflow_id": workflow_id,
                "state": workflow_state(counts),
                "counts": counts,
                "tasks": tasks,
            })
            return

        if path.startswith("/tasks/"):
            task_id = path[len("/tasks/"):]
            retry_sync_completions(task_id)
            task = get_task(task_id)

            if task is None:
                self.send_json(
                    {"ok": False, "error": "task not found"},
                    404,
                )
                return

            if task["status"] in LATE_RESULT_STATUSES:
                task = adopt_late_result(task_id) or task

            task = task_view(task)

            # Read here rather than in get_task(): the worker calls that
            # on every poll and has no use for progress, so it should not
            # pay a file read per dispatch. A finished task has had its
            # progress file discarded, so this is None for terminal
            # states without needing a status check.
            task["progress"] = read_progress_file(task_id)

            task["depends_on"] = list_dependencies(task_id)

            # Only while queued. A running task's reason would be stale
            # the moment it was read, and asking herdr on every poll of
            # every task would cost a call for nothing.
            task["queued_reason"] = None
            if task["status"] == "queued":
                waiting = unmet_dependencies(task_id)
                task["queued_reason"] = (
                    "Waiting for " + ", ".join(
                        f"task {t} ({status})" for t, status in waiting
                    ) + " to finish before it can start."
                    if waiting else bounded_queue_explanation(task["agent"])
                )

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
        with track_inflight():
            self._do_POST_guarded()

    def _do_POST_guarded(self):
        try:
            self._do_POST()
        except subprocess.TimeoutExpired as e:
            # Not a 500: the bridge did not break. It is up, and one call
            # behind it is stuck -- the same distinction the client draws
            # between a dead channel and a stalled request.
            self.send_json(
                {
                    "ok": False,
                    "reason": "herdr_timeout",
                    "error": (
                        f"herdr did not answer within {e.timeout:g}s. The "
                        "bridge itself is up; the herdr call behind this "
                        "endpoint is slow or hung on the host."
                    ),
                },
                504,
            )
        except AgentNotFoundError as e:
            # Not a server fault: the caller named something that is not
            # running. Say which, so they can correct it in one step.
            self.send_json(
                {"ok": False, "reason": "agent_not_found", "error": str(e)},
                404,
            )
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

        # After authentication, so a caller without the token learns nothing
        # about the limits. Refused on the header alone: the body is never read.
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1

        if length < 0:
            self.send_json({"ok": False, "error": "invalid Content-Length"}, 400)
            self.close_connection = True
            return

        if length > MAX_BODY_BYTES:
            self.send_json({
                "ok": False,
                "error": f"request body too large: the limit is {MAX_BODY_BYTES} bytes",
            }, 413)
            self.close_connection = True
            return

        if path.startswith("/tasks/") and path.endswith("/cancel"):
            # Nothing in the body is used. It is read so the connection can be
            # closed with nothing left unread, which on Linux would otherwise
            # reset it and could take the reply with it.
            try:
                self.read_json()
            except ValueError:
                pass

            task_id = path[len("/tasks/"):-len("/cancel")]
            task = get_task(task_id)

            if task is None:
                self.send_json({"ok": False, "error": "task not found"}, 404)
                return

            # Cancelling twice, or losing a race to another canceller, still
            # ends with the task cancelled -- which is what was asked for.
            if task["status"] == "cancelled" or cancel_task(task_id):
                current = "cancelled"
            else:
                current = get_task(task_id)["status"]

            if current == "cancelled":
                self.send_json({
                    "ok": True, "task_id": task_id, "status": "cancelled",
                })
                return

            self.send_json({
                "ok": False,
                "task_id": task_id,
                "status": current,
                "error": (
                    f"This task has already started or finished (status: "
                    f"{current}), and the bridge cannot stop an agent "
                    "mid-task. Only a task still queued can be cancelled."
                ),
            }, 409)
            return

        if path == "/delegate":
            try:
                body = self.read_json()

                task = body["task"].strip()
                agent_name = validate_agent_name(
                    body.get("agent", DEFAULT_AGENT)
                )

                slurm_policy = validate_slurm_policy(body.get("slurm_policy"))
                reroute = validate_reroute(body.get("reroute"))
                workflow_id = validate_workflow_id(body.get("workflow_id"))
                depends_on = validate_depends_on(body.get("depends_on"))
                idempotency_key = validate_idempotency_key(
                    body.get("idempotency_key")
                )

                timeout_ms = validate_timeout_ms(int(
                    body.get(
                        "timeout_ms",
                        21600000,  # 6 hours
                    )
                ))

                if not task:
                    raise ValueError("task cannot be empty")

                validate_task_size(task, slurm_policy)

            except Exception as e:
                self.send_json(invalid_request_payload(e), 400)
                return

            fingerprint = None
            if idempotency_key:
                # Built from the request as the caller made it -- before the
                # agent is resolved -- so a retry matches even if the live
                # agent set changed in between.
                fingerprint = request_fingerprint(
                    task, agent_name, body.get("slurm_policy"), timeout_ms,
                    reroute, workflow_id, depends_on,
                )

                # A replay is served before anything else can refuse it. The
                # original is already queued, so re-resolving its agent
                # would turn a harmless retry into a 404 for work that
                # exists, and a full queue would reject a request that
                # creates nothing.
                existing = find_task_by_idempotency_key(idempotency_key)
                if existing:
                    try:
                        check_replay(existing, fingerprint)
                    except IdempotencyConflictError as e:
                        self.send_json({"ok": False, "error": str(e)}, 422)
                        return

                    self.send_json({
                        "ok": True,
                        "task_id": existing["task_id"],
                        "status": existing["status"],
                        "replayed": True,
                    }, 202)
                    return

            try:
                agent_name = agent_or_auto(agent_name)
            except AgentNotFoundError as e:
                self.send_json(
                    {"ok": False, "reason": "agent_not_found", "error": str(e)},
                    404,
                )
                return

            try:
                task_id, replayed = enqueue_task(
                    task, timeout_ms, agent_name, slurm_policy,
                    idempotency_key, fingerprint, reroute,
                    workflow_id, depends_on,
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
            except DependencyError as e:
                self.send_json(
                    {
                        "ok": False,
                        "reason": e.reason,
                        "task_ids": e.task_ids,
                        "error": str(e),
                    },
                    422,
                )
                return
            except IdempotencyConflictError as e:
                self.send_json({"ok": False, "error": str(e)}, 422)
                return

            if replayed:
                # Lost the race to a concurrent request carrying the same
                # key; that one's task is the answer.
                self.send_json({
                    "ok": True,
                    "task_id": task_id,
                    "status": get_task(task_id)["status"],
                    "replayed": True,
                }, 202)
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

            slurm_policy = validate_slurm_policy(body.get("slurm_policy"))

            read_lines = validate_read_lines(int(
                body.get("lines", 500)
            ))

            if not task:
                raise ValueError("task cannot be empty")

            validate_task_size(task, slurm_policy)

        except Exception as e:
            self.send_json(invalid_request_payload(e), 400)
            return

        return self._run_sync_request(body, task, agent_name, timeout_ms, slurm_policy, read_lines)

    def _run_sync_request(self, body, task, agent_name, timeout_ms, slurm_policy, read_lines):
        with execution_budget(timeout_ms):
            return self._execute_sync_request(body, task, agent_name, timeout_ms, slurm_policy, read_lines)

    def _execute_sync_request(self, body, task, agent_name, timeout_ms, slurm_policy, read_lines):
        path = urlparse(self.path).path
        try:
            agent_name = agent_or_auto(agent_name)
        except AgentNotFoundError as e:
            self.send_json(
                {"ok": False, "reason": "agent_not_found", "error": str(e)},
                404,
            )
            return

        task_id = str(uuid.uuid4())
        attempt_started = False
        response_started = False

        try:
            if path == "/prompt":
                # A caller who names the agent means that agent. Moving the
                # prompt to another one acts on the wrong session -- and the
                # usual reason to prompt a specific agent is to deal with the
                # very condition that opened its circuit.
                prompt_result, used_agent = run_with_quota_failover(
                    agent_name,
                    lambda target: run_prompt_only(
                        target, task_id, task, timeout_ms, slurm_policy
                    ),
                    failover=body.get("agent") is None,
                )

                response_started = True
                self.send_json({
                    "ok": True,
                    "task_id": task_id,
                    "agent": used_agent,
                    "prompt": prompt_result,
                })
                return

            def execute(target):
                nonlocal attempt_started
                begin_sync_attempt(task_id, task, timeout_ms, agent_name, target, slurm_policy)
                attempt_started = True
                return execute_sentinel_task(
                    target, task_id, task, timeout_ms, read_lines, slurm_policy,
                )

            result_text, used_agent = run_with_quota_failover(agent_name, execute)
            note = routing_note(agent_name, used_agent)
            persistence_error = None
            try:
                if not persist_sync_completion(task_id, result_text, note):
                    persistence_error = "Task changed state before its result could be committed"
            except (sqlite3.Error, OSError) as error:
                persistence_error = str(error)
            if persistence_error is not None:
                defer_sync_completion(task_id, result_text, note)
                response_started = True
                self.send_json({
                    "ok": False, "task_id": task_id, "agent": used_agent,
                    "status": "completion_pending",
                    "error": f"Result delivered; database persistence is pending: {persistence_error}",
                    "hint": "Query this task_id later. Do not execute the task again.",
                }, 503)
                return

            result_data, _ = parse_structured_result(result_text)

            response_started = True
            self.send_json({
                "ok": True,
                "task_id": task_id,
                "agent": used_agent,
                "result": {
                    "text": result_text,
                    "format": "json" if result_data else "text",
                    "data": result_data,
                },
            })
            return

        except QuotaFailoverExhaustedError as e:
            if path == "/ask" and attempt_started:
                record_sync_failure(task_id, task, timeout_ms, agent_name, slurm_policy,
                                    "quota_exhausted", str(e))
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

        except AgentQuotaExhaustedError as e:
            # Only a request that named its agent gets here: failover would
            # have turned this into QuotaFailoverExhaustedError above.
            self.send_json(
                {
                    "ok": False,
                    "task_id": task_id,
                    "agent": e.agent_name,
                    "reason": e.reason,
                    "kind": e.kind,
                    "error": str(e),
                    "raw_output": e.raw_output,
                },
                502,
            )
            return

        except SentinelBusyError as e:
            if path == "/ask" and attempt_started:
                record_sync_failure(task_id, task, timeout_ms, agent_name, slurm_policy,
                                    "error", str(e))
            self.send_json(
                {
                    "ok": False,
                    "status": "busy",
                    **({"task_id": task_id} if attempt_started else {}),
                    "agent": agent_name,
                    "agent_status": e.agent_status,
                },
                409,
            )
            return

        except SentinelUnavailableError as e:
            if path == "/ask" and attempt_started:
                record_sync_failure(task_id, task, timeout_ms, agent_name, slurm_policy,
                                    "error", str(e))
            self.send_json(
                {
                    "ok": False,
                    "status": "unavailable",
                    **({"task_id": task_id} if attempt_started else {}),
                    "agent": agent_name,
                    "reason": "unable_to_query_sentinel",
                    "detail": str(e),
                },
                503,
            )
            return

        except TimeoutError as e:
            payload = {
                "ok": False,
                "task_id": task_id,
                "error": str(e),
            }

            # Orphaned, as the async worker does for the same failure: the
            # bridge lost track, and the agent may still be working.
            if path == "/ask" and record_sync_failure(
                task_id, task, timeout_ms, agent_name, slurm_policy,
                "orphaned", str(e),
            ):
                payload["hint"] = SYNC_RECOVERY_HINT

            self.send_json(payload, 504)
            return

        except SentinelResultMissingError as e:
            payload = {
                "ok": False,
                "task_id": task_id,
                "error": str(e),
                "raw_output": e.raw_output,
            }

            if e.reason:
                payload["reason"] = e.reason

            if path == "/ask" and record_sync_failure(
                task_id, task, timeout_ms, agent_name, slurm_policy,
                "error", str(e),
            ):
                payload["hint"] = SYNC_RECOVERY_HINT

            self.send_json(payload, 502)
            return

        except SentinelPromptError as e:
            payload = {
                "ok": False,
                "task_id": task_id,
                "error": str(e),
            }

            if e.reason:
                payload["reason"] = e.reason

            # Where the prompt may have landed, a result can still follow, so
            # leave the row it would be adopted into. A confirmed block is an
            # error; "unknown whether delivered" is lost tracking.
            if path == "/ask":
                if record_sync_failure(
                    task_id, task, timeout_ms, agent_name, slurm_policy,
                    "error" if e.reason == "blocked_confirmed" else "orphaned",
                    str(e),
                ):
                    payload["hint"] = SYNC_RECOVERY_HINT

            self.send_json(payload, 500)
            return

        except Exception as e:
            if response_started:
                raise  # a lost HTTP reply must not rewrite the execution outcome
            payload = {"ok": False, "error": f"internal error: {e}"}
            if path == "/ask" and attempt_started:
                payload["task_id"] = task_id
                if record_sync_failure(
                    task_id, task, timeout_ms, agent_name, slurm_policy,
                    "orphaned", str(e),
                ):
                    payload["hint"] = SYNC_RECOVERY_HINT
            self.send_json(payload, 500)
            return


def main():
    problems = startup_security_problems()

    # `--check` only looks. The restart script runs it before stopping the
    # running bridge, so a settings change that would be refused costs nothing.
    if "--check" in sys.argv[1:]:
        for problem in problems:
            print(f"bridge --check: REFUSED -- {problem}")
        if not problems:
            print("bridge --check: ok "
                  f"(authentication: {'on' if AUTH_TOKEN else 'OFF, by request'})")
        return 2 if problems else 0

    if problems:
        for problem in problems:
            print(f"REFUSING TO START: {problem}")
        return 2

    # Before anything is created, so the database is born private.
    for permission_warning in harden_permissions():
        print(f"WARNING: {permission_warning}")

    try:
        with bridge_instance():
            # Reserve the listener before recovery or dispatch changes any task.
            # Windows SO_REUSEADDR permits two listeners on the same address.
            server = ThreadingHTTPServer((HOST, PORT), Handler, bind_and_activate=False)
            server.allow_reuse_address = os.name != "nt"
            try:
                server.server_bind()
                server.server_activate()
                init_db()
                recovered = adopt_all_late_results()
                if recovered:
                    print(f"Recovered {recovered} late result(s) for failed or orphaned tasks")
                swept = purge_stale_result_files()
                if swept:
                    print(f"Swept {swept} result file(s) older than {RESULT_RETENTION_DAYS}d")
                for startup_warning in (auth_token_warning(), result_dir_scope_warning()):
                    if startup_warning:
                        print(startup_warning)
                serve_bridge(server)
            finally:
                server.server_close()
    except (InstanceAlreadyRunningError, OSError) as error:
        print(f"Bridge could not start: {error}")
        return 1
    return 0


def serve_bridge(server):
    global _worker_thread, _worker_stop
    _worker_stop = threading.Event()
    worker = TaskWorkerPool(_worker_stop)
    _worker_thread = worker
    worker.start()

    print(f"Sentinel Bridge v{BRIDGE_VERSION}")
    print(f"Default agent: {DEFAULT_AGENT}")
    print(f"Database: {DB_PATH}")
    print(f"Listening: http://{HOST}:{PORT}")

    shutdown_done = threading.Event()
    signalled = threading.Event()
    signal_guard = threading.Lock()

    def on_signal(signum, frame):
        # server.shutdown() waits for serve_forever() to return, and this
        # handler runs on the thread that is inside it, so it has to be called
        # from another thread or it would wait on itself.
        if not signal_guard.acquire(blocking=False):
            return  # only the first drain may release shutdown_done
        signalled.set()
        name = signal.Signals(signum).name

        def run():
            graceful_shutdown(server, signame=name)
            shutdown_done.set()

        threading.Thread(target=run, daemon=True, name="sentinel-shutdown").start()

    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            signal.signal(sig, on_signal)

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        print("\nStopping.")

    if signalled.is_set():
        shutdown_done.wait(timeout=SHUTDOWN_GRACE_SEC + 5)
    else:
        graceful_shutdown(server, signame="KeyboardInterrupt")
    _worker_stop.set()
    _work_available.set()


if __name__ == "__main__":
    sys.exit(main())

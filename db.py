"""SQLite (WAL mode) job store. Every stage output is committed here before
the job advances, so a crash-restart resumes from the last completed stage
instead of re-running the whole pipeline.

Two rules keep the state machine honest when the scheduler and the Telegram
poller (separate processes) touch the same row:

* Every status change goes through `claim_job` / `transition`, which are single
  conditional UPDATEs ("only if the row is still in the status I read"). A stale
  read can therefore never overwrite a newer state — a timeout sweep cannot fail a
  job the human just approved, and a job cannot be claimed twice.
* Timestamps are stored in one fixed format (`to_iso`) so that comparing them as
  strings in SQL is the same as comparing them as times.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional, Sequence

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    status            TEXT NOT NULL DEFAULT 'PENDING',
    current_stage     TEXT NOT NULL,
    input_payload     TEXT NOT NULL,           -- JSON
    stage_outputs     TEXT NOT NULL DEFAULT '{}',  -- JSON: {stage: output}
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    next_retry_at     TEXT,                    -- ISO8601 UTC
    paused            INTEGER NOT NULL DEFAULT 0,
    pause_requested   INTEGER NOT NULL DEFAULT 0,
    pending_question  TEXT,                    -- JSON: {question, options}
    pending_payload   TEXT,                    -- JSON: stage draft awaiting approval
    notified          TEXT NOT NULL DEFAULT '[]',  -- JSON list of sent event keys
    last_error        TEXT,
    locked_by         TEXT,                    -- "host:pid" of the worker running the stage
    locked_at         TEXT,                    -- ISO8601 UTC: when it claimed the job (the lease start)
    wait_since        TEXT,                    -- ISO8601 UTC: first moment of the current unbroken wait
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stage_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER NOT NULL REFERENCES jobs(id),
    stage        TEXT NOT NULL,
    attempt      INTEGER NOT NULL,
    status       TEXT NOT NULL,   -- success | error | rate_limited | busy
    provider     TEXT,
    duration_ms  INTEGER,
    error        TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stage_runs_job ON stage_runs(job_id);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns added after the first release. CREATE TABLE IF NOT EXISTS won't touch an existing
# table, so init_db adds any that an older database is missing.
ADDED_COLUMNS = {"locked_by": "TEXT", "locked_at": "TEXT", "wait_since": "TEXT"}

COLUMNS = {
    "id", "status", "current_stage", "input_payload", "stage_outputs", "attempt_count", "next_retry_at",
    "paused", "pause_requested", "pending_question", "pending_payload", "notified", "last_error",
    "created_at", "updated_at", *ADDED_COLUMNS,
}
JSON_FIELDS = {"input_payload", "stage_outputs", "pending_question", "pending_payload", "notified"}


def to_iso(dt: datetime) -> str:
    """The only timestamp format stored: UTC, fixed width, always with microseconds. Fixed width is
    what makes `<=` on these strings agree with time order (isoformat() drops the fraction at .000000,
    and SQLite's datetime() uses a space where isoformat() uses 'T')."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def now_iso() -> str:
    return to_iso(datetime.now(timezone.utc))


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        for column, decl in ADDED_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {decl}")


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    for field in JSON_FIELDS:
        raw = job.get(field)
        job[field] = json.loads(raw) if raw else ({} if field != "notified" else [])
    return job


def _check_columns(fields: dict[str, Any]) -> None:
    unknown = set(fields) - COLUMNS
    if unknown:
        raise ValueError(f"unknown jobs column(s): {', '.join(sorted(unknown))}")


def _assignments(fields: dict[str, Any]) -> tuple[str, list[Any]]:
    """`col = ?, ...` and its values. Column names are checked against COLUMNS first, so nothing
    caller-supplied ever reaches the SQL text."""
    _check_columns(fields)
    columns, values = [], []
    for key, value in fields.items():
        if key in JSON_FIELDS and value is not None and not isinstance(value, str):
            value = json.dumps(value)
        columns.append(f"{key} = ?")
        values.append(value)
    return ", ".join(columns), values


def create_job(input_payload: dict[str, Any], first_stage: str) -> int:
    now = now_iso()
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO jobs
               (status, current_stage, input_payload, stage_outputs, notified, created_at, updated_at)
               VALUES ('PENDING', ?, ?, '{}', '[]', ?, ?)""",
            (first_stage, json.dumps(input_payload), now, now),
        )
        return cur.lastrowid


def get_job(job_id: int) -> Optional[dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None


def update_job(job_id: int, **fields: Any) -> None:
    """Unconditional write. Prefer `transition` for anything that changes `status`."""
    if not fields:
        return
    assignments, values = _assignments({**fields, "updated_at": now_iso()})
    with get_connection() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", [*values, job_id])


def transition(job_id: int, from_status: str, *, clear_notified_prefix: Optional[str] = None, **fields: Any) -> bool:
    """Apply `fields` iff the job is still in `from_status`; True if it was applied.

    All-or-nothing, so a crash cannot leave a half-applied change. `clear_notified_prefix` drops
    matching event keys from `notified` inside the same write (regenerate uses it so the next draft
    is announced again).
    """
    _check_columns(fields)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if clear_notified_prefix is not None:
            row = conn.execute("SELECT notified FROM jobs WHERE id = ? AND status = ?", (job_id, from_status)).fetchone()
            if row is None:
                return False
            fields = {**fields, "notified": [k for k in json.loads(row["notified"] or "[]") if not k.startswith(clear_notified_prefix)]}
        assignments, values = _assignments({**fields, "updated_at": now_iso()})
        cur = conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ? AND status = ?", [*values, job_id, from_status])
        return cur.rowcount == 1


def claim_job(job_id: int, owner: str) -> bool:
    """Atomically move a runnable job to RUNNING and record who holds it. Runnable means PENDING, or
    SCHEDULED with its retry time reached (or unset), and not paused. False if it isn't, or if
    another worker got there first."""
    now = now_iso()
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE jobs SET status = 'RUNNING', locked_by = ?, locked_at = ?, updated_at = ?
               WHERE id = ? AND paused = 0
                 AND (status = 'PENDING'
                      OR (status = 'SCHEDULED' AND (next_retry_at IS NULL OR next_retry_at <= ?)))""",
            (owner, now, now, job_id, now),
        )
        return cur.rowcount == 1


def due_jobs() -> list[dict[str, Any]]:
    now = now_iso()
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE paused = 0
                 AND (status = 'PENDING'
                      OR (status = 'SCHEDULED' AND (next_retry_at IS NULL OR next_retry_at <= ?)))
               ORDER BY created_at ASC""",
            (now,),
        ).fetchall()
        return [_row_to_job(r) for r in rows]


def running_jobs() -> list[dict[str, Any]]:
    with get_connection() as conn:
        return [_row_to_job(r) for r in conn.execute("SELECT * FROM jobs WHERE status = 'RUNNING' ORDER BY id").fetchall()]


def jobs_with_status(statuses: Sequence[str]) -> list[dict[str, Any]]:
    marks = ", ".join("?" for _ in statuses)
    with get_connection() as conn:
        rows = conn.execute(f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY id", tuple(statuses)).fetchall()
        return [_row_to_job(r) for r in rows]


def stale_needs_input_jobs(timeout_hours: int) -> list[dict[str, Any]]:
    cutoff = to_iso(datetime.now(timezone.utc) - timedelta(hours=timeout_hours))
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = 'NEEDS_INPUT' AND updated_at <= ?",
            (cutoff,),
        ).fetchall()
        return [_row_to_job(r) for r in rows]


def already_notified(job: dict[str, Any], event_key: str) -> bool:
    return event_key in job.get("notified", [])


def mark_notified(job_id: int, event_key: str) -> None:
    """Read-modify-write inside one write transaction: the scheduler and the poller both call this,
    and a plain read-then-update let one overwrite the other's key."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT notified FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return
        keys = json.loads(row["notified"] or "[]")
        if event_key in keys:
            return
        keys.append(event_key)
        conn.execute("UPDATE jobs SET notified = ?, updated_at = ? WHERE id = ?", (json.dumps(keys), now_iso(), job_id))


def log_stage_run(
    job_id: int,
    stage: str,
    attempt: int,
    status: str,
    started_at: str,
    finished_at: str,
    provider: Optional[str] = None,
    duration_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO stage_runs
               (job_id, stage, attempt, status, provider, duration_ms, error, started_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, stage, attempt, status, provider, duration_ms, error, started_at, finished_at),
        )


def get_kv(key: str) -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_kv(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

"""SQLite (WAL mode) job store. Every stage output is committed here before
the job advances, so a crash-restart resumes from the last completed stage
instead of re-running the whole pipeline.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

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
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stage_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER NOT NULL REFERENCES jobs(id),
    stage        TEXT NOT NULL,
    attempt      INTEGER NOT NULL,
    status       TEXT NOT NULL,   -- success | error | rate_limited
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

JSON_FIELDS = {"input_payload", "stage_outputs", "pending_question", "pending_payload", "notified"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    for field in JSON_FIELDS:
        raw = job.get(field)
        job[field] = json.loads(raw) if raw else ({} if field != "notified" else [])
    return job


def create_job(input_payload: dict[str, Any], first_stage: str) -> int:
    now = _now()
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
    if not fields:
        return
    fields["updated_at"] = _now()
    columns = []
    values = []
    for key, value in fields.items():
        if key in JSON_FIELDS and value is not None and not isinstance(value, str):
            value = json.dumps(value)
        columns.append(f"{key} = ?")
        values.append(value)
    values.append(job_id)
    with get_connection() as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(columns)} WHERE id = ?", values)


def due_jobs() -> list[dict[str, Any]]:
    now = _now()
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


def stale_needs_input_jobs(timeout_hours: int) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE status = 'NEEDS_INPUT'
                 AND updated_at <= datetime('now', ? || ' hours')""",
            (f"-{timeout_hours}",),
        ).fetchall()
        return [_row_to_job(r) for r in rows]


def already_notified(job: dict[str, Any], event_key: str) -> bool:
    return event_key in job.get("notified", [])


def mark_notified(job_id: int, event_key: str) -> None:
    job = get_job(job_id)
    if job is None:
        return
    notified = job["notified"]
    if event_key not in notified:
        notified.append(event_key)
        update_job(job_id, notified=notified)


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

"""Runs one job through its current stage, and resumes a NEEDS_INPUT job
once a Telegram button tap has answered it. Called by scheduler.py and
telegram_poller.py — never call stage functions directly from anywhere else,
or stage_runs/notified bookkeeping goes stale.
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from typing import Any

import config
import db
import notifier
import rate_limiter
from exceptions import HumanInputRequired, RateLimitError
from pipeline import STAGE_FUNCTIONS, next_stage_after


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_job(job_id: int) -> None:
    job = db.get_job(job_id)
    if job is None or job["status"] != "PENDING":
        return  # stale dispatch — another poll cycle already moved it

    db.update_job(job_id, status="RUNNING")
    stage = job["current_stage"]
    attempt = job["attempt_count"] + 1
    started_at = _now_iso()
    stage_fn = STAGE_FUNCTIONS[stage]

    try:
        t0 = time.monotonic()
        result = stage_fn(job, job["stage_outputs"])
        duration_ms = int((time.monotonic() - t0) * 1000)

    except RateLimitError as exc:
        finished_at = _now_iso()
        retry_at = rate_limiter.compute_rate_limit_retry_at(exc.provider, explicit_retry_at=exc.retry_at)
        db.log_stage_run(job_id, stage, attempt, "rate_limited", started_at, finished_at,
                          provider=exc.provider, error=str(exc))
        db.update_job(job_id, status="SCHEDULED", next_retry_at=retry_at.isoformat(), last_error=str(exc))
        notifier.notify_rate_limited(db.get_job(job_id), exc.provider, retry_at.isoformat())
        return

    except HumanInputRequired as exc:
        finished_at = _now_iso()
        db.log_stage_run(job_id, stage, attempt, "success", started_at, finished_at)
        db.update_job(
            job_id,
            status="NEEDS_INPUT",
            attempt_count=0,
            pending_question={"question": exc.question, "options": exc.options},
            pending_payload=exc.payload,
        )
        notifier.notify_input_required(db.get_job(job_id))
        return

    except Exception as exc:  # noqa: BLE001 — deliberately broad: any other failure gets backoff
        finished_at = _now_iso()
        error_text = f"{exc}\n{traceback.format_exc()}"
        db.log_stage_run(job_id, stage, attempt, "error", started_at, finished_at, error=str(exc))
        if attempt >= config.MAX_STAGE_ATTEMPTS:
            db.update_job(job_id, status="FAILED", attempt_count=attempt, last_error=error_text)
            notifier.notify_failed(db.get_job(job_id))
        else:
            retry_at = rate_limiter.compute_generic_backoff_retry_at(attempt)
            db.update_job(job_id, status="SCHEDULED", attempt_count=attempt,
                           next_retry_at=retry_at.isoformat(), last_error=error_text)
        return

    finished_at = _now_iso()
    db.log_stage_run(job_id, stage, attempt, "success", started_at, finished_at, duration_ms=duration_ms)
    outputs = job["stage_outputs"]
    outputs[stage] = result
    _advance(job_id, stage, outputs)


def _advance(job_id: int, completed_stage: str, outputs: dict[str, Any]) -> None:
    next_stage = next_stage_after(completed_stage)
    if next_stage is None:
        db.update_job(job_id, status="COMPLETED", stage_outputs=outputs, attempt_count=0)
        notifier.notify_completed(db.get_job(job_id))
    else:
        db.update_job(job_id, status="PENDING", current_stage=next_stage, stage_outputs=outputs, attempt_count=0)


def resume_from_input(job_id: int, answer: str) -> None:
    """Called by telegram_poller.py when a job:<id>:<answer> button is tapped."""
    job = db.get_job(job_id)
    if job is None or job["status"] != "NEEDS_INPUT":
        return  # stale button on an already-resolved job — ignore

    if answer == "approve":
        outputs = job["stage_outputs"]
        outputs[job["current_stage"]] = job["pending_payload"]
        db.update_job(job_id, pending_question=None, pending_payload=None)
        _advance(job_id, job["current_stage"], outputs)
    elif answer == "regenerate":
        db.update_job(job_id, status="PENDING", pending_question=None, pending_payload=None, attempt_count=0)
    else:
        print(f"[worker] job #{job_id}: unrecognized answer {answer!r}, ignoring")

"""Runs one job through its current stage, resumes a NEEDS_INPUT job once a
Telegram button tap has answered it, and reclaims jobs whose worker died.
Called by scheduler.py and telegram_poller.py — never call stage functions
directly from anywhere else, or stage_runs/notified bookkeeping goes stale.

State changes are all "only if the job is still in the state I read"
(db.claim_job / db.transition), so the scheduler, the poller and a timeout
sweep can race without one overwriting another.
"""
from __future__ import annotations

import os
import socket
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import config
import db
import notifier
import pipeline
import rate_limiter
from exceptions import HumanInputRequired, PermanentStageError, RateLimitError, StageBusy, StageInProgress, UpstreamUnavailable
from pipeline import STAGE_FUNCTIONS, next_stage_after

# Where a job goes when ContentPipe says the identical run is still in flight and gives no Retry-After.
DEFAULT_BUSY_WAIT_SECONDS = 30
# Between two calls of a multi-call stage (ContentRender stopped at its time budget) when it names no time.
DEFAULT_PROGRESS_WAIT_SECONDS = 30

# Cleared whenever a job leaves RUNNING, so a stale lock never lingers on a finished row.
UNLOCK = {"locked_by": None, "locked_at": None}


def lock_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    return True


def notify_safely(fn: Callable[[dict[str, Any]], Any], job_id: int) -> None:
    """A notification must never undo or strand a state change that is already committed. Failed
    sends are retried by notifier.resend_missed_notifications(), so swallowing here loses nothing."""
    try:
        job = db.get_job(job_id)
        if job is not None:
            fn(job)
    except Exception as exc:  # noqa: BLE001
        print(f"[worker] job #{job_id}: {getattr(fn, '__name__', 'notification')} failed: {notifier.redact_secrets(str(exc))}")


# ------------------------------------------------------------------------------------ running a stage

def run_job(job_id: int) -> None:
    if not db.claim_job(job_id, lock_owner()):
        return  # not runnable: another poll already took it, it finished, it is paused, or its retry time hasn't come

    job = db.get_job(job_id)
    stage = job["current_stage"]
    attempt = job["attempt_count"] + 1
    started_at = db.now_iso()

    try:
        stage_fn = STAGE_FUNCTIONS[stage]  # inside the try: an unknown stage must fail the attempt, not strand the job in RUNNING
        t0 = time.monotonic()
        result = stage_fn(job, job["stage_outputs"])
        duration_ms = int((time.monotonic() - t0) * 1000)

    except RateLimitError as exc:
        retry_at = rate_limiter.compute_rate_limit_retry_at(exc.provider, explicit_retry_at=exc.retry_at)
        db.log_stage_run(job_id, stage, attempt, "rate_limited", started_at, db.now_iso(), provider=exc.provider, error=str(exc))
        if _wait(job, retry_at, f"rate limited by {exc.provider}: {exc}"):
            notify_safely(lambda j: notifier.notify_rate_limited(j, exc.provider, db.to_iso(retry_at)), job_id)
        return

    except StageBusy as exc:
        retry_at = exc.retry_at or datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_BUSY_WAIT_SECONDS)
        db.log_stage_run(job_id, stage, attempt, "busy", started_at, db.now_iso(), error=str(exc))
        _wait(job, retry_at, f"stage busy: {exc}")  # no attempt consumed, nobody paged
        return

    except StageInProgress as exc:
        retry_at = exc.retry_at or datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_PROGRESS_WAIT_SECONDS)
        db.log_stage_run(job_id, stage, attempt, "in_progress", started_at, db.now_iso(), error=str(exc))
        # Real progress, not a wait: no attempt consumed, nobody paged, and the MAX_WAIT_DAYS clock is cleared.
        db.transition(job_id, "RUNNING", status="SCHEDULED", attempt_count=0, next_retry_at=db.to_iso(retry_at),
                      last_error=None, wait_since=None, **UNLOCK)
        return

    except UpstreamUnavailable as exc:
        retry_at = _overload_retry_at(job, exc.retry_at)
        db.log_stage_run(job_id, stage, attempt, "unavailable", started_at, db.now_iso(), provider=exc.provider, error=str(exc))
        if _wait(job, retry_at, f"upstream unavailable: {exc}"):
            since = _parse_iso(job.get("wait_since"))
            waited = (datetime.now(timezone.utc) - since).total_seconds() if since else 0.0
            if waited >= notifier.LONG_WAIT_SECONDS:  # a blip that clears in a minute is not worth a page
                notify_safely(lambda j: notifier.notify_upstream_unavailable(j, waited), job_id)
        return

    except HumanInputRequired as exc:
        db.log_stage_run(job_id, stage, attempt, "success", started_at, db.now_iso())
        db.transition(
            job_id, "RUNNING",
            status="NEEDS_INPUT", attempt_count=0, wait_since=None, **UNLOCK,
            pending_question={"question": exc.question, "options": exc.options},
            pending_payload=exc.payload,
        )
        notify_safely(notifier.notify_input_required, job_id)
        return

    except PermanentStageError as exc:
        db.log_stage_run(job_id, stage, attempt, "error", started_at, db.now_iso(), error=str(exc))
        _fail(job_id, attempt, str(exc))
        return

    except Exception as exc:  # noqa: BLE001 — deliberately broad: any other failure gets backoff
        error_text = f"{exc}\n{traceback.format_exc()}"
        db.log_stage_run(job_id, stage, attempt, "error", started_at, db.now_iso(), error=str(exc))
        if attempt >= config.MAX_STAGE_ATTEMPTS:
            _fail(job_id, attempt, error_text)
        else:
            retry_at = rate_limiter.compute_generic_backoff_retry_at(attempt)
            db.transition(job_id, "RUNNING", status="SCHEDULED", attempt_count=attempt,
                          next_retry_at=db.to_iso(retry_at), last_error=error_text, **UNLOCK)
        return

    db.log_stage_run(job_id, stage, attempt, "success", started_at, db.now_iso(), duration_ms=duration_ms)
    outputs = job["stage_outputs"]
    outputs[stage] = result
    _advance(job_id, stage, outputs, from_status="RUNNING")


def _wait(job: dict[str, Any], retry_at: datetime, reason: str) -> bool:
    """Park a RUNNING job until `retry_at` without consuming an attempt. Gives up (FAILED) once the
    job has been waiting continuously for MAX_WAIT_DAYS. True if it was parked, False if it failed."""
    now = datetime.now(timezone.utc)
    since = _parse_iso(job.get("wait_since")) or now
    if now - since > timedelta(days=config.MAX_WAIT_DAYS):
        _fail(job["id"], job["attempt_count"], f"Gave up after waiting more than {config.MAX_WAIT_DAYS} days. Last reason: {reason}")
        return False
    db.transition(job["id"], "RUNNING", status="SCHEDULED", next_retry_at=db.to_iso(retry_at),
                  last_error=reason, wait_since=db.to_iso(since), **UNLOCK)
    return True


def _overload_retry_at(job: dict[str, Any], hinted: Optional[datetime]) -> datetime:
    """When to re-poll a ContentPipe that reported its providers overloaded. Starts at its own
    Retry-After (30 s) and stretches to half the outage's age, capped at OVERLOAD_MAX_WAIT_SECONDS:
    quick to notice recovery, quiet through a long one. Never earlier than what ContentPipe asked."""
    now = datetime.now(timezone.utc)
    since = _parse_iso(job.get("wait_since")) or now
    grown = now + timedelta(seconds=min((now - since).total_seconds() / 2, config.OVERLOAD_MAX_WAIT_SECONDS))
    return max(hinted or now + timedelta(seconds=DEFAULT_BUSY_WAIT_SECONDS), grown)


def _fail(job_id: int, attempt: int, error: str) -> None:
    if db.transition(job_id, "RUNNING", status="FAILED", attempt_count=attempt, last_error=error, **UNLOCK):
        notify_safely(notifier.notify_failed, job_id)


def _advance(job_id: int, completed_stage: str, outputs: dict[str, Any], from_status: str, **extra: Any) -> bool:
    """Commit a finished stage's output and move on — one atomic write, so a crash cannot leave the
    output stored without the stage advanced (or the reverse)."""
    next_stage = next_stage_after(completed_stage)
    common = dict(stage_outputs=outputs, attempt_count=0, wait_since=None, **UNLOCK, **extra)
    if next_stage is None:
        applied = db.transition(job_id, from_status, status="COMPLETED", **common)
        if applied:
            notify_safely(notifier.notify_completed, job_id)
        return applied
    return db.transition(job_id, from_status, status="PENDING", current_stage=next_stage, **common)


# ------------------------------------------------------------------------------------ crash recovery

def _lock_is_dead(job: dict[str, Any], now: datetime) -> bool:
    """A RUNNING job is orphaned if its worker is gone. Two independent signals:
    the lease ran out (covers pid reuse after a reboot, and locks from another machine), or the
    lock names a pid on this host that no longer exists (covers a launchd restart within seconds)."""
    locked_at = _parse_iso(job.get("locked_at")) or _parse_iso(job.get("updated_at"))
    if locked_at is None or (now - locked_at).total_seconds() > config.RUNNING_LEASE_SECONDS:
        return True
    host, _, pid_text = (job.get("locked_by") or "").rpartition(":")
    if host == socket.gethostname() and pid_text.isdigit():
        pid = int(pid_text)
        # Our own pid: reclaim runs between jobs on the scheduler thread, so nothing of ours is mid-stage.
        return pid == os.getpid() or not _pid_alive(pid)
    return False


def reclaim_orphaned_jobs() -> list[int]:
    """Requeue RUNNING jobs whose worker died (kill -9, launchd restart, machine slept and was killed).
    Without this they sat in RUNNING forever: due_jobs() never returns that status.

    The crash counts as an attempt, so a job that keeps killing its worker ends FAILED instead of
    looping. Re-running the stage is safe: ContentPipe resumes an interrupted /api/script run from its
    journal on the identical request, and answers 409 (handled as StageBusy) if it is still going.
    """
    now = datetime.now(timezone.utc)
    reclaimed: list[int] = []
    for job in db.running_jobs():
        if not _lock_is_dead(job, now):
            continue
        job_id, attempt = job["id"], job["attempt_count"] + 1
        reason = f"Orphaned in RUNNING: worker {job.get('locked_by') or 'unknown'} died mid-stage"
        exhausted = attempt >= config.MAX_STAGE_ATTEMPTS
        if not db.transition(job_id, "RUNNING", status="FAILED" if exhausted else "PENDING",
                             attempt_count=attempt, last_error=reason, **UNLOCK):
            continue  # it finished (or was reclaimed) while we looked
        db.log_stage_run(job_id, job["current_stage"], attempt, "error", job.get("locked_at") or db.now_iso(), db.now_iso(), error=reason)
        print(f"[worker] job #{job_id}: {reason}; {'failing it' if exhausted else 're-queued'}")
        reclaimed.append(job_id)
        if exhausted:
            notify_safely(notifier.notify_failed, job_id)
    return reclaimed


# ------------------------------------------------------------------------------------ human answers

def resume_from_input(job_id: int, answer: str) -> bool:
    """Called by telegram_poller.py when a job:<id>:<answer> button is tapped.
    True if the answer was applied; False if it was ignored (stale tap, unknown answer, nothing to approve)."""
    job = db.get_job(job_id)
    if job is None or job["status"] != "NEEDS_INPUT":
        return False  # stale button on an already-resolved job

    if answer == "approve":
        draft = job["pending_payload"]
        if not draft:
            print(f"[worker] job #{job_id}: approve ignored — no stored draft to commit")
            return False
        outputs = job["stage_outputs"]
        hook = pipeline.ON_APPROVE.get(job["current_stage"])
        if hook is not None and not _run_hook(job_id, "approve", hook, job, outputs):
            return False
        outputs[job["current_stage"]] = draft
        return _advance(job_id, job["current_stage"], outputs, from_status="NEEDS_INPUT",
                        pending_question=None, pending_payload=None)

    if answer == "regenerate":
        hook = pipeline.ON_REGENERATE.get(job["current_stage"])
        if hook is not None and not _run_hook(job_id, "regenerate", hook, job, job["stage_outputs"]):
            return False
        # Forget the approval announcement too: its dedupe key is otherwise identical for the next
        # draft, which would then never be sent.
        return db.transition(job_id, "NEEDS_INPUT", clear_notified_prefix="needs_input:",
                             status="PENDING", pending_question=None, pending_payload=None, attempt_count=0)

    print(f"[worker] job #{job_id}: unrecognized answer {answer!r}, ignoring")
    return False


def _run_hook(job_id: int, what: str, hook: Callable[[dict[str, Any], dict[str, Any]], None], job: dict[str, Any], outputs: dict[str, Any]) -> bool:
    """A human decision that ContentRender must also hear. If it cannot be delivered the job stays exactly where it
    is, still waiting, so tapping the same button again retries — nothing is half-applied."""
    try:
        hook(job, outputs)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[worker] job #{job_id}: {what} could not reach ContentRender, job left waiting: {notifier.redact_secrets(str(exc))}")
        return False


REGEN_KINDS = {"images": "still", "narration": "narration"}


def regenerate_scenes(job_id: int, scenes: list[int]) -> tuple[bool, str]:
    """Telegram `/regen <job> <scene numbers>`: redo just those scenes at the images or narration checkpoint, then
    run the stage again. Returns (applied, message for the human)."""
    job = db.get_job(job_id)
    if job is None:
        return False, f"No job #{job_id}."
    kind = REGEN_KINDS.get(job["current_stage"])
    if job["status"] != "NEEDS_INPUT" or kind is None:
        return False, f"Job #{job_id} is not waiting at an images or narration checkpoint."
    try:
        pipeline.regenerate_scenes(job, job["stage_outputs"], kind, scenes)
    except Exception as exc:  # noqa: BLE001
        print(f"[worker] job #{job_id}: /regen failed: {notifier.redact_secrets(str(exc))}")
        return False, f"ContentRender refused: {notifier.redact_secrets(str(exc))[:300]}"
    applied = db.transition(job_id, "NEEDS_INPUT", clear_notified_prefix="needs_input:",
                            status="PENDING", pending_question=None, pending_payload=None, attempt_count=0)
    label = "stills" if kind == "still" else "narration"
    return applied, f"Redoing {label} for scene{'s' if len(scenes) != 1 else ''} {', '.join(str(n) for n in scenes)} on job #{job_id}." if applied else f"Job #{job_id} changed while I was working; nothing was applied."

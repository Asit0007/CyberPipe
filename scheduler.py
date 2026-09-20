"""Polling loop: reclaims jobs whose worker died, dispatches due jobs (PENDING, or
SCHEDULED past next_retry_at), fails NEEDS_INPUT jobs that timed out, and re-sends
Telegram messages that never got through. Run this and telegram_poller.py as two
long-running processes — see CLAUDE.md for the launchd deployment.
"""
from __future__ import annotations

import time
from typing import Callable

import config
import db
import notifier
import worker


def _handle_stale_needs_input() -> None:
    for job in db.stale_needs_input_jobs(config.NEEDS_INPUT_TIMEOUT_HOURS):
        # Conditional on the status the sweep read: a tap that landed in between wins.
        failed = db.transition(
            job["id"], "NEEDS_INPUT",
            status="FAILED", last_error=f"NEEDS_INPUT timed out after {config.NEEDS_INPUT_TIMEOUT_HOURS}h",
        )
        if failed:
            worker.notify_safely(notifier.notify_failed, job["id"])


def _guarded(label: str, fn: Callable[[], object]) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — one failing duty must not stop the others
        print(f"[scheduler] {label} raised: {notifier.redact_secrets(str(exc))}")


def tick() -> None:
    _guarded("orphan reclaim", worker.reclaim_orphaned_jobs)
    for job in db.due_jobs():
        print(f"[scheduler] running job #{job['id']} stage={job['current_stage']}")
        _guarded(f"job #{job['id']}", lambda job_id=job["id"]: worker.run_job(job_id))
    _guarded("stale NEEDS_INPUT sweep", _handle_stale_needs_input)
    _guarded("notification resend", notifier.resend_missed_notifications)


def main() -> None:
    db.init_db()
    print(f"[scheduler] started, polling every {config.POLL_INTERVAL_SECONDS}s "
          f"(db={config.DB_PATH}, contentpipe={config.CONTENTPIPE_BASE_URL})")
    while True:
        try:
            tick()
        except Exception as exc:  # noqa: BLE001 — the loop must survive a single bad tick
            print(f"[scheduler] tick raised: {exc}")
        time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

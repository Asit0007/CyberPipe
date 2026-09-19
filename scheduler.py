"""Polling loop: dispatches due jobs (PENDING, or SCHEDULED past
next_retry_at) and fails NEEDS_INPUT jobs that timed out. Run this and
telegram_poller.py as two long-running processes — see CLAUDE.md for the
Mac-vs-VPS deployment decision that's still open.
"""
from __future__ import annotations

import time

import config
import db
import notifier
import worker


def _handle_stale_needs_input() -> None:
    for job in db.stale_needs_input_jobs(config.NEEDS_INPUT_TIMEOUT_HOURS):
        db.update_job(
            job["id"],
            status="FAILED",
            last_error=f"NEEDS_INPUT timed out after {config.NEEDS_INPUT_TIMEOUT_HOURS}h",
        )
        notifier.notify_failed(db.get_job(job["id"]))


def tick() -> None:
    for job in db.due_jobs():
        print(f"[scheduler] running job #{job['id']} stage={job['current_stage']}")
        worker.run_job(job["id"])
    _handle_stale_needs_input()


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

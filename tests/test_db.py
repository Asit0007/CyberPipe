"""db.py: timestamp comparison, atomic transitions, migration, and lost-update safety."""
from __future__ import annotations

import sqlite3
import threading
import unittest
from datetime import datetime, timedelta, timezone

import config
import db
from tests.support import DbTestCase


class StaleNeedsInputTests(DbTestCase):
    def stale_ids(self, hours: int = 72) -> list[int]:
        return [j["id"] for j in db.stale_needs_input_jobs(hours)]

    def job_updated(self, ago_kwargs: dict, status: str = "NEEDS_INPUT", iso: str | None = None) -> int:
        job_id = self.make_job("script", status=status)
        self.set_raw(job_id, updated_at=iso or self.ago(**ago_kwargs))
        return job_id

    def test_job_just_past_the_timeout_is_flagged(self):
        """updated_at is ISO with a 'T'; SQLite's datetime() uses a space. 'T' > ' ', so a job stayed
        'fresh' for up to 24h past the timeout (73h-old was NOT flagged, 90h-old was)."""
        job_id = self.job_updated({"hours": 73})
        self.assertIn(job_id, self.stale_ids())

    def test_job_just_inside_the_timeout_is_not_flagged(self):
        job_id = self.job_updated({"hours": 71})
        self.assertNotIn(job_id, self.stale_ids())

    def test_timestamp_without_fractional_seconds_compares_correctly(self):
        iso = (datetime.now(timezone.utc) - timedelta(hours=73)).replace(microsecond=0).isoformat()
        self.assertNotIn(".", iso)
        self.assertIn(self.job_updated({}, iso=iso), self.stale_ids())

    def test_only_needs_input_jobs_are_considered(self):
        job_id = self.job_updated({"hours": 200}, status="PENDING")
        self.assertNotIn(job_id, self.stale_ids())


class IsoFormatTests(unittest.TestCase):
    def test_every_stored_timestamp_has_one_fixed_shape(self):
        for dt in (datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc),
                   datetime(2026, 9, 20, 12, 0, 0, 500, tzinfo=timezone.utc)):
            self.assertRegex(db.to_iso(dt), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00$")

    def test_string_order_equals_time_order(self):
        base = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
        stamps = [base + timedelta(seconds=s, microseconds=us) for s in (0, 1, 59) for us in (0, 1, 999999)]
        self.assertEqual(sorted(db.to_iso(d) for d in stamps), [db.to_iso(d) for d in sorted(stamps)])

    def test_other_timezones_are_normalised_to_utc_and_naive_is_treated_as_utc(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        self.assertEqual(db.to_iso(datetime(2026, 9, 20, 17, 30, tzinfo=ist)), db.to_iso(datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)))
        self.assertEqual(db.to_iso(datetime(2026, 9, 20, 12, 0)), db.to_iso(datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)))


class TransitionTests(DbTestCase):
    def test_applies_every_field_when_the_status_matches(self):
        job_id = self.make_job("script", status="NEEDS_INPUT", pending_payload={"a": 1})
        self.assertTrue(db.transition(job_id, "NEEDS_INPUT", status="COMPLETED", pending_payload=None, stage_outputs={"script": {"a": 1}}))
        job = db.get_job(job_id)
        self.assertEqual((job["status"], job["stage_outputs"]), ("COMPLETED", {"script": {"a": 1}}))

    def test_changes_nothing_when_the_status_has_moved_on(self):
        job_id = self.make_job("script", status="COMPLETED")
        self.assertFalse(db.transition(job_id, "NEEDS_INPUT", status="FAILED", last_error="x"))
        job = db.get_job(job_id)
        self.assertEqual((job["status"], job["last_error"]), ("COMPLETED", None))

    def test_clear_notified_prefix_is_part_of_the_same_write(self):
        job_id = self.make_job("script", status="NEEDS_INPUT", notified=["needs_input:script:0", "completed"])
        db.transition(job_id, "NEEDS_INPUT", clear_notified_prefix="needs_input:", status="PENDING")
        job = db.get_job(job_id)
        self.assertEqual((job["status"], job["notified"]), ("PENDING", ["completed"]))

    def test_unknown_column_is_rejected_and_nothing_is_written(self):
        job_id = self.make_job("script", status="NEEDS_INPUT")
        with self.assertRaises(ValueError):
            db.transition(job_id, "NEEDS_INPUT", status="FAILED", not_a_column=1)
        self.assertEqual(db.get_job(job_id)["status"], "NEEDS_INPUT")

    def test_update_job_also_rejects_unknown_columns(self):
        job_id = self.make_job("script")
        with self.assertRaises(ValueError):
            db.update_job(job_id, **{"status = 'FAILED', id": 1})


class ClaimTests(DbTestCase):
    def test_pending_and_due_scheduled_jobs_are_claimable(self):
        pending = self.make_job("script")
        due = self.make_job("script", status="SCHEDULED", next_retry_at=self.ago(minutes=1))
        no_time = self.make_job("script", status="SCHEDULED")
        self.assertTrue(all(db.claim_job(j, "h:1") for j in (pending, due, no_time)))
        self.assertEqual({db.get_job(j)["status"] for j in (pending, due, no_time)}, {"RUNNING"})

    def test_future_scheduled_paused_running_and_finished_jobs_are_not_claimable(self):
        cases = [
            self.make_job("script", status="SCHEDULED", next_retry_at=self.ahead(hours=1)),
            self.make_job("script", paused=1),
            self.make_job("script", status="RUNNING"),
            self.make_job("script", status="NEEDS_INPUT"),
            self.make_job("script", status="COMPLETED"),
            self.make_job("script", status="FAILED"),
        ]
        self.assertFalse(any(db.claim_job(j, "h:1") for j in cases))


class MigrationTests(unittest.TestCase):
    def test_a_database_created_before_the_lock_columns_existed_is_upgraded_in_place(self):
        import os, tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            conn = sqlite3.connect(path)
            conn.executescript(
                """CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL DEFAULT 'PENDING',
                   current_stage TEXT NOT NULL, input_payload TEXT NOT NULL, stage_outputs TEXT NOT NULL DEFAULT '{}',
                   attempt_count INTEGER NOT NULL DEFAULT 0, next_retry_at TEXT, paused INTEGER NOT NULL DEFAULT 0,
                   pause_requested INTEGER NOT NULL DEFAULT 0, pending_question TEXT, pending_payload TEXT,
                   notified TEXT NOT NULL DEFAULT '[]', last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                   INSERT INTO jobs (status, current_stage, input_payload, created_at, updated_at)
                   VALUES ('SCHEDULED', 'plan', '{"messageText":"keep me"}', 'c', 'u');"""
            )
            conn.commit()
            conn.close()
            with mock.patch.object(config, "DB_PATH", path):
                db.init_db()
                db.init_db()  # idempotent
                job = db.get_job(1)
                self.assertEqual((job["status"], job["input_payload"]["messageText"]), ("SCHEDULED", "keep me"))
                self.assertIn("locked_by", job)
                self.assertIn("wait_since", job)


class NotifiedConcurrencyTests(DbTestCase):
    def test_concurrent_mark_notified_calls_do_not_lose_keys(self):
        """mark_notified was read-modify-write across two statements, so the scheduler and the poller
        could overwrite each other's key."""
        job_id = self.make_job("script")
        errors: list[Exception] = []

        def mark(prefix: str) -> None:
            try:
                for i in range(25):
                    db.mark_notified(job_id, f"{prefix}:{i}")
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=mark, args=(p,)) for p in ("a", "b")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(len(db.get_job(job_id)["notified"]), 50)

    def test_marking_twice_records_one_key(self):
        job_id = self.make_job("script")
        db.mark_notified(job_id, "completed")
        db.mark_notified(job_id, "completed")
        self.assertEqual(db.get_job(job_id)["notified"], ["completed"])


if __name__ == "__main__":
    unittest.main()

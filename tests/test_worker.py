"""State-machine regressions for worker.py + scheduler.py.

Each test is named for the failure it prevents. See CLAUDE.md "Tier 1 audit fixes (2026-09-20)".
"""
from __future__ import annotations

import socket
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import config
import db
import notifier
import scheduler
import worker
from exceptions import HumanInputRequired, PermanentStageError, RateLimitError, StageBusy
from tests.support import DbTestCase

THIS_HOST = socket.gethostname()
DRAFT = {"title": "XZ backdoor", "scenes": [{"sceneNumber": 1, "narration": "Hello."}]}


def draft_stage(job, outputs):
    raise HumanInputRequired("Approve draft?", ["approve", "regenerate"], dict(DRAFT))


def ok_stage(job, outputs):
    return {"done": True}


class SchedulerDispatchTests(DbTestCase):
    def test_scheduled_job_runs_once_its_retry_time_passes(self):
        """run_job() used to reject anything not PENDING, so SCHEDULED jobs (every rate-limited or
        backed-off job) were returned by due_jobs() and then silently never run."""
        job_id = self.make_job("script", status="SCHEDULED", next_retry_at=self.ago(minutes=5))
        with mock.patch.dict(worker.STAGE_FUNCTIONS, {"script": ok_stage}):
            scheduler.tick()
        self.assertEqual(db.get_job(job_id)["status"], "COMPLETED")

    def test_scheduled_job_is_left_alone_before_its_retry_time(self):
        job_id = self.make_job("script", status="SCHEDULED", next_retry_at=self.ahead(hours=2))
        stage = mock.Mock(side_effect=ok_stage)
        with mock.patch.dict(worker.STAGE_FUNCTIONS, {"script": stage}):
            scheduler.tick()
        stage.assert_not_called()
        self.assertEqual(db.get_job(job_id)["status"], "SCHEDULED")

    def test_rate_limit_round_trip_resumes_and_completes(self):
        job_id = self.make_job("script")
        outcomes = [RateLimitError("contentpipe:script", retry_at=None), None]

        def stage(job, outputs):
            err = outcomes.pop(0)
            if err:
                raise err
            return {"done": True}

        with mock.patch.dict(worker.STAGE_FUNCTIONS, {"script": stage}):
            scheduler.tick()
            job = db.get_job(job_id)
            self.assertEqual(job["status"], "SCHEDULED")
            self.assertEqual(job["attempt_count"], 0, "a rate limit is not a failed attempt")
            self.set_raw(job_id, next_retry_at=self.ago(seconds=1))
            scheduler.tick()
        self.assertEqual(db.get_job(job_id)["status"], "COMPLETED")
        limited = [m for m in self.telegram.messages if "rate limited" in m["text"]]
        self.assertEqual(len(limited), 1)

    def test_each_tick_re_sends_an_approval_request_that_never_got_through(self):
        job_id = self.make_job("script", status="NEEDS_INPUT", pending_payload=dict(DRAFT),
                               pending_question={"question": "Approve?", "options": ["approve", "regenerate"]})
        self.assertEqual(db.get_job(job_id)["notified"], [])
        scheduler.tick()
        self.assertEqual(len(db.get_job(job_id)["notified"]), 1)
        self.assertEqual(len(self.telegram.messages), 1)
        scheduler.tick()
        self.assertEqual(len(self.telegram.messages), 1, "and not again once delivered")

    def test_one_raising_job_does_not_stop_the_rest_of_the_tick(self):
        a = self.make_job("script")
        b = self.make_job("script")
        real = worker.run_job

        def flaky(job_id):
            if job_id == a:
                raise RuntimeError("boom")
            return real(job_id)

        with mock.patch.dict(worker.STAGE_FUNCTIONS, {"script": ok_stage}), mock.patch.object(worker, "run_job", flaky), \
                self.captured_stdout():
            scheduler.tick()
        self.assertEqual(db.get_job(b)["status"], "COMPLETED")


class RunJobTests(DbTestCase):
    def test_claim_is_atomic_so_a_job_cannot_run_twice(self):
        job_id = self.make_job("script")
        self.assertTrue(db.claim_job(job_id, "host:1"))
        self.assertFalse(db.claim_job(job_id, "host:2"))
        stage = mock.Mock(side_effect=ok_stage)
        with mock.patch.dict(worker.STAGE_FUNCTIONS, {"script": stage}):
            worker.run_job(job_id)  # already RUNNING elsewhere
        stage.assert_not_called()

    def test_run_job_records_which_process_holds_the_job(self):
        job_id = self.make_job("script")
        seen = {}

        def stage(job, outputs):
            seen.update(db.get_job(job_id))
            return {"done": True}

        with mock.patch.dict(worker.STAGE_FUNCTIONS, {"script": stage}):
            worker.run_job(job_id)
        self.assertEqual(seen["status"], "RUNNING")
        self.assertRegex(seen["locked_by"], r"^.+:\d+$")

    def test_unknown_stage_does_not_strand_the_job_in_running(self):
        """STAGE_FUNCTIONS[stage] used to be looked up outside the try block."""
        job_id = self.make_job("bogus-stage")
        with self.captured_stdout():
            worker.run_job(job_id)
        job = db.get_job(job_id)
        self.assertEqual(job["status"], "SCHEDULED")
        self.assertEqual(job["attempt_count"], 1)

    def test_a_failing_notification_does_not_strand_or_undo_the_job(self):
        job_id = self.make_job("script")
        with mock.patch.dict(worker.STAGE_FUNCTIONS, {"script": draft_stage}), \
                mock.patch.object(notifier, "notify_input_required", side_effect=RuntimeError("db locked")), \
                self.captured_stdout():
            worker.run_job(job_id)
        job = db.get_job(job_id)
        self.assertEqual(job["status"], "NEEDS_INPUT")
        self.assertEqual(job["pending_payload"]["title"], "XZ backdoor")

    def test_generic_failures_back_off_then_fail_after_max_attempts(self):
        job_id = self.make_job("script")
        boom = mock.Mock(side_effect=RuntimeError("upstream 500"))
        with mock.patch.dict(worker.STAGE_FUNCTIONS, {"script": boom}), mock.patch.object(config, "MAX_STAGE_ATTEMPTS", 2), \
                self.captured_stdout():
            worker.run_job(job_id)
            self.assertEqual(db.get_job(job_id)["status"], "SCHEDULED")
            self.set_raw(job_id, next_retry_at=self.ago(seconds=1))
            worker.run_job(job_id)
        self.assertEqual(db.get_job(job_id)["status"], "FAILED")
        self.assertEqual(len([m for m in self.telegram.messages if "failed" in m["text"]]), 1)


class WaitingStateTests(DbTestCase):
    """409 in_progress, zero quota, and the cap on waiting."""

    def test_stage_busy_reschedules_without_burning_an_attempt_or_paging(self):
        job_id = self.make_job("script")
        busy = mock.Mock(side_effect=StageBusy("/api/script run already in progress", retry_at=None))
        with mock.patch.dict(worker.STAGE_FUNCTIONS, {"script": busy}):
            worker.run_job(job_id)
        job = db.get_job(job_id)
        self.assertEqual(job["status"], "SCHEDULED")
        self.assertEqual(job["attempt_count"], 0)
        self.assertEqual(self.telegram.messages, [])
        self.assertIsNotNone(job["wait_since"])

    def test_permanent_error_fails_immediately_and_says_why(self):
        """zero_quota can never heal by waiting; it used to consume all five backoff attempts (~9h)."""
        job_id = self.make_job("script")
        stage = mock.Mock(side_effect=PermanentStageError("no quota exists for this key; enable billing"))
        with mock.patch.dict(worker.STAGE_FUNCTIONS, {"script": stage}):
            worker.run_job(job_id)
        job = db.get_job(job_id)
        self.assertEqual(job["status"], "FAILED")
        self.assertIn("billing", job["last_error"])
        self.assertEqual(stage.call_count, 1)
        self.assertEqual(len([m for m in self.telegram.messages if "failed" in m["text"]]), 1)

    def test_a_job_that_has_waited_too_long_fails_instead_of_retrying_forever(self):
        job_id = self.make_job("script", wait_since=self.ago(days=config.MAX_WAIT_DAYS + 1))
        limited = mock.Mock(side_effect=RateLimitError("contentpipe:script"))
        with mock.patch.dict(worker.STAGE_FUNCTIONS, {"script": limited}):
            worker.run_job(job_id)
        job = db.get_job(job_id)
        self.assertEqual(job["status"], "FAILED")
        self.assertIn("waiting", job["last_error"].lower())

    def test_wait_clock_resets_after_a_successful_stage(self):
        job_id = self.make_job("research", wait_since=self.ago(days=1))
        with mock.patch.dict(worker.STAGE_FUNCTIONS, {"research": ok_stage}):
            worker.run_job(job_id)
        self.assertIsNone(db.get_job(job_id)["wait_since"])


class RateLimitNotificationTests(DbTestCase):
    """A per-minute limit re-polls every minute with a new retry time each time; that must not page
    the human every minute. One message per unbroken wait, plus one if the wait turns long."""

    def limited_run(self, job_id: int, retry_in: timedelta) -> None:
        self.set_raw(job_id, next_retry_at=self.ago(seconds=1))  # due again
        stage = mock.Mock(side_effect=RateLimitError("contentpipe:script", retry_at=datetime.now(timezone.utc) + retry_in))
        with mock.patch.dict(worker.STAGE_FUNCTIONS, {"script": stage}):
            worker.run_job(job_id)

    def rate_limit_messages(self) -> list[str]:
        return [m["text"] for m in self.telegram.messages if "rate limited" in m["text"]]

    def test_repeated_short_waits_page_once(self):
        job_id = self.make_job("script")
        for _ in range(5):
            self.limited_run(job_id, timedelta(seconds=60))
        self.assertEqual(db.get_job(job_id)["status"], "SCHEDULED")
        self.assertEqual(len(self.rate_limit_messages()), 1)
        self.assertIn("Short wait", self.rate_limit_messages()[0])

    def test_a_short_wait_that_turns_long_pages_again_once(self):
        job_id = self.make_job("script")
        self.limited_run(job_id, timedelta(seconds=60))
        self.limited_run(job_id, timedelta(hours=16))
        self.limited_run(job_id, timedelta(hours=15))
        messages = self.rate_limit_messages()
        self.assertEqual(len(messages), 2)
        self.assertNotIn("Short wait", messages[1])

    def test_a_new_wait_after_progress_is_announced(self):
        job_id = self.make_job("script")
        self.limited_run(job_id, timedelta(seconds=60))
        db.update_job(job_id, wait_since=None)  # what a successful stage does
        self.limited_run(job_id, timedelta(seconds=60))
        self.assertEqual(len(self.rate_limit_messages()), 2)


class OrphanReclaimTests(DbTestCase):
    def running(self, locked_by, locked_ago, attempt_count=0, stage="script"):
        job_id = self.make_job(stage)
        self.set_raw(job_id, status="RUNNING", locked_by=locked_by, locked_at=self.ago(**locked_ago), attempt_count=attempt_count)
        return job_id

    def test_running_job_whose_process_died_is_requeued(self):
        """Killed scheduler (launchd restart, sleep+kill, OOM) used to leave the job in RUNNING forever."""
        job_id = self.running(f"{THIS_HOST}:424242", {"seconds": 30})
        with mock.patch.object(worker, "_pid_alive", return_value=False):
            self.assertEqual(worker.reclaim_orphaned_jobs(), [job_id])
        job = db.get_job(job_id)
        self.assertEqual(job["status"], "PENDING")
        self.assertEqual(job["attempt_count"], 1, "a crash counts as an attempt so a poison job cannot loop forever")
        self.assertIn("orphan", job["last_error"].lower())
        self.assertIsNone(job["locked_by"])

    def test_orphan_is_then_actually_run_again(self):
        job_id = self.running(f"{THIS_HOST}:424242", {"seconds": 30})
        with mock.patch.object(worker, "_pid_alive", return_value=False), mock.patch.dict(worker.STAGE_FUNCTIONS, {"script": ok_stage}):
            scheduler.tick()
        self.assertEqual(db.get_job(job_id)["status"], "COMPLETED")

    def test_running_job_with_a_live_process_is_left_alone(self):
        job_id = self.running(f"{THIS_HOST}:424242", {"seconds": 30})
        with mock.patch.object(worker, "_pid_alive", return_value=True):
            self.assertEqual(worker.reclaim_orphaned_jobs(), [])
        self.assertEqual(db.get_job(job_id)["status"], "RUNNING")

    def test_expired_lease_is_reclaimed_even_if_the_pid_looks_alive(self):
        """pid reuse after a reboot: fall back to the lease."""
        job_id = self.running(f"{THIS_HOST}:424242", {"seconds": config.RUNNING_LEASE_SECONDS + 60})
        with mock.patch.object(worker, "_pid_alive", return_value=True):
            self.assertEqual(worker.reclaim_orphaned_jobs(), [job_id])

    def test_job_locked_on_another_host_is_only_reclaimed_by_lease(self):
        fresh = self.running("other-host:1", {"seconds": 30})
        stale = self.running("other-host:2", {"seconds": config.RUNNING_LEASE_SECONDS + 60})
        with mock.patch.object(worker, "_pid_alive", return_value=False):
            self.assertEqual(worker.reclaim_orphaned_jobs(), [stale])
        self.assertEqual(db.get_job(fresh)["status"], "RUNNING")

    def test_repeatedly_crashing_job_ends_in_failed_with_a_notification(self):
        job_id = self.running(f"{THIS_HOST}:424242", {"seconds": 30}, attempt_count=config.MAX_STAGE_ATTEMPTS - 1)
        with mock.patch.object(worker, "_pid_alive", return_value=False):
            worker.reclaim_orphaned_jobs()
        self.assertEqual(db.get_job(job_id)["status"], "FAILED")
        self.assertEqual(len([m for m in self.telegram.messages if "failed" in m["text"]]), 1)

    def test_only_running_jobs_are_reclaimed(self):
        pending = self.make_job("script")
        needs_input = self.make_job("script", status="NEEDS_INPUT")
        with mock.patch.object(worker, "_pid_alive", return_value=False):
            self.assertEqual(worker.reclaim_orphaned_jobs(), [])
        self.assertEqual(db.get_job(pending)["status"], "PENDING")
        self.assertEqual(db.get_job(needs_input)["status"], "NEEDS_INPUT")


class RegenerateTests(DbTestCase):
    def test_regenerated_draft_sends_a_fresh_approval_message(self):
        """The dedupe key needs_input:script:0 was reused, so the 2nd draft never reached Telegram."""
        job_id = self.make_job("script")
        with mock.patch.dict(worker.STAGE_FUNCTIONS, {"script": draft_stage}):
            worker.run_job(job_id)
            self.assertEqual(len(self.telegram.messages), 1)
            self.assertTrue(worker.resume_from_input(job_id, "regenerate"))
            self.assertEqual(db.get_job(job_id)["status"], "PENDING")
            worker.run_job(job_id)
        self.assertEqual(len(self.telegram.messages), 2)
        self.assertIn("reply_markup", self.telegram.messages[1])
        self.assertEqual(db.get_job(job_id)["status"], "NEEDS_INPUT")

    def test_regenerate_can_be_repeated(self):
        job_id = self.make_job("script")
        with mock.patch.dict(worker.STAGE_FUNCTIONS, {"script": draft_stage}):
            worker.run_job(job_id)
            for _ in range(3):
                worker.resume_from_input(job_id, "regenerate")
                worker.run_job(job_id)
        self.assertEqual(len(self.telegram.messages), 4)

    def test_regenerate_clears_only_the_approval_keys(self):
        job_id = self.make_job("script", status="NEEDS_INPUT", pending_payload=dict(DRAFT),
                               pending_question={"question": "q", "options": ["approve", "regenerate"]},
                               notified=["rate_limited:script:2026-09-20T00:00:00+00:00", "needs_input:script:0"])
        worker.resume_from_input(job_id, "regenerate")
        self.assertEqual(db.get_job(job_id)["notified"], ["rate_limited:script:2026-09-20T00:00:00+00:00"])


class ApprovalTests(DbTestCase):
    def needs_input(self, stage="script", payload=None, **extra):
        return self.make_job(stage, status="NEEDS_INPUT", pending_payload=DRAFT if payload is None else payload,
                             pending_question={"question": "q", "options": ["approve", "regenerate"]}, **extra)

    def test_approve_commits_the_draft_and_completes_in_one_step(self):
        job_id = self.needs_input()
        self.assertTrue(worker.resume_from_input(job_id, "approve"))
        job = db.get_job(job_id)
        self.assertEqual(job["status"], "COMPLETED")
        self.assertEqual(job["stage_outputs"]["script"]["title"], "XZ backdoor")
        self.assertEqual(job["pending_payload"], {})
        self.assertFalse(job["pending_question"])
        self.assertEqual(len([m for m in self.telegram.messages if "completed" in m["text"]]), 1)

    def test_approve_state_survives_a_failing_completion_notification(self):
        """approve used to clear pending_payload in one write and store it in another; a crash between lost the script."""
        job_id = self.needs_input()
        with mock.patch.object(notifier, "notify_completed", side_effect=RuntimeError("telegram down")), self.captured_stdout():
            worker.resume_from_input(job_id, "approve")
        job = db.get_job(job_id)
        self.assertEqual(job["status"], "COMPLETED")
        self.assertEqual(job["stage_outputs"]["script"]["title"], "XZ backdoor")

    def test_approve_advances_a_mid_pipeline_stage(self):
        job_id = self.needs_input(stage="research", payload={"topicTitle": "x"})
        worker.resume_from_input(job_id, "approve")
        job = db.get_job(job_id)
        self.assertEqual((job["status"], job["current_stage"], job["attempt_count"]), ("PENDING", "plan", 0))

    def test_approve_with_no_stored_draft_refuses_instead_of_completing_with_nothing(self):
        job_id = self.needs_input(payload={})
        with self.captured_stdout():
            self.assertFalse(worker.resume_from_input(job_id, "approve"))
        self.assertEqual(db.get_job(job_id)["status"], "NEEDS_INPUT")

    def test_second_tap_after_the_first_is_a_no_op(self):
        job_id = self.needs_input()
        self.assertTrue(worker.resume_from_input(job_id, "approve"))
        self.assertFalse(worker.resume_from_input(job_id, "regenerate"))
        self.assertFalse(worker.resume_from_input(job_id, "approve"))
        self.assertEqual(db.get_job(job_id)["status"], "COMPLETED")
        self.assertEqual(db.get_job(job_id)["stage_outputs"]["script"]["title"], "XZ backdoor")

    def test_unknown_answers_and_unknown_jobs_are_ignored(self):
        job_id = self.needs_input()
        with self.captured_stdout():
            self.assertFalse(worker.resume_from_input(job_id, "maybe"))
            self.assertFalse(worker.resume_from_input(9999, "approve"))
        self.assertEqual(db.get_job(job_id)["status"], "NEEDS_INPUT")


class StaleNeedsInputTests(DbTestCase):
    def test_timed_out_job_is_failed_and_reported_once(self):
        job_id = self.make_job("script", status="NEEDS_INPUT")
        self.set_raw(job_id, updated_at=self.ago(hours=config.NEEDS_INPUT_TIMEOUT_HOURS + 1))
        scheduler._handle_stale_needs_input()
        scheduler._handle_stale_needs_input()
        self.assertEqual(db.get_job(job_id)["status"], "FAILED")
        self.assertEqual(len([m for m in self.telegram.messages if "failed" in m["text"]]), 1)

    def test_a_tap_that_lands_during_the_sweep_wins_over_the_timeout(self):
        """The sweep read a NEEDS_INPUT snapshot; the human approved before it wrote FAILED."""
        job_id = self.make_job("script", status="NEEDS_INPUT", pending_payload=dict(DRAFT))
        snapshot = db.get_job(job_id)
        worker.resume_from_input(job_id, "approve")
        with mock.patch.object(db, "stale_needs_input_jobs", return_value=[snapshot]):
            scheduler._handle_stale_needs_input()
        self.assertEqual(db.get_job(job_id)["status"], "COMPLETED")
        self.assertEqual([m for m in self.telegram.messages if "failed" in m["text"]], [])


if __name__ == "__main__":
    unittest.main()

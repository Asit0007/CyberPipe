"""notifier.py: an event counts as notified only if Telegram actually accepted it."""
from __future__ import annotations

import unittest
from unittest import mock

import requests

import db
import notifier
from tests.support import DbTestCase, FakeResponse, TEST_TOKEN

TITLE = 'Approve "AT&T breach <script>alert(1)</script>"?'


class DeliveryTests(DbTestCase):
    def input_job(self, question: str = TITLE, payload: dict | None = None, stage: str = "script", **extra) -> dict:
        job_id = self.make_job(stage, status="NEEDS_INPUT", pending_payload=payload or {},
                               pending_question={"question": question, "options": ["approve", "regenerate"]}, **extra)
        return db.get_job(job_id)

    def test_message_is_plain_text_so_angle_brackets_and_ampersands_cannot_break_it(self):
        """parse_mode=HTML + an unescaped title ('AT&T', '<script>') made Telegram answer 400."""
        job = self.input_job()
        notifier.notify_input_required(job)
        sent = self.telegram.messages[0]
        self.assertNotIn("parse_mode", sent)
        self.assertIn("AT&T breach <script>alert(1)</script>", sent["text"])

    def test_rejected_send_is_not_recorded_as_notified(self):
        job = self.input_job()
        self.telegram.status_code = 400
        with self.captured_stdout():
            notifier.notify_input_required(job)
        self.assertEqual(db.get_job(job["id"])["notified"], [])

    def test_network_failure_is_not_recorded_as_notified(self):
        job = self.input_job()
        self.telegram.fail_with = requests.ConnectionError("no route")
        with self.captured_stdout():
            notifier.notify_input_required(job)
        self.assertEqual(db.get_job(job["id"])["notified"], [])

    def test_accepted_send_is_recorded_and_not_repeated(self):
        job = self.input_job()
        notifier.notify_input_required(job)
        notifier.notify_input_required(db.get_job(job["id"]))
        self.assertEqual(len(self.telegram.messages), 1)

    def test_network_error_log_never_contains_the_bot_token(self):
        """requests puts the full URL, /bot<TOKEN>/sendMessage, in its exception text."""
        job = self.input_job()
        self.telegram.fail_with = requests.ConnectionError(f"HTTPSConnectionPool: Max retries with url: /bot{TEST_TOKEN}/sendMessage")
        with self.captured_stdout() as out:
            notifier.notify_input_required(job)
        self.assertNotIn(TEST_TOKEN, out.getvalue())
        self.assertNotIn(TEST_TOKEN.split(":")[1], out.getvalue())

    def test_http_error_body_log_never_contains_the_bot_token(self):
        job = self.input_job()
        self.telegram.responder = lambda m, kw: FakeResponse(401, text=f"Unauthorized for /bot{TEST_TOKEN}/sendMessage")
        with self.captured_stdout() as out:
            notifier.notify_input_required(job)
        self.assertNotIn(TEST_TOKEN, out.getvalue())

    def test_text_over_telegrams_limit_is_truncated_rather_than_rejected(self):
        job = self.input_job(question="x" * 6000)
        notifier.notify_input_required(job)
        self.assertLessEqual(len(self.telegram.messages[0]["text"]), 4096)
        self.assertEqual(len(db.get_job(job["id"])["notified"]), 1)

    def test_redact_secrets_masks_the_token_in_any_position(self):
        text = f"boom https://api.telegram.org/bot{TEST_TOKEN}/getUpdates and again {TEST_TOKEN}"
        cleaned = notifier.redact_secrets(text)
        self.assertNotIn(TEST_TOKEN, cleaned)
        self.assertNotIn("AAfake", cleaned)
        self.assertIn("api.telegram.org", cleaned)


class UnconfiguredTelegramTests(DbTestCase):
    telegram_configured = False

    def test_events_before_telegram_is_configured_are_not_lost(self):
        """Unconfigured send_message returned silently and the event was still marked notified, so an
        approval that arrived before the bot existed was never sent once it did."""
        job_id = self.make_job("script", status="NEEDS_INPUT", pending_payload={},
                               pending_question={"question": "Approve?", "options": ["approve", "regenerate"]})
        with self.captured_stdout():
            notifier.notify_input_required(db.get_job(job_id))
        self.assertEqual(db.get_job(job_id)["notified"], [])
        self.assertEqual(self.telegram.calls, [])

        import config
        with mock.patch.object(config, "TELEGRAM_BOT_TOKEN", TEST_TOKEN), mock.patch.object(config, "TELEGRAM_CHAT_ID", "42"):
            self.assertEqual(notifier.resend_missed_notifications(), 1)
        self.assertEqual(len(self.telegram.messages), 1)

    def test_the_sweep_is_silent_while_unconfigured(self):
        self.make_job("script", status="NEEDS_INPUT")
        with self.captured_stdout() as out:
            self.assertEqual(notifier.resend_missed_notifications(), 0)
        self.assertEqual(out.getvalue(), "")

    def test_the_not_configured_warning_is_printed_once_not_every_tick(self):
        job_id = self.make_job("script", status="NEEDS_INPUT",
                               pending_question={"question": "q", "options": ["approve"]})
        with self.captured_stdout() as out:
            for _ in range(3):
                notifier.notify_input_required(db.get_job(job_id))
        self.assertEqual(out.getvalue().count("not set"), 1)


class ResendSweepTests(DbTestCase):
    def test_missed_approval_is_resent_after_an_outage(self):
        job_id = self.make_job("script", status="NEEDS_INPUT", pending_payload={},
                               pending_question={"question": "Approve?", "options": ["approve", "regenerate"]})
        clock = {"t": 1000.0}
        notifier._monotonic = lambda: clock["t"]
        self.telegram.status_code = 500
        with self.captured_stdout():
            notifier.notify_input_required(db.get_job(job_id))
        self.assertEqual(db.get_job(job_id)["notified"], [])
        self.telegram.status_code = 200
        clock["t"] += 10_000  # well past any cooldown
        self.assertEqual(notifier.resend_missed_notifications(), 1)
        self.assertEqual(len(db.get_job(job_id)["notified"]), 1)
        self.assertEqual(notifier.resend_missed_notifications(), 0)

    def test_failed_deliveries_are_retried_no_faster_than_the_cooldown(self):
        job_id = self.make_job("script", status="NEEDS_INPUT",
                               pending_question={"question": "q", "options": ["approve"]})
        self.telegram.status_code = 500
        clock = {"t": 1000.0}
        notifier._monotonic = lambda: clock["t"]
        with self.captured_stdout():
            notifier.resend_missed_notifications()
            attempts = len(self.telegram.calls)
            clock["t"] += 10
            notifier.resend_missed_notifications()
            self.assertEqual(len(self.telegram.calls), attempts, "inside the cooldown: no new attempt")
            clock["t"] += 10_000
            notifier.resend_missed_notifications()
        self.assertGreater(len(self.telegram.calls), attempts)

    def test_completed_and_failed_jobs_that_never_got_their_message_are_covered(self):
        done = self.make_job("script", status="COMPLETED")
        failed = self.make_job("script", status="FAILED", last_error="upstream exploded")
        self.assertEqual(notifier.resend_missed_notifications(), 2)
        texts = " ".join(m["text"] for m in self.telegram.messages)
        self.assertIn(f"#{done}", texts)
        self.assertIn("upstream exploded", texts)
        self.assertEqual(notifier.resend_missed_notifications(), 0)


class DraftAttachmentTests(DbTestCase):
    DRAFT = {"title": "XZ backdoor", "estimatedTotalDuration": 40, "scenes": [
        {"sceneNumber": 1, "title": "Cold open", "actPhase": "Hook", "durationEst": 12, "narration": "A maintainer added a backdoor."},
        {"sceneNumber": 2, "title": "Context", "actPhase": "Context", "durationEst": 28, "narration": "liblzma sits under sshd on many distros."}]}

    def make(self, payload=None, stage="script"):
        job_id = self.make_job(stage, status="NEEDS_INPUT", pending_payload=self.DRAFT if payload is None else payload,
                               pending_question={"question": "Approve?", "options": ["approve", "regenerate"]},
                               stage_outputs={"research": {"retrievedSources": [{"id": "S1", "ok": True, "title": "Openwall", "url": "https://example.org/a"}]}})
        return db.get_job(job_id)

    def test_the_approver_receives_the_actual_script_not_just_a_summary(self):
        """Approve/regenerate used to be tapped against a one-line summary."""
        job = self.make()
        notifier.notify_input_required(job)
        docs = self.telegram.of("sendDocument")
        self.assertEqual(len(docs), 1)
        filename, content = docs[0]["files"]["document"][:2]
        self.assertEqual(filename, f"job-{job['id']}-script-draft.md")
        self.assertIn("A maintainer added a backdoor.", content.decode())
        order = [c["method"] for c in self.telegram.calls]
        self.assertLess(order.index("sendDocument"), order.index("sendMessage"), "buttons must be the last thing in the chat")

    def test_failed_attachment_does_not_block_the_approval_message_but_says_so(self):
        job = self.make()
        self.telegram.responder = lambda method, kw: FakeResponse(500) if method == "sendDocument" else FakeResponse(200)
        with self.captured_stdout():
            notifier.notify_input_required(job)
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertIn("attach", self.telegram.messages[0]["text"].lower())
        self.assertEqual(len(db.get_job(job["id"])["notified"]), 1)

    def test_no_attachment_for_a_stage_with_no_scenes(self):
        job = self.make(payload={"topicTitle": "x"}, stage="research")
        notifier.notify_input_required(job)
        self.assertEqual(self.telegram.of("sendDocument"), [])
        self.assertEqual(len(self.telegram.messages), 1)


if __name__ == "__main__":
    unittest.main()

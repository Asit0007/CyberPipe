"""telegram_poller.py: one bad update must never wedge the approval queue."""
from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

import requests

import db
import telegram_poller
import worker
from tests.support import DbTestCase, FakeResponse, TEST_TOKEN


def tap(update_id: int, job_id: int, answer: str, user_id: int = 42) -> dict[str, Any]:
    return {"update_id": update_id, "callback_query": {"id": f"cq{update_id}", "from": {"id": user_id}, "data": f"job:{job_id}:{answer}"}}


class PollerTests(DbTestCase):
    def serve(self, updates: list[dict], answer_status: int = 200) -> None:
        def responder(method: str, kwargs: dict) -> FakeResponse:
            if method == "getUpdates":
                return FakeResponse(200, {"ok": True, "result": updates})
            if method == "answerCallbackQuery":
                return FakeResponse(answer_status, text="Bad Request: query is too old")
            return FakeResponse(200)
        self.telegram.responder = responder

    def waiting_job(self) -> int:
        return self.make_job("script", status="NEEDS_INPUT", pending_payload={"title": "T", "scenes": []},
                             pending_question={"question": "q", "options": ["approve", "regenerate"]})

    def answers(self) -> list[str]:
        return [c["json"].get("text", "") for c in self.telegram.of("answerCallbackQuery")]

    def offset(self):
        return db.get_kv(telegram_poller.OFFSET_KV_KEY)

    # ------------------------------------------------------------------------------------------
    def test_a_failed_answerCallbackQuery_does_not_wedge_the_queue(self):
        """answerCallbackQuery 400s once a tap is old (Mac slept). That raised out of poll_once *before*
        the offset was saved, so the same update replayed forever and no later approval was ever read."""
        first, second = self.waiting_job(), self.waiting_job()
        self.serve([tap(7, first, "approve"), tap(8, second, "approve")], answer_status=400)
        with self.captured_stdout():
            telegram_poller.poll_once()
        self.assertEqual(self.offset(), "9")
        self.assertEqual(db.get_job(first)["status"], "COMPLETED")
        self.assertEqual(db.get_job(second)["status"], "COMPLETED")

    def test_a_crash_while_handling_one_update_skips_it_and_moves_on(self):
        job_id = self.waiting_job()
        self.serve([tap(7, 999, "approve"), tap(8, job_id, "approve")])
        real = worker.resume_from_input

        def flaky(jid, answer):
            if jid == 999:
                raise RuntimeError("database is locked")
            return real(jid, answer)

        with mock.patch.object(worker, "resume_from_input", flaky), self.captured_stdout():
            telegram_poller.poll_once()
        self.assertEqual(self.offset(), "9")
        self.assertEqual(db.get_job(job_id)["status"], "COMPLETED")
        self.assertTrue(any("again" in a.lower() for a in self.answers()), "tell the human to tap again")

    def test_an_error_that_escapes_update_handling_still_consumes_the_update(self):
        """The safety net in poll_once itself (the handler above catches its own errors, so this needs
        _process_update to blow up directly)."""
        self.serve([{"update_id": 7}, {"update_id": 8}])
        seen = []

        def explode(update):
            seen.append(update["update_id"])
            raise RuntimeError("unexpected")

        with mock.patch.object(telegram_poller, "_process_update", explode), self.captured_stdout():
            telegram_poller.poll_once()
        self.assertEqual(seen, [7, 8], "the second update must still be processed")
        self.assertEqual(self.offset(), "9")

    def test_acknowledging_a_tap_never_raises(self):
        self.telegram.status_code = 400
        with self.captured_stdout():
            telegram_poller._answer("cq1", "Recorded: approve")  # must not raise

    def test_api_errors_never_carry_the_bot_token(self):
        """requests' exception text includes the URL, /bot<TOKEN>/..., and the poller printed it into a log file."""
        self.telegram.fail_with = requests.ConnectionError(f"Max retries exceeded with url: /bot{TEST_TOKEN}/getUpdates")
        with self.assertRaises(telegram_poller.TelegramAPIError) as ctx:
            telegram_poller._api("getUpdates")
        self.assertNotIn(TEST_TOKEN, str(ctx.exception))
        self.assertNotIn(TEST_TOKEN.split(":")[1], str(ctx.exception))
        # `raise ... from exc` would also suppress context but keep the token-bearing original as __cause__,
        # and any traceback prints the cause. It must be cut entirely.
        self.assertIsNone(ctx.exception.__cause__, "the chained original would print the token in a traceback")
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_http_error_status_never_carries_the_bot_token(self):
        self.telegram.status_code = 400
        with self.assertRaises(telegram_poller.TelegramAPIError) as ctx:
            telegram_poller._api("answerCallbackQuery", callback_query_id="x")
        self.assertNotIn(TEST_TOKEN, str(ctx.exception))
        self.assertIn("400", str(ctx.exception))

    def test_getUpdates_failure_leaves_the_offset_alone(self):
        self.telegram.status_code = 502
        with self.assertRaises(telegram_poller.TelegramAPIError):
            telegram_poller.poll_once()
        self.assertIsNone(self.offset())

    def test_tap_from_another_user_is_refused_and_changes_nothing(self):
        job_id = self.waiting_job()
        self.serve([tap(7, job_id, "approve", user_id=999)])
        with self.captured_stdout():
            telegram_poller.poll_once()
        self.assertEqual(db.get_job(job_id)["status"], "NEEDS_INPUT")
        self.assertEqual(self.answers(), ["Unauthorized"])
        self.assertEqual(self.offset(), "8")

    def test_stale_tap_is_reported_as_already_handled_not_recorded(self):
        job_id = self.waiting_job()
        worker.resume_from_input(job_id, "approve")
        self.serve([tap(7, job_id, "regenerate")])
        with self.captured_stdout():
            telegram_poller.poll_once()
        self.assertEqual(db.get_job(job_id)["status"], "COMPLETED")
        self.assertEqual(len(self.answers()), 1)
        self.assertIn("already", self.answers()[0].lower())

    def test_a_real_tap_is_acknowledged_as_recorded(self):
        job_id = self.waiting_job()
        self.serve([tap(7, job_id, "regenerate")])
        with self.captured_stdout():
            telegram_poller.poll_once()
        self.assertEqual(db.get_job(job_id)["status"], "PENDING")
        self.assertIn("Recorded", self.answers()[0])

    def test_malformed_callback_data_is_ignored_and_consumed(self):
        bad = {"update_id": 7, "callback_query": {"id": "q", "from": {"id": 42}, "data": "job:notanumber:approve"}}
        self.serve([bad, {"update_id": 8, "callback_query": {"id": "q2", "from": {"id": 42}, "data": "garbage"}}, {"update_id": 9, "message": {"text": "hi"}}])
        with self.captured_stdout():
            telegram_poller.poll_once()
        self.assertEqual(self.offset(), "10")


if __name__ == "__main__":
    unittest.main()

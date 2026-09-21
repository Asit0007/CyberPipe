"""pipeline._post: how ContentPipe's strict-mode status codes map onto the worker's control flow."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import pipeline
from exceptions import HumanInputRequired, PermanentStageError, RateLimitError, StageBusy
from tests.support import FakeResponse


def reply(status: int, body: dict | None = None, retry_after: str | None = None) -> FakeResponse:
    r = FakeResponse(status, body if body is not None else {}, text=str(body))
    if retry_after is not None:
        r.headers["Retry-After"] = retry_after
    return r


class PostContractTests(unittest.TestCase):
    def post(self, response: FakeResponse):
        with mock.patch("pipeline.requests.post", return_value=response) as m:
            try:
                return pipeline._post("/api/script", {"x": 1}, provider="contentpipe:script"), m
            except Exception as exc:  # returned so each test can assert on the type
                return exc, m

    def test_the_strict_header_is_always_sent(self):
        _, m = self.post(reply(200, {"ok": 1}))
        self.assertEqual(m.call_args.kwargs["headers"], {"X-ContentPipe-Strict": "1"})

    def test_429_becomes_a_rate_limit_that_honours_retry_after(self):
        exc, _ = self.post(reply(429, {"kind": "per_day"}, retry_after="3600"))
        self.assertIsInstance(exc, RateLimitError)
        self.assertAlmostEqual((exc.retry_at - datetime.now(timezone.utc)).total_seconds(), 3600, delta=5)

    def test_409_in_progress_is_a_wait_not_a_failure(self):
        """ContentPipe is still generating this exact script; retrying immediately used to burn a backoff attempt."""
        exc, _ = self.post(reply(409, {"kind": "in_progress", "runId": "abc"}, retry_after="30"))
        self.assertIsInstance(exc, StageBusy)
        self.assertAlmostEqual((exc.retry_at - datetime.now(timezone.utc)).total_seconds(), 30, delta=5)

    def test_409_without_a_retry_after_still_gets_a_sane_default(self):
        exc, _ = self.post(reply(409, {"kind": "in_progress"}))
        self.assertIsInstance(exc, StageBusy)
        self.assertGreater(exc.retry_at, datetime.now(timezone.utc) + timedelta(seconds=5))

    def test_502_zero_quota_is_permanent(self):
        exc, _ = self.post(reply(502, {"kind": "zero_quota", "retryable": False, "error": "limit: 0"}))
        self.assertIsInstance(exc, PermanentStageError)
        self.assertIn("limit: 0", str(exc))

    def test_other_502_and_503_stay_ordinary_retryable_errors(self):
        for status, body in ((502, {"kind": "upstream_error", "retryable": False}), (503, {"kind": "upstream_unavailable"})):
            exc, _ = self.post(reply(status, body))
            self.assertIs(type(exc), RuntimeError, status)

    def test_a_non_json_error_body_does_not_mask_the_status(self):
        r = FakeResponse(502, {}, text="<html>Bad gateway</html>")
        r.json = mock.Mock(side_effect=ValueError("no json"))
        exc, _ = self.post(r)
        self.assertIs(type(exc), RuntimeError)
        self.assertIn("502", str(exc))

    def test_canned_fallback_content_is_never_accepted_as_a_result(self):
        exc, _ = self.post(reply(200, {"isQuotaFallback": True}))
        self.assertIsInstance(exc, RateLimitError)


if __name__ == "__main__":
    unittest.main()


class BrandDefaultTests(unittest.TestCase):
    """The show name in a script is the channel's, never the tool's, and our own channel is not a story's origin."""

    def stage(self, fn, payload, outputs=None):
        job = {"input_payload": payload}
        with mock.patch("pipeline._post", return_value={"scenes": [], "title": "t"}) as m:
            try:
                fn(job, outputs if outputs is not None else {"research": {}, "plan": {}})
            except HumanInputRequired:
                pass  # stage_script always ends at its approval checkpoint; the request body is what's under test
        return m.call_args.args[1]

    def test_script_brand_defaults_to_the_channel_not_the_tool(self):
        body = self.stage(pipeline.stage_script, {"messageText": "x"})
        self.assertEqual(body["channelBrandName"], "Blast Radius")
        self.assertNotIn("CyberPipe", body["channelBrandName"])

    def test_script_brand_follows_config_and_an_explicit_job_brand_wins(self):
        with mock.patch("pipeline.config.CHANNEL_BRAND_NAME", "Some Other Show"):
            self.assertEqual(self.stage(pipeline.stage_script, {"messageText": "x"})["channelBrandName"], "Some Other Show")
        self.assertEqual(self.stage(pipeline.stage_script, {"messageText": "x", "channelBrandName": "Job Show"})["channelBrandName"], "Job Show")

    def test_an_empty_brand_falls_back_rather_than_reaching_the_script(self):
        self.assertEqual(self.stage(pipeline.stage_script, {"messageText": "x", "channelBrandName": ""})["channelBrandName"], "Blast Radius")

    def test_research_gets_no_invented_source_channel(self):
        self.assertNotIn("channelName", self.stage(pipeline.stage_research, {"messageText": "x"}, outputs={}))

    def test_research_asks_for_depth_matching_the_script_length(self):
        # ContentPipe scales its key-fact target off this; without it a 9-minute script is
        # researched as though it were a 60-second one.
        self.assertEqual(self.stage(pipeline.stage_research, {"messageText": "x"}, outputs={})["targetDurationSec"], 585)
        body = self.stage(pipeline.stage_research, {"messageText": "x", "targetDurationSec": 60}, outputs={})
        self.assertEqual(body["targetDurationSec"], 60)

    def test_research_forwards_a_real_source_channel(self):
        self.assertEqual(self.stage(pipeline.stage_research, {"messageText": "x", "channelName": "r/netsec"}, outputs={})["channelName"], "r/netsec")


class SubmitJobBrandTests(unittest.TestCase):
    def submitted(self, *argv):
        import submit_job

        with mock.patch("sys.argv", ["submit_job.py", "--text", "story", *argv]), mock.patch("submit_job.db") as db:
            db.create_job.return_value = 1
            submit_job.main()
        return db.create_job.call_args.kwargs["input_payload"]

    def test_default_brand_is_the_channel_and_no_source_is_invented(self):
        payload = self.submitted()
        self.assertEqual(payload["channelBrandName"], "Blast Radius")
        self.assertNotIn("channelName", payload)

    def test_brand_and_source_are_separate_flags_and_the_old_flag_still_sets_the_brand(self):
        payload = self.submitted("--brand", "X Show", "--source-name", "r/netsec")
        self.assertEqual((payload["channelBrandName"], payload["channelName"]), ("X Show", "r/netsec"))
        self.assertEqual(self.submitted("--channel-name", "Legacy")["channelBrandName"], "Legacy")

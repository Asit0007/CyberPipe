"""pipeline._post: how ContentPipe's strict-mode status codes map onto the worker's control flow."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import pipeline
from exceptions import PermanentStageError, RateLimitError, StageBusy
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

"""Shared test scaffolding. Stdlib only (unittest) — CyberPipe has no test dependency.

Every test gets its own SQLite file and a Telegram that is a recording fake, so nothing
touches pipeline.db, the network, or a real bot. Run from the repo root:

    ./venv/bin/python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest import mock

import config
import db
import notifier
import pipeline

# Shaped like a bot token (digits:secret) so the redaction regex is really exercised, but the secret
# half is deliberately shorter than a real one (35 chars) so secret scanners don't flag this repo.
TEST_TOKEN = "123456789:AAfake-test-token-not-real-0000"
TEST_CHAT_ID = "42"


class FakeResponse:
    def __init__(self, status_code: int = 200, body: Optional[dict] = None, text: str = ""):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True, "result": []}
        self.text = text or str(self._body)
        self.headers: dict[str, str] = {}

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if not self.ok:
            import requests

            raise requests.HTTPError(
                f"{self.status_code} Client Error: Bad Request for url: "
                f"https://api.telegram.org/bot{TEST_TOKEN}/whatever"
            )


class FakeTelegram:
    """Stands in for `requests.post` inside notifier and telegram_poller."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_with: Optional[Exception] = None
        self.status_code = 200
        self.responder = None  # optional callable(method, kwargs) -> FakeResponse

    def __call__(self, url: str, **kwargs: Any) -> FakeResponse:
        method = url.rsplit("/", 1)[-1]
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.fail_with is not None:
            raise self.fail_with
        if self.responder is not None:
            return self.responder(method, kwargs)
        return FakeResponse(self.status_code)

    def of(self, method: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == method]

    @property
    def messages(self) -> list[dict[str, Any]]:
        return [c["json"] for c in self.of("sendMessage")]


class DbTestCase(unittest.TestCase):
    """Fresh DB + configured fake Telegram per test."""

    telegram_configured = True
    # The state-machine tests are about how a job moves through *some* stages (approve completes it, a rate limit
    # parks it), not about which stages exist, so they run against the original three; a job that approves its
    # script is COMPLETED there. Tests of the ContentRender stages set `stages = pipeline.PIPELINE_STAGES`.
    stages = ["research", "plan", "script"]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._patches = [
            mock.patch.object(config, "DB_PATH", os.path.join(self._tmp.name, "test.db")),
            mock.patch.object(config, "TELEGRAM_BOT_TOKEN", TEST_TOKEN if self.telegram_configured else ""),
            mock.patch.object(config, "TELEGRAM_CHAT_ID", TEST_CHAT_ID if self.telegram_configured else ""),
            mock.patch.object(pipeline, "PIPELINE_STAGES", list(self.stages)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.telegram = FakeTelegram()
        patch_post = mock.patch("notifier.requests.post", self.telegram)
        patch_post.start()
        self.addCleanup(patch_post.stop)
        notifier.reset_state_for_tests()
        db.init_db()

    # -- helpers -----------------------------------------------------------------------------
    def make_job(self, stage: str = "research", **fields: Any) -> int:
        job_id = db.create_job({"messageText": "story"}, stage)
        if fields:
            db.update_job(job_id, **fields)
        return job_id

    def set_raw(self, job_id: int, **columns: Any) -> None:
        """Write columns verbatim (bypasses update_job's bookkeeping, e.g. to backdate updated_at)."""
        sets = ", ".join(f"{k} = ?" for k in columns)
        with db.get_connection() as conn:
            conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*columns.values(), job_id))

    @staticmethod
    def ago(**delta: float) -> str:
        return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()

    @staticmethod
    def ahead(**delta: float) -> str:
        return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()

    @contextlib.contextmanager
    def captured_stdout(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yield buf

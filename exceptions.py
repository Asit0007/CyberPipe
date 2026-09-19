"""Control-flow exceptions stage functions raise instead of returning a result.

worker.run_job() catches these specifically, before the generic `except
Exception` backoff path. Anything else propagates and is treated as a plain
failure with exponential backoff.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


class RateLimitError(Exception):
    """Raise when a provider returns 429 / quota-exhausted.

    `retry_at`, if known (e.g. from a `Retry-After` header), is passed through
    to the scheduler as-is. Otherwise worker.py computes one via
    rate_limiter.compute_rate_limit_retry_at().
    """

    def __init__(self, provider: str, retry_at: Optional[datetime] = None, message: str = ""):
        self.provider = provider
        self.retry_at = retry_at
        super().__init__(message or f"rate limited by {provider}")


class HumanInputRequired(Exception):
    """Raise when a stage needs a Telegram approve/regenerate checkpoint.

    `payload` is the stage's already-computed draft output. It is stashed on
    the job row (pending_payload) so approval commits it without recomputing,
    and regeneration re-runs the stage fresh next time it's PENDING.
    """

    def __init__(self, question: str, options: list[str], payload: Optional[dict[str, Any]] = None):
        self.question = question
        self.options = options
        self.payload = payload or {}
        super().__init__(question)

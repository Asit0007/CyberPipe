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


class StageBusy(Exception):
    """Raise when the stage's work is already in flight elsewhere (ContentPipe answered 409
    `in_progress`: an identical /api/script run is still generating).

    Not a failure and not a rate limit: worker.py reschedules the job for `retry_at` without
    consuming an attempt and without paging anyone.
    """

    def __init__(self, message: str, retry_at: Optional[datetime] = None):
        self.retry_at = retry_at
        super().__init__(message)


class UpstreamUnavailable(Exception):
    """Raise when ContentPipe answered 503: every model provider behind it was overloaded or
    unreachable, after its own bounded wait. Transient by definition and not this job's fault, so
    worker.py parks the job (no attempt consumed) instead of burning the 5m/15m/45m/2h/6h backoff
    on an outage that a 30 s retry hint says is short. `retry_at` is that hint, if the response
    carried a Retry-After; the worker lengthens it the longer the outage lasts."""

    def __init__(self, provider: str, retry_at: Optional[datetime] = None, message: str = ""):
        self.provider = provider
        self.retry_at = retry_at
        super().__init__(message or f"{provider} unavailable")


class PermanentStageError(Exception):
    """Raise when retrying can never help (e.g. ContentPipe's `zero_quota`: the key has no quota
    and needs billing). worker.py fails the job immediately instead of burning every backoff attempt."""

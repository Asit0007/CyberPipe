"""Computes next_retry_at for a RateLimitError, in priority order:
1. An explicit Retry-After header (seconds or HTTP-date).
2. The provider's known daily reset time (config.PROVIDER_DAILY_RESET_UTC).
3. A flat default cooldown.

Kept separate from the generic per-stage backoff schedule in worker.py —
rate limits and ordinary failures are different problems with different
timelines.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import config


def parse_retry_after(header_value: Optional[str]) -> Optional[datetime]:
    if not header_value:
        return None
    header_value = header_value.strip()
    if header_value.isdigit():
        return datetime.now(timezone.utc) + timedelta(seconds=int(header_value))
    try:
        parsed = parsedate_to_datetime(header_value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def next_provider_reset(provider: str, now: Optional[datetime] = None) -> Optional[datetime]:
    reset_hhmm = config.PROVIDER_DAILY_RESET_UTC.get(provider)
    if not reset_hhmm:
        return None
    now = now or datetime.now(timezone.utc)
    hour, _, minute = reset_hhmm.partition(":")
    candidate = now.replace(hour=int(hour), minute=int(minute or 0), second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def compute_rate_limit_retry_at(
    provider: str,
    retry_after_header: Optional[str] = None,
    explicit_retry_at: Optional[datetime] = None,
) -> datetime:
    if explicit_retry_at is not None:
        return explicit_retry_at
    from_header = parse_retry_after(retry_after_header)
    if from_header is not None:
        return from_header
    from_provider = next_provider_reset(provider)
    if from_provider is not None:
        return from_provider
    return datetime.now(timezone.utc) + timedelta(seconds=config.DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS)


def compute_generic_backoff_retry_at(attempt: int) -> datetime:
    """attempt is 1-indexed (first failure = 1)."""
    schedule = config.BACKOFF_SCHEDULE_SECONDS
    seconds = schedule[min(attempt, len(schedule)) - 1]
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)

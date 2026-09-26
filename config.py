"""Environment loading and tunables. Stdlib only — no python-dotenv dependency.

.env is read once at import time. Restart scheduler.py / telegram_poller.py
after editing it (same gotcha as ContentPipe's server.ts).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")

# --- Telegram ---------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- Channel ------------------------------------------------------------------
# The show name ContentPipe writes into scripts ("Welcome back to ...", the publish package) when a job does
# not carry its own. This is the brand, not the tool: it must never default to "CyberPipe".
CHANNEL_BRAND_NAME = os.environ.get("CHANNEL_BRAND_NAME", "Blast Radius")

# --- ContentPipe (stages 1-3 call its API rather than reimplementing it) ---
CONTENTPIPE_BASE_URL = os.environ.get("CONTENTPIPE_BASE_URL", "http://localhost:3000")
CONTENTPIPE_TIMEOUT_SECONDS = int(os.environ.get("CONTENTPIPE_TIMEOUT_SECONDS", "180"))
# /api/script now makes many sequential LLM calls internally (one per
# narrative/visual-direction chunk — see ContentPipe's CLAUDE.md) rather than
# 2-3. Measured live 2026-09-19 under a free-tier quota crunch: individual
# 429s alone added 11-59s of retry wait each, across up to ~25 chunks — the
# old 180s default isn't close. This runs as a background job, not something
# a human is blocked on, so a long ceiling costs nothing in the common case.
CONTENTPIPE_SCRIPT_TIMEOUT_SECONDS = int(os.environ.get("CONTENTPIPE_SCRIPT_TIMEOUT_SECONDS", "1800"))

# --- ContentRender (stages 4-6: images, clips + narration, the Resolve bundle) ---
# ContentRender is a command line, not a server: every stage runs `cli.ts step` as a subprocess and gets one
# JSON outcome back. launchd has no PATH, so CONTENTRENDER_NODE should be an absolute path (default: the node
# found on PATH when this file is imported, which is right for a terminal and for a plist that sets PATH).
CONTENTRENDER_DIR = os.environ.get("CONTENTRENDER_DIR", str(BASE_DIR.parent / "ContentRender"))
CONTENTRENDER_NODE = os.environ.get("CONTENTRENDER_NODE", shutil.which("node") or "node")
# Seconds of work a single `step` may START (an asset already running finishes). Kept well under RUNNING_LEASE_SECONDS.
CONTENTRENDER_STEP_BUDGET_SECONDS = int(os.environ.get("CONTENTRENDER_STEP_BUDGET_SECONDS", "1200"))
# Hard ceiling on one subprocess: the budget plus the longest single thing it can be in the middle of
# (an AI clip, up to 10 min) and slack. Past this the process is killed and the attempt counts as an error.
CONTENTRENDER_TIMEOUT_SECONDS = int(os.environ.get("CONTENTRENDER_TIMEOUT_SECONDS", str(CONTENTRENDER_STEP_BUDGET_SECONDS + 900)))
# Where the approved script is written for ContentRender to read (one JSON file per job).
BRIEFS_DIR = os.environ.get("BRIEFS_DIR", str(BASE_DIR / "data" / "briefs"))

# --- Persistence -------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "pipeline.db"))

# --- Scheduler ---------------------------------------------------------------
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
NEEDS_INPUT_TIMEOUT_HOURS = int(os.environ.get("NEEDS_INPUT_TIMEOUT_HOURS", "72"))

# --- Retry / backoff ----------------------------------------------------------
# Generic per-stage failures (not rate limits): 5m, 15m, 45m, 2h, 6h — matches
# cyberpipeline-prompts.md Prompt 3. Rate limits use rate_limiter.py instead.
MAX_STAGE_ATTEMPTS = int(os.environ.get("MAX_STAGE_ATTEMPTS", "5"))
BACKOFF_SCHEDULE_SECONDS = [300, 900, 2700, 7200, 21600]

# Fallback cooldown when a RateLimitError carries no Retry-After and no known
# provider daily-reset time.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 24 * 3600

# Provider daily-reset times, UTC "HH:MM". Empty until stage 3+ call
# providers directly — the current stages 1-3 proxy through ContentPipe,
# which already does its own Gemini fallback/backoff internally, so a 429
# surfacing here just means ContentPipe itself is exhausted or down.
PROVIDER_DAILY_RESET_UTC: dict[str, str] = {}

# While ContentPipe's model providers are overloaded (its 503), a job re-polls at most this often once
# the outage has lasted a while: the wait grows to half the outage's age, capped here, so a multi-hour
# provider outage is checked every 15 minutes rather than every minute.
OVERLOAD_MAX_WAIT_SECONDS = int(os.environ.get("OVERLOAD_MAX_WAIT_SECONDS", "900"))

# A job stuck waiting on something external (rate limit, ContentPipe busy) gives up after this
# many days rather than retrying forever. Per-minute limits clear in seconds; a daily quota
# clears at midnight Pacific; seven days of either means something is actually wrong.
MAX_WAIT_DAYS = int(os.environ.get("MAX_WAIT_DAYS", "7"))

# A RUNNING job whose lease is older than this is treated as orphaned even if its recorded
# pid looks alive (pid reuse after a reboot; a lock written by another machine). It must
# exceed the longest legitimate stage: /api/script's own client timeout, plus slack.
RUNNING_LEASE_SECONDS = int(os.environ.get("RUNNING_LEASE_SECONDS", str(max(CONTENTPIPE_SCRIPT_TIMEOUT_SECONDS, CONTENTRENDER_TIMEOUT_SECONDS) + 600)))

# After a Telegram delivery fails, don't retry that same event more often than this.
TELEGRAM_RESEND_COOLDOWN_SECONDS = int(os.environ.get("TELEGRAM_RESEND_COOLDOWN_SECONDS", "300"))

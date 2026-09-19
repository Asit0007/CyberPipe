"""One-way Telegram notifications: completion, input-required, rate-limited,
failed. Idempotent per job via db.already_notified()/mark_notified() — each
event key is sent at most once per job.

No-ops with a warning if TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID aren't set yet,
so the rest of the orchestrator is runnable before Telegram is wired up.
"""
from __future__ import annotations

from typing import Any, Optional

import requests

import config
import db

API_BASE = "https://api.telegram.org"


def _configured() -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("[notifier] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — skipping notification")
        return False
    return True


def send_message(text: str, reply_markup: Optional[dict[str, Any]] = None) -> None:
    if not _configured():
        return
    url = f"{API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            print(f"[notifier] Telegram send failed: {resp.status_code} {resp.text}")
    except requests.RequestException as exc:
        print(f"[notifier] Telegram send raised: {exc}")


def _notify_once(job_id: int, event_key: str, text: str, reply_markup: Optional[dict[str, Any]] = None) -> None:
    job = db.get_job(job_id)
    if job is None or db.already_notified(job, event_key):
        return
    send_message(text, reply_markup)
    db.mark_notified(job_id, event_key)


def notify_input_required(job: dict[str, Any]) -> None:
    question = job["pending_question"].get("question", "Approval needed")
    options = job["pending_question"].get("options", ["approve", "regenerate"])
    keyboard = {
        "inline_keyboard": [[
            {"text": opt.capitalize(), "callback_data": f"job:{job['id']}:{opt}"} for opt in options
        ]]
    }
    text = f"🟡 Job #{job['id']} needs input (stage: {job['current_stage']})\n{question}"
    _notify_once(job["id"], f"needs_input:{job['current_stage']}:{job['attempt_count']}", text, keyboard)


def notify_rate_limited(job: dict[str, Any], provider: str, retry_at_iso: str) -> None:
    text = (
        f"⏳ Job #{job['id']} rate limited (stage: {job['current_stage']}, provider: {provider})\n"
        f"Retrying at {retry_at_iso}"
    )
    _notify_once(job["id"], f"rate_limited:{job['current_stage']}:{retry_at_iso}", text)


def notify_completed(job: dict[str, Any]) -> None:
    text = f"✅ Job #{job['id']} completed."
    _notify_once(job["id"], "completed", text)


def notify_failed(job: dict[str, Any]) -> None:
    error_excerpt = (job.get("last_error") or "")[:300]
    text = f"❌ Job #{job['id']} failed (stage: {job['current_stage']})\n{error_excerpt}"
    _notify_once(job["id"], f"failed:{job['current_stage']}:{job['attempt_count']}", text)

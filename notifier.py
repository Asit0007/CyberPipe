"""Telegram notifications: completion, input-required, rate-limited, failed.
Idempotent per job via db.already_notified()/mark_notified() — each event key is
sent at most once per job.

"Notified" means Telegram *accepted* the message. An event that failed to send
(Telegram down, bot not configured yet, a rejected request) is not recorded, and
resend_missed_notifications() — called every scheduler tick — delivers it later.
Recording it anyway would silently lose an approval request the human is waiting on.

Messages are plain text on purpose. They carry story titles and error text ("AT&T",
"<script>"), and Telegram's HTML mode answers 400 to any unescaped `<` or `&`.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

import config
import db
import review

API_BASE = "https://api.telegram.org"
MAX_TEXT_CHARS = 4096      # Telegram's sendMessage limit
MAX_CAPTION_CHARS = 1024   # ... and sendDocument's caption limit

_monotonic = time.monotonic
_last_failure: dict[tuple[int, str], float] = {}
_warned_unconfigured = False

_BOT_URL_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def reset_state_for_tests() -> None:
    """Test hook: module-level throttling state must not leak between tests."""
    global _monotonic, _warned_unconfigured
    _monotonic = time.monotonic
    _warned_unconfigured = False
    _last_failure.clear()


def redact_secrets(text: str) -> str:
    """Mask the bot token. `requests` puts the request URL — /bot<TOKEN>/method — in its exception
    text, and error bodies can echo it, so anything logged from a Telegram call goes through here."""
    text = _BOT_URL_TOKEN.sub("bot<redacted>", text)
    token = config.TELEGRAM_BOT_TOKEN
    if token:
        text = text.replace(token, "<redacted>")
        secret = token.partition(":")[2]
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _configured(*, quiet: bool = False) -> bool:
    global _warned_unconfigured
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        return True
    if not quiet and not _warned_unconfigured:
        # Once per process, not once per tick: events are held and sent when this is configured.
        print("[notifier] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — notifications are held until they are")
        _warned_unconfigured = True
    return False


def _call(method: str, **request_kwargs: Any) -> bool:
    """One Telegram API call. True only if Telegram accepted it."""
    url = f"{API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, timeout=180 if "files" in request_kwargs else 10, **request_kwargs)
    except requests.RequestException as exc:
        print(f"[notifier] Telegram {method} failed: {redact_secrets(str(exc))}")
        return False
    if not resp.ok:
        print(f"[notifier] Telegram {method} rejected: {resp.status_code} {redact_secrets(resp.text)[:300]}")
        return False
    return True


def send_message(text: str, reply_markup: Optional[dict[str, Any]] = None) -> bool:
    if not _configured():
        return False
    if len(text) > MAX_TEXT_CHARS:
        text = text[: MAX_TEXT_CHARS - 1] + "…"
    payload: dict[str, Any] = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _call("sendMessage", json=payload)


def send_document(filename: str, content: bytes, caption: Optional[str] = None) -> bool:
    if not _configured():
        return False
    data: dict[str, Any] = {"chat_id": config.TELEGRAM_CHAT_ID}
    if caption:
        data["caption"] = caption[:MAX_CAPTION_CHARS]
    return _call("sendDocument", data=data, files={"document": (filename, content, "text/markdown")})


PHOTOS_PER_GROUP = 10  # sendMediaGroup's maximum


def send_photos(photos: list[dict[str, Any]]) -> bool:
    """Photos as albums of up to ten (one message per album, so a 14-scene script is two). A lone photo cannot be an
    album, so a remainder of one goes as a plain photo. True only if every call was accepted."""
    if not _configured():
        return False
    ok = True
    for i in range(0, len(photos), PHOTOS_PER_GROUP):
        chunk = photos[i:i + PHOTOS_PER_GROUP]
        if len(chunk) == 1:
            p = chunk[0]
            data = {"chat_id": config.TELEGRAM_CHAT_ID, "caption": p["caption"][:MAX_CAPTION_CHARS]}
            ok &= _call("sendPhoto", data=data, files={"photo": (p["filename"], p["content"], "image/jpeg")})
            continue
        media = [{"type": "photo", "media": f"attach://p{n}", "caption": p["caption"][:MAX_CAPTION_CHARS]} for n, p in enumerate(chunk)]
        files = {f"p{n}": (p["filename"], p["content"], "image/jpeg") for n, p in enumerate(chunk)}
        ok &= _call("sendMediaGroup", data={"chat_id": config.TELEGRAM_CHAT_ID, "media": json.dumps(media)}, files=files)
    return ok


def send_audio(filename: str, content: bytes, caption: str = "") -> bool:
    if not _configured():
        return False
    data: dict[str, Any] = {"chat_id": config.TELEGRAM_CHAT_ID, "title": filename.rsplit(".", 1)[0]}
    if caption:
        data["caption"] = caption[:MAX_CAPTION_CHARS]
    return _call("sendAudio", data=data, files={"audio": (filename, content, "audio/mpeg")})


def send_video(filename: str, content: bytes, caption: str = "") -> bool:
    if not _configured():
        return False
    data: dict[str, Any] = {"chat_id": config.TELEGRAM_CHAT_ID, "supports_streaming": "true"}
    if caption:
        data["caption"] = caption[:MAX_CAPTION_CHARS]
    return _call("sendVideo", data=data, files={"video": (filename, content, "video/mp4")})


def send_media(attachments: list[dict[str, Any]]) -> list[str]:
    """Sends a media gate's files in a sensible order (photos, then audio, then video, then documents) and returns
    the names of any that Telegram did not accept."""
    failed: list[str] = []
    photos = [a for a in attachments if a["kind"] == "photo"]
    if photos and not send_photos(photos):
        failed.append(f"{len(photos)} photo(s)")
    for a in attachments:
        if a["kind"] == "audio" and not send_audio(a["filename"], a["content"], a["caption"]):
            failed.append(a["filename"])
        elif a["kind"] == "video" and not send_video(a["filename"], a["content"], a["caption"]):
            failed.append(a["filename"])
        elif a["kind"] == "document" and not send_document(a["filename"], a["content"], a["caption"]):
            failed.append(a["filename"])
    return failed


def _notify_once(
    job_id: int,
    event_key: str,
    text: str,
    reply_markup: Optional[dict[str, Any]] = None,
    document: Optional[tuple[str, bytes]] = None,
    media: Optional[list[dict[str, Any]]] = None,
    media_problems: Optional[list[str]] = None,
) -> bool:
    """Send at most once per (job, event). True if this call delivered it."""
    job = db.get_job(job_id)
    if job is None or db.already_notified(job, event_key):
        return False
    failed_at = _last_failure.get((job_id, event_key))
    if failed_at is not None and _monotonic() - failed_at < config.TELEGRAM_RESEND_COOLDOWN_SECONDS:
        return False
    if not _configured():
        return False

    if document is not None and not send_document(*document, caption=f"Job #{job_id} — full draft"):
        # Don't hold the approval back for it, but never let the human approve unaware.
        text += f"\n⚠️ The draft could not be attached to this message. Read job #{job_id}'s pending_payload before approving."
    unsent = list(media_problems or [])
    if media:
        unsent += send_media(media)
    if unsent:
        text += f"\n⚠️ Could not attach: {', '.join(unsent)}. Look in the run folder before approving."
    if not send_message(text, reply_markup):
        _last_failure[(job_id, event_key)] = _monotonic()
        return False
    _last_failure.pop((job_id, event_key), None)
    db.mark_notified(job_id, event_key)
    return True


def notify_input_required(job: dict[str, Any]) -> bool:
    pending = job.get("pending_question") or {}
    question = pending.get("question", "Approval needed")
    options = pending.get("options", ["approve", "regenerate"])
    keyboard = {
        "inline_keyboard": [[
            {"text": opt.capitalize(), "callback_data": f"job:{job['id']}:{opt}"} for opt in options
        ]]
    }
    text = f"🟡 Job #{job['id']} needs input (stage: {job['current_stage']})\n{question}"
    draft = review.render_review_markdown(job)
    document = (f"job-{job['id']}-script-draft.md", draft.encode("utf-8")) if draft else None
    media, media_problems = review.media_attachments(job)
    if (job.get("pending_payload") or {}).get("gate") in ("images", "narration", "final"):
        text += f"\nRegenerate redoes everything at this step; /regen {job['id']} 3,7 redoes just those scenes." if job["current_stage"] in ("images", "narration") else ""
    return _notify_once(job["id"], f"needs_input:{job['current_stage']}:{job['attempt_count']}", text, keyboard, document, media, media_problems)


# A wait at least this long changes what the human should expect ("tomorrow", not "in a minute").
LONG_WAIT_SECONDS = 15 * 60


def _seconds_until(iso: str) -> float:
    try:
        when = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return float("inf")  # unparseable: treat as long, so it is announced rather than swallowed
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - datetime.now(timezone.utc)).total_seconds()


def notify_rate_limited(job: dict[str, Any], provider: str, retry_at_iso: str) -> bool:
    """One message per unbroken wait (job.wait_since), not one per retry. Keyed on the retry time,
    a per-minute limit re-polled every minute paged the human every minute for up to MAX_WAIT_DAYS.
    A short wait that turns into a long one (a daily cap) still gets its own message."""
    long_wait = _seconds_until(retry_at_iso) >= LONG_WAIT_SECONDS
    text = (
        f"⏳ Job #{job['id']} rate limited (stage: {job['current_stage']}, provider: {provider})\n"
        f"Retrying at {retry_at_iso}"
    )
    if not long_wait:
        text += "\nShort wait: further retries stay quiet unless it becomes a long one."
    episode = job.get("wait_since") or retry_at_iso
    return _notify_once(job["id"], f"rate_limited:{job['current_stage']}:{episode}:{'long' if long_wait else 'short'}", text)


def notify_upstream_unavailable(job: dict[str, Any], waited_sec: float) -> bool:
    """One message per outage (job.wait_since), sent once it has lasted LONG_WAIT_SECONDS. Retries are
    silent before that, and after it, so a provider outage neither pages every minute nor goes unmentioned."""
    text = (
        f"⚠️ Job #{job['id']} has waited {int(waited_sec // 60)} min on ContentPipe (stage: {job['current_stage']}): "
        f"every model provider it uses is overloaded or unreachable.\n"
        f"Still retrying, no attempt used. It fails only after {config.MAX_WAIT_DAYS} days of continuous waiting."
    )
    episode = job.get("wait_since") or "unknown"
    return _notify_once(job["id"], f"unavailable:{job['current_stage']}:{episode}", text)


def notify_completed(job: dict[str, Any]) -> bool:
    text = f"✅ Job #{job['id']} completed."
    bundle = ((job.get("stage_outputs") or {}).get("bundle") or {}).get("review") or {}
    if bundle.get("summary"):
        text += f"\n{bundle['summary'].splitlines()[0]}"  # first line names the bundle folder
    return _notify_once(job["id"], "completed", text)


def notify_failed(job: dict[str, Any]) -> bool:
    error_excerpt = (job.get("last_error") or "")[:300]
    text = f"❌ Job #{job['id']} failed (stage: {job['current_stage']})\n{error_excerpt}"
    return _notify_once(job["id"], f"failed:{job['current_stage']}:{job['attempt_count']}", text)


def resend_missed_notifications() -> int:
    """Deliver messages that never got through: a job waiting on a human whose approval request was
    lost, or a finished job whose completion/failure message was. Idempotent — an event already
    delivered is skipped — and throttled per event after a failed attempt. Returns how many were sent.
    """
    if not _configured(quiet=True):
        return 0
    handlers = {"NEEDS_INPUT": notify_input_required, "COMPLETED": notify_completed, "FAILED": notify_failed}
    return sum(1 for job in db.jobs_with_status(tuple(handlers)) if handlers[job["status"]](job))

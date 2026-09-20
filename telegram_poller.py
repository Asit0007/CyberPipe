"""Long-polls getUpdates for inline-button taps of the form
job:<id>:<answer> and routes them to worker.resume_from_input(). This
prototype only handles those callback buttons — the slash-command dashboard
(/status, /jobs, /retry, ...) from Prompt 5 is a later phase, not scaffolded
yet.

Only TELEGRAM_CHAT_ID is authorized; every other chat is logged and ignored.

Robustness rules (each has a regression test in tests/test_poller.py):
* An update is *consumed* whether or not handling it succeeds. Leaving the offset
  unsaved on an error replays that update forever and blocks every later tap.
* Acknowledging a tap (answerCallbackQuery) is best-effort: Telegram rejects it once
  the tap is old (e.g. the Mac was asleep), and the tap itself is already applied.
* The bot token lives in every request URL, and `requests` puts the URL in its
  exception text — so nothing from an API call is logged without redaction.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import requests

import config
import db
import notifier
import worker

API_BASE = "https://api.telegram.org"
LONG_POLL_TIMEOUT_SECONDS = 30
OFFSET_KV_KEY = "telegram_update_offset"
ERROR_PAUSE_SECONDS = 5


class TelegramAPIError(RuntimeError):
    """A failed Telegram API call, with the bot token already scrubbed from the message."""


def _api(method: str, **params: Any) -> dict[str, Any]:
    url = f"{API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=params, timeout=LONG_POLL_TIMEOUT_SECONDS + 10)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        # `from None`: the chained original would print the token-bearing URL in any traceback.
        raise TelegramAPIError(f"{method} failed: {notifier.redact_secrets(str(exc))}") from None


def _answer(callback_query_id: str, text: Optional[str] = None) -> None:
    params: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        params["text"] = text
    try:
        _api("answerCallbackQuery", **params)
    except TelegramAPIError as exc:
        print(f"[telegram_poller] could not acknowledge tap: {exc}")


def _is_authorized(from_user: dict[str, Any]) -> bool:
    return str(from_user.get("id")) == str(config.TELEGRAM_CHAT_ID)


def _handle_callback_query(callback_query: dict[str, Any]) -> None:
    query_id = callback_query["id"]
    from_user = callback_query.get("from", {})
    if not _is_authorized(from_user):
        print(f"[telegram_poller] ignoring callback from unauthorized chat {from_user.get('id')}")
        _answer(query_id, "Unauthorized")
        return

    parts = callback_query.get("data", "").split(":")
    if len(parts) != 3 or parts[0] != "job":
        _answer(query_id)
        return

    _, job_id_str, answer = parts
    try:
        job_id = int(job_id_str)
    except ValueError:
        _answer(query_id, "Bad job id")
        return

    try:
        applied = worker.resume_from_input(job_id, answer)
    except Exception as exc:  # noqa: BLE001 — tell the human to tap again rather than wedge the queue
        print(f"[telegram_poller] job #{job_id}: resuming with {answer!r} raised: {notifier.redact_secrets(str(exc))}")
        _answer(query_id, "Something went wrong — please tap again")
        return

    if applied:
        _answer(query_id, f"Recorded: {answer}")
        print(f"[telegram_poller] job #{job_id} resumed with answer={answer!r}")
    else:
        _answer(query_id, "Already handled")
        print(f"[telegram_poller] job #{job_id}: {answer!r} ignored (already handled, or nothing to apply)")


def _process_update(update: dict[str, Any]) -> None:
    if "callback_query" in update:
        _handle_callback_query(update["callback_query"])
    # Plain text messages (slash commands) are out of scope for this prototype.


def _get_offset() -> Optional[int]:
    raw = db.get_kv(OFFSET_KV_KEY)
    return int(raw) if raw else None


def _set_offset(offset: int) -> None:
    db.set_kv(OFFSET_KV_KEY, str(offset))


def poll_once() -> None:
    offset = _get_offset()
    params: dict[str, Any] = {"timeout": LONG_POLL_TIMEOUT_SECONDS}
    if offset is not None:
        params["offset"] = offset
    result = _api("getUpdates", **params)
    for update in result.get("result", []):
        try:
            _process_update(update)
        except Exception as exc:  # noqa: BLE001 — a poison update must be skipped, not replayed forever
            print(f"[telegram_poller] update {update.get('update_id')} skipped after error: {notifier.redact_secrets(str(exc))}")
        _set_offset(update["update_id"] + 1)


def main() -> None:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("[telegram_poller] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — nothing to poll, exiting")
        return
    db.init_db()
    print("[telegram_poller] started long-polling")
    while True:
        try:
            poll_once()
        except Exception as exc:  # noqa: BLE001 — the loop must survive anything, and must not spin
            print(f"[telegram_poller] poll failed: {notifier.redact_secrets(str(exc))}")
            time.sleep(ERROR_PAUSE_SECONDS)


if __name__ == "__main__":
    main()

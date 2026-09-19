"""Long-polls getUpdates for inline-button taps of the form
job:<id>:<answer> and routes them to worker.resume_from_input(). This
prototype only handles those callback buttons — the slash-command dashboard
(/status, /jobs, /retry, ...) from Prompt 5 is a later phase, not scaffolded
yet.

Only TELEGRAM_CHAT_ID is authorized; every other chat is logged and ignored.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import requests

import config
import db
import worker

API_BASE = "https://api.telegram.org"
LONG_POLL_TIMEOUT_SECONDS = 30
OFFSET_KV_KEY = "telegram_update_offset"


def _api(method: str, **params: Any) -> dict[str, Any]:
    url = f"{API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, json=params, timeout=LONG_POLL_TIMEOUT_SECONDS + 10)
    resp.raise_for_status()
    return resp.json()


def _is_authorized(from_user: dict[str, Any]) -> bool:
    return str(from_user.get("id")) == str(config.TELEGRAM_CHAT_ID)


def _handle_callback_query(callback_query: dict[str, Any]) -> None:
    from_user = callback_query.get("from", {})
    data = callback_query.get("data", "")
    if not _is_authorized(from_user):
        print(f"[telegram_poller] ignoring callback from unauthorized chat {from_user.get('id')}")
        _api("answerCallbackQuery", callback_query_id=callback_query["id"], text="Unauthorized")
        return

    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "job":
        _api("answerCallbackQuery", callback_query_id=callback_query["id"])
        return

    _, job_id_str, answer = parts
    try:
        job_id = int(job_id_str)
    except ValueError:
        _api("answerCallbackQuery", callback_query_id=callback_query["id"], text="Bad job id")
        return

    worker.resume_from_input(job_id, answer)
    _api("answerCallbackQuery", callback_query_id=callback_query["id"], text=f"Recorded: {answer}")
    print(f"[telegram_poller] job #{job_id} resumed with answer={answer!r}")


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
    updates = result.get("result", [])
    for update in updates:
        _process_update(update)
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
        except requests.RequestException as exc:
            print(f"[telegram_poller] getUpdates failed: {exc}")
            time.sleep(5)
        except Exception as exc:  # noqa: BLE001 — the loop must survive a single bad update
            print(f"[telegram_poller] update handling raised: {exc}")


if __name__ == "__main__":
    main()

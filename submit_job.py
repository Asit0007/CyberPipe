"""CLI to enqueue a job for manual testing, e.g.:

    python3 submit_job.py --text "CVE-2026-XXXX: ..." --url "https://..." --url "https://..."

The scheduler picks it up on its next 60s tick. Real ingestion (Telegram
forward, CVE feed, etc.) is a later phase — this is just enough to exercise
the orchestrator end to end.
"""
from __future__ import annotations

import argparse

import config
import db
from pipeline import DEFAULT_CYBER_TONE, DEFAULT_TARGET_DURATION_SEC, PIPELINE_STAGES


def main() -> None:
    parser = argparse.ArgumentParser(description="Enqueue a CyberPipe job")
    parser.add_argument("--text", required=True, help="The news story / breach report / CVE text")
    parser.add_argument("--url", action="append", default=[], dest="urls", help="Source URL (repeatable)")
    parser.add_argument(
        "--brand", "--channel-name", dest="brand", default=config.CHANNEL_BRAND_NAME,
        help="Show name written into the script (default: CHANNEL_BRAND_NAME, i.e. the channel, not the tool)",
    )
    parser.add_argument("--source-name", default=None, help="Where the story came from (e.g. a Telegram feed); omit if unknown")
    parser.add_argument("--target-format", default="16:9", choices=["16:9", "9:16"])
    parser.add_argument("--target-tone", default=DEFAULT_CYBER_TONE)
    parser.add_argument("--target-duration-sec", type=int, default=DEFAULT_TARGET_DURATION_SEC)
    args = parser.parse_args()

    db.init_db()
    job_id = db.create_job(
        input_payload={
            "messageText": args.text,
            "sourceUrls": args.urls,
            **({"channelName": args.source_name} if args.source_name else {}),
            "channelBrandName": args.brand,
            "targetFormat": args.target_format,
            "targetTone": args.target_tone,
            "targetDurationSec": args.target_duration_sec,
        },
        first_stage=PIPELINE_STAGES[0],
    )
    print(f"Created job #{job_id}, stage={PIPELINE_STAGES[0]}. Start scheduler.py to run it.")


if __name__ == "__main__":
    main()

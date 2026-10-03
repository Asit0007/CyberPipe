"""Enqueue a job. Two ways in:

    # A story whose script already exists: from ContentPipe's `npm run story:start` (the usual way, 2026-10-03), or a
    # ContentRender run started by hand. Runs from the stills onward. See adopt.py.
    ./venv/bin/python submit_job.py adopt --brief "../ContentPipe/.runs/story-<slug>/brief.json"

    # A story from scratch: research -> plan -> script through ContentPipe, then the same render stages.
    ./venv/bin/python submit_job.py --text "CVE-2026-XXXX: ..." --url "https://..." --url "https://..."

Only one story runs at a time (they share the free image and clip quotas); --force overrides that.
The scheduler picks a new job up on its next 60 s tick.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import adopt
import config
import db
from pipeline import DEFAULT_CYBER_TONE, DEFAULT_TARGET_DURATION_SEC, PIPELINE_STAGES


def main_adopt(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="submit_job.py adopt", description="Run a story that already has a script, from the stills onward")
    parser.add_argument("--brief", required=True, type=Path, help="The script JSON: a story's brief.json, or a ContentRender run's own brief.json")
    parser.add_argument("--video-id", default=None, help="Run name; default: an existing run for this title, else <date>-<title slug>")
    parser.add_argument("--force", action="store_true", help="Start even though another job is still running")
    parser.add_argument("--no-export", dest="export", action="store_false", help="Don't ask ContentPipe for a fresh Markdown export")
    args = parser.parse_args(argv)
    db.init_db()
    try:
        _, notes = adopt.adopt(args.brief, video_id=args.video_id, force=args.force, export=args.export)
    except adopt.AdoptError as exc:
        print(f"Not adopted: {exc}", file=sys.stderr)
        return 1
    for note in notes:
        print(note)
    print("The scheduler picks it up within a minute; its gates arrive in Telegram.")
    return 0


def main_new(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Enqueue a CyberPipe job from a story (research -> plan -> script -> media)")
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
    parser.add_argument("--force", action="store_true", help="Start even though another job is still running")
    args = parser.parse_args(argv)

    db.init_db()
    try:
        adopt.guard_one_story(args.force)
    except adopt.AdoptError as exc:
        print(f"Not started: {exc}", file=sys.stderr)
        return 1
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
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["adopt"]:
        return main_adopt(argv[1:])
    return main_new(argv)


if __name__ == "__main__":
    sys.exit(main())

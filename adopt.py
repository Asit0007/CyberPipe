"""Hand CyberPipe a story whose script already exists, so it runs from the stills onward with Telegram gates.

Two cases, one command (`submit_job.py adopt --brief <file>`):

* A story scripted by hand with ContentPipe's `npm run story:start` (the owner's way of starting a story, 2026-10-03):
  `ContentPipe/.runs/story-<slug>/brief.json`. Edit that file first if the script needs changing; adopt checks it and,
  when the story's research and plan sit beside it, writes a fresh Markdown export so the export matches the edits.
* A ContentRender run already part-made by hand: pass that run's own `brief.json` (or let adopt find the run by
  title). The job starts at whichever gate the run is at, and every finished still and voice line is kept.

Safety: ContentRender's `step` with a brief that differs from its run's moves the whole run aside and starts over
(src/checkpoint/manifest.ts). So adopt never guesses. It asks ContentRender's `status`, which refuses a mismatched
brief without changing anything, and stops with a clear message when a run for the same title was made from a
different brief.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

import config
import db
import pipeline

ACTIVE_STATUSES = ("PENDING", "RUNNING", "SCHEDULED", "NEEDS_INPUT")
EXPORT_TIMEOUT_SECONDS = 120


class AdoptError(Exception):
    """A story that cannot be adopted as asked; the message says what to do instead."""


def load_brief(path: Path) -> dict[str, Any]:
    """The brief, checked enough that a hand edit that broke it fails here rather than at the first render step."""
    try:
        brief = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AdoptError(f"cannot read {path}: {exc.strerror or exc}") from None
    except ValueError as exc:
        raise AdoptError(f"{path} is not valid JSON ({exc}); fix the edit and try again") from None
    problems = []
    if not isinstance(brief, dict):
        raise AdoptError(f"{path} must hold one JSON object (a ContentPipe script)")
    if not str(brief.get("title") or "").strip():
        problems.append("it has no title")
    scenes = brief.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        problems.append("it has no scenes")
    else:
        for n, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict) or not str(scene.get("id") or "").strip():
                problems.append(f"scene {n} has no id")
            elif not str(scene.get("narration") or "").strip():
                problems.append(f"scene {n} ({scene['id']}) has no narration")
    if problems:
        raise AdoptError(f"{path} is not a usable script: {'; '.join(problems[:5])}")
    return brief


def active_jobs() -> list[dict[str, Any]]:
    return db.jobs_with_status(ACTIVE_STATUSES)


def guard_one_story(force: bool) -> None:
    """One story at a time (owner, 2026-10-03): they share the free image and clip quotas."""
    if force:
        return
    busy = active_jobs()
    if busy:
        listed = ", ".join(f"#{j['id']} ({j['current_stage']}, {j['status']})" for j in busy)
        raise AdoptError(f"job {listed} is still running; finish it first, or pass --force to run two stories at once")


def _status(brief_path: Path, vid: str) -> dict[str, Any]:
    return pipeline.render_cli("status", brief_path, vid)


def stage_for(status: dict[str, Any]) -> str:
    """Which CyberPipe stage picks up a ContentRender run, from the gates already approved there."""
    approvals = status.get("approvals") or {}
    approved = lambda gate: (approvals.get(gate) or {}).get("status") == "approved"  # noqa: E731
    if not approved("images"):
        return "images"
    if not approved("narration"):
        return "narration"
    if not approved("final"):
        return "bundle"
    raise AdoptError(f"run {status.get('videoId')} is already delivered; there is nothing left to do")


def _runs_with_title(title: str) -> list[str]:
    """Run folders whose own brief has this title (moved-aside `.superseded-` copies skipped)."""
    found = []
    root = pipeline.runs_dir()
    if not root.is_dir():
        return found
    for run in sorted(root.iterdir()):
        brief_file = run / "brief.json"
        if ".superseded-" in run.name or not (run / "manifest.json").is_file() or not brief_file.is_file():
            continue
        try:
            other = json.loads(brief_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(other, dict) and str(other.get("title") or "").strip() == title.strip():
            found.append(run.name)
    return found


def choose_run(brief_path: Path, brief: dict[str, Any], video_id: Optional[str], today: str) -> tuple[str, str]:
    """(video id, stage). An existing run is used only when ContentRender agrees the brief is the run's own."""
    if video_id is not None:
        if not pipeline.VIDEO_ID_RE.match(video_id):
            raise AdoptError(f"--video-id {video_id!r} must be 1-64 letters, digits, '-' or '_'")
        if not (pipeline.runs_dir() / video_id / "manifest.json").is_file():
            return video_id, "images"
        status = _status(brief_path, video_id)
        if status.get("status") != "ok":
            problems = "; ".join(status.get("problems") or ["unknown refusal"])
            if pipeline.BRIEF_CHANGED in problems:
                raise AdoptError(
                    f"run {video_id} was made from a different brief, and continuing it with this one would start it "
                    f"over. Pass the run's own brief ({pipeline.runs_dir() / video_id / 'brief.json'}), or a new --video-id"
                )
            raise AdoptError(f"ContentRender refused to report on run {video_id}: {problems}")
        return video_id, stage_for(status)

    mismatched = []
    for vid in _runs_with_title(brief["title"]):
        status = _status(brief_path, vid)
        if status.get("status") == "ok":
            return vid, stage_for(status)
        mismatched.append(vid)
    if mismatched:
        run = mismatched[0]
        raise AdoptError(
            f"a run for \"{brief['title']}\" already exists ({run}) but was made from a different brief. To continue it "
            f"and keep its stills, pass its own brief: --brief \"{pipeline.runs_dir() / run / 'brief.json'}\". To start a "
            f"separate run from this brief, pass --video-id <new id>"
        )
    base = f"{today}-{pipeline.slugify(brief['title'])}"
    vid, n = base, 2
    while (pipeline.runs_dir() / vid).exists():
        vid, n = f"{base}-{n}", n + 1
    return vid, "images"


def re_export(brief_path: Path, brief: dict[str, Any]) -> Optional[str]:
    """For a `story:start` brief (research.json and plan.json beside it), ask ContentPipe for a fresh Markdown export,
    so a hand-edited brief and its export agree. Returns the note to print; never fails the adoption."""
    folder = brief_path.parent
    research, plan = folder / "research.json", folder / "plan.json"
    if not (research.is_file() and plan.is_file()):
        return None
    try:
        body = {
            "script": brief,
            "research": json.loads(research.read_text(encoding="utf-8")),
            "plan": json.loads(plan.read_text(encoding="utf-8")),
            "channelBrandName": config.CHANNEL_BRAND_NAME,
        }
        resp = requests.post(f"{config.CONTENTPIPE_BASE_URL}/api/export/markdown", json=body, timeout=EXPORT_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return f"fresh export: {resp.json().get('relativePath', '(written)')}"
    except (OSError, ValueError, requests.RequestException) as exc:
        return f"warning: could not write a fresh export ({str(exc)[:160]}); the old export may not match your edits"


def adopt(brief_path: Path, video_id: Optional[str] = None, force: bool = False, export: bool = True,
          now: Optional[datetime] = None) -> tuple[int, list[str]]:
    brief_path = brief_path.expanduser().resolve()
    brief = load_brief(brief_path)
    guard_one_story(force)
    today = (now or datetime.now(timezone.utc)).astimezone().strftime("%Y-%m-%d")
    vid, stage = choose_run(brief_path, brief, video_id, today)
    for job in active_jobs():
        if pipeline.video_id(job) == vid:
            raise AdoptError(f"job #{job['id']} is already driving run {vid}")

    job_id = db.create_job(
        {"videoId": vid, "adoptedFrom": str(brief_path), "channelBrandName": config.CHANNEL_BRAND_NAME}, first_stage=stage
    )
    db.update_job(job_id, stage_outputs={"script": brief})
    existing = (pipeline.runs_dir() / vid / "manifest.json").is_file()
    notes = [f"job #{job_id}: {'continuing' if existing else 'new'} run {vid}, starting at the {stage} stage"]
    if export:
        note = re_export(brief_path, brief)
        if note:
            notes.append(note)
    return job_id, notes

"""Stage functions. Stages 1-3 call ContentPipe's existing /api/research,
/api/plan, /api/script endpoints rather than reimplementing research/script
generation — see the "CyberPipe orchestrator scope" decision. Stages 4-5
(hero image/video generation, FFmpeg/Remotion assembly) are out of scope for
this prototype: no TTS/image/video provider keys exist yet, and the goal
right now is proving the durability + human-in-the-loop core, not spending
on generation.

Contract (cyberpipeline-prompts.md Prompt 4):
    def stage_x(job: dict, outputs: dict) -> dict
Raise RateLimitError / HumanInputRequired instead of returning for those
cases; let any other exception propagate for worker.py's backoff handling.

Known integration gaps vs. the original spec (see CLAUDE.md "Integration
gaps" section) — ContentPipe's actual schemas don't carry entities/
CVE-hash artifacts, retention_beats[], or midroll_markers[]. Mid-roll
timestamps here are derived locally from scene durationEst instead of
expected from the API.
"""
from __future__ import annotations

from typing import Any

import requests

import config
from exceptions import HumanInputRequired, RateLimitError

PIPELINE_STAGES = ["research", "plan", "script"]


def next_stage_after(stage: str) -> str | None:
    idx = PIPELINE_STAGES.index(stage)
    if idx + 1 < len(PIPELINE_STAGES):
        return PIPELINE_STAGES[idx + 1]
    return None


def _post(path: str, body: dict[str, Any], provider: str) -> dict[str, Any]:
    url = f"{config.CONTENTPIPE_BASE_URL}{path}"
    try:
        resp = requests.post(url, json=body, timeout=config.CONTENTPIPE_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RuntimeError(f"{path} request failed: {exc}") from exc
    if resp.status_code == 429:
        raise RateLimitError(provider, message=f"{path} returned 429")
    if not resp.ok:
        raise RuntimeError(f"{path} returned {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def stage_research(job: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    payload = job["input_payload"]
    body = {
        "messageText": payload["messageText"],
        "channelName": payload.get("channelName", "CyberPipe"),
        "sourceUrls": payload.get("sourceUrls", []),
    }
    return _post("/api/research", body, provider="contentpipe:research")


def stage_plan(job: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    payload = job["input_payload"]
    body = {
        "researchData": outputs["research"],
        "targetFormat": payload.get("targetFormat", "16:9"),
        "targetTone": payload.get("targetTone", "Deep Dive Documentary"),
    }
    return _post("/api/plan", body, provider="contentpipe:plan")


def stage_script(job: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    payload = job["input_payload"]
    body = {
        "videoPlan": outputs["plan"],
        "researchData": outputs["research"],
        "channelBrandName": payload.get("channelBrandName", "CyberPipe"),
    }
    draft = _post("/api/script", body, provider="contentpipe:script")

    scene_count = len(draft.get("scenes", []))
    total_duration = draft.get("estimatedTotalDuration", 0)
    question = (
        f'Approve script draft "{draft.get("title", "untitled")}"? '
        f"{scene_count} scenes, ~{total_duration}s runtime."
    )
    # Mandatory human checkpoint (cyberpipeline-prompts.md Prompt 1 §Stage 2).
    # worker.py stashes `payload` as job.pending_payload; approving commits it
    # without recomputing, regenerating clears it and re-runs this stage.
    raise HumanInputRequired(question=question, options=["approve", "regenerate"], payload=draft)


STAGE_FUNCTIONS = {
    "research": stage_research,
    "plan": stage_plan,
    "script": stage_script,
}

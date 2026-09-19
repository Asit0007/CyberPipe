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

Integration gaps vs. the original spec, and how they're handled here (see
CLAUDE.md "Known integration gaps" for the full writeup — this is the fix,
that's the record of why it was needed):

- ContentPipe's researchSchema (server/schemas.ts) hard-requires
  hnCommunitySentiment and infotainmentAngles — Stage 1 always returns them,
  framed for ContentPipe's original HN/tech-infotainment niche. Neither
  /api/plan nor /api/script validates its input shape (both just
  JSON.stringify(researchData) into the prompt), so _reframe_research_for_forwarding
  drops those two fields from what's forwarded downstream, without touching
  ContentPipe's schema. stage_outputs['research'] keeps ContentPipe's
  original response intact; only the forwarded copy is reframed.
- No dedicated entities/CVE field exists either. _extract_cve_ids does a
  narrow regex pass (CVE ids are a reliable, unambiguous pattern) rather than
  attempting general entity extraction, which would need its own model call
  to do honestly.
- planSchema.tone (server/schemas.ts) is a hard Gemini-enforced enum of 4
  literal strings — passing a custom tone string cannot change the `tone`
  field's value itself, only the surrounding prose (hookStrategy,
  pacingStyle, narrativeBeats) that the same prompt also generates.
  DEFAULT_CYBER_TONE is that descriptive string; 'Deep Dive Documentary'
  remains the closest enum match and is what actually lands in `tone`.
- VideoScript has no top-level midroll_markers[]/retention_beats[].
  _compute_midroll_markers derives them locally from cumulative
  scene.durationEst, snapped to the nearest scene boundary.
- characterBible/styleGuide (server/schemas.ts productionBibleSchema) come
  from their own small-schema pass specifically because ContentPipe's
  CLAUDE.md documents large schemas silently dropping required fields — but
  "smaller schema" lowers the risk, it doesn't remove it. _script_coverage_warnings
  surfaces any gap in the Telegram approval question instead of assuming
  Stage 4 (not built yet) will always get a full production bible.
"""
from __future__ import annotations

import re
from typing import Any

import requests

import config
from exceptions import HumanInputRequired, RateLimitError

PIPELINE_STAGES = ["research", "plan", "script"]

# Prompt 1's tone spec verbatim: "authoritative, investigative, slightly
# urgent, no fearmongering, no clickbait". Shapes hookStrategy/pacingStyle/
# narrativeBeats prose; the `tone` field itself still snaps to the closest
# enum value (see module docstring).
DEFAULT_CYBER_TONE = (
    "Authoritative investigative documentary — slightly urgent, architectural "
    "depth over hype, no fearmongering, no clickbait. Closest available tone "
    "enum: Deep Dive Documentary."
)

# Prompt 1's 8-10 minute target, midpoint. ContentPipe's /api/plan and
# /api/script both honor targetDurationSec now (see ContentPipe's CLAUDE.md
# for the chunked-generation fix) — previously this had nowhere to go and
# every script defaulted to ContentPipe's ~60s Shorts-style pacing.
DEFAULT_TARGET_DURATION_SEC = 540

CVE_PATTERN = re.compile(r"CVE-\d{4}-\d{4,7}", re.IGNORECASE)

# ~2:30 and ~6:00 dual mid-roll placement (Prompt 1 §Retention Engineering).
MIDROLL_TARGETS_SEC = [150, 360]


def next_stage_after(stage: str) -> str | None:
    idx = PIPELINE_STAGES.index(stage)
    if idx + 1 < len(PIPELINE_STAGES):
        return PIPELINE_STAGES[idx + 1]
    return None


def _post(path: str, body: dict[str, Any], provider: str, timeout: int | None = None) -> dict[str, Any]:
    url = f"{config.CONTENTPIPE_BASE_URL}{path}"
    try:
        resp = requests.post(url, json=body, timeout=timeout or config.CONTENTPIPE_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RuntimeError(f"{path} request failed: {exc}") from exc
    if resp.status_code == 429:
        raise RateLimitError(provider, message=f"{path} returned 429")
    if not resp.ok:
        raise RuntimeError(f"{path} returned {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def _extract_cve_ids(research: dict[str, Any]) -> list[str]:
    haystacks = [
        research.get("summary", ""),
        research.get("coreTechExplanation", ""),
        " ".join(research.get("keyFacts") or []),
        " ".join(t.get("event", "") for t in (research.get("timeline") or [])),
    ]
    found: list[str] = []
    for text in haystacks:
        for match in CVE_PATTERN.findall(text or ""):
            cve = match.upper()
            if cve not in found:
                found.append(cve)
    return found


def _reframe_research_for_forwarding(research: dict[str, Any]) -> dict[str, Any]:
    """What actually gets sent to /api/plan and /api/script — see module
    docstring. The unmodified ContentPipe response stays in stage_outputs.
    """
    return {k: v for k, v in research.items() if k not in ("hnCommunitySentiment", "infotainmentAngles")}


def _compute_midroll_markers(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cumulative = 0.0
    boundaries: list[tuple[float, int, Any]] = []
    for i, scene in enumerate(scenes):
        cumulative += scene.get("durationEst", 0) or 0
        boundaries.append((cumulative, i, scene.get("id")))
    markers = []
    for target in MIDROLL_TARGETS_SEC:
        if not boundaries:
            break
        actual_sec, scene_index, scene_id = min(boundaries, key=lambda b: abs(b[0] - target))
        markers.append({
            "targetSec": target,
            "afterSceneIndex": scene_index,
            "afterSceneId": scene_id,
            "actualSec": actual_sec,
        })
    return markers


def _script_coverage_warnings(draft: dict[str, Any]) -> list[str]:
    warnings = []
    if not draft.get("characterBible"):
        warnings.append("no characterBible — hero images will lack consistent characters")
    if not draft.get("styleGuide"):
        warnings.append("no styleGuide — hero images will lack a consistent look")
    scenes = draft.get("scenes") or []
    if scenes:
        with_visual = sum(1 for s in scenes if s.get("visual"))
        with_motion = sum(1 for s in scenes if s.get("motion"))
        if with_visual < len(scenes):
            warnings.append(f"visual direction on {with_visual}/{len(scenes)} scenes")
        if with_motion < len(scenes):
            warnings.append(f"motion direction on {with_motion}/{len(scenes)} scenes")
    return warnings


def stage_research(job: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    payload = job["input_payload"]
    body = {
        "messageText": payload["messageText"],
        "channelName": payload.get("channelName", "CyberPipe"),
        "sourceUrls": payload.get("sourceUrls", []),
    }
    research = _post("/api/research", body, provider="contentpipe:research")
    research["cyberpipe_extracted"] = {"cveIds": _extract_cve_ids(research)}
    return research


def stage_plan(job: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    payload = job["input_payload"]
    body = {
        "researchData": _reframe_research_for_forwarding(outputs["research"]),
        "targetFormat": payload.get("targetFormat", "16:9"),
        "targetTone": payload.get("targetTone", DEFAULT_CYBER_TONE),
        "targetDurationSec": payload.get("targetDurationSec", DEFAULT_TARGET_DURATION_SEC),
    }
    return _post("/api/plan", body, provider="contentpipe:plan")


def stage_script(job: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    payload = job["input_payload"]
    body = {
        "videoPlan": outputs["plan"],
        "researchData": _reframe_research_for_forwarding(outputs["research"]),
        "channelBrandName": payload.get("channelBrandName", "CyberPipe"),
    }
    draft = _post("/api/script", body, provider="contentpipe:script", timeout=config.CONTENTPIPE_SCRIPT_TIMEOUT_SECONDS)
    draft["cyberpipe_midroll_markers"] = _compute_midroll_markers(draft.get("scenes") or [])

    scene_count = len(draft.get("scenes", []))
    total_duration = draft.get("estimatedTotalDuration", 0)
    warnings = _script_coverage_warnings(draft)
    question = (
        f'Approve script draft "{draft.get("title", "untitled")}"? '
        f"{scene_count} scenes, ~{total_duration}s runtime."
    )
    if warnings:
        question += "\n⚠️ " + "; ".join(warnings)

    # Mandatory human checkpoint (cyberpipeline-prompts.md Prompt 1 §Stage 2).
    # worker.py stashes `payload` as job.pending_payload; approving commits it
    # without recomputing, regenerating clears it and re-runs this stage.
    raise HumanInputRequired(question=question, options=["approve", "regenerate"], payload=draft)


STAGE_FUNCTIONS = {
    "research": stage_research,
    "plan": stage_plan,
    "script": stage_script,
}

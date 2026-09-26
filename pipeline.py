"""Stage functions. Stages 1-3 call ContentPipe's existing /api/research,
/api/plan, /api/script endpoints rather than reimplementing research/script
generation — see the "CyberPipe orchestrator scope" decision. Stages 4-5
(per-scene TTS/images, assembly) are not wired here yet: ContentPipe has
strict /api/tts and /api/generate-image and a render module
(server/assemble.ts), but nothing generates and checkpoints per-scene assets,
and the goal so far has been proving the durability + human-in-the-loop core.

Contract (cyberpipeline-prompts.md Prompt 4):
    def stage_x(job: dict, outputs: dict) -> dict
Raise RateLimitError / StageBusy / UpstreamUnavailable / HumanInputRequired
instead of returning for those cases; let any other exception propagate for
worker.py's backoff handling.

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

import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

import config
import rate_limiter
from exceptions import HumanInputRequired, PermanentStageError, RateLimitError, StageBusy, StageInProgress, UpstreamUnavailable

PIPELINE_STAGES = ["research", "plan", "script", "images", "narration", "bundle"]

# Prompt 1's tone spec verbatim: "authoritative, investigative, slightly
# urgent, no fearmongering, no clickbait". Shapes hookStrategy/pacingStyle/
# narrativeBeats prose; the `tone` field itself still snaps to the closest
# enum value (see module docstring).
DEFAULT_CYBER_TONE = (
    "Authoritative investigative documentary — slightly urgent, architectural "
    "depth over hype, no fearmongering, no clickbait. Closest available tone "
    "enum: Deep Dive Documentary."
)

# Prompt 1 asks for 8-10 minutes. ContentPipe's /api/plan and /api/script both
# honor targetDurationSec (see ContentPipe's CLAUDE.md for the chunked-generation
# fix) — previously this had nowhere to go and every script defaulted to
# ContentPipe's ~60s Shorts-style pacing.
# 585 s, not the 540 s midpoint: ContentPipe's audit calls a script short below
# 0.92 x target, and 0.92 x 540 = 497 s leaves almost no room over the 480 s
# mid-roll floor, while 0.92 x 585 = 538 s clears it comfortably.
DEFAULT_TARGET_DURATION_SEC = 585

CVE_PATTERN = re.compile(r"CVE-\d{4}-\d{4,7}", re.IGNORECASE)

# ~2:30 and ~6:00 dual mid-roll placement (Prompt 1 §Retention Engineering).
MIDROLL_TARGETS_SEC = [150, 360]


def next_stage_after(stage: str) -> str | None:
    idx = PIPELINE_STAGES.index(stage)
    if idx + 1 < len(PIPELINE_STAGES):
        return PIPELINE_STAGES[idx + 1]
    return None


# ContentPipe's default contract is "always return something": on quota exhaustion it answers
# HTTP 200 with canned XZ-backdoor content flagged `isQuotaFallback`, which is right for its UI
# and wrong for an orchestrator — this file used to hand that to the human approver as a real
# draft, and the 429 branch below could never fire. This header opts out: ContentPipe then
# answers 429 + Retry-After (quota), 503 + Retry-After (overload) or 502 (non-retryable),
# and an interrupted /api/script resumes from its last finished chunk on the identical re-POST.
STRICT_HEADERS = {"X-ContentPipe-Strict": "1"}


# Retry-After is absent on a 409 from an older ContentPipe; matches worker.DEFAULT_BUSY_WAIT_SECONDS.
DEFAULT_BUSY_WAIT_SECONDS = 30


def _error_body(resp: Any) -> dict[str, Any]:
    """ContentPipe's structured error body ({error, kind, retryable, ...}), or {} if it isn't JSON."""
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _post(path: str, body: dict[str, Any], provider: str, timeout: int | None = None) -> dict[str, Any]:
    url = f"{config.CONTENTPIPE_BASE_URL}{path}"
    try:
        resp = requests.post(url, json=body, headers=STRICT_HEADERS, timeout=timeout or config.CONTENTPIPE_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RuntimeError(f"{path} request failed: {exc}") from exc
    if resp.status_code == 429:
        # Retry-After is when the quota resets (seconds until midnight Pacific for a daily
        # limit), so the scheduler wakes at the right moment instead of guessing.
        raise RateLimitError(
            provider,
            retry_at=rate_limiter.parse_retry_after(resp.headers.get("Retry-After")),
            message=f"{path} returned 429: {resp.text[:300]}",
        )
    if resp.status_code == 409:
        # ContentPipe is still generating this exact run (a client timeout doesn't stop the server).
        # Not a failure: wait for it, and don't spend a backoff attempt.
        raise StageBusy(
            f"{path} run is already in progress on ContentPipe",
            retry_at=rate_limiter.parse_retry_after(resp.headers.get("Retry-After"))
            or datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_BUSY_WAIT_SECONDS),
        )
    if resp.status_code == 503:
        # Every model provider behind ContentPipe was overloaded or unreachable, after its own bounded
        # wait. Not a fault of this job, so it must not spend a backoff attempt: a multi-hour provider
        # outage used to fail jobs in ~9 h while ignoring the 30 s Retry-After.
        raise UpstreamUnavailable(
            provider,
            retry_at=rate_limiter.parse_retry_after(resp.headers.get("Retry-After")),
            message=f"{path} returned 503: {resp.text[:300]}",
        )
    if not resp.ok:
        body = _error_body(resp)
        if resp.status_code == 502 and body.get("kind") == "zero_quota":
            # The key has no quota at all (billing needed). No amount of waiting fixes that.
            raise PermanentStageError(f"{path}: {body.get('error') or 'no quota exists for this API key'} — enable billing")
        raise RuntimeError(f"{path} returned {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    # Belt and braces for an older ContentPipe that ignores the header.
    if isinstance(data, dict) and data.get("isQuotaFallback"):
        raise RateLimitError(provider, message=f"{path} returned canned fallback content (isQuotaFallback); refusing to treat it as real output")
    return data


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


def _server_midroll_markers(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """ContentPipe now places mid-rolls itself (eligibility >= 8:00, snapped to scene
    boundaries, semantic preference). Convert its markers to this module's shape; an empty
    list means "use the local fallback" (an older ContentPipe, or nothing placeable)."""
    scenes = draft.get("scenes") or []
    markers = []
    for m in draft.get("midrollMarkers") or []:
        idx = int(m.get("afterSceneNumber", 0)) - 1
        if not 0 <= idx < len(scenes):
            continue
        markers.append({
            "targetSec": m.get("targetSec"),
            "afterSceneIndex": idx,
            "afterSceneId": scenes[idx].get("id"),
            "actualSec": m.get("atSec"),
            "reason": m.get("reason"),
        })
    return markers


def _midroll_markers_for(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """Mid-roll placement for the approval document.

    ContentPipe owns this decision. When its response carries `midrollMarkers` at all, an EMPTY
    list is an answer, not a gap: under 8:00 it places none on purpose and says so in a warning.
    Falling back to the local guess then invented ~2:30 / ~6:00 markers (one after the last scene of
    a 5-minute script) and listed them beside ContentPipe's warning that none are eligible. The
    local computation is only for a ContentPipe too old to send the field.
    """
    if isinstance(draft.get("midrollMarkers"), list):
        return _server_midroll_markers(draft)
    return _compute_midroll_markers(draft.get("scenes") or [])


def _quality_check_notes(draft: dict[str, Any], limit: int = 4) -> list[str]:
    """The worst of ContentPipe's deterministic audit (errors first), one short line each,
    for the approval message. The full list stays on the draft for anyone who wants it."""
    checks = [c for c in (draft.get("qualityChecks") or []) if c.get("severity") in ("error", "warn")]
    notes = [f"{c['severity'].upper()} {c.get('id')}: {str(c.get('message', ''))[:140]}" for c in checks[:limit]]
    if len(checks) > limit:
        notes.append(f"+{len(checks) - limit} more in the draft's qualityChecks")
    return notes


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
        "sourceUrls": payload.get("sourceUrls", []),
        # How much research the dossier needs depends on the length of the script it has to carry:
        # ContentPipe scales its key-fact target off this, and reports the shortfall in researchGaps
        # rather than padding. Omitting it would let a 9-minute documentary settle for four facts.
        "targetDurationSec": payload.get("targetDurationSec", DEFAULT_TARGET_DURATION_SEC),
    }
    # channelName is where the story came from (a Telegram feed, a wire) and goes into the research prompt as
    # its origin. Our own channel is not an origin, so it is only forwarded when the job names a real one.
    if payload.get("channelName"):
        body["channelName"] = payload["channelName"]
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
        "channelBrandName": payload.get("channelBrandName") or config.CHANNEL_BRAND_NAME,
    }
    draft = _post("/api/script", body, provider="contentpipe:script", timeout=config.CONTENTPIPE_SCRIPT_TIMEOUT_SECONDS)
    draft["cyberpipe_midroll_markers"] = _midroll_markers_for(draft)

    scene_count = len(draft.get("scenes", []))
    total_duration = draft.get("estimatedTotalDuration", 0)
    warnings = _script_coverage_warnings(draft)
    question = (
        f'Approve script draft "{draft.get("title", "untitled")}"? '
        f"{scene_count} scenes, ~{total_duration}s runtime."
    )
    if warnings:
        question += "\n⚠️ " + "; ".join(warnings)
    audit = _quality_check_notes(draft)
    if audit:
        question += "\n🔎 " + "\n🔎 ".join(audit)

    # Mandatory human checkpoint (cyberpipeline-prompts.md Prompt 1 §Stage 2).
    # worker.py stashes `payload` as job.pending_payload; approving commits it
    # without recomputing, regenerating clears it and re-runs this stage.
    raise HumanInputRequired(question=question, options=["approve", "regenerate"], payload=draft)


# ------------------------------------------------------------------------------------ ContentRender stages
#
# Stages 4-6 hand the approved script to ContentRender (../ContentRender) and let it do the media work:
#
#   images     one still per scene                      -> gate "images"    (Telegram: the stills)
#   narration  AI clips, then two-voice narration       -> gate "narration" (Telegram: one audio file)
#   bundle     timeline, rough cut, Resolve bundle      -> gate "final"     (Telegram: the rough cut)
#
# ContentRender keeps its own manifest per run (`job-<id>`), so a crash, a quota wall or a re-run resumes where
# it stopped; this side keeps the human decisions. Each call is `cli.ts step`, which does as much as its time
# budget allows and prints ONE JSON outcome as its last line (see ContentRender/src/step.ts). The exit code only
# says whether the process itself crashed, so a "rate_limited" or "error" outcome is a normal exit.

# Which ContentRender gate each of our stages waits at, and which gate must already be closed when it starts.
RENDER_GATES = {"images": ("images", None), "narration": ("narration", "images"), "bundle": ("final", "narration")}


def _render_command(*args: str) -> list[str]:
    return [config.CONTENTRENDER_NODE, "node_modules/tsx/dist/cli.mjs", "scripts/cli.ts", *args]


def _write_brief(job: dict[str, Any], outputs: dict[str, Any]) -> Path:
    """The approved script, where ContentRender reads it. Rewritten only when it changed."""
    script = outputs.get("script")
    if not script or not script.get("scenes"):
        raise PermanentStageError("the approved script has no scenes, so there is nothing to render")
    path = Path(config.BRIEFS_DIR) / f"job-{job['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(script, ensure_ascii=False)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
    return path


def _run_render(command: str, job: dict[str, Any], brief: Path, *extra: str) -> dict[str, Any]:
    """Runs one ContentRender command and returns its JSON outcome. A crash, a timeout, or output that is not JSON
    raises RuntimeError, so it takes the ordinary backoff path (5m/15m/45m...)."""
    args = [command, "--brief", str(brief), "--video-id", f"job-{job['id']}", *extra]
    try:
        done = subprocess.run(
            _render_command(*args), cwd=config.CONTENTRENDER_DIR, capture_output=True, text=True,
            timeout=config.CONTENTRENDER_TIMEOUT_SECONDS, env={**os.environ, "CONTENTPIPE_BASE_URL": config.CONTENTPIPE_BASE_URL},
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ContentRender {command} exceeded {config.CONTENTRENDER_TIMEOUT_SECONDS}s and was killed") from exc
    except OSError as exc:
        raise RuntimeError(f"could not start ContentRender ({config.CONTENTRENDER_NODE} in {config.CONTENTRENDER_DIR}): {exc}") from exc
    lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
    tail = done.stderr.strip().splitlines()[-6:]
    if done.returncode != 0:
        raise RuntimeError(f"ContentRender {command} crashed (exit {done.returncode}): {' | '.join(tail)}")
    try:
        outcome = json.loads(lines[-1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError(f"ContentRender {command} printed no JSON outcome. stderr: {' | '.join(tail)}") from exc
    if not isinstance(outcome, dict) or "status" not in outcome:
        raise RuntimeError(f"ContentRender {command} printed an unrecognised outcome: {lines[-1][:200]}")
    return outcome


def _parse_iso(value: Any) -> datetime | None:
    """ContentRender writes JS-style timestamps ("...T18:00:00.000Z"); older Pythons cannot read the Z."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _render_ok(command: str, job: dict[str, Any], brief: Path, *extra: str) -> dict[str, Any]:
    """For the small commands (approve, regenerate): anything but status "ok" is an error."""
    outcome = _run_render(command, job, brief, *extra)
    if outcome["status"] != "ok":
        raise RuntimeError(f"ContentRender {command} refused: {'; '.join(outcome.get('problems') or [json.dumps(outcome)[:200]])}")
    return outcome


def _drive(job: dict[str, Any], outputs: dict[str, Any], stage: str) -> dict[str, Any]:
    expected_gate, previous_gate = RENDER_GATES[stage]
    brief = _write_brief(job, outputs)
    if previous_gate:
        # Idempotent: the human already said yes in Telegram, this makes ContentRender's manifest agree even if a
        # crash landed between the two.
        _render_ok("approve", job, brief, "--gate", previous_gate)
    outcome = _run_render("step", job, brief, "--budget", str(config.CONTENTRENDER_STEP_BUDGET_SECONDS))
    status = outcome["status"]

    if status == "progress":
        wait = max(5, int(outcome.get("retryInSec", 30)))
        raise StageInProgress(f'{stage}: {outcome.get("note", "more to do")}', retry_at=datetime.now(timezone.utc) + timedelta(seconds=wait))
    if status == "rate_limited":
        retry_at = _parse_iso(outcome.get("retryAt"))
        provider = outcome.get("provider", "contentrender")
        if outcome.get("kind") == "overloaded":
            raise UpstreamUnavailable(provider, retry_at=retry_at, message=outcome.get("message", ""))
        raise RateLimitError(provider, retry_at=retry_at, message=outcome.get("message", ""))
    if status == "error":
        raise PermanentStageError("; ".join(outcome.get("problems") or ["ContentRender reported an error"]))
    if status == "delivered":
        return {"delivered": True, "videoId": outcome.get("videoId")}
    if status == "gate":
        if outcome.get("gate") != expected_gate:
            raise PermanentStageError(f'ContentRender is waiting at the "{outcome.get("gate")}" gate but stage {stage} expected "{expected_gate}"')
        review = outcome.get("review") or {}
        question = f'{review.get("title", "Render")} — {review.get("summary", "approval needed")}'
        raise HumanInputRequired(question=question, options=["approve", "regenerate"], payload={"gate": expected_gate, "review": review})
    raise PermanentStageError(f"unknown ContentRender outcome {status!r}")


def stage_images(job: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    return _drive(job, outputs, "images")


def stage_narration(job: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    return _drive(job, outputs, "narration")


def stage_bundle(job: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    return _drive(job, outputs, "bundle")


# Human decisions have to reach ContentRender's manifest too. worker.resume_from_input calls these: ON_REGENERATE
# before it re-queues a stage (so the redo starts from clean assets), ON_APPROVE before it advances (only the last
# gate needs it; earlier ones are closed at the start of the next stage). A hook that raises leaves the job
# waiting, so the same tap can simply be repeated.
def _hook(command: str, *extra: str) -> Callable[[dict[str, Any], dict[str, Any]], None]:
    def run(job: dict[str, Any], outputs: dict[str, Any]) -> None:
        _render_ok(command, job, _write_brief(job, outputs), *extra)
    return run


ON_APPROVE: dict[str, Callable[[dict[str, Any], dict[str, Any]], None]] = {"bundle": _hook("approve", "--gate", "final")}
ON_REGENERATE: dict[str, Callable[[dict[str, Any], dict[str, Any]], None]] = {
    "images": _hook("regenerate", "--kind", "still", "--all"),
    "narration": _hook("regenerate", "--kind", "narration", "--all"),
    "bundle": _hook("regenerate", "--kind", "render"),
}


def regenerate_scenes(job: dict[str, Any], outputs: dict[str, Any], kind: str, scenes: list[int]) -> None:
    """Per-scene redo from Telegram (/regen). `kind` is "still" or "narration"."""
    _render_ok("regenerate", job, _write_brief(job, outputs), "--kind", kind, "--scenes", ",".join(str(n) for n in scenes))


STAGE_FUNCTIONS = {
    "research": stage_research,
    "plan": stage_plan,
    "script": stage_script,
    "images": stage_images,
    "narration": stage_narration,
    "bundle": stage_bundle,
}

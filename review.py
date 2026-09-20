"""The document a human reads before tapping approve.

The script stage is the pipeline's mandatory human checkpoint, but the Telegram message can only
carry a summary. This renders the whole draft — what the approver most needs to know first (is it
complete, what did the audit flag), then every scene's narration with its running timestamp, then
the sources that were actually read — as Markdown that is attached to the approval message.
"""
from __future__ import annotations

from typing import Any, Optional


def _clock(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 60}:{total % 60:02d}"


def _sources(research: dict[str, Any]) -> list[str]:
    lines = []
    for src in research.get("retrievedSources") or []:
        sid = src.get("id", "?")
        if src.get("ok"):
            via = src.get("via")
            note = f" ({via})" if via and via != "direct" else ""
            lines.append(f"- [{sid}] {src.get('title') or src.get('url')} — {src.get('url')}{note}")
        else:
            lines.append(f"- [{sid}] {src.get('url')} — not retrieved ({src.get('error') or 'unknown error'})")
    return lines


def render_review_markdown(job: dict[str, Any]) -> Optional[str]:
    """None when the pending draft has no scenes (e.g. a research-stage checkpoint): nothing to attach."""
    draft = job.get("pending_payload") or {}
    scenes = draft.get("scenes") or []
    if not scenes:
        return None

    gen = draft.get("generation") or {}
    requested = gen.get("requestedScenes")
    produced = gen.get("producedScenes") or len(scenes)
    runtime = draft.get("estimatedTotalDuration") or sum(float(s.get("durationEst") or 0) for s in scenes)

    out = [f"# {draft.get('title') or 'Untitled script'}", ""]
    out.append(
        f"Job #{job.get('id')} · {produced}/{requested} scenes" if requested else f"Job #{job.get('id')} · {produced} scenes"
    )
    out[-1] += f" · ~{_clock(runtime)} runtime" + (" · **INCOMPLETE**" if gen and gen.get("complete") is False else "")
    out.append("")

    for note in gen.get("degraded") or []:
        out.append(f"- ⚠️ {note}")
    checks = [c for c in draft.get("qualityChecks") or [] if c.get("severity") in ("error", "warn")]
    for c in checks:
        out.append(f"- {str(c.get('severity')).upper()} {c.get('id')}: {c.get('message')}")
    for m in draft.get("midrollMarkers") or []:
        out.append(f"- Mid-roll #{m.get('index')} at {m.get('timestamp')} — {m.get('reason')}")
    if not draft.get("midrollMarkers"):
        for m in draft.get("cyberpipe_midroll_markers") or []:
            out.append(f"- Mid-roll at {_clock(m.get('actualSec') or 0)} — after scene {int(m.get('afterSceneIndex', 0)) + 1}")
    if out[-1] != "":
        out.append("")

    sources = _sources((job.get("stage_outputs") or {}).get("research") or {})
    if sources:
        out += ["## Sources read", *sources, ""]

    out.append("## Script")
    t = 0.0
    for i, scene in enumerate(scenes, 1):
        dur = float(scene.get("durationEst") or 0)
        heading = f"### {scene.get('sceneNumber') or i}. {scene.get('title') or 'Scene'}"
        if scene.get("actPhase"):
            heading += f" — {scene['actPhase']}"
        out += ["", f"{heading} · {_clock(t)}–{_clock(t + dur)}", str(scene.get("narration") or "").strip()]
        if scene.get("citations"):
            out.append("Sources: " + " ".join(f"[{c}]" for c in scene["citations"]))
        t += dur
    return "\n".join(out) + "\n"

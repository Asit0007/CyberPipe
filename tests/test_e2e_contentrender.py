"""CyberPipe against the REAL ContentRender command line, on stub media (test patterns and tones — no quota, no network).

Proves the two repos agree on the protocol end to end: the brief file, the JSON outcomes, the three gates, the
approvals reaching ContentRender's manifest, and the Telegram media. Slow (ffmpeg, ~1 min), so it only runs when asked:

    CONTENTRENDER_E2E=1 ./venv/bin/python -m unittest tests.test_e2e_contentrender -v

Needs node and ffmpeg, and ../ContentRender with its dependencies installed (`npm install` there).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import db
import pipeline
import worker
from tests.support import DbTestCase

RENDER_DIR = Path(config.BASE_DIR).parent / "ContentRender"
READY = os.environ.get("CONTENTRENDER_E2E") == "1" and shutil.which("node") and shutil.which("ffmpeg") and (RENDER_DIR / "node_modules/tsx/dist/cli.mjs").exists()

SCRIPT = {
    "title": "The Quiet Backdoor", "aspectRatio": "16:9",
    "chapters": [{"startSec": 0, "timestamp": "0:00", "label": "The hook"}],
    "midrollMarkers": [{"index": 1, "afterSceneNumber": 2, "atSec": 20, "timestamp": "0:20", "reason": "problem set up"}],
    "scenes": [
        {"id": f"scene-{n}", "sceneNumber": n, "title": f"Scene {n}", "actPhase": "Hook" if n < 3 else "The breakdown",
         "narration": f"This is the narration for scene number {n}, said at a steady pace for the test.", "durationEst": 8, "visualPrompt": f"A dark server room, angle {n}",
         **({"speaker": "analyst"} if n == 3 else {}), "sound": {"transitionIn": "dissolve" if n == 2 else "cut", "sfxCue": "glass break" if n == 4 else ""}}
        for n in range(1, 5)
    ],
}


@unittest.skipUnless(READY, "set CONTENTRENDER_E2E=1 (needs node, ffmpeg and ../ContentRender installed)")
class EndToEnd(DbTestCase):
    stages = pipeline.PIPELINE_STAGES

    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (
            mock.patch.object(config, "BRIEFS_DIR", os.path.join(self.tmp.name, "briefs")),
            mock.patch.object(config, "CONTENTRENDER_DIR", str(RENDER_DIR)),
            mock.patch.dict(os.environ, {"RENDER_DIR": os.path.join(self.tmp.name, "render"), "CLIP_SLOTS_MAX": "2"}),
            mock.patch.object(pipeline, "_render_command", self.stubbed),
        ):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def stubbed(*args: str) -> list[str]:
        """Same command, but on stub media and under a `stub-` video id (ContentRender refuses --stub otherwise)."""
        out = [config.CONTENTRENDER_NODE, "node_modules/tsx/dist/cli.mjs", "scripts/cli.ts"]
        for i, a in enumerate(args):
            out.append(f"stub-{a}" if i and args[i - 1] == "--video-id" else a)
        return [*out, "--stub"] if args[0] == "step" else out

    def step(self, job_id: int, want_status: str, want_stage: str) -> dict:
        worker.run_job(job_id)
        job = db.get_job(job_id)
        self.assertEqual((job["status"], job["current_stage"]), (want_status, want_stage), job.get("last_error"))
        return job

    def test_script_approved_to_a_delivered_bundle(self):
        job_id = self.make_job("images", stage_outputs={"script": SCRIPT})

        job = self.step(job_id, "NEEDS_INPUT", "images")
        self.assertEqual(job["pending_payload"]["gate"], "images")
        self.assertEqual(len(job["pending_payload"]["review"]["files"]), 4)
        self.assertEqual([c["method"] for c in self.telegram.calls][-2:], ["sendMediaGroup", "sendMessage"])
        self.assertTrue(worker.resume_from_input(job_id, "approve"))

        job = self.step(job_id, "NEEDS_INPUT", "narration")
        self.assertEqual(job["pending_payload"]["gate"], "narration")
        self.assertIn("sendAudio", [c["method"] for c in self.telegram.calls])
        self.assertTrue(worker.resume_from_input(job_id, "approve"))

        job = self.step(job_id, "NEEDS_INPUT", "bundle")
        self.assertEqual(job["pending_payload"]["gate"], "final")
        self.assertIn("sendVideo", [c["method"] for c in self.telegram.calls])
        bundle = Path(job["pending_payload"]["review"]["summary"].splitlines()[0].split(": ", 1)[1])
        self.assertTrue((bundle / f"stub-job-{job_id}.fcpxml").is_file(), list(bundle.iterdir()))

        self.assertTrue(worker.resume_from_input(job_id, "approve"))
        self.assertEqual(db.get_job(job_id)["status"], "COMPLETED")
        self.assertIn("Bundle ready", self.telegram.messages[-1]["text"])

        manifest = json.loads((bundle.parent / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "delivered", "the final approval reached ContentRender's manifest")
        self.assertTrue(all(a["status"] == "approved" for a in manifest["approvals"].values()))

    def test_regenerating_one_scene_from_telegram_redoes_only_that_still(self):
        job_id = self.make_job("images", stage_outputs={"script": SCRIPT})
        self.step(job_id, "NEEDS_INPUT", "images")
        run = Path(os.environ["RENDER_DIR"]) / "runs" / f"stub-job-{job_id}"
        before = {p.name: p.stat().st_mtime_ns for p in (run / "stills").iterdir()}

        ok, _ = worker.regenerate_scenes(job_id, [2])
        self.assertTrue(ok)
        self.step(job_id, "NEEDS_INPUT", "images")
        after = {p.name: p.stat().st_mtime_ns for p in (run / "stills").iterdir()}
        changed = [n for n in before if before[n] != after[n]]
        self.assertEqual(changed, ["scene-002.png"])


if __name__ == "__main__":
    unittest.main()

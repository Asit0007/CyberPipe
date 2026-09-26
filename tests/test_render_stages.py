"""Stages 4-6 (images, narration, bundle): CyberPipe drives ContentRender's command line and relays its gates.

ContentRender itself is never run here — `pipeline._run_render` / `subprocess.run` are faked, and the tests
use the real outcome shapes documented in ContentRender/src/step.ts. Its own tests cover what it does.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest import mock

import config
import db
import notifier
import pipeline
import review
import telegram_poller
import worker
from exceptions import HumanInputRequired, PermanentStageError, RateLimitError, StageInProgress, UpstreamUnavailable
from tests.support import DbTestCase, FakeResponse

SCRIPT = {"title": "The Quiet Backdoor", "scenes": [{"id": "s1", "sceneNumber": 1, "narration": "n", "durationEst": 5, "visualPrompt": "p"}]}


def done(stdout: str = "", stderr: str = "", code: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)


def outcome_line(**o: Any) -> str:
    return "npm noise\n" + json.dumps(o) + "\n"


class FakeRender:
    """Stands in for pipeline._run_render: records every call and answers from a script of outcomes per command."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.step_outcomes: list[dict[str, Any]] = []
        self.refuse: Optional[str] = None  # command name that answers status "error"

    def __call__(self, command: str, job: dict, brief: Any, *extra: str) -> dict[str, Any]:
        self.calls.append((command, extra))
        if self.refuse == command:
            return {"status": "error", "problems": [f"{command} refused"]}
        if command == "step":
            return self.step_outcomes.pop(0)
        return {"status": "ok"}

    def commands(self) -> list[str]:
        return [c for c, _ in self.calls]


class RenderTestCase(DbTestCase):
    stages = pipeline.PIPELINE_STAGES

    def setUp(self) -> None:
        super().setUp()
        self.briefs = tempfile.TemporaryDirectory()
        self.addCleanup(self.briefs.cleanup)
        p = mock.patch.object(config, "BRIEFS_DIR", self.briefs.name)
        p.start()
        self.addCleanup(p.stop)
        self.render = FakeRender()
        p = mock.patch.object(pipeline, "_run_render", self.render)
        p.start()
        self.addCleanup(p.stop)

    def job_at(self, stage: str, **fields: Any) -> int:
        return self.make_job(stage, stage_outputs={"script": SCRIPT}, **fields)

    def stage_status(self, job_id: int) -> list[str]:
        with db.get_connection() as conn:
            return [r[0] for r in conn.execute("SELECT status FROM stage_runs WHERE job_id = ? ORDER BY id", (job_id,))]


class RunRenderTests(unittest.TestCase):
    """The subprocess wrapper, with subprocess.run faked."""

    def job(self) -> dict[str, Any]:
        return {"id": 12}

    def run_with(self, result: Any = None, exc: Optional[Exception] = None):
        with mock.patch.object(pipeline.subprocess, "run", side_effect=exc, return_value=result) as run:
            try:
                out = pipeline._run_render("step", self.job(), "/b/job-12.json", "--budget", "60")
            except Exception as e:  # noqa: BLE001
                out = e
        return out, run

    def test_it_runs_the_cli_in_contentrender_with_the_brief_and_video_id_and_reads_the_last_json_line(self):
        out, run = self.run_with(done(outcome_line(status="progress", stage="stills", note="more", retryInSec=30)))
        self.assertEqual(out["status"], "progress")
        args, kwargs = run.call_args
        self.assertEqual(args[0][1:], ["node_modules/tsx/dist/cli.mjs", "scripts/cli.ts", "step", "--brief", "/b/job-12.json", "--video-id", "job-12", "--budget", "60"])
        self.assertEqual(kwargs["cwd"], config.CONTENTRENDER_DIR)
        self.assertEqual(kwargs["env"]["CONTENTPIPE_BASE_URL"], config.CONTENTPIPE_BASE_URL)
        self.assertEqual(kwargs["timeout"], config.CONTENTRENDER_TIMEOUT_SECONDS)

    def test_a_crash_carries_the_stderr_tail_and_takes_the_backoff_path(self):
        out, _ = self.run_with(done(stderr="a\nb\nboom: kokoro missing", code=1))
        self.assertIsInstance(out, RuntimeError)
        self.assertIn("crashed (exit 1)", str(out))
        self.assertIn("boom: kokoro missing", str(out))

    def test_no_json_a_timeout_and_a_missing_node_are_all_ordinary_errors(self):
        for result, exc, want in [
            (done("just words\n"), None, "printed no JSON outcome"),
            (done('{"nope": 1}\n'), None, "unrecognised outcome"),
            (None, subprocess.TimeoutExpired("node", 5), "exceeded"),
            (None, FileNotFoundError(2, "No such file", "node"), "could not start"),
        ]:
            out, _ = self.run_with(result, exc)
            self.assertIsInstance(out, RuntimeError)
            self.assertIn(want, str(out))


class BriefTests(RenderTestCase):
    def test_the_approved_script_is_written_once_and_rewritten_when_it_changes(self):
        job = {"id": 3}
        path = pipeline._write_brief(job, {"script": SCRIPT})
        self.assertEqual(path.name, "job-3.json")
        self.assertEqual(json.loads(path.read_text())["title"], "The Quiet Backdoor")
        before = path.stat().st_mtime_ns
        pipeline._write_brief(job, {"script": SCRIPT})
        self.assertEqual(path.stat().st_mtime_ns, before, "unchanged script is not rewritten")
        pipeline._write_brief(job, {"script": {**SCRIPT, "title": "Changed"}})
        self.assertEqual(json.loads(path.read_text())["title"], "Changed")

    def test_a_script_without_scenes_is_refused_permanently(self):
        with self.assertRaises(PermanentStageError):
            pipeline._write_brief({"id": 3}, {"script": {"title": "x", "scenes": []}})
        with self.assertRaises(PermanentStageError):
            pipeline._write_brief({"id": 3}, {})


class DriveTests(RenderTestCase):
    """How each ContentRender outcome becomes a CyberPipe stage result or control-flow exception."""

    def drive(self, stage: str, outcome: dict[str, Any]):
        self.render.step_outcomes.append(outcome)
        job = {"id": 5}
        return pipeline._drive(job, {"script": SCRIPT}, stage)

    def test_progress_becomes_stage_in_progress_at_the_time_it_asked_for(self):
        with self.assertRaises(StageInProgress) as cm:
            self.drive("images", {"status": "progress", "stage": "stills", "note": "time budget used", "retryInSec": 45})
        wait = (cm.exception.retry_at - datetime.now(timezone.utc)).total_seconds()
        self.assertTrue(40 < wait <= 45, wait)

    def test_rate_limited_keeps_contentrenders_retry_time_including_its_JS_style_Z_timestamp(self):
        at = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        with self.assertRaises(RateLimitError) as cm:
            self.drive("images", {"status": "rate_limited", "provider": "contentpipe:image", "kind": "quota", "retryAfterSec": 7200, "retryAt": at, "message": "daily"})
        self.assertEqual(cm.exception.provider, "contentpipe:image")
        self.assertAlmostEqual((cm.exception.retry_at - datetime.now(timezone.utc)).total_seconds(), 7200, delta=5)

    def test_overload_is_an_upstream_unavailable_wait_not_a_quota_wait(self):
        with self.assertRaises(UpstreamUnavailable):
            self.drive("narration", {"status": "rate_limited", "provider": "contentpipe:tts", "kind": "overloaded", "retryAfterSec": 30, "retryAt": "2030-01-01T00:00:00.000Z", "message": "busy"})

    def test_error_is_permanent_and_lists_every_problem(self):
        with self.assertRaises(PermanentStageError) as cm:
            self.drive("images", {"status": "error", "problems": ["scene 2 still: x", "regenerate it"]})
        self.assertIn("scene 2 still: x; regenerate it", str(cm.exception))

    def test_the_expected_gate_becomes_a_human_checkpoint_carrying_the_review(self):
        review_data = {"gate": "images", "title": "The Quiet Backdoor", "summary": "14 stills.", "files": []}
        with self.assertRaises(HumanInputRequired) as cm:
            self.drive("images", {"status": "gate", "gate": "images", "review": review_data})
        self.assertEqual(cm.exception.options, ["approve", "regenerate"])
        self.assertEqual(cm.exception.payload, {"gate": "images", "review": review_data})
        self.assertIn("14 stills.", cm.exception.question)

    def test_a_gate_the_stage_did_not_expect_is_an_error_not_a_silent_approval(self):
        with self.assertRaises(PermanentStageError) as cm:
            self.drive("images", {"status": "gate", "gate": "narration", "review": {}})
        self.assertIn('expected "images"', str(cm.exception))

    def test_delivered_returns_a_result(self):
        self.assertEqual(self.drive("bundle", {"status": "delivered", "videoId": "job-5"}), {"delivered": True, "videoId": "job-5"})

    def test_each_stage_first_closes_the_previous_gate_and_only_then_steps(self):
        for stage, previous in [("narration", "images"), ("bundle", "narration")]:
            self.render.calls.clear()
            with self.assertRaises(HumanInputRequired):
                self.drive(stage, {"status": "gate", "gate": pipeline.RENDER_GATES[stage][0], "review": {}})
            self.assertEqual(self.render.calls[0], ("approve", ("--gate", previous)))
            self.assertEqual(self.render.commands(), ["approve", "step"])
        self.render.calls.clear()
        with self.assertRaises(HumanInputRequired):
            self.drive("images", {"status": "gate", "gate": "images", "review": {}})
        self.assertEqual(self.render.commands(), ["step"], "the first render stage has nothing to close")

    def test_a_refused_previous_approval_stops_before_any_work_and_is_an_ordinary_error_so_it_backs_off(self):
        self.render.refuse = "approve"
        with self.assertRaises(RuntimeError):
            self.drive("narration", {"status": "progress"})
        self.assertEqual(self.render.commands(), ["approve"])


class WorkerFlowTests(RenderTestCase):
    def photo(self, name: str = "still-001.jpg", size: int = 10) -> str:
        path = os.path.join(self.briefs.name, name)
        with open(path, "wb") as f:
            f.write(b"\xff" * size)
        return path

    def gate(self, gate: str, files: Optional[list] = None, summary: str = "s") -> dict[str, Any]:
        return {"status": "gate", "gate": gate, "review": {"gate": gate, "title": "T", "summary": summary, "files": files or []}}

    def test_progress_requeues_without_an_attempt_a_page_or_the_wait_clock(self):
        job_id = self.job_at("images", wait_since=self.ago(days=6))
        self.render.step_outcomes.append({"status": "progress", "stage": "stills", "note": "more", "retryInSec": 30})
        worker.run_job(job_id)
        job = db.get_job(job_id)
        self.assertEqual((job["status"], job["current_stage"], job["attempt_count"]), ("SCHEDULED", "images", 0))
        self.assertIsNone(job["wait_since"], "real progress clears the MAX_WAIT_DAYS clock")
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(self.stage_status(job_id), ["in_progress"])

    def test_a_progress_job_runs_again_once_its_time_comes(self):
        job_id = self.job_at("images")
        self.render.step_outcomes += [{"status": "progress", "retryInSec": 30}, self.gate("images")]
        worker.run_job(job_id)
        self.assertNotIn(job_id, [j["id"] for j in db.due_jobs()], "not due before its retry time")
        self.set_raw(job_id, next_retry_at=self.ago(seconds=1))
        worker.run_job(job_id)
        self.assertEqual(db.get_job(job_id)["status"], "NEEDS_INPUT")

    def test_the_images_gate_sends_the_stills_as_an_album_then_the_buttons(self):
        files = [{"kind": "photo", "path": self.photo(f"s{i}.jpg"), "caption": f"Scene {i}"} for i in range(1, 4)]
        job_id = self.job_at("images")
        self.render.step_outcomes.append(self.gate("images", files, "3 stills."))
        worker.run_job(job_id)
        self.assertEqual([c["method"] for c in self.telegram.calls], ["sendMediaGroup", "sendMessage"])
        media = json.loads(self.telegram.of("sendMediaGroup")[0]["data"]["media"])
        self.assertEqual([m["caption"] for m in media], ["Scene 1", "Scene 2", "Scene 3"])
        self.assertEqual(set(self.telegram.of("sendMediaGroup")[0]["files"]), {"p0", "p1", "p2"})
        msg = self.telegram.messages[0]
        self.assertIn("3 stills.", msg["text"])
        self.assertIn(f"/regen {job_id} 3,7", msg["text"])
        self.assertEqual([b["callback_data"] for b in msg["reply_markup"]["inline_keyboard"][0]], [f"job:{job_id}:approve", f"job:{job_id}:regenerate"])

    def test_approving_the_images_gate_moves_on_to_narration_with_the_review_stored(self):
        job_id = self.job_at("images", status="NEEDS_INPUT", pending_payload={"gate": "images", "review": {"files": []}}, pending_question={"question": "q", "options": ["approve", "regenerate"]})
        self.assertTrue(worker.resume_from_input(job_id, "approve"))
        job = db.get_job(job_id)
        self.assertEqual((job["status"], job["current_stage"]), ("PENDING", "narration"))
        self.assertEqual(job["stage_outputs"]["images"]["gate"], "images")
        self.assertEqual(self.render.calls, [], "the images approval reaches ContentRender at the start of the next stage")

    def test_the_final_approval_reaches_contentrender_before_the_job_completes(self):
        job_id = self.job_at("bundle", status="NEEDS_INPUT", pending_payload={"gate": "final", "review": {"summary": "Bundle ready: /runs/job-9/resolve\nmore"}}, pending_question={"question": "q", "options": ["approve", "regenerate"]})
        self.assertTrue(worker.resume_from_input(job_id, "approve"))
        self.assertEqual(self.render.calls, [("approve", ("--gate", "final"))])
        self.assertEqual(db.get_job(job_id)["status"], "COMPLETED")
        self.assertIn("Bundle ready: /runs/job-9/resolve", self.telegram.messages[-1]["text"])
        self.assertNotIn("more", self.telegram.messages[-1]["text"], "only the line naming the folder")

    def test_if_contentrender_cannot_hear_the_approval_the_job_stays_waiting_and_the_tap_can_be_repeated(self):
        job_id = self.job_at("bundle", status="NEEDS_INPUT", pending_payload={"gate": "final", "review": {}}, pending_question={"question": "q", "options": ["approve", "regenerate"]})
        self.render.refuse = "approve"
        with self.captured_stdout():
            self.assertFalse(worker.resume_from_input(job_id, "approve"))
        self.assertEqual(db.get_job(job_id)["status"], "NEEDS_INPUT")
        self.render.refuse = None
        self.assertTrue(worker.resume_from_input(job_id, "approve"))
        self.assertEqual(db.get_job(job_id)["status"], "COMPLETED")

    def test_regenerate_clears_the_stage_in_contentrender_first_then_reruns_it(self):
        for stage, want in [("images", ("--kind", "still", "--all")), ("narration", ("--kind", "narration", "--all")), ("bundle", ("--kind", "render"))]:
            self.render.calls.clear()
            job_id = self.job_at(stage, status="NEEDS_INPUT", pending_payload={"gate": "x", "review": {}}, pending_question={"question": "q", "options": ["approve", "regenerate"]})
            self.assertTrue(worker.resume_from_input(job_id, "regenerate"))
            self.assertEqual(self.render.calls, [("regenerate", want)])
            job = db.get_job(job_id)
            self.assertEqual((job["status"], job["current_stage"]), ("PENDING", stage))

    def test_per_scene_regen_redoes_only_those_scenes_and_requeues(self):
        job_id = self.job_at("images", status="NEEDS_INPUT", pending_payload={"gate": "images", "review": {}}, pending_question={"question": "q", "options": ["approve", "regenerate"]})
        ok, msg = worker.regenerate_scenes(job_id, [3, 7])
        self.assertTrue(ok)
        self.assertEqual(self.render.calls, [("regenerate", ("--kind", "still", "--scenes", "3,7"))])
        self.assertEqual(db.get_job(job_id)["status"], "PENDING")
        self.assertIn("scenes 3, 7", msg)

    def test_per_scene_regen_uses_the_narration_kind_at_the_narration_gate(self):
        job_id = self.job_at("narration", status="NEEDS_INPUT", pending_payload={"gate": "narration", "review": {}}, pending_question={"question": "q", "options": ["approve", "regenerate"]})
        ok, _ = worker.regenerate_scenes(job_id, [4])
        self.assertTrue(ok)
        self.assertEqual(self.render.calls, [("regenerate", ("--kind", "narration", "--scenes", "4"))])

    def test_per_scene_regen_is_refused_anywhere_else(self):
        script_gate = self.make_job("script", status="NEEDS_INPUT", pending_payload={"scenes": []})
        running = self.job_at("images", status="SCHEDULED")
        for job_id in (script_gate, running, 999):
            ok, msg = worker.regenerate_scenes(job_id, [1])
            self.assertFalse(ok)
            self.assertTrue(msg)
        self.assertEqual(self.render.calls, [])

    def test_contentrender_refusing_a_scene_regen_leaves_the_job_waiting(self):
        job_id = self.job_at("images", status="NEEDS_INPUT", pending_payload={"gate": "images", "review": {}}, pending_question={"question": "q", "options": ["approve", "regenerate"]})
        self.render.refuse = "regenerate"
        with self.captured_stdout():
            ok, msg = worker.regenerate_scenes(job_id, [99])
        self.assertFalse(ok)
        self.assertIn("refused", msg)
        self.assertEqual(db.get_job(job_id)["status"], "NEEDS_INPUT")


class MediaNotificationTests(RenderTestCase):
    def photos(self, n: int) -> list[dict[str, Any]]:
        return [{"kind": "photo", "filename": f"s{i}.jpg", "content": b"x", "caption": f"Scene {i}"} for i in range(1, n + 1)]

    def test_fourteen_stills_are_two_albums(self):
        self.assertEqual(notifier.send_media(self.photos(14)), [])
        self.assertEqual([c["method"] for c in self.telegram.calls], ["sendMediaGroup", "sendMediaGroup"])
        self.assertEqual([len(json.loads(c["data"]["media"])) for c in self.telegram.of("sendMediaGroup")], [10, 4])

    def test_a_lone_remainder_goes_as_a_plain_photo_because_an_album_needs_two(self):
        notifier.send_media(self.photos(11))
        self.assertEqual([c["method"] for c in self.telegram.calls], ["sendMediaGroup", "sendPhoto"])
        self.assertEqual(self.telegram.of("sendPhoto")[0]["data"]["caption"], "Scene 11")

    def test_audio_and_video_use_their_own_methods_and_captions_are_cut_to_the_limit(self):
        notifier.send_media([
            {"kind": "audio", "filename": "narration.mp3", "content": b"a", "caption": "x" * 2000},
            {"kind": "video", "filename": "preview.mp4", "content": b"v", "caption": "rough cut"},
        ])
        self.assertEqual([c["method"] for c in self.telegram.calls], ["sendAudio", "sendVideo"])
        self.assertEqual(len(self.telegram.of("sendAudio")[0]["data"]["caption"]), notifier.MAX_CAPTION_CHARS)
        self.assertEqual(self.telegram.of("sendVideo")[0]["data"]["supports_streaming"], "true")

    def test_a_failed_upload_is_named_in_the_approval_message_so_nobody_approves_blind(self):
        path = os.path.join(self.briefs.name, "narration.mp3")
        with open(path, "wb") as f:
            f.write(b"a")
        job_id = self.job_at("narration", status="NEEDS_INPUT", pending_question={"question": "Q", "options": ["approve", "regenerate"]},
                             pending_payload={"gate": "narration", "review": {"files": [{"kind": "audio", "path": path, "caption": "c"}, {"kind": "video", "path": "/nope/preview.mp4", "caption": "v"}]}})
        self.telegram.responder = lambda m, k: FakeResponse(400 if m == "sendAudio" else 200, text="bad")
        with self.captured_stdout():
            self.assertTrue(notifier.notify_input_required(db.get_job(job_id)))
        text = self.telegram.messages[-1]["text"]
        self.assertIn("Could not attach", text)
        self.assertIn("narration.mp3", text)
        self.assertIn("preview.mp4", text)

    def test_review_attachments_skip_missing_oversized_and_unknown_files_and_say_so(self):
        ok = os.path.join(self.briefs.name, "ok.jpg")
        big = os.path.join(self.briefs.name, "big.jpg")
        for p, n in [(ok, 5), (big, 50)]:
            with open(p, "wb") as f:
                f.write(b"x" * n)
        job = {"pending_payload": {"review": {"files": [
            {"kind": "photo", "path": ok, "caption": "a"}, {"kind": "photo", "path": big, "caption": "b"},
            {"kind": "photo", "path": "/missing.jpg", "caption": "c"}, {"kind": "gif", "path": ok, "caption": "d"},
        ]}}}
        with mock.patch.object(review, "MAX_PHOTO_BYTES", 10):
            attachments, problems = review.media_attachments(job)
        self.assertEqual([a["filename"] for a in attachments], ["ok.jpg"])
        self.assertEqual(len(problems), 3)
        self.assertTrue(any("over Telegram's limit" in p for p in problems))
        self.assertTrue(any("unknown kind" in p for p in problems))

    def test_a_script_checkpoint_still_attaches_its_document_and_no_media(self):
        job_id = self.make_job("script", status="NEEDS_INPUT", pending_question={"question": "Q", "options": ["approve", "regenerate"]},
                               pending_payload={"title": "T", "scenes": [{"narration": "n", "durationEst": 5}]})
        notifier.notify_input_required(db.get_job(job_id))
        self.assertEqual([c["method"] for c in self.telegram.calls], ["sendDocument", "sendMessage"])
        self.assertNotIn("/regen", self.telegram.messages[0]["text"])


class RegenCommandTests(RenderTestCase):
    def message(self, text: str, user_id: int = 42) -> dict[str, Any]:
        return {"update_id": 1, "message": {"from": {"id": user_id}, "text": text}}

    def replies(self) -> list[str]:
        return [c["json"]["text"] for c in self.telegram.of("sendMessage")]

    def test_parsing(self):
        for text, want in [("/regen 12 3,7", (12, [3, 7])), ("/regen #12 3 7 9", (12, [3, 7, 9])), ("/regen@my_bot 2 5, 5", (2, [5])), ("/REGEN 1 1", (1, [1]))]:
            self.assertEqual(telegram_poller.parse_regen(text), want, text)
        for text in ["/regen", "/regen 12", "/regen x 3", "/regen 12 a", "/regen 12 0", "/regen 12 -1", "regen 12 3"]:
            self.assertIsNone(telegram_poller.parse_regen(text), text)

    def test_the_command_redoes_the_scenes_and_replies(self):
        job_id = self.job_at("images", status="NEEDS_INPUT", pending_payload={"gate": "images", "review": {}}, pending_question={"question": "q", "options": ["approve", "regenerate"]})
        telegram_poller._process_update(self.message(f"/regen {job_id} 3,7"))
        self.assertEqual(self.render.calls, [("regenerate", ("--kind", "still", "--scenes", "3,7"))])
        self.assertEqual(db.get_job(job_id)["status"], "PENDING")
        self.assertIn("scenes 3, 7", self.replies()[-1])

    def test_a_malformed_command_gets_the_usage_line_and_changes_nothing(self):
        telegram_poller._process_update(self.message("/regen banana"))
        self.assertEqual(self.replies(), [telegram_poller.REGEN_USAGE])
        self.assertEqual(self.render.calls, [])

    def test_anyone_but_the_owner_is_ignored_silently(self):
        job_id = self.job_at("images", status="NEEDS_INPUT", pending_payload={"gate": "images", "review": {}}, pending_question={"question": "q", "options": ["approve", "regenerate"]})
        with self.captured_stdout():
            telegram_poller._process_update(self.message(f"/regen {job_id} 1", user_id=666))
        self.assertEqual(self.render.calls, [])
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(db.get_job(job_id)["status"], "NEEDS_INPUT")

    def test_other_text_is_left_alone(self):
        telegram_poller._process_update(self.message("hello"))
        telegram_poller._process_update(self.message("/status"))
        self.assertEqual(self.telegram.calls, [])


if __name__ == "__main__":
    unittest.main()

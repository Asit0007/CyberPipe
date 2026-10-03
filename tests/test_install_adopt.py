"""The 2026-10-03 additions: readable run names, the guard that never lets `step` restart a run, `adopt`, one story
at a time, `/finish`, `/status`, `/help`, the one-week gate timeout and its reminder. ContentRender is faked here
(its real outcome shapes); tests/test_e2e_contentrender.py drives the real one."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import adopt
import config
import db
import notifier
import pipeline
import scheduler
import submit_job
import telegram_poller
import worker
from tests.support import TEST_CHAT_ID
from tests.test_render_stages import SCRIPT, RenderTestCase

BRIEF = {"title": "How a Zero-Permission App Could Control Your OnePlus", "aspectRatio": "16:9",
         "scenes": [{"id": "scene-1", "sceneNumber": 1, "narration": "It starts with an app.", "durationEst": 8, "visualPrompt": "p"}]}
CHANGED = {"status": "error", "problems": ["the brief has changed since this run started, so it is a different run; use `step` with a new --video-id"]}


def status_ok(images: str = "pending", narration: str = "pending", final: str = "pending", vid: str = "v") -> dict[str, Any]:
    return {"status": "ok", "videoId": vid, "stage": {},
            "approvals": {"images": {"status": images}, "narration": {"status": narration}, "final": {"status": final}}}


class Base(RenderTestCase):
    def make_run(self, vid: str, brief: dict[str, Any] = BRIEF) -> Path:
        run = Path(self.runs.name) / vid
        run.mkdir(parents=True)
        (run / "manifest.json").write_text("{}")
        (run / "brief.json").write_text(json.dumps(brief))
        return run

    def write_brief(self, brief: Any = BRIEF, folder: str = "story") -> Path:
        d = Path(self.briefs.name) / folder
        d.mkdir(parents=True, exist_ok=True)
        path = d / "brief.json"
        path.write_text(brief if isinstance(brief, str) else json.dumps(brief))
        return path

    def message(self, text: str) -> dict[str, Any]:
        return {"update_id": 1, "message": {"text": text, "from": {"id": int(TEST_CHAT_ID)}}}


class VideoIdTests(Base):
    def test_a_job_gets_a_readable_run_name_on_its_first_render_call_and_keeps_it(self):
        job_id = self.job_at("images")
        self.render.step_outcomes = [{"status": "progress", "retryInSec": 5}, {"status": "progress", "retryInSec": 5}]
        worker.run_job(job_id)
        vid = pipeline.video_id(db.get_job(job_id))
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertEqual(vid, f"{today}-the-quiet-backdoor-j{job_id}")
        self.assertRegex(vid, pipeline.VIDEO_ID_RE)
        db.update_job(job_id, stage_outputs={"script": {**SCRIPT, "title": "A Different Title"}}, next_retry_at=None)
        worker.run_job(job_id)
        self.assertEqual(pipeline.video_id(db.get_job(job_id)), vid, "a later title change never renames the run")

    def test_a_job_whose_old_style_run_exists_keeps_that_name(self):
        job_id = self.job_at("images")
        (Path(self.runs.name) / f"job-{job_id}").mkdir()
        self.render.step_outcomes = [{"status": "progress", "retryInSec": 5}]
        worker.run_job(job_id)
        self.assertEqual(pipeline.video_id(db.get_job(job_id)), f"job-{job_id}")

    def test_the_slug_is_ascii_hyphenated_and_capped(self):
        self.assertEqual(pipeline.slugify("Café — “Zero-Day” Attack!"), "cafe-zero-day-attack")
        self.assertLessEqual(len(pipeline.slugify("word " * 40)), 40)
        self.assertEqual(pipeline.slugify("!!!"), "video")


class SupersedeGuardTests(Base):
    def test_a_run_made_from_a_different_brief_fails_the_job_and_step_never_runs(self):
        job_id = self.job_at("images", input_payload={"videoId": "existing-run"})
        self.make_run("existing-run")
        original = self.render.__call__
        self.render_calls = []

        def fake(command, job, brief, *extra):
            self.render_calls.append(command)
            return CHANGED if command == "status" else original(command, job, brief, *extra)

        with mock.patch.object(pipeline, "_run_render", fake), self.captured_stdout():
            worker.run_job(job_id)
        job = db.get_job(job_id)
        self.assertEqual(job["status"], "FAILED")
        self.assertIn("different brief", job["last_error"])
        self.assertEqual(self.render_calls, ["status"], "step must never run against a mismatched run")
        self.assertTrue((Path(self.runs.name) / "existing-run" / "manifest.json").exists())

    def test_a_matching_run_is_checked_and_then_stepped(self):
        job_id = self.job_at("images", input_payload={"videoId": "existing-run"})
        self.make_run("existing-run")
        self.render.step_outcomes = [{"status": "progress", "retryInSec": 5}]
        worker.run_job(job_id)
        self.assertEqual(self.render.commands(), ["status", "step"])

    def test_no_run_yet_means_no_status_call(self):
        job_id = self.job_at("images")
        self.render.step_outcomes = [{"status": "progress", "retryInSec": 5}]
        worker.run_job(job_id)
        self.assertEqual(self.render.commands(), ["step"])


class AdoptTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.status_answers: dict[str, dict[str, Any]] = {}
        self.status_calls: list[str] = []

        def render_cli(command, brief, vid, *extra):
            self.assertEqual(command, "status", "adopt only ever asks, it never changes a run")
            self.status_calls.append(vid)
            return self.status_answers.get(vid, {"status": "error", "problems": [f'no run "{vid}" to status']})

        p = mock.patch.object(pipeline, "render_cli", render_cli)
        p.start()
        self.addCleanup(p.stop)

    def adopt(self, path: Path, **kw: Any):
        return adopt.adopt(path, export=kw.pop("export", False), **kw)

    def test_a_fresh_story_gets_a_new_dated_run_at_the_images_stage(self):
        job_id, notes = self.adopt(self.write_brief())
        job = db.get_job(job_id)
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(pipeline.video_id(job), f"{today}-how-a-zero-permission-app-could-control")
        self.assertEqual((job["current_stage"], job["status"]), ("images", "PENDING"))
        self.assertEqual(job["stage_outputs"]["script"], BRIEF)
        self.assertIn("new run", notes[0])

    def test_an_existing_run_with_this_brief_is_continued_at_its_gate(self):
        for approvals, stage in ((("pending",), "images"), (("approved",), "narration"), (("approved", "approved"), "bundle")):
            with self.subTest(stage=stage):
                with db.get_connection() as conn:
                    conn.execute("DELETE FROM jobs")
                vid = f"run-{stage}"
                self.make_run(vid)
                self.status_answers = {vid: status_ok(*approvals, vid=vid)}
                job_id, notes = self.adopt(Path(self.runs.name) / vid / "brief.json")
                job = db.get_job(job_id)
                self.assertEqual((pipeline.video_id(job), job["current_stage"]), (vid, stage))
                self.assertIn("continuing", notes[0])
                import shutil
                shutil.rmtree(Path(self.runs.name) / vid)

    def test_a_run_for_the_same_title_made_from_a_different_brief_is_refused_with_the_way_out(self):
        self.make_run("2026-09-30-how-a-zero-permission-app-could-control")
        self.status_answers = {"2026-09-30-how-a-zero-permission-app-could-control": CHANGED}
        edited = {**BRIEF, "scenes": [{**BRIEF["scenes"][0], "narration": "Edited."}]}
        with self.assertRaises(adopt.AdoptError) as err:
            self.adopt(self.write_brief(edited))
        self.assertIn("keep its stills", str(err.exception))
        self.assertIn("--video-id", str(err.exception))
        self.assertEqual(adopt.active_jobs(), [], "nothing is created when adopt refuses")

    def test_an_explicit_video_id_for_a_mismatched_run_is_refused(self):
        self.make_run("taken")
        self.status_answers = {"taken": CHANGED}
        with self.assertRaises(adopt.AdoptError) as err:
            self.adopt(self.write_brief(), video_id="taken")
        self.assertIn("different brief", str(err.exception))

    def test_a_delivered_run_is_refused(self):
        self.make_run("done")
        self.status_answers = {"done": status_ok("approved", "approved", "approved", vid="done")}
        with self.assertRaises(adopt.AdoptError):
            self.adopt(Path(self.runs.name) / "done" / "brief.json")

    def test_a_broken_hand_edit_is_caught_before_anything_happens(self):
        cases = {
            "{ not json": "not valid JSON",
            json.dumps({"title": "T", "scenes": []}): "no scenes",
            json.dumps({"title": "T", "scenes": [{"id": "s1", "narration": "  "}]}): "no narration",
            json.dumps({"scenes": [{"id": "s1", "narration": "n"}]}): "no title",
        }
        for text, why in cases.items():
            with self.subTest(why=why):
                with self.assertRaises(adopt.AdoptError) as err:
                    self.adopt(self.write_brief(text, folder=why.replace(" ", "-")))
                self.assertIn(why, str(err.exception))
        self.assertEqual(self.status_calls, [])

    def test_one_story_at_a_time_unless_forced(self):
        self.adopt(self.write_brief())
        other = {**BRIEF, "title": "Another Story"}
        with self.assertRaises(adopt.AdoptError) as err:
            self.adopt(self.write_brief(other, folder="other"))
        self.assertIn("--force", str(err.exception))
        job_id, _ = self.adopt(self.write_brief(other, folder="other"), force=True)
        self.assertEqual(len(adopt.active_jobs()), 2)

    def test_the_same_run_is_never_driven_by_two_jobs(self):
        self.make_run("shared")
        self.status_answers = {"shared": status_ok(vid="shared")}
        self.adopt(Path(self.runs.name) / "shared" / "brief.json")
        with self.assertRaises(adopt.AdoptError) as err:
            self.adopt(Path(self.runs.name) / "shared" / "brief.json", force=True)
        self.assertIn("already driving", str(err.exception))

    def test_a_story_start_brief_gets_a_fresh_export_and_a_failed_export_only_warns(self):
        path = self.write_brief()
        (path.parent / "research.json").write_text(json.dumps({"summary": "r"}))
        (path.parent / "plan.json").write_text(json.dumps({"tone": "Deep Dive Documentary"}))
        posted = []

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": True, "relativePath": "exports/x.md"}

        def post(url, json=None, timeout=None):
            posted.append((url, json))
            return Resp()

        with mock.patch("adopt.requests.post", post):
            _, notes = adopt.adopt(path)
        self.assertTrue(posted[0][0].endswith("/api/export/markdown"))
        self.assertEqual(posted[0][1]["script"], BRIEF)
        self.assertEqual(posted[0][1]["plan"]["tone"], "Deep Dive Documentary")
        self.assertIn("exports/x.md", notes[-1])

        import requests
        with db.get_connection() as conn:
            conn.execute("DELETE FROM jobs")
        with mock.patch("adopt.requests.post", side_effect=requests.ConnectionError("refused")):
            job_id, notes = adopt.adopt(path)
        self.assertIn("warning", notes[-1])
        self.assertIsNotNone(db.get_job(job_id), "a failed export never blocks the adoption")

    def test_the_command_line_reports_a_refusal_without_a_traceback(self):
        path = self.write_brief("{ broken")
        with self.captured_stdout(), mock.patch("sys.stderr") as err:
            self.assertEqual(submit_job.main(["adopt", "--brief", str(path), "--no-export"]), 1)
        self.assertIn("Not adopted", "".join(c.args[0] for c in err.write.call_args_list))


class FinishTests(Base):
    def test_finish_holds_the_job_while_contentrender_works_then_makes_it_due(self):
        job_id = self.job_at("bundle", status="SCHEDULED", next_retry_at=self.ahead(hours=20))
        seen = []

        def fake(command, job, brief, *extra):
            seen.append((command, db.get_job(job_id)["status"]))
            return {"status": "ok", "finished": 4}

        with mock.patch.object(pipeline, "_run_render", fake):
            ok, reply = worker.finish_clips(job_id)
        self.assertTrue(ok)
        self.assertEqual(seen, [("finish-clips", "RUNNING")], "the scheduler cannot claim it mid-call")
        job = db.get_job(job_id)
        self.assertEqual(job["status"], "SCHEDULED")
        self.assertIsNone(job["locked_by"])
        self.assertLessEqual(job["next_retry_at"], db.now_iso())
        self.assertIn("4 clip(s)", reply)

    def test_finish_is_refused_outside_the_clips_wait(self):
        for stage, status in (("images", "SCHEDULED"), ("bundle", "RUNNING"), ("bundle", "NEEDS_INPUT")):
            with self.subTest(stage=stage, status=status):
                job_id = self.job_at(stage, status=status)
                ok, reply = worker.finish_clips(job_id)
                self.assertFalse(ok)
                self.assertEqual(db.get_job(job_id)["status"], status)
        self.assertEqual(self.render.calls, [])

    def test_a_refusal_releases_the_job(self):
        job_id = self.job_at("bundle", status="SCHEDULED")
        self.render.refuse = "finish-clips"
        with self.captured_stdout():
            ok, reply = worker.finish_clips(job_id)
        self.assertFalse(ok)
        self.assertEqual(db.get_job(job_id)["status"], "SCHEDULED")
        self.assertIsNone(db.get_job(job_id)["locked_by"])

    def test_the_finish_command_and_help(self):
        job_id = self.job_at("bundle", status="SCHEDULED")
        telegram_poller._process_update(self.message(f"/Finish #{job_id}"))
        self.assertEqual(self.render.commands(), ["finish-clips"])
        telegram_poller._process_update(self.message("/finish"))
        self.assertIn("Usage: /finish", self.telegram.messages[-1]["text"])
        telegram_poller._process_update(self.message("/help"))
        for command in ("/status", "/resume", "/finish", "/regen"):
            self.assertIn(command, self.telegram.messages[-1]["text"])


class StatusTests(Base):
    def test_status_lists_unfinished_jobs_only(self):
        self.assertEqual(worker.status_report(), "No active jobs.")
        waiting = self.job_at("bundle", status="SCHEDULED", next_retry_at=self.ahead(hours=3), last_error="paused on clips\nmore")
        gate = self.job_at("images", status="NEEDS_INPUT")
        self.job_at("bundle", status="COMPLETED")
        report = worker.status_report()
        lines = report.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn(f"#{waiting} The Quiet Backdoor · bundle · SCHEDULED until", lines[0])
        self.assertIn("(paused on clips)", lines[0])
        self.assertIn(f"#{gate}", lines[1])
        self.assertIn("NEEDS_INPUT since", lines[1])
        telegram_poller._process_update(self.message("/status"))
        self.assertEqual(self.telegram.messages[-1]["text"], report)


class GateTimeoutTests(Base):
    def gate(self, hours_ago: float) -> int:
        job_id = self.job_at("images", status="NEEDS_INPUT", pending_payload={"gate": "images", "review": {}},
                             pending_question={"question": "Stills ready", "options": ["approve", "regenerate"]})
        self.set_raw(job_id, updated_at=db.to_iso(datetime.now(timezone.utc)) if hours_ago == 0 else
                     db.to_iso(datetime.fromisoformat(self.ago(hours=hours_ago))))
        return job_id

    def test_the_default_timeout_is_a_week(self):
        # The code default; a machine's .env may still override it, so read it from the source of truth.
        self.assertIn('"NEEDS_INPUT_TIMEOUT_HOURS", "168"', Path(config.__file__).read_text())
        job_id = self.gate(100)
        with mock.patch.object(config, "NEEDS_INPUT_TIMEOUT_HOURS", 168), self.captured_stdout():
            scheduler._handle_stale_needs_input()
        self.assertEqual(db.get_job(job_id)["status"], "NEEDS_INPUT", "100 h used to fail it (72 h); now it waits")

    def test_one_reminder_with_the_buttons_after_a_day(self):
        job_id = self.gate(25)
        scheduler._remind_waiting_gates()
        scheduler._remind_waiting_gates()
        reminders = [m for m in self.telegram.messages if m["text"].startswith("🔔")]
        self.assertEqual(len(reminders), 1)
        self.assertIn(f"job:{job_id}:approve", json.dumps(reminders[0]["reply_markup"]))
        self.assertIn("Run folder:", reminders[0]["text"])

    def test_no_reminder_before_a_day_or_when_turned_off(self):
        self.gate(2)
        scheduler._remind_waiting_gates()
        self.gate(30)
        with mock.patch.object(config, "NEEDS_INPUT_REMINDER_HOURS", 0):
            scheduler._remind_waiting_gates()
        self.assertEqual([m for m in self.telegram.messages if m["text"].startswith("🔔")], [])

    def test_regenerate_lets_the_next_draft_be_reminded_about(self):
        job_id = self.gate(25)
        scheduler._remind_waiting_gates()
        worker.resume_from_input(job_id, "regenerate")
        self.assertFalse(any(k.startswith("needs_input:reminder") for k in db.get_job(job_id)["notified"]))


class MessageTests(Base):
    def test_clip_pause_and_completion_name_the_run_folder_and_finish(self):
        job_id = self.job_at("bundle", input_payload={"videoId": "my-run"})
        notifier.notify_clips_paused(db.get_job(job_id), {"made": 1, "waiting": 2}, "2026-10-04T08:00:00+00:00")
        text = self.telegram.messages[-1]["text"]
        self.assertIn(f"/finish {job_id}", text)
        self.assertIn(str(Path(self.runs.name) / "my-run"), text)
        db.update_job(job_id, status="COMPLETED", stage_outputs={"script": SCRIPT, "bundle": {"delivered": True}})
        notifier.notify_completed(db.get_job(job_id))
        self.assertIn("resolve/*.fcpxml", self.telegram.messages[-1]["text"])


class ReviewFixTests(Base):
    """The three code-review findings of 2026-10-03."""

    def deliver(self, vid: str) -> None:
        (Path(self.runs.name) / vid / "manifest.json").write_text(json.dumps({"status": "delivered"}))

    def test_adopt_never_asks_contentrender_about_a_delivered_run(self):
        calls = []
        with mock.patch.object(pipeline, "render_cli", lambda *a: calls.append(a) or status_ok()):
            self.make_run("finished")
            self.deliver("finished")
            for kwargs in ({"video_id": "finished"}, {}):  # explicit id, and found by title
                with self.subTest(**kwargs), self.assertRaises(adopt.AdoptError) as err:
                    adopt.adopt(Path(self.runs.name) / "finished" / "brief.json", export=False, **kwargs)
                self.assertIn("already delivered", str(err.exception))
        self.assertEqual(calls, [], "a status probe of a delivered run used to move it aside (older ContentRender)")
        self.assertEqual(sorted(p.name for p in Path(self.runs.name).iterdir()), ["finished"])

    def test_the_restart_guard_stops_at_a_delivered_run_without_status_or_step(self):
        job_id = self.job_at("bundle", input_payload={"videoId": "finished"})
        self.make_run("finished")
        self.deliver("finished")
        with self.captured_stdout():
            worker.run_job(job_id)
        self.assertEqual(db.get_job(job_id)["status"], "FAILED")
        self.assertIn("already delivered", db.get_job(job_id)["last_error"])
        self.assertNotIn("status", self.render.commands())
        self.assertNotIn("step", self.render.commands())

    def test_runs_dir_follows_render_dir_in_contentrenders_own_env_and_our_env_wins(self):
        cr = tempfile.TemporaryDirectory()
        self.addCleanup(cr.cleanup)
        (Path(cr.name) / ".env").write_text("# RENDER_DIR=ignored\nRENDER_DIR=\"elsewhere\"\n")
        with mock.patch.object(config, "CONTENTRENDER_RUNS_DIR", ""), mock.patch.object(config, "CONTENTRENDER_DIR", cr.name), \
                mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("RENDER_DIR", None)
            self.assertEqual(pipeline.runs_dir(), Path(cr.name) / "elsewhere" / "runs")
            env["RENDER_DIR"] = "/abs/render"
            self.assertEqual(pipeline.runs_dir(), Path("/abs/render/runs"))
            env.pop("RENDER_DIR")
            (Path(cr.name) / ".env").unlink()
            self.assertEqual(pipeline.runs_dir(), Path(cr.name) / "output" / "runs")

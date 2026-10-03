# CyberPipe

Durable job orchestrator for a cybersecurity-documentary video pipeline:
news story/CVE/breach report in, an 8-10 minute scripted video brief out,
with SQLite-backed job state, automatic rate-limit retry, and a Telegram
approve/regenerate checkpoint before a script is considered final.

Sibling repo to [`ContentPipe`](https://github.com/Asit0007/ContentPipe) —
this project doesn't reimplement research or script generation, it calls
ContentPipe's `/api/research`, `/api/plan`, and `/api/script` endpoints for
that. Python, stdlib + `requests` + `sqlite3` only. See `CLAUDE.md` for the
full architecture, the job state machine, current integration gaps, and the
macOS deployment writeup — this file is quick start only.

**Two repos, two jobs.** ContentPipe is the engine (it does every model call, the
research, the script and its audit, and owns media and assembly); CyberPipe is the
orchestrator around it (it turns a story into a durable job, survives crashes and
quota hits, and makes a human approve the script). Neither duplicates the other's
work: ContentPipe retries within one request for seconds, CyberPipe retries across
requests for minutes to days. ContentPipe's README has the side-by-side table.

---

## Quick start

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Then create a `.env` in this folder (gitignored) with the variables you need from [Environment](#environment).
None is required, so an empty `.env` runs; for real use set at least `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and,
under launchd, `CONTENTRENDER_NODE`. There is no `.env.example` (removed 2026-10-01): that table is the reference.

`ContentPipe` must be running separately for stages 1-3 (and for the images, analyst voice and clips
of stages 4-6) to have something to call. On the owner's Mac a LaunchAgent (`com.asitminz.contentpipe`)
already keeps it up on port 3000; anywhere else:

```bash
cd ../ContentPipe && npm run dev   # http://localhost:3000
```

Then, from this repo. **The usual way (owner, 2026-10-03):** write the script with ContentPipe's
`npm run story:start`, edit its `brief.json` by hand if you want, then hand it over; CyberPipe runs it from the stills
onward with Telegram gates:

```bash
./venv/bin/python submit_job.py adopt --brief "../ContentPipe/.runs/story-<slug>/brief.json"
# a ContentRender run started by hand: pass that run's own brief (its stills and voice lines are kept)
./venv/bin/python submit_job.py adopt --brief "../ContentRender/output/runs/<run>/brief.json"
```

`adopt` checks the brief (valid JSON, a title, scenes with narration), finds an existing run for the same title and
continues it only if ContentRender agrees it is the run's own brief, otherwise refuses with the way out (a different
brief would make ContentRender start the run over). For a `story:start` brief it also asks ContentPipe for a fresh
Markdown export, so your edits show up there (`--no-export` skips it). New runs are named `<date>-<title slug>`.

Or a story from scratch, research → plan → script through ContentPipe with a Telegram script gate:

```bash
./venv/bin/python submit_job.py --text "A critical auth bypass in..." --url "https://..."
```

Only **one story runs at a time** (they share the free image and clip quotas); `--force` overrides it, on either
command. To run the services by hand instead of installing them (see Deployment):

```bash
./venv/bin/python scheduler.py          # separate terminal — polls every 60s, runs due jobs
./venv/bin/python telegram_poller.py    # separate terminal — only needed once TELEGRAM_* is set
```

**Telegram commands** (only your chat is answered): `/status` (every unfinished job: stage, status, next run),
`/resume <job>` (run a waiting job now, e.g. once hand-made clips are in), `/finish <job>` (stop waiting for clips;
scenes without one keep their still), `/regen <job> <scenes>` (at the stills or narration gate, redo just those
scenes), `/help`. A gate left unanswered gets one reminder after `NEEDS_INPUT_REMINDER_HOURS` (24) and fails after
`NEEDS_INPUT_TIMEOUT_HOURS` (168) without an answer. Messages name the run folder on this Mac.

Two flags on `submit_job.py` are worth telling apart, because they used to be the same
value and it produced a script that welcomed viewers to a tool:

| Flag | Means | Default |
|---|---|---|
| `--brand` | The **show** name written into the script. `--channel-name` is kept as an alias. | `CHANNEL_BRAND_NAME` (see below) |
| `--source-name` | Where the **story** came from — a Telegram feed, a wire. Goes into the research prompt as its origin. | Omitted. Our own channel is not a story's origin, so nothing is sent unless you name one. |

Also sent to `/api/research`: the target duration. ContentPipe scales how much research it
asks for off the length of the script it has to carry, so a 9-minute documentary isn't
researched as though it were a 60-second short. The default target is 585 s (`DEFAULT_TARGET_DURATION_SEC`):
ContentPipe's audit calls a script short below 0.92 × target, and 585 s keeps that floor (538 s) safely above the
480 s mid-roll minimum, which the old 540 s default did not.

Every ContentPipe call sends `X-ContentPipe-Strict: 1`, so a quota hit or outage
comes back as `429`/`503` with `Retry-After` — which becomes a scheduled retry at
the right time — rather than canned sample content that would look like a real
draft. An interrupted script resumes from ContentPipe's last finished chunk when
the job retries. Details in `CLAUDE.md` ("Fail-closed contract").

`scheduler.py` picks up the job on its next tick and runs it through
research → plan → script. Without `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`
set, it stops at the script-approval checkpoint with no way to tap approve.
That's expected for a first run: the approval request is **held, not lost**, and
is sent on the next tick once Telegram is configured (restart `scheduler.py`
after editing `.env`). It still proves the pipeline and the rate-limit/backoff
paths work.

### What it does when things go wrong

| Situation | Behaviour |
|---|---|
| ContentPipe returns 429 (quota) | Job is `SCHEDULED` for the `Retry-After` time, no attempt consumed. One ⏳ message per unbroken wait (plus one more if a short wait turns into a long one), not one per retry. Gives up after `MAX_WAIT_DAYS`. |
| ContentPipe returns 409 (identical script still generating) | Waits and retries quietly — not counted as a failure. |
| ContentPipe returns 503 (every model provider overloaded) | Job is `SCHEDULED` like a rate limit, no attempt consumed: first retry after its `Retry-After`, then a wait of half the outage's age up to `OVERLOAD_MAX_WAIT_SECONDS`. You get one message only if the outage lasts 15 minutes. Gives up after `MAX_WAIT_DAYS`. |
| ContentRender pauses the clips step (the free video quota is spent) | Job is `SCHEDULED` until the quota is expected back, no attempt consumed and **no `MAX_WAIT_DAYS` limit**. One Telegram message per calendar day paused, saying which clips are waiting and where to drop hand-made ones. `/resume <job>` wakes it at once. ContentRender itself gives up after 3 days and finishes with Ken Burns stills. |
| ContentPipe returns 502 `zero_quota` (key has no quota) | Fails immediately with a note to enable billing. |
| Any other stage error | Backoff 5m / 15m / 45m / 2h / 6h, then `FAILED` after `MAX_STAGE_ATTEMPTS`. |
| Scheduler killed or Mac restarted mid-stage | The job is re-queued on the next tick (counts as an attempt); ContentPipe resumes from its last finished chunk. |
| Telegram down or bot not configured | Messages are re-sent every tick (at most once per `TELEGRAM_RESEND_COOLDOWN_SECONDS` per event) until Telegram accepts them. |
| You tap Regenerate | A fresh draft is generated and its approval request is sent again. |
| No tap for `NEEDS_INPUT_TIMEOUT_HOURS` | Job fails with a notification; a tap that lands during the sweep wins. |

The approval message attaches the whole draft as `job-<id>-script-draft.md`
(every scene's narration with timestamps, ContentPipe's audit findings, the
sources actually read), so you approve what you've read, not a summary.

---

Which component calls which model, and when, across CyberPipe → ContentPipe → ContentRender: see the call tree in [`../ContentPipe/README.md`](https://github.com/Asit0007/ContentPipe#how-the-calls-are-divided) ("How the calls are divided").

## Environment

| Variable | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | No | Human-in-the-loop checkpoints and notifications. Without it `scheduler.py` holds notifications until it's set, and `telegram_poller.py` exits immediately. |
| `TELEGRAM_CHAT_ID` | No | Only this chat is authorized to resume a job. |
| `CHANNEL_BRAND_NAME` | No | The show name ContentPipe writes into scripts when a job doesn't carry its own. Defaults to `Blast Radius`, matching ContentPipe's `DEFAULT_CHANNEL_BRAND`. Never set this to the name of this tool — it ends up spoken on camera. |
| `CONTENTPIPE_BASE_URL` | No | Defaults to `http://localhost:3000`. |
| `CONTENTPIPE_TIMEOUT_SECONDS` | No | Per-request timeout for research/plan, defaults to 600 (was 180, shorter than one stalled model inside ContentPipe). |
| `CONTENTPIPE_SCRIPT_TIMEOUT_SECONDS` | No | Defaults to 1800 (30 min) — `/api/script` makes many sequential LLM calls internally for a long-form script. |
| `CONTENTRENDER_DIR` | No | Where ContentRender is checked out. Defaults to `../ContentRender`. Stages 4-6 run its command line there. |
| `CONTENTRENDER_NODE` | No | Absolute path of `node` (launchd has no PATH). Defaults to the `node` on PATH. |
| `CONTENTRENDER_STEP_BUDGET_SECONDS` / `CONTENTRENDER_TIMEOUT_SECONDS` | No | Seconds of work one call may start (1200) / the ceiling on one call (budget + 900). |
| `BRIEFS_DIR` | No | Where the approved script is written for ContentRender. Defaults to `./data/briefs`. |
| `DB_PATH` | No | Defaults to `pipeline.db` in this repo. |
| `POLL_INTERVAL_SECONDS` | No | Scheduler tick interval, defaults to 60. |
| `NEEDS_INPUT_TIMEOUT_HOURS` | No | A job waiting on your tap fails after this long without an answer, counted from the last message about it; defaults to 168 (a week; it was 72 until 2026-10-03). |
| `NEEDS_INPUT_REMINDER_HOURS` | No | One reminder (with the buttons) once a gate has waited this long; defaults to 24, `0` turns it off. |
| `CONTENTRENDER_RUNS_DIR` | No | ContentRender's runs folder; defaults to its own, `<CONTENTRENDER_DIR>/<RENDER_DIR or output>/runs`. Read only: to name a run in messages and match an existing run. |
| `MAX_STAGE_ATTEMPTS` | No | Ordinary failures before `FAILED`, defaults to 5. |
| `MAX_WAIT_DAYS` | No | Give up on a job that has been continuously rate-limited, waiting on a busy ContentPipe, or waiting out a provider outage (503), defaults to 7. |
| `OVERLOAD_MAX_WAIT_SECONDS` | No | Longest gap between re-polls of a ContentPipe whose providers are overloaded, defaults to 900. |
| `RUNNING_LEASE_SECONDS` | No | A `RUNNING` job with no live worker is re-queued; this lease must exceed the longest stage. Defaults to the longer of the script and ContentRender timeouts, + 600. |
| `TELEGRAM_RESEND_COOLDOWN_SECONDS` | No | Minimum gap before re-trying a Telegram message that failed to send, defaults to 300. |

`.env` is gitignored and read once at process start — restart
`scheduler.py`/`telegram_poller.py` after editing it.

---

## Tests

Stdlib `unittest` only — no test dependency, no network, no real Telegram, and each test gets
its own temporary SQLite file:

```bash
./venv/bin/python -m unittest discover -s tests -t . -v
```

199 tests, 2 of them opt-in and skipped by default (run 2026-10-03), covering adopt, the restart guard, run names, one story at a time, `/status` `/finish` `/help` and the gate reminder (tests/test_install_adopt.py), plus the job state machine (retry dispatch, crash recovery, regenerate/approve,
timeouts, the clips pause and `/resume`), Telegram delivery and the poller, how ContentPipe's status codes map onto
worker behaviour, and what the request bodies sent to ContentPipe actually contain. See `CLAUDE.md`
"Tier 1 audit fixes" for what each guards against.

---

## The story cycle (built 2026-09-29; checklist in `../plan-story-cycle.md`)

The owner's workflow: submit one story **by hand** (nothing starts on a schedule); CyberPipe runs research → plan →
script → images → narration → clips → bundle; when the free video quota runs out, the clips step **pauses** and
**resumes on its own when the quota is expected back** (there is no fixed time of day); the cycle ends at the approved
Resolve bundle.

- **Built:** the clips-pause state (waits days, outside `MAX_WAIT_DAYS`, one message per day) and Telegram
  `/resume <job>`, which wakes any waiting job.
- **Built 2026-10-03:** `submit_job.py adopt`, one story at a time, readable run names, the guard that stops a job
  instead of letting a changed brief restart a run, `/status`, `/finish`, `/help`, the 168 h gate timeout and its 24 h
  reminder, and `deploy/install.sh` / `uninstall.sh`.
- **Not done, owner only:** LaunchAgents for the scheduler and poller. The Telegram bot already exists (@Cyber_Pipe_07_Bot, display name "ContentPipe"; token and chat id in `.env`, a test message delivered 2026-10-03), so no new bot is needed.

**CyberPipe has never been installed or run a job** (checked 2026-10-03): a `.env` with a working Telegram bot exists, but there is
no `pipeline.db` and no LaunchAgent, and the launcher app built 2026-09-19 still points at the old `~/Documents` path (rebuild it
with `deploy/build-launcher.sh` before installing). The install and remaining build are planned in `../plan-cyberpipe.md`. The first real story (OnePlus, 2026-09-30) is therefore being run by hand: ContentPipe's
`npm run story:start` for the script, then ContentRender's command line for the media.

**Next steps:** (1) the owner runs `./deploy/install.sh`; (2) `submit_job.py adopt` the OnePlus run with its own
brief, and take it through the gates in Telegram, which also closes `../plan-resolve-bundle.md`.

## Current status

| Stage | Status |
|---|---|
| 1. Research | Built — calls ContentPipe `/api/research`, forwarding the target duration so the dossier is researched to the depth the script needs |
| 2. Plan | Built — calls ContentPipe `/api/plan`, including target video duration |
| 3. Script | Built — calls ContentPipe `/api/script`, mandatory Telegram approve/regenerate checkpoint; the approval message includes ContentPipe's audit findings (runtime shortfall, unsourced figures, mid-roll eligibility) and attaches the full draft |
| Edit a script | Decided 2026-10-03: edit the story's `brief.json` (from `story:start`) before `submit_job.py adopt`, which checks it and re-exports. The `--text` path's Telegram script gate still commits the draft as-is |
| 4. `images` — stills | Built (2026-09-26) — runs [ContentRender](../ContentRender)'s command line; gate: the stills as Telegram albums; `/regen <job> 3,7` redoes single scenes |
| 5. `narration` — two-voice narration | Built — same CLI; Kokoro narrates locally, Charon (via ContentPipe) reads the analyst lines; gate: one MP3 |
| 6. `bundle` — AI clips, then the DaVinci Resolve bundle | Built — clips run here, after the narration gate (since 2026-09-29): a clip that cannot be made becomes a Ken Burns fallback, a spent free quota pauses the job (see the table above). Then the FCPXML timeline, captions, rough-cut MP4; gate: the rough cut; approve → `COMPLETED`. Verified end to end on stub media and, for the media stages, once on real quota; the FCPXML imports into Resolve 18.6 (owner, 2026-09-27). Not yet run on a real story through Telegram: CyberPipe has never been installed |
| Telegram commands | Built — approve/regenerate buttons, `/status`, `/resume`, `/finish`, `/regen`, `/help` (2026-10-03) |
| Post-publish analytics feedback loop | Not started — needs YouTube Data + Analytics OAuth |

A `COMPLETED` job now means the Resolve bundle was approved (stages 4-6 above). Full gap analysis against the original spec, including two
operational constraints discovered while testing (a real Gemini schema
limit, and Gemini's 20-requests/day/model free-tier cap — which ContentPipe's
provider chain now spreads across DeepSeek / Grok / free tiers once their keys
are set) live in `CLAUDE.md`.

---

## Deployment

Runs on a Mac via `launchd`, not a VPS. Install both services with one command (auto-mode sessions can't, so run it
yourself):

```bash
./deploy/install.sh --dry-run   # checks venv, .env, node, ContentRender, Kokoro, ContentPipe; renders the plists
./deploy/install.sh             # rebuilds the launcher app, loads com.asitminz.cyberpipe.scheduler and .poller
./deploy/uninstall.sh           # unloads and removes both; keeps pipeline.db and data/
```

See `CLAUDE.md`'s "Deployment — Mac via launchd" for why it is a launcher app and two KeepAlive agents, and why that's
convenience-grade rather than true 24/7 durability.

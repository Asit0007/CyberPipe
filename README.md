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

---

## Quick start

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
```

`ContentPipe` must be running separately for stages 1-3 to have something
to call:

```bash
cd ../ContentPipe && npm run dev   # http://localhost:3000
```

Then, from this repo:

```bash
python3 submit_job.py --text "A critical auth bypass in..." --url "https://..."
python3 scheduler.py          # separate terminal — polls every 60s, runs due jobs
python3 telegram_poller.py    # separate terminal — only needed once TELEGRAM_* is set
```

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
| ContentPipe returns 429 (quota) | Job is `SCHEDULED` for the `Retry-After` time, no attempt consumed, one ⏳ message. Gives up after `MAX_WAIT_DAYS`. |
| ContentPipe returns 409 (identical script still generating) | Waits and retries quietly — not counted as a failure. |
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

## Environment

| Variable | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | No | Human-in-the-loop checkpoints and notifications. Without it `scheduler.py` holds notifications until it's set, and `telegram_poller.py` exits immediately. |
| `TELEGRAM_CHAT_ID` | No | Only this chat is authorized to resume a job. |
| `CHANNEL_BRAND_NAME` | No | The show name ContentPipe writes into scripts when a job doesn't carry its own. Defaults to `Blast Radius`, matching ContentPipe's `DEFAULT_CHANNEL_BRAND`. Never set this to the name of this tool — it ends up spoken on camera. |
| `CONTENTPIPE_BASE_URL` | No | Defaults to `http://localhost:3000`. |
| `CONTENTPIPE_TIMEOUT_SECONDS` | No | Per-request timeout for research/plan, defaults to 180. |
| `CONTENTPIPE_SCRIPT_TIMEOUT_SECONDS` | No | Defaults to 1800 (30 min) — `/api/script` makes many sequential LLM calls internally for a long-form script. |
| `DB_PATH` | No | Defaults to `pipeline.db` in this repo. |
| `POLL_INTERVAL_SECONDS` | No | Scheduler tick interval, defaults to 60. |
| `NEEDS_INPUT_TIMEOUT_HOURS` | No | A job waiting on your tap fails after this long, defaults to 72. |
| `MAX_STAGE_ATTEMPTS` | No | Ordinary failures before `FAILED`, defaults to 5. |
| `MAX_WAIT_DAYS` | No | Give up on a job that has been continuously rate-limited or waiting on a busy ContentPipe, defaults to 7. |
| `RUNNING_LEASE_SECONDS` | No | A `RUNNING` job with no live worker is re-queued; this lease must exceed the longest stage. Defaults to the script timeout + 600. |
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

102 tests covering the job state machine (retry dispatch, crash recovery, regenerate/approve,
timeouts), Telegram delivery and the poller, how ContentPipe's status codes map onto worker
behaviour, and what the request bodies sent to ContentPipe actually contain. See `CLAUDE.md`
"Tier 1 audit fixes" for what each guards against.

---

## Current status

| Stage | Status |
|---|---|
| 1. Research | Built — calls ContentPipe `/api/research`, forwarding the target duration so the dossier is researched to the depth the script needs |
| 2. Plan | Built — calls ContentPipe `/api/plan`, including target video duration |
| 3. Script | Built — calls ContentPipe `/api/script`, mandatory Telegram approve/regenerate checkpoint; the approval message includes ContentPipe's audit findings (runtime shortfall, unsourced figures, mid-roll eligibility) and attaches the full draft |
| Edit / upload a revised script | Not built — approve still commits the LLM draft as-is; the spec's "human rewrite is mandatory" needs a decision on how a revised script comes back |
| 4. Image/video generation | Not started — no TTS/image/video provider keys yet |
| 5. FFmpeg/Remotion assembly | Built in ContentPipe (`server/assemble.ts` + sidecar SRT captions, proven on a stub render), but only as a module and script. CyberPipe does not call it yet, and nothing generates per-scene TTS and images to feed it |
| Telegram `/status /jobs /retry ...` dashboard | Not started — only the approve/regenerate buttons work today |
| Post-publish analytics feedback loop | Not started — needs YouTube Data + Analytics OAuth |

A `COMPLETED` job today means "script approved" — there's no stage after
`script` yet. Full gap analysis against the original spec, including two
operational constraints discovered while testing (a real Gemini schema
limit, and the free tier's 20-requests/day/model cap) live in `CLAUDE.md`.

---

## Deployment

Runs on a Mac via `launchd`, not a VPS — see `CLAUDE.md`'s "Deployment —
Mac via launchd" section for the full setup (`deploy/build-launcher.sh`,
the two LaunchAgent plists) and why that's convenience-grade rather than
true 24/7 durability.

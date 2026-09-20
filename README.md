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

Every ContentPipe call sends `X-ContentPipe-Strict: 1`, so a quota hit or outage
comes back as `429`/`503` with `Retry-After` — which becomes a scheduled retry at
the right time — rather than canned sample content that would look like a real
draft. An interrupted script resumes from ContentPipe's last finished chunk when
the job retries. Details in `CLAUDE.md` ("Fail-closed contract").

`scheduler.py` picks up the job on its next tick and runs it through
research → plan → script. Without `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`
set, it'll stop at the script-approval checkpoint with no way to tap
approve — that's expected for a first run; it still proves the pipeline and
the rate-limit/backoff paths work.

---

## Environment

| Variable | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | No | Human-in-the-loop checkpoints and notifications. Both `scheduler.py` and `telegram_poller.py` run fine without it — they just skip Telegram until it's set. |
| `TELEGRAM_CHAT_ID` | No | Only this chat is authorized to resume a job. |
| `CONTENTPIPE_BASE_URL` | No | Defaults to `http://localhost:3000`. |
| `CONTENTPIPE_SCRIPT_TIMEOUT_SECONDS` | No | Defaults to 1800 (30 min) — `/api/script` makes many sequential Gemini calls internally for a long-form script. |
| `DB_PATH` | No | Defaults to `pipeline.db` in this repo. |
| `POLL_INTERVAL_SECONDS` | No | Scheduler tick interval, defaults to 60. |

`.env` is gitignored and read once at process start — restart
`scheduler.py`/`telegram_poller.py` after editing it.

---

## Current status

| Stage | Status |
|---|---|
| 1. Research | Built — calls ContentPipe `/api/research` |
| 2. Plan | Built — calls ContentPipe `/api/plan`, including target video duration |
| 3. Script | Built — calls ContentPipe `/api/script`, mandatory Telegram approve/regenerate checkpoint; the approval message includes ContentPipe's audit findings (runtime shortfall, unsourced figures, mid-roll eligibility) |
| 4. Image/video generation | Not started — no TTS/image/video provider keys yet |
| 5. FFmpeg/Remotion assembly | Not started |
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

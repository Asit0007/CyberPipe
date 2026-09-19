# CLAUDE.md — CyberPipe

Durable job orchestrator for a cybersecurity-documentary video pipeline.
Sibling repo to `../ContentPipe/` — this project does not reimplement
research/script generation, it calls ContentPipe's existing API for that.
Full product spec lives in `../ContentPipe/cyberpipeline-prompts.md`
(7 prompts + 2 appendices) — read that for tone, format, monetization, and
retention-engineering requirements. This file covers the orchestrator layer
only: what's built, what's stubbed, and what's still a gap.

## Why a separate repo from ContentPipe

ContentPipe's own CLAUDE.md documents "no video rendering — a deliberate
decision" and a single-process Node/Express/Vite architecture with no
database. This project needs Python, SQLite WAL, and long-running polling
services — a different runtime paradigm. Keeping them separate avoids
breaking ContentPipe's documented boundary and a Node/Python stack clash in
one repo.

## Architecture

```
submit_job.py ──> jobs table (PENDING) ──┐
                                          │
                    ┌─────────────────────▼──────────────────────┐
                    │  scheduler.py  (60s poll loop)               │
                    │  due_jobs(): PENDING, or SCHEDULED past       │
                    │  next_retry_at, not paused                    │
                    └─────────────────┬─────────────────────────────┘
                                      │ worker.run_job(id)
                                      ▼
                    ┌──────────────────────────────────────────────┐
                    │  worker.py                                     │
                    │  runs pipeline.STAGE_FUNCTIONS[current_stage]  │
                    │  against ContentPipe's HTTP API                │
                    └───┬─────────────┬─────────────┬───────────────┘
                        │ success     │ RateLimitError │ HumanInputRequired
                        ▼             ▼             ▼
                 advance stage   SCHEDULED      NEEDS_INPUT
                 or COMPLETED    + next_retry_at + pending_payload
                        │             │             │
                        ▼             ▼             ▼
                 notifier.py ──> Telegram (✅ / ⏳ / 🟡 / ❌)
                                                       │
                                          tap approve/regenerate
                                                       ▼
                                          telegram_poller.py
                                          ──> worker.resume_from_input()
```

`ContentPipe` must be running separately (`npm run dev` in that repo) for
stages 1-3 to have something to call.

## Job state machine

`PENDING → RUNNING → {SCHEDULED | NEEDS_INPUT | COMPLETED | FAILED}`

- **SCHEDULED**: either rate-limited (retry per `rate_limiter.py`: `Retry-After`
  header → provider daily reset → 24h default) or a generic error under
  backoff (`config.BACKOFF_SCHEDULE_SECONDS`: 5m/15m/45m/2h/6h,
  `config.MAX_STAGE_ATTEMPTS` before FAILED).
- **NEEDS_INPUT**: only the `script` stage raises this today
  (`pipeline.stage_script`), mirroring the spec's mandatory human checkpoint.
  `job.pending_payload` holds the already-generated draft so approving
  doesn't recompute it; regenerating clears it and re-runs the stage.
  `scheduler.py` fails any NEEDS_INPUT job older than
  `config.NEEDS_INPUT_TIMEOUT_HOURS` (default 72h).
- Notifications are idempotent via `jobs.notified` (a JSON list of event
  keys) — see `notifier._notify_once`.

## What's stubbed vs. built

| Stage | Status |
|---|---|
| 1. Research | Built — calls ContentPipe `/api/research` |
| 2. Plan | Built — calls ContentPipe `/api/plan` |
| 3. Script | Built — calls ContentPipe `/api/script`, raises the mandatory human checkpoint |
| 4. Image/video generation | **Not started.** No TTS/image/video provider keys exist yet (ElevenLabs/Resemble, Replicate/fal.ai, Seedance/Kling/LTX/Runway/Pika) — deferred on purpose until the durability/HITL core above is proven, per the 2026-09-19 scoping decision. |
| 5. FFmpeg/Remotion assembly | **Not started.** |
| Telegram `/status /jobs /retry ...` dashboard (Prompt 5) | **Not started.** `telegram_poller.py` only handles the `job:<id>:<answer>` approve/regenerate buttons. |
| Analytics feedback loop (Prompt 7) | **Not started.** Needs YouTube Data + Analytics OAuth. |

A COMPLETED job today just means "script approved" — there is no stage after
`script` yet. That's intentional: `pipeline.PIPELINE_STAGES` is the seam
where stages 4/5 get appended once their providers are chosen and keyed.

## Known integration gaps vs. the original prompt spec

Verified against ContentPipe's actual `src/types.ts` on 2026-09-19:

- The spec's Stage 1 output assumes `entities`, technical artifacts
  (CVE ids/hashes/code snippets), and `visual cues`. ContentPipe's
  `ResearchData` has none of these — it has `hnCommunitySentiment` and
  `infotainmentAngles` instead, left over from ContentPipe's original
  tech/HN framing. These don't fit "authoritative, no fearmongering"
  cybersecurity tone and are currently passed through unfiltered into the
  script prompt.
- The spec's Stage 2 output assumes top-level `retention_beats[]` and
  `midroll_markers[]`. `VideoScript` has neither — only an optional
  per-scene `retentionNote` string. **The ~2:30/~6:00 mid-roll timestamps
  are not computed anywhere yet** — they need to be derived from cumulative
  `scene.durationEst` once stage 4/5 exist, not fetched from the API.
- `characterBible`/`styleGuide` (which hero-image consistency in stage 4
  will depend on) are optional fields that ContentPipe's own CLAUDE.md
  documents as sometimes silently dropped by the model on large schemas.
  Stage 4, whenever it's built, needs a fallback for their absence, not an
  assumption they're always present.
- No `VideoPlan.tone` value matches "authoritative, investigative, slightly
  urgent, no fearmongering" — `stage_plan` currently defaults to
  `"Deep Dive Documentary"` as the closest fit.

## Deployment target — open decision

The original spec assumes a Linux VPS (systemd units, always-on). Nothing's
provisioned yet. If this instead runs on the same Mac as JobPipe/quant_bot,
note that `scheduler.py`'s 60s poll needs to be *continuously* alive, unlike
JobPipe's once-daily cron — JobPipe's own CLAUDE.md (§7.56, §7.58) documents
that launchd only replays a `StartCalendarInterval` missed while the Mac was
*asleep*, never while it was *powered off*, which is a much bigger problem
for something that's supposed to be polling every minute. Resolve this
before writing systemd units or a launchd plist for these two processes.

## Running it locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN/CHAT_ID once you have a bot

python3 submit_job.py --text "..." --url "https://..."
python3 scheduler.py          # separate terminal — polls every 60s
python3 telegram_poller.py    # separate terminal — only needed once Telegram is configured
```

Without Telegram configured, the job will still run through research → plan
→ script and then sit in NEEDS_INPUT forever (no way to tap approve) — that's
expected; it proves the pipeline and the rate-limit/backoff paths work, but
manual DB surgery (`update_job(id, status="PENDING", ...)`) is the only way
to unblock it until a bot token exists.

## Pitfalls

| Pitfall | Detail |
|---|---|
| `.env` read once | Same gotcha as ContentPipe — restart `scheduler.py`/`telegram_poller.py` after editing `.env`. |
| Stale button taps | `worker.resume_from_input` checks `job.status == "NEEDS_INPUT"` before acting, so a tap on an old message for an already-resolved job is a silent no-op, not a crash. |
| ContentPipe must be running | Stages 1-3 are plain HTTP calls to `CONTENTPIPE_BASE_URL`. If ContentPipe isn't up, every job fails with a connection error and goes through the generic backoff path, not the rate-limit path. |
| WAL mode + concurrent processes | `scheduler.py` and `telegram_poller.py` both open their own connections via `db.get_connection()`; SQLite WAL handles this fine at this scale. Move to Postgres only if running >5 workers (per the original spec's own threshold — not needed now). |

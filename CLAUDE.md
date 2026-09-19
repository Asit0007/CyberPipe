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

## Deployment — Mac via launchd (decided 2026-09-19)

The original spec assumed a Linux VPS with systemd units. Decision: run on
the same Mac as JobPipe/quant_bot instead, via launchd — no VPS provisioned
or planned right now.

**Same TCC problem as JobPipe, same fix.** CyberPipe lives under
`~/Documents` too, so a bare LaunchAgent calling `scheduler.py` or
`bash run-service.sh` directly gets the identical exit-126 exec denial
JobPipe measured 2026-09-10 — launchd holds no Documents-folder grant, only
Terminal/VS Code/Claude Code do. The fix is the same trick: a signed,
ad-hoc-codesigned Mach-O in `~/Applications/CyberPipe Services.app` that TCC
can be handed a Full Disk Access grant for, which `run-service.sh` and
everything under it (the venv python, the repo) inherits by
responsible-process attribution. See `deploy/cyberpipe-launcher.c` for the
full writeup — it's deliberately the same shape as
`../JobPipe/deploy/jobpipe-launcher.c`.

**Different launchd shape from JobPipe, because the workload is different.**
JobPipe is a once-a-day batch job, so it uses `StartCalendarInterval` and
relies on launchd replaying a firing missed while the Mac was asleep.
CyberPipe's `scheduler.py` and `telegram_poller.py` are meant to run
*continuously*, so both LaunchAgents use `RunAtLoad + KeepAlive` instead —
start when the Agent loads (login), restart on any exit. This is actually a
better fit for sleep than JobPipe's problem: macOS suspends a running
process across sleep rather than killing it, so `scheduler.py`'s
`time.sleep(60)` loop just runs a little long across a nap instead of
missing a narrow firing window entirely.

**One launcher bundle, two LaunchAgents.** Building two separate signed
bundles would mean clicking through the Full Disk Access grant twice.
Instead `deploy/run-service.sh` takes `scheduler` or `poller` as an argument
and dispatches to the right python module; `deploy/com.asitminz.cyberpipe.
scheduler.plist.example` and `.poller.plist.example` both point at the same
bundle with different arguments.

**What's still true regardless of launchd:** a Mac that's fully powered off
runs nothing — no rate-limit retries fire, no Telegram taps get processed,
jobs just sit until it boots and the Agents reload. `RunAtLoad`/`KeepAlive`
only closes the "asleep" gap, not the "off" one. Per the outer workspace
CLAUDE.md's own framing: treat this LaunchAgent setup as a convenience for a
laptop that's usually on, not as production-grade 24/7 durability — that
would still mean a VPS if it ever matters (e.g. once Telegram approvals need
to be timely even with the laptop closed for a day).

**Setup**, once `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are in `.env` (the
poller runs fine before that too — see `deploy/*.plist.example` headers):

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./deploy/build-launcher.sh
# GUI step, cannot be scripted:
#   System Settings > Privacy & Security > Full Disk Access > +
#   > ~/Applications/CyberPipe Services.app > turn it on

cp deploy/com.asitminz.cyberpipe.scheduler.plist.example \
   ~/Library/LaunchAgents/com.asitminz.cyberpipe.scheduler.plist
cp deploy/com.asitminz.cyberpipe.poller.plist.example \
   ~/Library/LaunchAgents/com.asitminz.cyberpipe.poller.plist
# then replace __REPO_ROOT__ and __HOME__ in both copies with real paths

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.asitminz.cyberpipe.scheduler.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.asitminz.cyberpipe.poller.plist
launchctl print gui/$(id -u)/com.asitminz.cyberpipe.scheduler   # verify running
launchctl print gui/$(id -u)/com.asitminz.cyberpipe.poller
```

Logs land in `data/logs/scheduler.log` and `data/logs/poller.log` (the
services' own stdout, appended across restarts) plus
`data/logs/launchd-{scheduler,poller}.{out,err}.log` (anything that fails
before the script itself can log). None of `data/`, the venv, or the built
`.app` bundle are committed.

Compiled and smoke-tested 2026-09-19: `build-launcher.sh` produces a
correctly signed bundle, and invoking it directly with `scheduler` / `poller`
confirmed the full dispatch chain (bundle → bash → venv python → the
service) works. Not yet bootstrapped into launchd — that needs the manual
Full Disk Access grant first, which is GUI-only.

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

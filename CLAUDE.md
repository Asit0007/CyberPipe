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

## Integration gaps vs. the original prompt spec

Verified against ContentPipe's actual `src/types.ts` and `server/schemas.ts`.
The first four were fixed 2026-09-19, entirely inside `pipeline.py` — no
ContentPipe changes, keeping the "calls the API, doesn't rewrite it"
boundary from the scoping decision. The fifth was discovered the same day
while verifying the fix and is **not fixable from CyberPipe's side** — see
below.

- ~~Stage 1 output assumes `entities`/CVE ids/`visual cues`; ContentPipe's
  `ResearchData` has none, and its `hnCommunitySentiment`/
  `infotainmentAngles` leak HN/infotainment framing into the script
  prompt.~~ **Fixed:** `_extract_cve_ids` does a narrow regex pass for CVE
  ids (general entity extraction would need its own model call to do
  honestly, so it isn't attempted); `_reframe_research_for_forwarding` drops
  `hnCommunitySentiment`/`infotainmentAngles` from what's sent to `/api/plan`
  and `/api/script` (neither endpoint validates its input shape — both just
  `JSON.stringify` it into the prompt). `stage_outputs['research']` still
  keeps ContentPipe's original, unmodified response.
- ~~No top-level `retention_beats[]`/`midroll_markers[]`.~~ **Fixed:**
  `_compute_midroll_markers` derives the ~2:30/~6:00 placement locally from
  cumulative `scene.durationEst`, snapped to the nearest scene boundary,
  stashed as `cyberpipe_midroll_markers` on the script draft.
- ~~`characterBible`/`styleGuide` are optional and sometimes silently
  dropped.~~ **Partially fixed:** `_script_coverage_warnings` checks for
  their absence (and per-scene `visual`/`motion` coverage) and appends
  warnings to the Telegram approval question, so a human sees "⚠️ no
  styleGuide" before approving rather than Stage 4 discovering it silently
  later. Doesn't recover the missing data, just surfaces it — there's
  nothing to recover it *with* until Stage 4 exists.
- ~~No `VideoPlan.tone` value matches "authoritative, no fearmongering".~~
  **Fixed to the extent possible:** `planSchema.tone` (`server/schemas.ts`)
  is a hard Gemini-enforced enum of 4 literals — no request parameter can
  change the `tone` field's actual value, only the surrounding prose. Kept
  `"Deep Dive Documentary"` as the closest match and added
  `DEFAULT_CYBER_TONE`, a fuller descriptive string sent as `targetTone` to
  steer `hookStrategy`/`pacingStyle`/`narrativeBeats`. **Verified this has
  limited effect**: a live test run still came back with `pacingStyle:
  "Fast-cut with terminal memes, code alerts, and dramatic pauses"` — the
  literal example value from `/api/plan`'s own prompt template in
  `server.ts`. This is the same "inline example wins over instructions"
  failure mode ContentPipe's own CLAUDE.md documents elsewhere
  (`src/types.ts`/prompt-example drift causing `visual`/`motion` to go
  missing) — here it means `targetTone` competing against a hardcoded
  example rather than a schema gap. Fixing it for real means editing
  ContentPipe's prompt template, which is out of scope for CyberPipe.

### New, more serious gap found while verifying the above (2026-09-19)

`server/schemas.ts` `scriptSchema.scenes` has **`minItems: 5, maxItems: 6`**,
and the prompt instructs 8-15s of narration per scene — a hard ceiling
around **90 seconds of total runtime**. A live test job came back with 5
scenes and `estimatedTotalDuration: 58`. CyberPipe's spec target is
**8-10 minutes (480-600s)** — roughly 6-7x more scenes than the schema will
ever allow `/api/script` to return, at any pacing.

This is not a missing-field problem like the other four — it's a hard
capacity ceiling on the endpoint CyberPipe's Stage 2/3 were built to reuse.
`/api/plan` has the same shape: `targetDurationSec` isn't even a request
parameter (only `researchData`/`targetFormat`/`targetTone` are read from the
body) — it's a literal `60` in the prompt's inline JSON example, ignored
regardless of what's sent. ContentPipe's script generation is built for
Shorts/Reels/TikTok pacing (5-6 scenes, 8-15s each), not an 8-10 minute
YouTube documentary — because that's what ContentPipe was actually built
for; long-form cybersecurity documentaries were never its use case.

**Not yet resolved. Real options, not yet decided:**
1. Patch ContentPipe's `/api/plan`/`/api/script` to accept a real
   `targetDurationSec` and raise the `scenes` schema ceiling — a generically
   useful capability fix (any long-form use case hits this, not just
   CyberPipe), backward-compatible (existing callers omitting the param get
   identical default behavior). Crosses the "don't rewrite ContentPipe"
   boundary from the original scoping decision, even if narrowly scoped.
2. Have CyberPipe call `/api/script` multiple times (once per narrative act
   from the plan) and stitch the results into one long-form script locally,
   leaving ContentPipe's schema untouched.
3. Accept ContentPipe's ~90s output as one *segment* and have CyberPipe's own
   (unbuilt) Stage 2.5 expand/pad it into a full 8-10 minute structure before
   Stage 3 image/video generation.

Whichever gets picked has to happen before the mid-roll markers this session
just added mean anything — right now they degenerate to "after scene 5" for
both the ~2:30 and ~6:00 targets, because the whole video is shorter than
either target.

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

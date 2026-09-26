# CLAUDE.md — CyberPipe

Durable job orchestrator for a cybersecurity-documentary video pipeline.
Sibling repo to `../ContentPipe/` — this project does not reimplement
research/script generation, it calls ContentPipe's existing API for that.
Full product spec lives in `../ContentPipe/cyberpipeline-prompts.md`
(7 prompts + 2 appendices) — read that for tone, format, monetization, and
retention-engineering requirements. (That file is **gitignored** in ContentPipe,
so it exists only on this machine — it will not be in a fresh clone.) This file covers the orchestrator layer
only: what's built, what's stubbed, and what's still a gap.

## Why a separate repo from ContentPipe

ContentPipe is a single-process Node/Express/Vite server with no database and
no notion of a job. This project needs Python, SQLite WAL, and long-running
polling services under launchd — a different runtime paradigm. Keeping them
separate avoids a Node/Python stack clash in one repo and keeps the contract
between them an HTTP one (strict mode), which is what lets either side be tested
against a stub of the other. (The original rationale also cited ContentPipe's
"no video rendering" rule; that changed on 2026-09-21 when `server/assemble.ts`
was built in ContentPipe — rendering belongs there, orchestration here.)

**Where they overlap (deliberately small):** both default the show name to
"Blast Radius" and the target to 585 s (CyberPipe always sends both, so
ContentPipe's copies only matter for the UI); both persist progress
(ContentPipe per chunk in `.runs/`, CyberPipe per stage in SQLite); and
`pipeline._compute_midroll_markers` is a local fallback for mid-rolls that
ContentPipe now computes itself. That overlap is resolved in ContentPipe's favour
(`pipeline._midroll_markers_for`, 2026-09-21): if its response carries `midrollMarkers`
at all — even an empty list, which is what it returns under 8:00 alongside a warning —
that is the answer, and the local guess is used only for a ContentPipe too old to send
the field. It used to fall back on an empty list, which invented ~2:30 / ~6:00 markers
(one after the last scene of a 5-minute script) next to ContentPipe's "not eligible" warning.

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
stages 1-3 — and, for images, clips and the analyst voice, stages 4-5 — to have something to call.

## Job state machine

`PENDING → RUNNING → {SCHEDULED | NEEDS_INPUT | COMPLETED | FAILED}`

Every status change is a conditional write — `db.claim_job` (PENDING or due
SCHEDULED → RUNNING) and `db.transition(id, from_status, ...)` ("only if the row is
still in the status I read"). That is what lets the scheduler, the Telegram poller
and the timeout sweep race safely: a stale read can never overwrite newer state.

- **RUNNING** carries a lock (`locked_by` = `host:pid`, `locked_at`). A RUNNING job
  is orphaned — and `worker.reclaim_orphaned_jobs()` (every tick) re-queues it — if
  its pid is gone on this host, or its lease (`config.RUNNING_LEASE_SECONDS`, default
  script timeout + 10 min) expired. The crash counts as an attempt, so a job that
  keeps killing its worker ends FAILED. Re-running is safe: ContentPipe resumes an
  interrupted script from its journal, and 409s if the first run is still going.
- **SCHEDULED**: rate-limited (retry per `rate_limiter.py`: `Retry-After` header →
  provider daily reset → 24h default), waiting on a busy ContentPipe (409
  `in_progress`; `StageBusy`), or a generic error under backoff
  (`config.BACKOFF_SCHEDULE_SECONDS`: 5m/15m/45m/2h/6h, `config.MAX_STAGE_ATTEMPTS`
  before FAILED). Rate limits, busy-waits and provider outages consume **no attempt**, but
  `wait_since` caps an unbroken wait at `config.MAX_WAIT_DAYS` (7) so a stuck job fails
  instead of retrying forever. `PermanentStageError` (ContentPipe `zero_quota` — needs
  billing) fails immediately.
  **A 503 from ContentPipe** (every model provider behind it overloaded, after its own
  bounded wait) raises `UpstreamUnavailable` and is a wait too (fixed 2026-09-21; it used to
  be an ordinary error that spent a backoff attempt and ignored its 30 s `Retry-After`, so a
  multi-hour provider outage would have FAILED a job in ~9 h). The first retry follows the
  hint; the wait then stretches to half the outage's age, capped at
  `config.OVERLOAD_MAX_WAIT_SECONDS` (15 min) — quick to notice recovery, quiet through a
  long outage. Runs are logged `unavailable`. The human is told once, after the outage has
  lasted 15 min (`notify_upstream_unavailable`, keyed on `wait_since`); a blip that clears
  in a minute pages nobody. A 502 (bad key, rejected request) stays an ordinary error.
- **NEEDS_INPUT**: raised by `script` (`pipeline.stage_script`, the spec's mandatory human checkpoint) and, since
  2026-09-26, by the three ContentRender stages (`images`, `narration`, `bundle`) — see "ContentRender stages" below.
  `job.pending_payload` holds the already-generated draft so approving
  doesn't recompute it; regenerating clears it and re-runs the stage. Approve
  commits the draft and advances in **one** write. The approval message carries the
  whole draft as `job-<id>-script-draft.md` (`review.py`). `scheduler.py` fails any
  NEEDS_INPUT job older than `config.NEEDS_INPUT_TIMEOUT_HOURS` (default 72h,
  measured from the job's last update — a delayed notification restarts the clock).
- **Notifications** are idempotent via `jobs.notified` (a JSON list of event keys),
  and an event is recorded **only if Telegram accepted it**. Failed or held
  (bot not configured yet) events are re-sent by
  `notifier.resend_missed_notifications()` each tick, throttled per event by
  `config.TELEGRAM_RESEND_COOLDOWN_SECONDS`. Messages are plain text (no
  `parse_mode`): titles like "AT&T" or `<script>` broke Telegram's HTML mode.
  Regenerate clears the `needs_input:` keys so the next draft is announced again.
- **Rate-limit messages are one per unbroken wait**, keyed
  `rate_limited:<stage>:<wait_since>:<short|long>` (long = retry ≥ 15 min away). They used
  to be keyed on the retry time, which changes on every re-poll, so a per-minute limit (or a
  daily cap ContentPipe misreported as 60 s — fixed there too, 2026-09-21) paged Telegram
  every minute for up to `MAX_WAIT_DAYS`. A short wait that turns long still pages once more.

### Channel brand

The show name ContentPipe writes into scripts is `config.CHANNEL_BRAND_NAME` (env `CHANNEL_BRAND_NAME`, default "Blast Radius") unless a job's `channelBrandName` overrides it — it must never be the tool's name ("CyberPipe"). `channelName` is a different thing: the story's *origin* (a Telegram feed, a wire), which ContentPipe puts in the research prompt. It is only forwarded when a job names a real one (`submit_job.py --source-name`); our own channel is not an origin. `--channel-name` is kept as an alias of `--brand`.

### Tier 1 audit fixes (2026-09-20)

Found by auditing the (never-yet-run) orchestrator; each has regression tests in
`tests/`, and each test was checked by re-introducing its bug (mutation check):

| Bug | Was | Guard |
|---|---|---|
| SCHEDULED jobs never re-ran | `run_job` accepted only PENDING, so every rate-limited/backed-off job stayed SCHEDULED forever — the core retry promise did not work through the scheduler | `test_worker.SchedulerDispatchTests` |
| Regenerate went silent | dedupe key `needs_input:script:0` reused for the next draft | `RegenerateTests` |
| Crash orphaned jobs | killed worker left RUNNING forever; `due_jobs` never returns it | `OrphanReclaimTests` (+ a real `kill -9` run) |
| Poller poison pill | a 400 from `answerCallbackQuery` (old tap) raised before the offset was saved → same update replayed forever | `test_poller` |
| Bot token in logs | `requests` puts `/bot<TOKEN>/…` in exception text | `test_poller`, `test_notifier` |
| Lost notifications | event marked sent when Telegram was unconfigured/down/400 | `test_notifier` |
| Timeout up to 24h late | ISO `T` vs SQLite `datetime()` space compared as strings | `test_db.StaleNeedsInputTests` |
| Non-atomic approve, blind approval | draft cleared and stored in two writes; approver saw a summary | `ApprovalTests`, `DraftAttachmentTests` |
| 409 / zero_quota retried as failures | burned backoff attempts | `test_pipeline_http`, `WaitingStateTests` |

**Still open from that audit:** an *edit/upload revised script* path. The spec says
"human rewrite is mandatory", but approve still commits the LLM draft as-is (the
human now at least reads it). Needs a design decision on how a revised script comes
back (Telegram document reply vs. re-ingest through ContentPipe).

## What's stubbed vs. built

| Stage | Status |
|---|---|
| 1. Research | Built — calls ContentPipe `/api/research` |
| 2. Plan | Built — calls ContentPipe `/api/plan` |
| 3. Script | Built — calls ContentPipe `/api/script`, raises the mandatory human checkpoint |
| 4. `images` — stills | **Built (2026-09-26)** — runs ContentRender's CLI; gate: the stills as Telegram albums. |
| 5. `narration` — AI clips + two-voice narration | **Built** — same CLI; gate: one MP3 of the whole narration. Kokoro is local, Charon goes through ContentPipe. |
| 6. `bundle` — the DaVinci Resolve bundle | **Built** — FCPXML timeline, captions, rough-cut MP4 under `ContentRender/.render/runs/job-<id>/resolve/`; gate: the rough cut. Approve → COMPLETED. Verified end to end on **stub media** (tests/test_e2e_contentrender.py); not yet on a real story, and the timeline is not yet proven to import cleanly into Resolve. |
| Telegram `/status /jobs /retry ...` dashboard (Prompt 5) | **Not started.** `telegram_poller.py` only handles the `job:<id>:<answer>` approve/regenerate buttons. |
| Analytics feedback loop (Prompt 7) | **Not started.** Needs YouTube Data + Analytics OAuth. |

A COMPLETED job now means the Resolve bundle was approved. `pipeline.PIPELINE_STAGES` is
`research → plan → script → images → narration → bundle`.

### ContentRender stages (2026-09-26)

`../ContentRender` is a **command line, not a server**. Each of the three stages calls
`node node_modules/tsx/dist/cli.mjs scripts/cli.ts step --brief data/briefs/job-<id>.json --video-id job-<id>`
(`pipeline._run_render`) and reads **one JSON outcome from the last stdout line**; the exit code only says the
process crashed (→ ordinary backoff). ContentRender keeps its own per-asset manifest, so a crash or a quota wall resumes where it stopped.

| Outcome | Becomes |
|---|---|
| `progress` (time budget used, or a retryable failure) | `StageInProgress` — re-queued at `retryInSec`, **no attempt, no page, `wait_since` cleared** (each call moved the run forward) |
| `gate` (the one this stage expects) | `HumanInputRequired`, payload `{gate, review}`; the notifier sends the review files |
| `rate_limited` | `RateLimitError` (quota) / `UpstreamUnavailable` (overloaded), with ContentRender's own retry time |
| `error` | `PermanentStageError` — a human has to fix something (e.g. a scene gave up after 3 attempts) |
| a gate the stage did not expect | `PermanentStageError`, never a silent approval |

Human decisions reach ContentRender's manifest: each stage first runs `approve --gate <previous>` (idempotent, so a crash between the
Telegram tap and the manifest cannot desync them); the final approval and every regenerate go through
`pipeline.ON_APPROVE` / `ON_REGENERATE`, called by `worker.resume_from_input` **before** it moves the job — a hook that fails leaves
the job waiting, so repeating the tap retries. `/regen <job> <scenes>` (telegram_poller) redoes just those scenes at the images or
narration gate (`worker.regenerate_scenes`). The notifier sends stills as `sendMediaGroup` albums of ≤10 (a lone one as `sendPhoto`),
the narration as `sendAudio`, the rough cut as `sendVideo` (only if ≤ 48 MB); anything it could not attach is named in the approval
message so nobody approves blind.

Config (`.env.example`): `CONTENTRENDER_DIR`, `CONTENTRENDER_NODE` (**absolute path** — launchd has no PATH),
`CONTENTRENDER_STEP_BUDGET_SECONDS` (1200), `CONTENTRENDER_TIMEOUT_SECONDS` (budget + 900), `BRIEFS_DIR`. `RUNNING_LEASE_SECONDS` now defaults to the
longer of the script and ContentRender timeouts, plus 600. **ContentPipe must be running** for images, clips and Charon; Kokoro needs
`npm run kokoro:setup` in ContentRender. Tests: `tests/test_render_stages.py` (fakes `_run_render`), and the opt-in
`CONTENTRENDER_E2E=1 ./venv/bin/python -m unittest tests.test_e2e_contentrender` drives the **real** CLI on stub media through all three gates.
The older state-machine tests are pinned to the original three stages via `DbTestCase.stages`.

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
  stashed as `cyberpipe_midroll_markers` on the script draft. ContentPipe now
  computes this itself (`midrollMarkers`, with the >= 8:00 eligibility rule,
  plus chapters and a `qualityChecks` audit) and `_server_midroll_markers`
  prefers it; the local function is the fallback for an older ContentPipe.
- ~~`characterBible`/`styleGuide` are optional and sometimes silently
  dropped.~~ **Partially fixed:** `_script_coverage_warnings` checks for
  their absence (and per-scene `visual`/`motion` coverage) and appends
  warnings to the Telegram approval question, so a human sees "⚠️ no
  styleGuide" before approving rather than Stage 4 discovering it silently
  later. Doesn't recover the missing data, just surfaces it — there's
  nothing to recover it *with* until Stage 4 exists.
- ~~No `VideoPlan.tone` value matches "authoritative, no fearmongering".~~
  **Fixed.** `planSchema.tone` is still a hard Gemini-enforced enum of 4
  literals — `"Deep Dive Documentary"` remains the closest match and
  `DEFAULT_CYBER_TONE` (a fuller descriptive string sent as `targetTone`)
  still only steers `hookStrategy`/`pacingStyle`/`narrativeBeats` prose, not
  the `tone` field's value itself. What changed: a live test first showed
  this had *no* real effect — `pacingStyle` came back as the literal example
  string from `/api/plan`'s prompt template, the same "inline example wins
  over instructions" failure ContentPipe's own CLAUDE.md documents elsewhere.
  That genuinely needed a ContentPipe-side fix, which happened as part of
  the duration-ceiling patch below: `generateSceneChunk` in `server.ts` now
  branches its writer persona and narration-style instructions on
  `videoPlan.tone === 'Deep Dive Documentary'` — an actual investigative-
  documentary voice instead of the hardcoded infotainment one, for that tone
  value only. Every other tone value keeps ContentPipe's original behavior.

### Duration ceiling — found 2026-09-19, fixed the same day in ContentPipe

`scriptSchema.scenes` had `minItems: 5, maxItems: 6` and 8-15s of narration
per scene — a hard ceiling around 90 seconds, vs. CyberPipe's 8-10 minute
target. `/api/plan` didn't even accept a duration parameter. **Fixed
directly in ContentPipe** (option 1 from the original three — see that
repo's `CLAUDE.md` "chunked script generation" section for the full
writeup): `/api/plan` now honors a real `targetDurationSec`, and
`/api/script` generates scenes in chunks sized to reach it, carrying prior
scenes forward as context for continuity. `pipeline.py`'s `stage_plan` now
sends `targetDurationSec` (default `DEFAULT_TARGET_DURATION_SEC = 585`, inside the
spec's 8-10 minutes; 540 left too little room over the 480 s mid-roll floor once
ContentPipe's shortfall tolerance became 0.92) — before this fix it was never sent at all, so
every script silently inherited ContentPipe's ~60s Shorts-style default.

**Two operational constraints surfaced while verifying this, worth knowing
before assuming a script will always come back at full length:**

1. **A real schema limit, not a bug.** ContentPipe's per-scene `infographic`
   field's nesting makes Gemini hard-reject the request once a chunk's
   `maxItems` reaches 4 — reproducible across every model, confirmed against
   the *original, unmodified* schema too (it was always there, just never
   exercised past 6 items before). This is why chunk size is 3 for the
   narrative pass, not something larger — don't "optimize" it back up without
   re-testing live first.
2. **Gemini's free tier caps at 20 requests/day per model.** A 9-minute
   script now needs ~25 internal Gemini calls (vs. 2-3 before), so a single
   long-form script generation can burn through a meaningful chunk of a
   day's quota by itself. Watch this against the bi-weekly cadence once
   real production runs start — it's a capacity planning question, not
   just a testing artifact. *(2026-09-21: ContentPipe's provider chain —
   on its `main` since 2026-09-21 — spreads text calls across
   DeepSeek / Grok / free tiers with Gemini last, and a strict-mode 429 then
   reports the earliest retry across ALL providers. Until provider keys are
   added to ContentPipe's `.env` this is still the whole budget.)*

`pipeline._compute_midroll_markers` now has a script long enough for the
~2:30/~6:00 placement to mean something, instead of degenerating to "after
the last scene" on a 58s draft.

### Fail-closed contract — found and fixed 2026-09-19 (both repos)

ContentPipe's default contract is "always return something": on quota
exhaustion or an outage every endpoint answered **HTTP 200 with canned
XZ-backdoor content** flagged `isQuotaFallback`. `_post` only reacted to HTTP
429, which ContentPipe never sent, so the rate-limit path in `worker.py` was
unreachable for stages 1-3, and a rate-limited run put fake research → plan →
script in front of the Telegram approver as a real draft. (Reproduced live: a
brief `503 high demand` from all three models produced exactly that.)

`_post` now sends `X-ContentPipe-Strict: 1`. ContentPipe then answers
`429` + `Retry-After` for quota (a daily limit → seconds until midnight
Pacific), `503` + `Retry-After` for overload, `502` for non-retryable
failures — and never fallback content. A 429 becomes
`RateLimitError(retry_at=<Retry-After>)`, which `worker.py` already honours as
`explicit_retry_at`. An `isQuotaFallback` body is still rejected defensively
(older ContentPipe). 503/502 take the generic backoff path.

**Resume needs nothing from CyberPipe.** ContentPipe checkpoints every finished
chunk of a script to `.runs/` and resumes an interrupted run when the *same*
request arrives again (keyed by a hash of plan + research + brand); a
delivered run is never replayed, so "regenerate" still starts fresh. That
matters: a 9-minute script is ~25 Gemini calls against ~20/day per model, so
restarting from zero after a quota hit would never converge.

A script still comes back with `generation` (`complete`, requested vs produced
scenes, `degraded[]`) and `qualityChecks`. The approval question appends the
worst few audit lines (errors first) as `🔎` rows, so the human sees "runtime
83% of target" or "narration states a CVE not in the sources" before tapping
approve. `_script_coverage_warnings` is now largely redundant with
`generation` but harmless.

Verified end to end 2026-09-19 with CyberPipe's real `stage_script` against the
real ContentPipe (stub upstream): daily-quota fault → `RateLimitError` whose
`retry_at` matched midnight Pacific; after a simulated reset → the approval
checkpoint, having re-spent only the unfinished calls.

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

See `README.md` for the quick-start commands and environment variables.
One gotcha not obvious from there: without Telegram configured, a job sits
in `NEEDS_INPUT` forever with no way to tap approve — manual DB surgery
(`db.update_job(id, status="PENDING", pending_question=None,
pending_payload=None)`) is the only way to unblock it until a bot token
exists.

## Pitfalls

| Pitfall | Detail |
|---|---|
| `.env` read once | Same gotcha as ContentPipe — restart `scheduler.py`/`telegram_poller.py` after editing `.env`. |
| Stale button taps | `worker.resume_from_input` only acts on a job still in NEEDS_INPUT (and the write is conditional on it), so a tap on an old message is a no-op; the poller answers "Already handled". A tap can never double-apply. |
| Logging Telegram errors | The bot token is in every request URL and `requests` prints the URL in its exceptions. Anything logged from a Telegram call must go through `notifier.redact_secrets`; `telegram_poller._api` raises `TelegramAPIError` `from None` for the same reason. |
| Writing job status | Use `db.transition(id, from_status, ...)` / `db.claim_job`, not `db.update_job`, for any status change — `update_job` is unconditional and can clobber a newer state. |
| Timestamps | Store only `db.to_iso(...)` (fixed-width UTC). SQL compares them as strings; `datetime.isoformat()` drops the fraction at `.000000` and SQLite's `datetime()` uses a space, both of which break that. |
| Strict header | Stages 1-3 rely on `X-ContentPipe-Strict: 1` (in `pipeline.STRICT_HEADERS`). Without it ContentPipe returns canned content with HTTP 200 on quota exhaustion. Don't drop it when refactoring `_post`. |
| ContentPipe must be running | Stages 1-3 are plain HTTP calls to `CONTENTPIPE_BASE_URL`. If ContentPipe isn't up, every job fails with a connection error and goes through the generic backoff path, not the rate-limit path. |
| WAL mode + concurrent processes | `scheduler.py` and `telegram_poller.py` both open their own connections via `db.get_connection()`; SQLite WAL handles this fine at this scale. Move to Postgres only if running >5 workers (per the original spec's own threshold — not needed now). |

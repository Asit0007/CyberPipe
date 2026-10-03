#!/bin/bash
# Install CyberPipe's two services (scheduler + Telegram poller) as LaunchAgents on this Mac.
#
#   ./deploy/install.sh --dry-run    # check everything and render the plists to a temp folder; change nothing
#   ./deploy/install.sh              # the same checks, then rebuild the launcher app and load both agents
#
# Safe to re-run: it unloads an agent that is already loaded before loading the new copy.
# Removing them again: ./deploy/uninstall.sh
#
# What it does NOT do: grant Full Disk Access. That is a GUI-only step and is probably not needed any more, because
# the repo now lives under ~/Developer, which macOS does not guard (it was needed under ~/Documents). If the launchd
# logs show "Operation not permitted" or exit 126, grant it to ~/Applications/CyberPipe Services.app and re-run this.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

PY="$ROOT/venv/bin/python3"
AGENTS="$HOME/Library/LaunchAgents"
APP_BIN="$HOME/Applications/CyberPipe Services.app/Contents/MacOS/CyberPipeServices"
LABELS=(com.asitminz.cyberpipe.scheduler com.asitminz.cyberpipe.poller)
problems=0
ok()   { echo "  ok    $*"; }
warn() { echo "  warn  $*"; }
bad()  { echo "  FAIL  $*"; problems=$((problems + 1)); }

echo "CyberPipe install ($( [ $DRY = 1 ] && echo 'dry run' || echo 'for real' )) — $ROOT"
echo "checks:"

if [ -x "$PY" ] && "$PY" -c "import requests" 2>/dev/null; then ok "venv with requests"; else bad "no venv: python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"; fi
[ -f .env ] && ok ".env present" || bad ".env missing (see the README's Environment table)"

# Read the settings the services will see, through config.py itself (never print the token).
eval "$("$PY" - <<'PYEOF' 2>/dev/null
import os, shlex, shutil, config
print("HAS_TOKEN=%d" % bool(config.TELEGRAM_BOT_TOKEN))
print("HAS_CHAT=%d" % bool(config.TELEGRAM_CHAT_ID))
print("NODE=%s" % shlex.quote(config.CONTENTRENDER_NODE))
print("CR_DIR=%s" % shlex.quote(config.CONTENTRENDER_DIR))
print("CP_URL=%s" % shlex.quote(config.CONTENTPIPE_BASE_URL))
print("GATE_HOURS=%d" % config.NEEDS_INPUT_TIMEOUT_HOURS)
PYEOF
)"
[ "${HAS_TOKEN:-0}" = 1 ] && [ "${HAS_CHAT:-0}" = 1 ] && ok "Telegram bot token and chat id set" || bad "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env"
case "${NODE:-}" in
  /*) [ -x "$NODE" ] && ok "node: $NODE" || bad "CONTENTRENDER_NODE=$NODE is not executable" ;;
  *)  bad "CONTENTRENDER_NODE must be an absolute path (launchd has no PATH); set it in .env, e.g. $(command -v node)" ;;
esac
if [ -d "${CR_DIR:-/nonexistent}/node_modules" ]; then ok "ContentRender: $CR_DIR"; else bad "ContentRender not installed at ${CR_DIR:-?} (npm install there)"; fi
[ -d "${CR_DIR:-/nonexistent}/.venv-kokoro" ] && ok "Kokoro installed" || warn "Kokoro not set up (npm run kokoro:setup in ContentRender); narration will fail until it is"
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "${CP_URL:-http://localhost:3000}/api/models")
[ "$code" = 200 ] && ok "ContentPipe answers at $CP_URL" || warn "ContentPipe did not answer at $CP_URL (HTTP $code); jobs wait until it does"
ok "a gate waits ${GATE_HOURS:-?} h for an answer"

if [ $problems -gt 0 ]; then
  echo "$problems problem(s); nothing installed."
  exit 1
fi

OUT="$(mktemp -d)"
"$PY" - "$ROOT" "$HOME" "$OUT" <<'PYEOF' || { echo "could not render the plists"; exit 1; }
import sys
from pathlib import Path
from xml.sax.saxutils import escape
root, home, out = sys.argv[1:4]
for name in ("scheduler", "poller"):
    src = Path(root, "deploy", f"com.asitminz.cyberpipe.{name}.plist.example").read_text()
    # XML-escape the paths: an "&" in a folder name made an installed plist invalid once (JobPipe, 2026-09-30).
    text = src.replace("__REPO_ROOT__", escape(root)).replace("__HOME__", escape(home))
    Path(out, f"com.asitminz.cyberpipe.{name}.plist").write_text(text)
PYEOF
for label in "${LABELS[@]}"; do
  plutil -lint -s "$OUT/$label.plist" && ok "rendered $label.plist" || { bad "$label.plist is not a valid plist"; }
done
[ $problems -gt 0 ] && exit 1

if [ $DRY = 1 ]; then
  echo "dry run: plists rendered to $OUT; the launcher would be rebuilt from $ROOT/deploy/run-service.sh."
  exit 0
fi

echo "installing:"
mkdir -p "$ROOT/data/logs" "$AGENTS"
./deploy/build-launcher.sh >/dev/null || { echo "  FAIL  build-launcher.sh"; exit 1; }
if strings "$APP_BIN" | grep -qF "$ROOT/deploy/run-service.sh"; then ok "launcher rebuilt for $ROOT"; else echo "  FAIL  the launcher does not point at $ROOT/deploy/run-service.sh"; exit 1; fi

# bootout returns before launchd has finished removing the service, and a bootstrap straight after it fails with
# "Bootstrap failed: 5: Input/output error" (code review, 2026-10-03). So: unload both, wait until launchd no longer
# knows either label, then load each with a few retries; never stop half way with one agent down.
for label in "${LABELS[@]}"; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null && echo "  ...   unloading the old $label"
done
for label in "${LABELS[@]}"; do
  for _ in $(seq 1 20); do launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1 || break; sleep 0.5; done
done
failed=0
for label in "${LABELS[@]}"; do
  cp "$OUT/$label.plist" "$AGENTS/$label.plist"
  loaded=0
  for attempt in 1 2 3 4 5; do
    if launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist" 2>/dev/null; then loaded=1; break; fi
    sleep $attempt
  done
  if [ $loaded = 1 ]; then ok "loaded $label"; else echo "  FAIL  could not load $label (launchctl bootstrap gui/$(id -u) \"$AGENTS/$label.plist\")"; failed=1; fi
done
[ $failed = 1 ] && { echo "re-run ./deploy/install.sh; if it fails again, see the command above"; exit 1; }
sleep 3
for label in "${LABELS[@]}"; do
  state=$(launchctl print "gui/$(id -u)/$label" 2>/dev/null | awk -F'= ' '/^\tstate/ {print $2; exit}')
  echo "  $label: ${state:-unknown}"
done
cat <<EOF
done. Next:
  - tail -f "$ROOT/data/logs/scheduler.log"     (want: "[scheduler] started, polling every 60s")
  - send /status to the bot in Telegram          (want: "No active jobs.")
  - if a log shows "Operation not permitted" / exit 126: grant Full Disk Access to
    ~/Applications/CyberPipe Services.app, then re-run this script.
EOF

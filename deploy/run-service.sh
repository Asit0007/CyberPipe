#!/bin/bash
# The scheduled entry point for CyberPipe's two long-running services,
# driven by the LaunchAgents in deploy/com.asitminz.cyberpipe.*.plist.example.
#
# Same launchd blind spots as JobPipe's run-daily.sh (see its header for the
# full writeup): no Homebrew on PATH, a bare system python3 with none of the
# deps, no working directory, and buffered output vanishing on a kill --
# unbuffered and an explicit venv are not optional.
#
# Takes the service name as an argument -- "scheduler" or "poller" -- so one
# launcher bundle drives both LaunchAgents instead of building two. No
# default: a stray LaunchServices launch of the bundle (double-click,
# Spotlight, adding it to a Privacy pane, which launches it) must not
# silently start a background service.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export PYTHONUNBUFFERED=1
export LANG="${LANG:-en_US.UTF-8}"

PY="$ROOT/venv/bin/python3"
LOG_DIR="$ROOT/data/logs"
mkdir -p "$LOG_DIR"

if [ $# -eq 0 ]; then
  echo "usage: $(basename "$0") scheduler|poller"
  exit 2
fi

case "$1" in
  scheduler) SCRIPT="scheduler.py" ;;
  poller)    SCRIPT="telegram_poller.py" ;;
  *)         echo "unknown service: $1 (expected scheduler|poller)"; exit 2 ;;
esac
LOG="$LOG_DIR/$1.log"

# Append, not truncate: KeepAlive restarts this on every crash, and each
# restart should extend the log, not erase the evidence of the last one.
exec >> "$LOG" 2>&1
echo "==============================================================="
echo "cyberpipe $1  --  starting $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "  root:   $ROOT"
echo "  python: $PY"
echo "==============================================================="

if [ ! -x "$PY" ]; then
  echo "! no venv at $PY -- run: python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
  exit 1
fi

# exec, not a subshell call: launchctl's SIGTERM on bootout must reach the
# python process directly for a clean shutdown, not get lost one process up.
exec "$PY" "$SCRIPT"

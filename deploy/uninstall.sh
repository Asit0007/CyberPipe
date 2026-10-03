#!/bin/bash
# Unload and remove CyberPipe's two LaunchAgents. Keeps pipeline.db, data/ and the launcher app, so a later
# ./deploy/install.sh picks every job up where it stopped.
set -uo pipefail
for label in com.asitminz.cyberpipe.scheduler com.asitminz.cyberpipe.poller; do
  if launchctl bootout "gui/$(id -u)/$label" 2>/dev/null; then echo "unloaded $label"; else echo "$label was not loaded"; fi
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
echo "done. Jobs and their state are kept in pipeline.db."

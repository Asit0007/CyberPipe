#!/bin/bash
# Build and install "CyberPipe Services.app" -- the TCC identity both
# LaunchAgents run as. See the header of cyberpipe-launcher.c for why this
# exists at all.
#
#   ./deploy/build-launcher.sh
#
# Then, ONCE, by hand:
#   System Settings > Privacy & Security > Full Disk Access > +
#   > select ~/Applications/CyberPipe Services.app  > toggle it on
#
# Re-running this rebuilds the binary, which changes its cdhash, which can
# make macOS ask for the grant again. Nothing here needs to change day to
# day -- all the logic lives in run-service.sh, which the bundle only execs.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Applications/CyberPipe Services.app"
BIN="$APP/Contents/MacOS/CyberPipeServices"

mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>CyberPipe Services</string>
    <key>CFBundleDisplayName</key><string>CyberPipe Services</string>
    <key>CFBundleIdentifier</key><string>com.asitminz.cyberpipe.launcher</string>
    <key>CFBundleExecutable</key><string>CyberPipeServices</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <!-- No Dock icon, no menu bar: it execs a shell and exits. -->
    <key>LSBackgroundOnly</key><true/>
</dict>
</plist>
PLIST

cc -O2 -Wall -Wextra \
   -DSCRIPT_PATH="\"$ROOT/deploy/run-service.sh\"" \
   -o "$BIN" "$ROOT/deploy/cyberpipe-launcher.c"

# Ad-hoc signature. TCC needs a stable code identity to hang the grant on; an
# unsigned binary gets re-evaluated and the grant does not stick.
codesign --force --sign - --identifier com.asitminz.cyberpipe.launcher "$APP"
codesign --verify --strict "$APP"

echo "built  $APP"
echo "  targets: $ROOT/deploy/run-service.sh"
echo "  drives both LaunchAgents: com.asitminz.cyberpipe.scheduler and .poller"
echo
echo "NEXT, and it cannot be scripted -- TCC grants are GUI-only:"
echo "  System Settings > Privacy & Security > Full Disk Access > + "
echo "  > $APP > turn it on"

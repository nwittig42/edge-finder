#!/bin/bash
# Runs engine.py 24/7 as a macOS LaunchAgent: starts at login, restarts if it crashes,
# and keeps the Mac from idle-sleeping while plugged in (caffeinate -is).
#   ./install_engine.sh          install / restart
#   ./install_engine.sh remove   stop and uninstall
set -euo pipefail
LABEL="com.edgefinder.engine"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "remove" ]; then rm -f "$PLIST"; echo "Stopped and removed."; exit 0; fi
mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-is</string>
    <string>$PY</string><string>$DIR/engine.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$DIR/logs/engine.out</string>
  <key>StandardErrorPath</key><string>$DIR/logs/engine.out</string>
</dict></plist>
PL
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Engine installed and running. Log: $DIR/logs/engine.log"

#!/bin/bash
# Installs the Edge Finder schedule as a macOS LaunchAgent (runs while you're logged in and the Mac is awake).
#   ./install_schedule.sh daily 14      -> once a day at 2pm
#   ./install_schedule.sh every 30      -> every 30 minutes
#   ./install_schedule.sh remove        -> uninstall
set -euo pipefail
LABEL="com.edgefinder.scan"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "remove" ]; then rm -f "$PLIST"; echo "Removed."; exit 0; fi

case "${1:-daily}" in
  daily) EXTRA=""; SCHED="<key>StartCalendarInterval</key><dict><key>Hour</key><integer>${2:-14}</integer><key>Minute</key><integer>0</integer></dict>" ;;
  every) EXTRA="<string>--only-hits</string>"; SCHED="<key>StartInterval</key><integer>$(( ${2:-30} * 60 ))</integer>" ;;
  *) echo "usage: $0 daily HOUR | every MINUTES | remove"; exit 1 ;;
esac

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/edge_finder.py</string>$EXTRA</array>
  <key>WorkingDirectory</key><string>$DIR</string>
  $SCHED
  <key>StandardOutPath</key><string>$DIR/logs/run.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/run.log</string>
</dict></plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed: $1 ${2:-}. Logs: $DIR/logs/run.log"

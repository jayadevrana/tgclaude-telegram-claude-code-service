#!/bin/bash
# Install the daemon as a per-user LaunchAgent so it runs 24/7, restarts on crash,
# starts at login, and keeps the Mac awake (caffeinate). Per-user (NOT root) because
# the Claude Max OAuth credential lives in your login keychain.
#
#   bash install_launchagent.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.bluealgo.tgclaude"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# Prefer a local venv python if present, else system python3.
if [ -x "$HERE/.venv/bin/python3" ]; then
  PY="$HERE/.venv/bin/python3"
else
  PY="$(command -v python3)"
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HERE/logs"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-dimsu</string>
    <string>$PY</string>
    <string>$HERE/daemon.py</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$HERE/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$HERE/logs/launchd.err.log</string>
</dict>
</plist>
PLIST

echo "Wrote $PLIST"
echo "Using python: $PY"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Loaded. Tail logs with:  tail -f \"$HERE/logs/daemon.log\""
echo "Stop with:  launchctl unload \"$PLIST\""

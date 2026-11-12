#!/bin/bash
# Install the interim health check as a launchd agent (every 2 minutes, in the logged-in session).
# Run from the M4:  scripts/m14.sh 'bash -s' < scripts/setup/m14_health.sh
# Needs the Discord webhook in the Keychain first: on the M14, bash ~/scribe-bot/scripts/setup/set_api_key.sh discord
set -euo pipefail
PLIST="$HOME/Library/LaunchAgents/com.scribebot.healthwatch.plist"
SCRIPT="$HOME/scribe-bot/scripts/health_watch.sh"
mkdir -p "$(dirname "$PLIST")" "$HOME/scribe-bot-data/logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.scribebot.healthwatch</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$SCRIPT</string></array>
  <key>StartInterval</key><integer>120</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HOME/scribe-bot-data/logs/health.stdout.log</string>
  <key>StandardErrorPath</key><string>$HOME/scribe-bot-data/logs/health.stdout.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/com.scribebot.healthwatch" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "health watch installed: runs every 2 minutes"

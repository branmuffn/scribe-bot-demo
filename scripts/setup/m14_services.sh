#!/bin/bash
# Install (or re-install) the Scribe Bot launchd services on the M14: worker and web. Re-runnable.
# Run on the M14 (from the M4: scripts/m14.sh "bash ~/scribe-bot/scripts/setup/m14_services.sh").
# Remove:  bash ~/scribe-bot/scripts/setup/m14_services.sh --remove
set -euo pipefail

SERVICES="com.scribebot.worker com.scribebot.web"
SRC="$HOME/scribe-bot/scripts/launchd"
DEST="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
mkdir -p "$DEST" "$HOME/scribe-bot-data/logs"

for s in $SERVICES; do
  launchctl bootout "$DOMAIN/$s" 2>/dev/null || true
  if [ "${1:-}" = "--remove" ]; then
    rm -f "$DEST/$s.plist"
    echo "removed $s"
    continue
  fi
  sed "s|__HOME__|$HOME|g" "$SRC/$s.plist.template" > "$DEST/$s.plist"
  plutil -lint -s "$DEST/$s.plist"
  launchctl bootstrap "$DOMAIN" "$DEST/$s.plist"
  echo "installed $s"
done

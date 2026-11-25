#!/bin/bash
# Turn on (or re-install) auto-deploy on the M14: launchd runs scripts/auto_deploy.sh every 5 minutes. Re-runnable.
# Run on the M14:  bash ~/scribe-bot/scripts/setup/m14_auto_deploy.sh
# Turn it off:     bash ~/scribe-bot/scripts/setup/m14_auto_deploy.sh --remove   (deploying by hand still works)
set -euo pipefail

LABEL=com.scribebot.autodeploy
SRC="$HOME/scribe-bot/scripts/launchd/$LABEL.plist.template"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/scribe-bot-data/logs"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "--remove" ]; then
  rm -f "$DEST"
  echo "$(date '+%Y-%m-%d %H:%M:%S') off: turned off by hand" >> "$HOME/scribe-bot-data/logs/auto-deploy.log"
  echo "auto-deploy is off (removed $LABEL)"
  exit 0
fi
sed "s|__HOME__|$HOME|g" "$SRC" > "$DEST"
plutil -lint -s "$DEST"
launchctl bootstrap "$DOMAIN" "$DEST"
echo "$(date '+%Y-%m-%d %H:%M:%S') on: installed; checks GitHub every 5 minutes" >> "$HOME/scribe-bot-data/logs/auto-deploy.log"
echo "auto-deploy is on: it checks GitHub every 5 minutes (log: ~/scribe-bot-data/logs/auto-deploy.log)"

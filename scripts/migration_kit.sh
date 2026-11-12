#!/bin/bash
# Migration kit (SPEC §3.11, A6): move Scribe Bot to new hardware. Run on the M14.
# Usage:  bash ~/scribe-bot/scripts/migration_kit.sh [--dry-run]    (dry run is the default and only mode so far)
#
# Plan (what the real run will do, outline):
#   1. Stop the worker and web services (launchctl bootout) so the database is quiet.
#   2. Snapshot the database (SQLite backup API) and run a final restic backup to every destination.
#   3. Package: code commit id, ~/scribe-bot-data (minus tmp/ and temporary WAVs), launchd plists, Syncthing config,
#      into one restic snapshot tagged "migration" (encrypted; the new machine restores it with the recovery sheet).
#   4. List the secrets to re-enter on the new machine (names only; values never leave the Keychain).
#   5. On the new machine: install uv, clone the repo at that commit, restore the snapshot, re-enter secrets with
#      scripts/setup/set_api_key.sh, run scripts/setup/m14_services.sh and scripts/setup/m14_syncthing.sh, deploy.
set -euo pipefail

MODE="${1:---dry-run}"
DATA="${SCRIBE_BOT_DATA:-$HOME/scribe-bot-data}"
REPO="${SCRIBE_BOT_REPO:-$HOME/scribe-bot}"

if [ "$MODE" != "--dry-run" ]; then
  echo "Only --dry-run exists so far; the real migration is run by hand with Claude using the plan in this file." >&2
  exit 2
fi

echo "MIGRATION KIT (dry run: nothing is stopped, copied, or changed)"
echo
echo "Code:      $REPO at $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo '(not a git checkout)')"
if [ -d "$DATA" ]; then
  echo "Data:      $DATA ($(du -sh "$DATA" 2>/dev/null | cut -f1) total)"
  for d in "$DATA"/*; do
    [ -e "$d" ] || continue
    case "$(basename "$d")" in tmp) note=" (skipped)";; *) note="";; esac
    echo "           - $(basename "$d"): $(du -sh "$d" 2>/dev/null | cut -f1)$note"
  done
else
  echo "Data:      $DATA (missing!)"
fi
echo "Services:  $(ls "$HOME/Library/LaunchAgents"/com.scribebot.*.plist 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')"
echo "Secrets to re-enter (names only):"
for s in anthropic discord discord-bot restic; do
  if security find-generic-password -a brian-personal -s "scribe-bot.$s" >/dev/null 2>&1; then
    echo "           - scribe-bot.$s (present)"
  fi
done
echo "Backups:   destinations from 'python -m scribe_bot.backup status'"
echo
echo "Dry run OK."

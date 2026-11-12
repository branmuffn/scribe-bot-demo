#!/bin/bash
# Set up encrypted backups on the M14 (M6, SPEC §3.11). Re-runnable.
# Without --install it only PRINTS what it would do (nothing is installed or changed).
# Run in Terminal ON THE M14 (it touches the Keychain):  bash ~/scribe-bot/scripts/setup/m14_backup.sh [--install]
#
# What --install does:
#   1. brew install restic rclone (only the ones missing)
#   2. Creates a random backup password in the Keychain (service scribe-bot.restic) if there isn't one.
#      It is never shown here; print it once for your password manager with the recovery sheet (step 4).
# What Brian does by hand afterwards (it needs a Google sign-in in a browser):
#   3. rclone config  -> n (new remote) -> name e.g. gdrive-brian -> storage "drive" -> leave client id/secret empty
#      -> scope "drive.file" (rclone can only see files it created) -> sign in with that Google account.
#      Then register it:  ~/.local/bin/uv run --project ~/scribe-bot python -m scribe_bot.backup \
#                           add-destination "Brian Drive" gdrive-brian brian@example.com
#      Repeat for each Drive account.
#   4. ~/.local/bin/uv run --project ~/scribe-bot python -m scribe_bot.backup recovery-sheet --with-password
#      and save the output in your password manager. Without that password the backups can't be restored.
set -euo pipefail

INSTALL=0
[ "${1:-}" = "--install" ] && INSTALL=1
BREW=$(command -v brew || echo /usr/local/bin/brew)
do_or_say() { if [ "$INSTALL" = 1 ]; then "$@"; else echo "would run: $*"; fi; }

for tool in restic rclone; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "$tool: already installed ($("$tool" version 2>/dev/null | head -1))"
  else
    do_or_say "$BREW" install "$tool"
  fi
done

if security find-generic-password -a brian-personal -s scribe-bot.restic >/dev/null 2>&1; then
  echo "backup password: already in the Keychain (scribe-bot.restic)"
elif [ "$INSTALL" = 1 ]; then
  security add-generic-password -a brian-personal -s scribe-bot.restic \
    -l "Scribe Bot: restic backup password" -w "$(openssl rand -base64 33)"
  echo "backup password: created in the Keychain (scribe-bot.restic); not shown"
else
  echo "would create a random backup password in the Keychain (scribe-bot.restic)"
fi

echo
echo "Next, by hand (needs a browser sign-in): rclone config for each Google Drive account, then add-destination."
echo "See the comments at the top of this script for the exact steps."

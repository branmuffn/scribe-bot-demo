#!/bin/bash
# Run a command on the M14 inside Brian's logged-in session (where the Keychain is readable),
# using a one-off launchd job. Waits for it to finish and prints its output.
# Usage (from the M4):  scripts/m14_gui_run.sh "<command>"
set -euo pipefail
[ "$#" -eq 1 ] || { echo "usage: $0 \"<command>\"" >&2; exit 2; }
exec ssh -o BatchMode=yes m14 "bash -s -- $(printf %q "$1")" <<'REMOTE'
set -euo pipefail
CMD="$1"
LABEL="com.scribebot.oneoff.$$"
DIR="$HOME/scribe-bot-data/tmp"; mkdir -p "$DIR"
OUT="$DIR/$LABEL.out"; PLIST="$DIR/$LABEL.plist"
/usr/bin/python3 - "$PLIST" "$LABEL" "$CMD" "$OUT" <<'PY'
import plistlib, sys
plist, label, cmd, out = sys.argv[1:]
plistlib.dump({"Label": label, "ProgramArguments": ["/bin/bash", "-lc", cmd],
               "StandardOutPath": out, "StandardErrorPath": out, "RunAtLoad": True}, open(plist, "wb"))
PY
launchctl bootstrap "gui/$(id -u)" "$PLIST"
while launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -q "state = running"; do sleep 1; done
sleep 1
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
cat "$OUT"; rm -f "$OUT" "$PLIST"
REMOTE

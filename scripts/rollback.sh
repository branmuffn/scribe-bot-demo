#!/bin/bash
# Manual rollback: put the M14 back on the commit it ran before the last successful deploy (or a commit you name).
# Usage:  scripts/rollback.sh [commit]      DRY_RUN=1 prints the commands instead of running them
# Uses scripts/deploy.sh, so it gets the same health check (and rolls forward again if the old version is unhealthy).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  if [ "${DRY_RUN:-0}" = 1 ]; then
    echo "m14\$ grep result=ok ~/scribe-bot-data/logs/deploys.log | tail -1" >&2
    TARGET="<previous-commit>"
  else
    TARGET=$("$HERE/m14.sh" "grep 'result=ok' ~/scribe-bot-data/logs/deploys.log | tail -1" | sed -n 's/.*prev=\([0-9a-f]*\).*/\1/p')
  fi
  [ -n "$TARGET" ] || { echo "[rollback] no successful deploy recorded; name a commit: scripts/rollback.sh <commit>" >&2; exit 2; }
fi
echo "[rollback] going back to ${TARGET:0:7}"
exec "$HERE/deploy.sh" "$TARGET"

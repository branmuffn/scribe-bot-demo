#!/bin/bash
# Manual rollback: put the M14 back on the commit it ran before the last successful deploy (or a commit you name).
# Usage:  bash scripts/rollback.sh [commit]      DRY_RUN=1 prints the commands instead of running them
# Works on the M14 itself (local) or from the M4 (remote), like scripts/deploy.sh, which it uses: so it gets the same
# safety checks and health check (and rolls forward again if the old version is unhealthy).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-}"
MODE="${SCRIBE_DEPLOY:-}"
if [ -z "$MODE" ]; then
  if [ -d "$HOME/scribe-bot-data" ] && [ -d "$HOME/scribe-bot" ] && \
     [ "$(cd "$HERE/.." && pwd -P)" = "$(cd "$HOME/scribe-bot" && pwd -P)" ]; then MODE=local; else MODE=remote; fi
fi
LOOK="grep 'result=ok' ~/scribe-bot-data/logs/deploys.log | tail -1"
if [ -z "$TARGET" ]; then
  if [ "${DRY_RUN:-0}" = 1 ] && [ "$MODE" = remote ]; then
    echo "m14\$ $LOOK" >&2
    TARGET="<previous-commit>"
  else
    [ "${DRY_RUN:-0}" = 1 ] && echo "m14\$ $LOOK" >&2
    if [ "$MODE" = local ]; then LINE=$(bash -c "$LOOK" || true); else LINE=$("$HERE/m14.sh" "$LOOK" || true); fi
    TARGET=$(echo "$LINE" | sed -n 's/.*prev=\([0-9a-f]*\).*/\1/p')
  fi
  [ -n "$TARGET" ] || { echo "[rollback] no successful deploy recorded; name a commit: bash scripts/rollback.sh <commit>" >&2; exit 2; }
fi
echo "[rollback] $([ "${DRY_RUN:-0}" = 1 ] && echo 'DRY RUN: would go' || echo 'going') back to ${TARGET:0:7}"
# Hold auto-deploy (scripts/auto_deploy.sh) on what main is now, so it doesn't undo this rollback 5 minutes later.
# It starts again by itself when a newer commit lands on main (the fix).
if [ "${DRY_RUN:-0}" != 1 ]; then
  HOLD="mkdir -p ~/scribe-bot-data/auto-deploy && cd ~/scribe-bot && git fetch -q origin && git rev-parse origin/main > ~/scribe-bot-data/auto-deploy/hold"
  if [ "$MODE" = local ]; then bash -c "$HOLD" || true; else "$HERE/m14.sh" "$HOLD" || true; fi
  echo "[rollback] auto-deploy holds until a newer commit lands on main"
fi
SCRIBE_DEPLOY="$MODE" exec bash "$HERE/deploy.sh" "$TARGET"

#!/bin/bash
# Deploy to the M14 from the M4 (CLAUDE.md rule 9): one command, automatic rollback if the health check fails.
# Usage:  scripts/deploy.sh [commit]        (default: the latest origin/main)
#         DRY_RUN=1 scripts/deploy.sh       prints the M14 commands instead of running them
# Steps: note the M14's current commit -> move to the target -> uv sync -> migrations -> restart services ->
# health check (database + disk, the web app's /health, the worker running). On failure: back to the previous commit, sync, restart, health check, and say so.
# Migrations only go forward; older code must tolerate a newer schema (add columns/tables, never drop).
# Every deploy is recorded on the M14 in ~/scribe-bot-data/logs/deploys.log (scripts/rollback.sh reads it).
set -euo pipefail

TARGET="${1:-origin/main}"
DRY_RUN="${DRY_RUN:-0}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO='~/scribe-bot'
UV='~/.local/bin/uv'
SERVICES="com.scribebot.worker com.scribebot.web"
HEALTH_TRIES=3

say() { echo "[deploy] $*"; }

remote() {  # run on the M14 (or print, in dry-run mode)
  if [ "$DRY_RUN" = 1 ]; then
    echo "m14\$ $1" >&2
    [ -n "${2:-}" ] && echo "$2"   # stand-in output for dry runs
    return 0
  fi
  "$HERE/m14.sh" "$1"
}

move_to() {  # stay on main; --keep refuses if it would overwrite local changes on the M14
  remote "cd $REPO && git checkout -q main && git reset -q --keep $1"
}

sync_and_migrate() {
  remote "cd $REPO && $UV sync -q && $UV run python -m scribe_bot.migrate"
}

restart() {  # services not installed yet are skipped
  remote "for s in $SERVICES; do if launchctl print gui/\$(id -u)/\$s >/dev/null 2>&1; then launchctl kickstart -k gui/\$(id -u)/\$s; echo restarted \$s; fi; done"
}

services_up() {  # the web app answers /health and the worker is running (rule 9: a broken service rolls back)
  remote "launchctl print gui/\$(id -u)/com.scribebot.web >/dev/null 2>&1 || exit 0; curl -fsS -m 10 -o /dev/null http://127.0.0.1:8000/health && echo web ok" &&
  remote "launchctl print gui/\$(id -u)/com.scribebot.worker 2>/dev/null | grep -q 'state = running' || ! launchctl print gui/\$(id -u)/com.scribebot.worker >/dev/null 2>&1"
}

healthy() {
  for i in $(seq 1 "$HEALTH_TRIES"); do
    [ "$DRY_RUN" = 1 ] || sleep 3
    if remote "cd $REPO && $UV run python -m scribe_bot.health" '{"ok": true}' && services_up; then
      return 0
    fi
    say "health check failed (try $i of $HEALTH_TRIES)"
  done
  return 1
}

record() {
  remote "mkdir -p ~/scribe-bot-data/logs && echo \"\$(date '+%Y-%m-%d %H:%M:%S') prev=$1 target=$2 result=$3\" >> ~/scribe-bot-data/logs/deploys.log"
}

PREV=$(remote "cd $REPO && git rev-parse HEAD" "<current-commit>")
remote "cd $REPO && git fetch -q origin"
NEW=$(remote "cd $REPO && git rev-parse --verify $TARGET^{commit}" "<target-commit>")
say "M14 is at ${PREV:0:7}; deploying ${NEW:0:7}"

if move_to "$NEW" && sync_and_migrate && restart && healthy; then
  record "$PREV" "$NEW" ok
  say "deployed ${NEW:0:7}; health check passed"
  exit 0
fi

say "deploy failed; rolling back to ${PREV:0:7}"
if move_to "$PREV" && sync_and_migrate && restart && healthy; then
  record "$PREV" "$NEW" rolled-back
  say "ROLLED BACK to ${PREV:0:7}; the M14 is healthy on the old version"
else
  record "$PREV" "$NEW" rollback-failed
  say "ROLLBACK FAILED; the M14 needs attention (see ~/scribe-bot-data/logs on the M14)"
fi
exit 1

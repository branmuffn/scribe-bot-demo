#!/bin/bash
# Deploy Scribe Bot to the M14 (CLAUDE.md rule 9): one command, automatic rollback if the health check fails.
#
# Where it runs (Brian, 2026-11-25: deploy straight from the M14, no M4 needed):
#   On the M14:   cd ~/scribe-bot && bash scripts/deploy.sh        (local: no SSH)
#   From the M4:  scripts/deploy.sh                                 (remote: over SSH, needs the "m14" host in ~/.ssh/config)
#   It picks local by itself when it runs from ~/scribe-bot on a machine that has ~/scribe-bot-data (that's the M14).
#   SCRIBE_DEPLOY=local or SCRIBE_DEPLOY=remote forces one.
#
# Usage:  bash scripts/deploy.sh [commit]   (default: the latest origin/main, i.e. what's merged on GitHub)
#         DRY_RUN=1 bash scripts/deploy.sh  changes nothing: prints every step. On the M14 it also really checks the
#                                           read-only things (current and target commit, uncommitted changes, services).
#                                           Every line it prints says DRY RUN, and it ends with "DRY RUN finished: nothing
#                                           changed", never with "health check passed" (2026-11-25: a dry run that ended
#                                           with the success line looked like a real deploy).
#
# Before anything changes it stops, untouched, if: the M14's code folder has uncommitted changes to tracked files, or the
# web and worker launchd services aren't installed (bash scripts/setup/m14_services.sh installs them).
# Steps: note the current commit -> move to the target -> uv sync -> migrations -> restart services ->
# health check (database + disk, the web app's /health answering, the worker running). On failure: back to the previous
# commit, sync, restart, health check, and say so. Every deploy is logged in ~/scribe-bot-data/logs/deploys.log
# (scripts/rollback.sh reads it). Migrations only go forward; older code must tolerate a newer schema (add, never drop).
# Nothing in ~/scribe-bot-data is deleted or rewritten; the Keychain is never read.
set -euo pipefail

TARGET="${1:-origin/main}"
DRY_RUN="${DRY_RUN:-0}"
HERE="${DEPLOY_HERE:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck disable=SC2088  # the ~ is meant literally here: the M14's own shell expands it (local or over SSH)
REPO='~/scribe-bot'
# shellcheck disable=SC2088
UV='~/.local/bin/uv'
DATA='SCRIBE_BOT_DATA=~/scribe-bot-data'   # the same data folder the launchd services use, whatever this shell has set
SERVICES="com.scribebot.worker com.scribebot.web"
HEALTH_TRIES=5

short() { case "$1" in "<"*) echo "$1" ;; *) echo "${1:0:7}" ;; esac; }   # a commit's short form (dry-run stand-ins whole)
say() { if [ "$DRY_RUN" = 1 ]; then echo "[deploy] DRY RUN: $*"; else echo "[deploy] $*"; fi; }

# Local or remote?
MODE="${SCRIBE_DEPLOY:-}"
if [ -z "$MODE" ]; then
  if [ -d "$HOME/scribe-bot-data" ] && [ -d "$HOME/scribe-bot" ] && \
     [ "$(cd "$HERE/.." && pwd -P)" = "$(cd "$HOME/scribe-bot" && pwd -P)" ]; then MODE=local; else MODE=remote; fi
fi
case "$MODE" in local|remote) ;; *) say "SCRIBE_DEPLOY must be local or remote"; exit 2 ;; esac

# Local: git reset below rewrites this very file while bash is still reading it, so run from a copy in /tmp.
if [ "$MODE" = local ] && [ "$DRY_RUN" != 1 ] && [ -z "${DEPLOY_FROM_COPY:-}" ]; then
  COPY="$(mktemp "${TMPDIR:-/tmp}/scribe-deploy.XXXXXX")"
  cp "$0" "$COPY"
  DEPLOY_FROM_COPY="$COPY" DEPLOY_HERE="$HERE" SCRIBE_DEPLOY=local exec bash "$COPY" "$@"
fi
[ -n "${DEPLOY_FROM_COPY:-}" ] && trap 'rm -f "$DEPLOY_FROM_COPY"' EXIT

run() {  # run one command on the M14: here (local) or over SSH (remote)
  if [ "$MODE" = local ]; then bash -c "$1"; else "$HERE/m14.sh" "$1"; fi
}

remote() {  # a step that changes something: printed only in a dry run
  if [ "$DRY_RUN" = 1 ]; then
    echo "m14\$ $1" >&2
    [ -n "${2:-}" ] && echo "$2"   # stand-in output for dry runs
    return 0
  fi
  run "$1"
}

probe() {  # a read-only check: in a dry run on the M14 itself it really runs (it changes nothing); otherwise like remote
  if [ "$DRY_RUN" = 1 ] && [ "$MODE" = local ]; then echo "m14\$ $1" >&2; run "$1"; return; fi
  remote "$@"
}

move_to() {  # stay on main; --keep refuses if it would overwrite local changes on the M14
  remote "cd $REPO && git checkout -q main && git reset -q --keep $1"
}

sync_and_migrate() {
  remote "cd $REPO && $UV sync -q && $DATA $UV run python -m scribe_bot.migrate"
}

restart() {
  remote "for s in $SERVICES; do launchctl kickstart -k gui/\$(id -u)/\$s && echo restarted \$s || exit 1; done"
}

services_installed() {  # both launchd services must exist: a missing service is never counted as healthy
  probe "for s in $SERVICES; do launchctl print gui/\$(id -u)/\$s >/dev/null 2>&1 || { echo \"missing \$s\"; exit 1; }; done; echo services installed" "services installed"
}

services_up() {  # the web app answers /health and the worker is running
  remote "curl -fsS -m 10 -o /dev/null http://127.0.0.1:8000/health && echo web ok" &&
  remote "launchctl print gui/\$(id -u)/com.scribebot.worker 2>/dev/null | grep -q 'state = running' && echo worker running"
}

healthy() {
  for i in $(seq 1 "$HEALTH_TRIES"); do
    [ "$DRY_RUN" = 1 ] || sleep 3
    if remote "cd $REPO && $DATA $UV run python -m scribe_bot.health" && services_up; then
      return 0
    fi
    say "health check failed (try $i of $HEALTH_TRIES)"
  done
  return 1
}

record() {
  remote "mkdir -p ~/scribe-bot-data/logs && echo \"\$(date '+%Y-%m-%d %H:%M:%S') prev=$1 target=$2 result=$3 from=$MODE\" >> ~/scribe-bot-data/logs/deploys.log"
}

if [ "$DRY_RUN" = 1 ]; then
  echo "[deploy] ==== DRY RUN: nothing will change. Lines starting m14\$ are what a real deploy would run. ===="
fi
say "mode: $MODE"
PREV=$(probe "cd $REPO && git rev-parse HEAD" "<current-commit>")
probe "cd $REPO && git fetch -q origin"
NEW=$(probe "cd $REPO && git rev-parse --verify $TARGET^{commit}" "<target-commit>")

# Safety checks before anything changes
DIRTY=$(probe "cd $REPO && git status --porcelain --untracked-files=no" "")
if [ -n "$DIRTY" ]; then
  say "STOPPED, nothing changed: the M14's code folder has uncommitted changes:"
  # shellcheck disable=SC2001
  echo "$DIRTY" | sed 's/^/[deploy]   /'
  say "keep them with: cd ~/scribe-bot && git stash   (or undo them with git checkout -- <file>), then deploy again"
  exit 1
fi
if ! services_installed >/dev/null; then
  say "STOPPED, nothing changed: the web and worker launchd services aren't both installed."
  say "install them with: bash ~/scribe-bot/scripts/setup/m14_services.sh   then deploy again"
  exit 1
fi

say "M14 is at $(short "$PREV"); deploying $(short "$NEW")"
if [ "$DRY_RUN" != 1 ] && [ "$PREV" = "$NEW" ]; then say "already on ${NEW:0:7}; restarting and checking health anyway"; fi

if move_to "$NEW" && sync_and_migrate && restart && healthy; then
  record "$PREV" "$NEW" ok
  if [ "$DRY_RUN" = 1 ]; then
    if [ "$PREV" = "$NEW" ]; then would="only restart the services and check health (it's already on ${NEW:0:7})"
    else would="move the M14 from $(short "$PREV") to $(short "$NEW")"; fi
    echo "[deploy] ==== DRY RUN finished: nothing changed. A real deploy would $would. ===="
    echo "[deploy] DRY RUN: to deploy for real, run:  cd ~/scribe-bot && bash scripts/deploy.sh${1:+ $1}"
    exit 0
  fi
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

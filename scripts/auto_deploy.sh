#!/bin/bash
# Auto-deploy for the M14 (Brian, 2026-11-25): every 5 minutes launchd runs this. It asks GitHub which commit main is
# on and, if that's newer than what the M14 runs, deploys it with the normal scripts/deploy.sh (which checks health
# and rolls back by itself). Install, check, read the log, turn off: docs/AUTO_DEPLOY.md.
#
# The idle check (nothing new) is one `git ls-remote` and one `git rev-parse`: well under a second, a few MB.
# It does nothing, and tries again 5 minutes later, when:
#   - a recording is being processed right now (a job is running in the queue), so a restart can't interrupt it;
#   - the M14 is hot: Brian's watcher (tools/m14-health/m14 status) shows a 5-minute average of 90 °C or more, or the CPU
#     slowed below 100%. If the watcher isn't there, or its output can't be read, it carries on;
#   - another auto-deploy is still running (a lock).
# A commit whose deploy failed is not tried again by itself (no restart loop); a newer commit, or a deploy by hand, ends that.
# After a rollback by hand (scripts/rollback.sh) it holds the same way, so it doesn't undo the rollback.
# It never uses sudo, never stops processes, never reads the Keychain, and never prints secrets.
# One line per action goes to ~/scribe-bot-data/logs/auto-deploy.log; the last deploy's full output to auto-deploy.last.log.
set -uo pipefail

# The whole script is one function, read in full before it runs: a deploy rewrites this very file mid-run.
main() {
REPO="${AUTO_DEPLOY_REPO:-$HOME/scribe-bot}"
DATA="${SCRIBE_BOT_DATA:-$HOME/scribe-bot-data}"
LOGS="$DATA/logs"
STATE="$DATA/auto-deploy"
LOCK="$STATE/lock"
DB="$DATA/scribe-bot.db"
HEAT_TOOL="${AUTO_DEPLOY_HEAT_TOOL:-$HOME/Projects/portfolio-notes/tools/m14-health/m14}"
MAX_C="${AUTO_DEPLOY_MAX_C:-90}"
DEPLOY="${AUTO_DEPLOY_CMD:-$REPO/scripts/deploy.sh}"
mkdir -p "$LOGS" "$STATE"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOGS/auto-deploy.log"; }
date '+%Y-%m-%d %H:%M:%S' > "$STATE/last-check"   # so "is it running?" has an answer without filling the log

# One at a time. mkdir is atomic; a lock left by a run that died (its PID is gone) is taken over.
if ! mkdir "$LOCK" 2>/dev/null; then
  old=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then  # kill -0 only asks "is it alive?"; it sends nothing
    log "skip: another auto-deploy is running (pid $old)"; exit 0; fi
  rm -rf "$LOCK"; mkdir "$LOCK" || exit 0
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# Which commit is main on at GitHub, and which does the M14 run?
export GIT_TERMINAL_PROMPT=0 GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=20
REMOTE=$(git -C "$REPO" ls-remote origin refs/heads/main 2>/dev/null | cut -f1)
if [ -z "$REMOTE" ]; then
  # Offline or GitHub unreachable: say so once, not every 5 minutes.
  [ -f "$STATE/offline" ] || log "skip: couldn't reach GitHub (will keep trying quietly)"
  touch "$STATE/offline"; exit 0
fi
[ -f "$STATE/offline" ] && { rm -f "$STATE/offline"; log "GitHub reachable again"; }
HEAD=$(git -C "$REPO" rev-parse HEAD 2>/dev/null || true)
if [ "$REMOTE" = "$HEAD" ]; then                                     # nothing new: the usual case
  # R4 (2026-11-25): Brian looked for the log and it didn't exist, because quiet checks only touch last-check.
  # The first check writes one line, so the log always shows auto-deploy is alive.
  [ -f "$LOGS/auto-deploy.log" ] || log "on: checking every 5 minutes; up to date at ${HEAD:0:7} (quiet checks only update auto-deploy/last-check)"
  exit 0
fi
if [ "$REMOTE" = "$(cat "$STATE/failed" 2>/dev/null)" ]; then exit 0; fi   # already failed once; logged then
if [ "$REMOTE" = "$(cat "$STATE/hold" 2>/dev/null)" ]; then                # rolled back by hand (scripts/rollback.sh)
  [ -f "$STATE/hold-said" ] || { log "hold: rolled back by hand; waiting for a newer commit on main"; touch "$STATE/hold-said"; }
  exit 0
fi
rm -f "$STATE/hold-said"

# Not while a recording is being processed (read-only look at the queue).
if [ -f "$DB" ]; then
  if ! running=$(python3 -c 'import sqlite3, sys
c = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True, timeout=5)
print(c.execute("SELECT count(*) FROM job WHERE status = ?", ("running",)).fetchone()[0])' "$DB" 2>/dev/null); then
    log "skip ${REMOTE:0:7}: couldn't read the job queue (will try again)"; exit 0
  fi
  if [ "${running:-0}" != 0 ]; then
    [ "$(cat "$STATE/waiting" 2>/dev/null)" = "busy $REMOTE" ] || log "wait ${REMOTE:0:7}: a recording is being processed"
    echo "busy $REMOTE" > "$STATE/waiting"; exit 0
  fi
fi

# Not while the M14 is hot. `m14 status` prints "🌡 Heat 80 °C" (this second's hottest core: it swings 10+ °C),
# "↳ last 5 min: average 65 °C, peak 72 °C" and "⚡ Speed 100%". It waits when the 5-minute average is at the limit or
# more (the Heat line only if there's no average), or the CPU slowed below 100%. A line it can't read doesn't stop it.
if [ -x "$HEAT_TOOL" ]; then
  out=$("$HEAT_TOOL" status 2>/dev/null || true)
  heat=$(printf '%s\n' "$out" | grep -Eio 'average[^0-9]*[0-9]+' | grep -Eo '[0-9]+$' | head -1)
  [ -n "$heat" ] || heat=$(printf '%s\n' "$out" | grep -Eio 'heat[^0-9]*[0-9]+' | grep -Eo '[0-9]+$' | head -1)
  speed=$(printf '%s\n' "$out" | grep -Eio 'speed[^0-9%]*[0-9]+ ?%' | grep -Eo '[0-9]+' | tail -1)
  if { [ -n "$heat" ] && [ "$heat" -ge "$MAX_C" ]; } || { [ -n "$speed" ] && [ "$speed" -lt 100 ]; }; then
    [ "$(cat "$STATE/waiting" 2>/dev/null)" = "hot $REMOTE" ] || \
      log "wait ${REMOTE:0:7}: the M14 is hot (${heat:-?} °C, speed ${speed:-?}%)"
    echo "hot $REMOTE" > "$STATE/waiting"; exit 0
  fi
fi
rm -f "$STATE/waiting"

# Deploy it, gently. deploy.sh does the health check and the rollback; its whole output is kept in auto-deploy.last.log.
log "deploy ${REMOTE:0:7} (M14 was on ${HEAD:0:7})"
if SCRIBE_DEPLOY=local nice -n 10 bash "$DEPLOY" "$REMOTE" > "$LOGS/auto-deploy.last.log" 2>&1; then
  rm -f "$STATE/failed"
  log "deployed ${REMOTE:0:7}: health check passed"
else
  echo "$REMOTE" > "$STATE/failed"
  log "FAILED ${REMOTE:0:7}: $(tail -1 "$LOGS/auto-deploy.last.log" | sed 's/^\[deploy\] //'). Not retried until a newer commit or a deploy by hand."
fi
}
main "$@"
exit $?

#!/bin/bash
# Interim M14 health check (2026-10-19), until the M1 Status page and M5 Discord bot replace it.
# Runs every 2 minutes as a launchd agent in the logged-in session (it reads the Discord webhook
# from the Keychain). Messages are status only, never meeting content (CLAUDE.md, Discord rule).
#   - On battery for 2+ min: alert. Below 30%: louder alert. Charger back: all-clear.
#   - Internet down 6+ min: switch Wi-Fi off/on once to reconnect. Every drop is logged.
# Unsent messages are queued and delivered when the internet returns.
set -uo pipefail

STATE_DIR="$HOME/scribe-bot-data/health"
LOG="$HOME/scribe-bot-data/logs/health.log"
QUEUE="$STATE_DIR/queue.txt"
mkdir -p "$STATE_DIR" "$(dirname "$LOG")"
NOW=$(date +%s)
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }
get() { cat "$STATE_DIR/$1" 2>/dev/null || true; }
put() { echo "$2" > "$STATE_DIR/$1"; }
clear_state() { rm -f "$STATE_DIR/$1"; }

webhook() {
  security find-generic-password -a brian-personal -s scribe-bot.discord -w 2>/dev/null
}

send_queue() {  # deliver queued messages in order; stop at the first failure
  [ -s "$QUEUE" ] || return 0
  local url; url=$(webhook) || return 0
  [ -n "$url" ] || return 0
  local rest; rest=$(mktemp)
  local failed=0
  while IFS= read -r msg; do
    if [ "$failed" = 0 ] && curl -fsS -m 10 -H "Content-Type: application/json" \
         -d "$(/usr/bin/python3 -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}))' "$msg")" \
         "$url" >/dev/null 2>&1; then
      :
    else
      failed=1; echo "$msg" >> "$rest"
    fi
  done < "$QUEUE"
  mv "$rest" "$QUEUE"
}

alert() { echo "$1" >> "$QUEUE"; log "alert: $1"; }

# --- Power ---
BATT=$(pmset -g batt)
PCT=$(echo "$BATT" | grep -Eo '[0-9]+%' | head -1 | tr -d '%')
if echo "$BATT" | grep -q "'Battery Power'"; then
  since=$(get on_battery_since); [ -n "$since" ] || { since=$NOW; put on_battery_since "$NOW"; log "on battery ($PCT%)"; }
  if [ $((NOW - since)) -ge 120 ] && [ -z "$(get alerted_battery)" ]; then
    alert "⚠️ M14 is running on battery (${PCT}%). Check the charger cable."; put alerted_battery 1
  fi
  if [ "${PCT:-100}" -lt 30 ] && [ -z "$(get alerted_low)" ]; then
    alert "🔴 M14 battery low (${PCT}%). It will go to sleep soon unless it's plugged in."; put alerted_low 1
  fi
else
  if [ -n "$(get on_battery_since)" ]; then
    log "back on power ($PCT%)"
    [ -n "$(get alerted_battery)" ] && alert "✅ M14 is back on power (${PCT}%)."
    clear_state on_battery_since; clear_state alerted_battery; clear_state alerted_low
  fi
fi

# --- Internet ---
if ping -c 1 -t 5 1.1.1.1 >/dev/null 2>&1 || curl -fsS -m 8 -o /dev/null https://www.apple.com/library/test/success.html 2>/dev/null; then
  down=$(get net_down_since)
  if [ -n "$down" ]; then
    log "internet back after $(( (NOW - down) / 60 )) min"
    alert "✅ M14 internet was down for about $(( (NOW - down) / 60 )) min and is back."
    clear_state net_down_since; clear_state wifi_cycled
  fi
else
  down=$(get net_down_since); [ -n "$down" ] || { down=$NOW; put net_down_since "$NOW"; log "internet down (Wi-Fi: $(networksetup -getairportpower en0 2>/dev/null | awk '{print $NF}'))"; }
  if [ $((NOW - down)) -ge 360 ] && [ -z "$(get wifi_cycled)" ]; then
    log "cycling Wi-Fi off/on"
    networksetup -setairportpower en0 off; sleep 5; networksetup -setairportpower en0 on
    put wifi_cycled 1
  fi
fi

# --- Disk (alert at 85% used; all-clear below 80%) ---
USED=$(df -P "$HOME" | awk 'NR==2 {gsub("%", "", $5); print $5}')
if [ "${USED:-0}" -ge 85 ] && [ -z "$(get alerted_disk)" ]; then
  alert "⚠️ M14 disk is ${USED}% full."; put alerted_disk 1
elif [ "${USED:-0}" -lt 80 ] && [ -n "$(get alerted_disk)" ]; then
  alert "✅ M14 disk is back to ${USED}% full."; clear_state alerted_disk
fi

# --- Scribe Bot services (web app answers and the worker checked in recently; 2 failed checks in a row) ---
HEALTH=$(curl -fsS -m 10 http://127.0.0.1:8000/health 2>/dev/null || true)
if echo "$HEALTH" | grep -q '"worker":{"alive":true'; then
  if [ -n "$(get alerted_app)" ]; then alert "✅ Scribe Bot is running again."; fi
  clear_state app_fails; clear_state alerted_app
else
  prev=$(get app_fails); fails=$(( ${prev:-0} + 1 )); put app_fails "$fails"
  if [ "$fails" -ge 2 ] && [ -z "$(get alerted_app)" ]; then
    alert "🔴 Scribe Bot isn't responding on the M14 (web app or worker down)."; put alerted_app 1
  fi
fi

send_queue

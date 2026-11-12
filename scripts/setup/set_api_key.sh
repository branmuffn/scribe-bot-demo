#!/bin/bash
# Store an API key in the M14's macOS Keychain (CLAUDE.md rule 4). The key is typed at a hidden
# prompt: it never appears on screen, in shell history, in files, or in git.
# Run in Terminal ON THE M14:  bash ~/scribe-bot/scripts/setup/set_api_key.sh [provider]
# Providers: anthropic (default), google (Gemini AI Studio key), openai, discord (#scribe-alerts webhook),
#            discord-bot (#scribe-bot webhook; optional, alerts' webhook is used if missing).
set -euo pipefail

PROVIDER="${1:-anthropic}"
SERVICE="scribe-bot.$PROVIDER"
ACCOUNT="brian-personal"

echo "Paste your $PROVIDER key (or webhook link) and press Enter. Nothing will show while you paste; that's normal."
read -rs KEY
echo
[ -n "$KEY" ] || { echo "No key entered; nothing saved."; exit 1; }
case "$PROVIDER:$KEY" in
  anthropic:sk-ant-*) ;;
  anthropic:*) echo "That doesn't look like an Anthropic key (should start with sk-ant-). Nothing saved."; exit 1 ;;
  google:AIza*) ;;
  google:*) echo "That doesn't look like a Google AI Studio key (should start with AIza). Nothing saved."; exit 1 ;;
  openai:sk-ant-*) echo "That's an Anthropic key, not an OpenAI key. Nothing saved."; exit 1 ;;
  openai:sk-*) ;;
  openai:*) echo "That doesn't look like an OpenAI key (should start with sk-). Nothing saved."; exit 1 ;;
  discord:https://discord.com/api/webhooks/*|discord:https://discordapp.com/api/webhooks/*) ;;
  discord-bot:https://discord.com/api/webhooks/*|discord-bot:https://discordapp.com/api/webhooks/*) ;;
  discord:*|discord-bot:*) echo "That doesn't look like a Discord webhook link (should start with https://discord.com/api/webhooks/). Nothing saved."; exit 1 ;;
  *) echo "Unknown provider '$PROVIDER' (use anthropic, google, openai, discord, or discord-bot). Nothing saved."; exit 1 ;;
esac

security add-generic-password -U -a "$ACCOUNT" -s "$SERVICE" \
  -l "Scribe Bot: $PROVIDER API key ($ACCOUNT)" -w "$KEY"
unset KEY

if security find-generic-password -a "$ACCOUNT" -s "$SERVICE" >/dev/null 2>&1; then
  echo "Saved to the Keychain as \"$SERVICE\". You can close this window."
else
  echo "Something went wrong; the key was not saved." >&2
  exit 1
fi

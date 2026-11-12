#!/bin/bash
# Run Scribe Bot in Demo mode: a separate ./demo-data folder with made-up meetings, served at http://127.0.0.1:8000
# No accounts, keys, phone, speech models or audio needed. Ctrl-C to stop. Delete ./demo-data to start over.
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="${SCRIBE_BOT_DATA:-$PWD/demo-data}"
uv sync --quiet
uv run python -m scribe_bot.demo_data "$DATA"
echo "Open http://127.0.0.1:${SCRIBE_BOT_PORT:-8000} (Ctrl-C to stop)"
SCRIBE_BOT_DATA="$DATA" exec uv run python -m scribe_bot.web

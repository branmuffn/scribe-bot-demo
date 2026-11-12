#!/bin/sh
# Run a command on the M14 over SSH (Tailscale).
# Usage: scripts/m14.sh "<command>"     (no argument opens an interactive shell)
# Needs the "m14" host entry in ~/.ssh/config on the M4 (see docs/M0_RESULTS.md, M0a).
set -eu
if [ "$#" -eq 0 ]; then
  exec ssh m14
fi
exec ssh -o BatchMode=yes -o ConnectTimeout=10 m14 "$@"

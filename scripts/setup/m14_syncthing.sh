#!/bin/bash
# Install and configure Syncthing on the M14 (M0d). Safe to re-run.
# Run from the M4:  scripts/m14.sh 'bash -s' < scripts/setup/m14_syncthing.sh
#
# Privacy settings per SPEC §9.3: no global/local discovery, no relays, no NAT
# traversal, listen only on the Tailscale address, GUI on localhost only,
# no usage or crash reporting, no auto-upgrade.
set -euo pipefail

VERSION=v2.1.6
BIN="$HOME/.local/bin/syncthing"
ST_HOME="$HOME/Library/Application Support/Syncthing"
DATA="$HOME/scribe-bot-data"
PLIST="$HOME/Library/LaunchAgents/com.scribebot.syncthing.plist"
TAILSCALE=/Applications/Tailscale.app/Contents/MacOS/Tailscale

# 1. Install the official binary, checksum-verified
if ! "$BIN" version 2>/dev/null | grep -q "$VERSION"; then
  F="syncthing-macos-amd64-$VERSION.zip"
  T=$(mktemp -d)
  curl -fsSL -o "$T/$F" "https://github.com/syncthing/syncthing/releases/download/$VERSION/$F"
  curl -fsSL -o "$T/sums.asc" "https://github.com/syncthing/syncthing/releases/download/$VERSION/sha256sum.txt.asc"
  WANT=$(grep " $F\$" "$T/sums.asc" | awk '{print $1}')
  GOT=$(shasum -a 256 "$T/$F" | awk '{print $1}')
  [ -n "$WANT" ] && [ "$WANT" = "$GOT" ] || { echo "Checksum mismatch for $F" >&2; exit 1; }
  unzip -q "$T/$F" -d "$T"
  mkdir -p "$(dirname "$BIN")"
  install -m 755 "$T/syncthing-macos-amd64-$VERSION/syncthing" "$BIN"
  xattr -d com.apple.quarantine "$BIN" 2>/dev/null || true
  rm -rf "$T"
fi

# 2. Folders
mkdir -p "$DATA/inbound" "$DATA/logs"
chmod 700 "$DATA"

# 3. Generate keys/config once, then apply privacy settings
[ -f "$ST_HOME/config.xml" ] || "$BIN" generate --home="$ST_HOME" >/dev/null

TS_IP=$("$TAILSCALE" ip -4)
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true   # config is edited while stopped

/usr/bin/python3 - "$ST_HOME/config.xml" "$TS_IP" <<'PY'
import sys, xml.etree.ElementTree as ET
path, ts_ip = sys.argv[1], sys.argv[2]
tree = ET.parse(path)
opts = tree.getroot().find("options")
settings = {
    "globalAnnounceEnabled": "false",
    "localAnnounceEnabled": "false",
    "relaysEnabled": "false",
    "natEnabled": "false",
    "startBrowser": "false",
    "urAccepted": "-1",
    "crashReportingEnabled": "false",
    "autoUpgradeIntervalH": "0",
}
for key, value in settings.items():
    el = opts.find(key)
    if el is None:
        el = ET.SubElement(opts, key)
    el.text = value
for el in opts.findall("listenAddress"):
    opts.remove(el)
ET.SubElement(opts, "listenAddress").text = f"tcp://{ts_ip}:22000"
gui = tree.getroot().find("gui")
gui.find("address").text = "127.0.0.1:8384"
tree.write(path)
PY

# 4. Start at login, restart on crash
mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.scribebot.syncthing</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN</string>
    <string>serve</string>
    <string>--home=$ST_HOME</string>
    <string>--no-browser</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$DATA/logs/syncthing.log</string>
  <key>StandardErrorPath</key><string>$DATA/logs/syncthing.log</string>
</dict>
</plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Syncthing $("$BIN" version | awk '{print $2}') running; listening on tcp://$TS_IP:22000"
echo "M14 device ID: $("$BIN" device-id --home="$ST_HOME")"

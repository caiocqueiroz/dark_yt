#!/usr/bin/env bash
# Installs clipper/server.py as a launchd user agent (starts at login, restarts on crash).
# Needs the external disk (venv, models and work dir live there).
set -euo pipefail
cd "$(dirname "$0")/.."
root=$PWD
label=com.pavanatto.clipper
plist=~/Library/LaunchAgents/$label.plist
base=/Volumes/MacNVMe/pavanatto-cuts
python_bin=$base/clipper-venv/bin/python

[ -x "$python_bin" ] || { echo "venv missing: uv venv --python 3.12 $base/clipper-venv && VIRTUAL_ENV=$base/clipper-venv uv pip install -r clipper/requirements.txt" >&2; exit 1; }

mkdir -p logs ~/Library/LaunchAgents
cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>$python_bin</string><string>-u</string><string>$root/clipper/server.py</string></array>
  <key>WorkingDirectory</key><string>$root/clipper</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$root/logs/clipper.log</string>
  <key>StandardErrorPath</key><string>$root/logs/clipper.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
sleep 3
curl -sf "http://127.0.0.1:$(grep -E '^CLIPPER_PORT=' .env | cut -d= -f2 || echo 8788)/healthz" && echo " $label running"

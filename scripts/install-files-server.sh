#!/usr/bin/env bash
# Installs scripts/files-server.py (read-only video browser) as a launchd user agent.
# Needs the external disk (venv, models and work dir live there).
set -euo pipefail
cd "$(dirname "$0")/.."
root=$PWD
label=com.pavanatto.files
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
  <array><string>$python_bin</string><string>-u</string><string>$root/scripts/files-server.py</string></array>
  <key>WorkingDirectory</key><string>$root</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$root/logs/files-server.log</string>
  <key>StandardErrorPath</key><string>$root/logs/files-server.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
sleep 3
ip=$(grep -E '^TAILSCALE_IP=' .env | cut -d= -f2)
code=$(curl -s -o /dev/null -w '%{http_code}' "http://${ip:-127.0.0.1}:$(grep -E '^FILES_PORT=' .env | cut -d= -f2 || echo 8090)/")
[ "$code" = 401 ] && echo "$label running on http://${ip:-127.0.0.1}:8090 (auth required)" || { echo "unexpected HTTP $code"; exit 1; }

#!/usr/bin/env bash
# Installs bridge/server.mjs as a launchd user agent (starts at login, restarts on crash).
# The agent runs in the user's GUI session so the Claude Code / Codex logins (keychain) work.
set -euo pipefail
cd "$(dirname "$0")/.."
root=$PWD
label=com.pavanatto.llm-bridge
plist=~/Library/LaunchAgents/$label.plist
node_bin=$(command -v node)

mkdir -p logs ~/Library/LaunchAgents
cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>$node_bin</string><string>$root/bridge/server.mjs</string></array>
  <key>WorkingDirectory</key><string>$root</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$root/logs/llm-bridge.log</string>
  <key>StandardErrorPath</key><string>$root/logs/llm-bridge.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
sleep 2
curl -sf "http://127.0.0.1:$(grep -E '^LLM_BRIDGE_PORT=' .env | cut -d= -f2 || echo 8787)/healthz" && echo " $label running"

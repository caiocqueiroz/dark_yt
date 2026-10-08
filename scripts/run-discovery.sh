#!/usr/bin/env bash
# Runs the discovery workflow now. Usage: scripts/run-discovery.sh [--dry]   (--dry = classify only)
set -euo pipefail
cd "$(dirname "$0")/.."
auto=true; [ "${1:-}" = "--dry" ] && auto=false
token=$(grep -E '^LLM_BRIDGE_TOKEN=' .env | cut -d= -f2)
curl -sf -X POST "http://127.0.0.1:5678/webhook/radar-discovery" -H "Authorization: Bearer $token" \
  -H 'Content-Type: application/json' -d "{\"auto_process\":$auto}" && echo
echo "started; follow with: scripts/show-execution.py last"

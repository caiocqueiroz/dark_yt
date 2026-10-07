#!/usr/bin/env bash
# Triggers PAVANATO - 01 - PILOT on the running n8n (production webhook).
# Usage: scripts/run-pilot.sh <youtube_url> [--force]
set -euo pipefail
cd "$(dirname "$0")/.."
url=${1:?usage: scripts/run-pilot.sh <youtube_url> [--force]}
force=false; [ "${2:-}" = "--force" ] && force=true
token=$(grep -E '^LLM_BRIDGE_TOKEN=' .env | cut -d= -f2)
curl -sf -X POST "http://127.0.0.1:5678/webhook/pavanato-pilot" \
  -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  -d "{\"source_url\":\"$url\",\"force_reprocess\":$force}" && echo
echo "started; follow with: scripts/show-execution.py last"

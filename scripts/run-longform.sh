#!/usr/bin/env bash
# Triggers the long-form workflow on the running n8n (production webhook).
# Usage: scripts/run-longform.sh <youtube_url> [--subject "Nome Sobrenome"] [--notes "..."] [--mode best_of|continuous] [--minutes N] [--force] [--no-upload]
set -euo pipefail
cd "$(dirname "$0")/.."
url=${1:?usage: scripts/run-longform.sh <youtube_url> [--subject NAME] [--notes TEXT] [--mode best_of|continuous] [--minutes N] [--force] [--no-upload]}
shift
force=false; upload=true; subject=''; notes=''; mode=best_of; minutes=5
while [ $# -gt 0 ]; do
  case $1 in
    --force) force=true ;;
    --no-upload) upload=false ;;
    --subject) subject=$2; shift ;;
    --notes) notes=$2; shift ;;
    --mode) mode=$2; shift ;;
    --minutes) minutes=$2; shift ;;
    *) echo "unknown option $1" >&2; exit 1 ;;
  esac
  shift
done
token=$(grep -E '^LLM_BRIDGE_TOKEN=' .env | cut -d= -f2)
body=$(python3 -c 'import json,sys; a=sys.argv; d={"source_url":a[1],"force_reprocess":a[2]=="true","upload":a[3]=="true","mode":a[6],"target_minutes":float(a[7])}
if a[4]: d["subject_name"]=a[4]
if a[5]: d["subject_notes"]=a[5]
print(json.dumps(d))' "$url" "$force" "$upload" "$subject" "$notes" "$mode" "$minutes")
curl -sf -X POST "http://127.0.0.1:5678/webhook/radar-longform" \
  -H "Authorization: Bearer $token" -H 'Content-Type: application/json' -d "$body" && echo
echo "started; follow with: scripts/show-execution.py last"

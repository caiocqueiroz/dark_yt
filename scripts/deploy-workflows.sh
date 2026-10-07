#!/usr/bin/env bash
# Imports workflows/*.json into n8n and publishes them (n8n 2.x runs the PUBLISHED version,
# `import:workflow` alone only updates the draft). Restarts n8n so webhooks/triggers reload.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH
for f in workflows/*.json; do
  id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$f")
  docker cp "$f" pavanatto-n8n:/tmp/wf.json
  docker exec pavanatto-n8n n8n import:workflow --input=/tmp/wf.json >/dev/null
  docker exec pavanatto-n8n n8n publish:workflow --id="$id" | tail -1
done
# Two restarts: right after the first one, n8n has served the previously published version once.
for r in 1 2; do
  docker compose restart >/dev/null 2>&1
  for i in $(seq 1 30); do curl -sf http://127.0.0.1:5678/healthz >/dev/null && break; sleep 2; done
  sleep 3
done
echo "deployed"

#!/usr/bin/env bash
# Prints a fresh YouTube access token for the n8n credential (refresh-token grant). Never logs it.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH
rt=$(docker exec pavanatto-n8n n8n export:credentials --id=youtubeOAuth00001 --decrypted 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["data"]["oauthTokenData"]["refresh_token"])')
curl -sf https://oauth2.googleapis.com/token \
  --data-urlencode "client_id=$(grep -E '^GOOGLE_CLIENT_ID=' .env | cut -d= -f2)" \
  --data-urlencode "client_secret=$(grep -E '^GOOGLE_CLIENT_SECRET=' .env | cut -d= -f2)" \
  --data-urlencode "refresh_token=$rt" -d grant_type=refresh_token \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'

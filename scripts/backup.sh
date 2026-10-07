#!/usr/bin/env bash
# Backs up the n8n volume and exports workflows to workflows/.
# The archive contains encrypted credentials: keep it local, never commit it.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH

backup_dir=$(grep -E '^BACKUP_DIR=' .env | cut -d= -f2-)
backup_dir=${backup_dir:-$PWD/backups}
[ -d "$backup_dir" ] || { echo "backup dir not found: $backup_dir (disk unmounted?)" >&2; exit 1; }

stamp=$(date +%Y%m%d-%H%M%S)
if docker exec pavanatto-n8n n8n export:workflow --all --separate --pretty --output=/home/node/.n8n/wf-export/ >/dev/null 2>&1; then
  docker cp pavanatto-n8n:/home/node/.n8n/wf-export/. workflows/
else
  echo "no workflows exported (none exist yet?)" >&2
fi
docker exec pavanatto-n8n rm -rf /home/node/.n8n/wf-export

docker run --rm -v pavanatto_n8n_data:/data -v "$backup_dir":/backup alpine \
  tar czf "/backup/n8n_data-$stamp.tgz" -C /data .
ls -1t "$backup_dir"/n8n_data-*.tgz | tail -n +15 | xargs -r rm --
echo "$backup_dir/n8n_data-$stamp.tgz"

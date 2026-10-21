#!/usr/bin/env bash
# Restore a backup made by backup.sh:  ./restore.sh backups/backup-YYYYMMDD-HHMMSS.tar.gz
# Restores the database and .env, then restarts the bot.
set -euo pipefail
cd "$(dirname "$0")"
file="${1:?usage: ./restore.sh backups/backup-YYYYMMDD-HHMMSS.tar.gz}"
[ -f "$file" ] || { echo "no such file: $file"; exit 1; }
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
tar xzf "$file" -C "$tmp"
[ -f "$tmp/outflow_alert.db" ] || { echo "backup has no database"; exit 1; }

if [ -f .env ]; then cp .env ".env.before-restore"; fi
cp "$tmp/.env" .env && chmod 600 .env

docker compose stop outflow-alert || true
docker compose run --rm --no-deps -T -u root -v "$tmp:/restore:ro" --entrypoint sh outflow-alert -c '
  rm -f /data/outflow_alert.db /data/outflow_alert.db-wal /data/outflow_alert.db-shm &&
  cp /restore/outflow_alert.db /data/outflow_alert.db &&
  chown 10001 /data/outflow_alert.db'
docker compose up -d
echo "restored from $file; bot restarted"

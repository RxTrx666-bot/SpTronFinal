#!/usr/bin/env bash
# Back up the bot's database and .env into ./backups/backup-<UTC time>.tar.gz (keeps the newest 30).
# Safe while the bot is running: SQLite's online backup makes a consistent copy.
set -euo pipefail
cd "$(dirname "$0")"
KEEP="${KEEP:-30}"
stamp="$(date -u +%Y%m%d-%H%M%S)"
mkdir -p backups
chmod 700 backups
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

docker compose exec -T outflow-alert python -c "
import sqlite3
src = sqlite3.connect('/data/outflow_alert.db')
dst = sqlite3.connect('/data/.backup.db')
src.backup(dst)
dst.close(); src.close()
"
docker cp "$(docker compose ps -q outflow-alert):/data/.backup.db" "$tmp/outflow_alert.db"
docker compose exec -T outflow-alert rm -f /data/.backup.db
cp .env "$tmp/.env"

out="backups/backup-$stamp.tar.gz"
tar czf "$out" -C "$tmp" outflow_alert.db .env
chmod 600 "$out"
ls -1t backups/backup-*.tar.gz | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "saved $out ($(du -h "$out" | cut -f1))"

#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
set -a
if test -f .env; then . ./.env; fi
set +a
backup_dir="${1:-backups/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$backup_dir"
docker compose exec -T postgres pg_dump -U fishagent -d fishagent --format=custom > "$backup_dir/fishagent.dump"
echo "PostgreSQL backup written to $backup_dir/fishagent.dump"
PYTHONPATH=src uv run python scripts/runtime_backup.py backup "$backup_dir"
echo "MinIO objects and Redis namespaces backed up under $backup_dir"

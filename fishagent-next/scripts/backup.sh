#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
backup_dir="${1:-backups/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$backup_dir"
docker compose exec -T postgres pg_dump -U fishagent -d fishagent --format=custom > "$backup_dir/fishagent.dump"
echo "PostgreSQL backup written to $backup_dir/fishagent.dump"

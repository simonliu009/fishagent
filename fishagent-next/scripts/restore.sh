#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
set -a
if test -f .env; then . ./.env; fi
set +a
dump_file="${1:?usage: ./scripts/restore.sh path/to/fishagent.dump}"
backup_dir="$dump_file"
if test -d "$dump_file"; then
  backup_dir="$dump_file"
  dump_file="$backup_dir/fishagent.dump"
else
  backup_dir="$(dirname "$dump_file")"
fi
test -f "$dump_file" || { echo "backup file not found: $dump_file" >&2; exit 1; }
docker compose exec -T postgres pg_restore -U fishagent -d fishagent --clean --if-exists < "$dump_file"
echo "PostgreSQL backup restored from $dump_file"
PYTHONPATH=src uv run python scripts/runtime_backup.py restore "$backup_dir"
echo "MinIO objects and Redis namespaces restored from $backup_dir"

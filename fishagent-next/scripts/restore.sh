#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
dump_file="${1:?usage: ./scripts/restore.sh path/to/fishagent.dump}"
test -f "$dump_file" || { echo "backup file not found: $dump_file" >&2; exit 1; }
docker compose exec -T postgres pg_restore -U fishagent -d fishagent --clean --if-exists < "$dump_file"
echo "PostgreSQL backup restored from $dump_file"

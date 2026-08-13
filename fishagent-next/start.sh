#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
if [ "$#" -gt 1 ]; then
  echo "usage: $0 [port]" >&2
  exit 2
fi
port="${1:-${FISHAGENT_PORT:-3000}}"
if ! [[ "$port" =~ ^[0-9]+$ ]] || ((10#$port < 1 || 10#$port > 65535)); then
  echo "invalid port: $port (expected 1-65535)" >&2
  exit 2
fi
export PYTHONPATH=src
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-/tmp/uv-python}"
echo "$$" > .fishagent.pid
echo "FishAgent Web listening on ${FISHAGENT_HOST:-0.0.0.0}:$port"
exec uv run uvicorn fishagent.web.app:app --host "${FISHAGENT_HOST:-0.0.0.0}" --port "$port"

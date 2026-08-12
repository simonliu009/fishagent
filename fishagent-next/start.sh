#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export PYTHONPATH=src
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-/tmp/uv-python}"
echo "$$" > .fishagent.pid
exec uv run python -m fishagent.web.server --host "${FISHAGENT_HOST:-0.0.0.0}" --port "${FISHAGENT_PORT:-3008}"

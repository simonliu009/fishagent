#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
docker compose up -d --build
uv sync --extra agent
uv run alembic upgrade head
echo "基础服务、Web、Worker、Beat 和 MQTT 已启动。"

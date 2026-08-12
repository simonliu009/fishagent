#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
docker compose up -d postgres redis minio
uv sync
echo "基础服务已启动。将 .env.example 复制为 .env 后运行 ./start.sh。"

# 运维手册

## 启停

```bash
cd fishagent-next
cp .env.example .env
./scripts/bootstrap.sh
./stop.sh
```

外部入口是 `http://localhost:3001`，应用直连检查是 `http://localhost:3000/health/ready`。`health/live` 只表示进程存活，`health/ready` 会检查 PostgreSQL、Redis 和 MinIO 能力。

## 故障处理

- PostgreSQL 不可用：停止写请求，先恢复数据库；不要清理 Compose volume。
- Redis 不可用：实时事件和 Celery 会延迟，但已保存的 PostgreSQL 事实不应丢失。
- MinIO 不可用：证据/视觉上传失败，遥测和低溶氧确定性链路仍可用。
- Worker 不可用：检查 `docker compose logs worker-default worker-vision`，恢复后由业务幂等键吸收重投。
- 摄像头不可用：页面显示 `UNAVAILABLE`，不得根据旧帧生成新结论。

## 备份与恢复

```bash
./scripts/backup.sh backups/$(date -u +%Y%m%dT%H%M%SZ)
./scripts/restore.sh backups/<timestamp>
```

备份目录包含 PostgreSQL custom dump、MinIO 对象和 Redis RDB。恢复前应停止 Web/Worker，恢复后执行 `docker compose up -d` 并检查迁移版本和 readiness。

## 发布检查

```bash
uv run pytest -q
uv run ruff check src tests migrations
uv run mypy src
docker compose config --quiet
curl --noproxy localhost,127.0.0.1 http://localhost:3001/healthz
```

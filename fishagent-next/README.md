# 智渔 Agent

根据上级目录 `智渔Agent-CrewAI-Python绿地开发计划.md` 落地的绿地垂直切片。

当前版本使用 `uv` 管理 Python 版本和项目元数据，重点覆盖：

- 养殖资产、传感器读数、设备影子状态。
- B-01 低溶氧事件闭环：感知、Agent 研判、策略门、模拟设备命令、复核、升级。
- 成功、复核失败、防重复三条演示路径。
- 只读/低风险自动/中高风险阻断的安全策略。
- HTTP API 与浏览器控制台，当前前端端口 `3008`；`3001` 保留给 nginx。
- PostgreSQL 持久化快照与 Outbox，Redis 实时事件发布，MinIO 健康探针。
- 大模型 API 配置入口、API Key 脱敏展示，以及可选的管理员登录和 CSRF 防护。

## uv 环境

```bash
cd fishagent-next
uv python pin 3.12
uv sync
PYTHONPATH=src uv run python -m fishagent.cli doctor
```

## 启动

开发机或单机部署先启动基础服务：

```bash
cd fishagent-next
./scripts/bootstrap.sh
cp .env.example .env
./start.sh
```

`.env` 默认连接本机 Docker Compose 提供的 PostgreSQL `5432`、Redis `6379` 和 MinIO `9000`。不要把真实密码或 API Key 提交到 Git。

访问：

- 控制台：http://localhost:3008
- 健康检查：http://localhost:3008/health/ready
- API 状态：http://localhost:3008/api/v1/state

当前应用直接监听 `3008`。已有 nginx 的 `3001` 端口不由本项目接管；若需要通过 nginx 访问，应将 nginx upstream 指向 `127.0.0.1:3008`。

## 运行时基础设施

- **PostgreSQL**：业务状态的最终来源，保存养殖资产、读数、事件闭环、审批、命令和调度状态；同时记录 Outbox 事件，重启后可恢复。
- **Redis**：实时事件加速通道，用于发布 `fishagent.events`；Redis 不承担业务数据最终持久化，短暂不可用时健康检查会标记 degraded。
- **MinIO**：S3 兼容的对象存储边界，供照片、视频和证据文件使用；当前切片接入健康探针，媒体上传适配器会在后续相机/视觉模块中扩展。

SQLite 适合单进程、本地演示或单元测试，所以测试仍可以通过清空 `FISHAGENT_DATABASE_URL` 使用内存存储。但生产运行需要并发写入、事务、Outbox、重启恢复和多进程部署，SQLite 的文件锁和单机边界不适合作为此系统的共享业务源；因此默认采用 PostgreSQL，而不是把 SQLite 伪装成生产持久层。

备份与恢复：

```bash
./scripts/backup.sh
./scripts/restore.sh backups/<backup-file>.dump
```

## 可选认证

本地演示默认关闭认证。部署时设置：

```dotenv
FISHAGENT_AUTH_ENABLED=true
FISHAGENT_ADMIN_USERNAME=admin
FISHAGENT_ADMIN_PASSWORD=change-me
```

登录入口为 `/api/v1/auth/login`，浏览器控制台会自动显示登录面板；写请求必须携带登录响应中的 CSRF Token。认证开启但未设置管理员密码时，`/health/ready` 返回 `503`。

## 演示接口

```bash
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3008/api/v1/demo/init
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3008/api/v1/demo/success
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3008/api/v1/demo/failure
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3008/api/v1/demo/dedup
```

资产管理接口：

```bash
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3008/api/v1/farms \
  -H 'Content-Type: application/json' \
  -d '{"id":"farm-a","name":"东区养殖场","location":"湖州"}'

curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3008/api/v1/ponds \
  -H 'Content-Type: application/json' \
  -d '{"id":"P-01","farm_id":"farm-a","name":"P-01 精养池","species":"草鱼"}'
```

传入真实形态的遥测批量读数：

```bash
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3008/api/v1/telemetry/readings:batch \
  -H 'Content-Type: application/json' \
  -d '{"readings":[{"pond_id":"B-01","metric":"DO","value":2.1,"source_event_id":"manual-001"}]}'
```

审批、人工任务和调度接口：

```bash
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3008/api/v1/demo/approval
curl --noproxy localhost,127.0.0.1 http://localhost:3008/api/v1/approvals
curl --noproxy localhost,127.0.0.1 http://localhost:3008/api/v1/manual-tasks
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3008/api/v1/scheduled-jobs:dispatch
```

`POST /api/v1/action-proposals/{id}/approve` 只允许已创建的 L2 提案进入设备执行；L3 只会创建人工任务。服务进程每 5 秒运行一次轻量调度循环，复核和巡查作业也可以通过 `scheduled-jobs:dispatch` 显式触发。

## 测试

```bash
python3 -m unittest discover -s tests
```

或：

```bash
PYTHONPATH=src uv run python -m unittest discover -s tests
```

## 边界说明

当前是可运行的模块化单体垂直切片：PostgreSQL、Redis 和 MinIO 已接入运行时；调度仍是进程内轻量循环，尚未替换为 Celery Worker/Beat；Agent 编排仍由本地应用服务实现，尚未替换为 CrewAI；页面使用现有 HTTP 控制台，尚未替换为 NiceGUI。真实设备/MQTT、相机视觉和媒体上传仍是后续适配边界。

当前实现不在启动时隐式 seed；演示数据通过页面按钮、`/api/v1/demo/init` 或 demo 命令显式初始化。大模型配置保存到 `data/runtime_config.json`，API 响应不会回显完整密钥。

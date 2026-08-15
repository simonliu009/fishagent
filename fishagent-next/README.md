# 智渔 Agent

根据上级目录 `智渔Agent-CrewAI-Python绿地开发计划.md` 落地的绿地垂直切片。

当前版本使用 `uv` 管理 Python 版本和项目元数据，采用 FastAPI/Uvicorn Web、Celery Worker/Beat 和模块化领域服务，重点覆盖：

- 养殖资产、传感器读数、设备影子状态。
- 四池塘演示场景：B-01 至 B-04 各配套氨氮、亚硝酸根离子、浊度、叶绿素、溶解氧、pH、水温传感器，以及增氧机、投喂机、阀门、摄像头和 24 小时趋势模拟数据；总计 28 台设备中 27 台在线。
- 默认初始化页面保持正常，异常通过监控总览右下角的 Agent Demo 浮动入口注入；常规演示保留低溶氧和双传感器失效，多模态案例纳入巡塘 SOP。
- 水面/水下摄像头、天气上下文和病害知识库模拟数据，支持浮头、病害、摄食异常和天气防护四个多模态 Agent 案例。
- 只读/低风险自动/中高风险阻断的安全策略。
- HTTP API 与浏览器控制台，当前端口 `3000`；`3001` 保留给 Nginx。
- 智渔 AI 流式聊天、Agent 运行轨迹、告警闭环、人工任务、主动传感器上报和 MQTT 设备控制。
- 每日报告手动生成、历史版本管理、HTML 下载与删除；调度服务每天本地时间 `23:59` 自动生成当日报告。
- PostgreSQL 持久化快照与 Outbox，Redis 实时事件发布，MinIO 健康探针。
- Celery Beat 到期任务分发、默认 Worker 执行和 MQTT 遥测适配器。
- CrewAI/LLM 主决策运行时，设备写操作仍只能经过确定性策略门。
- FastAPI OpenAPI、WebSocket 事件回放、S3/MinIO 证据上传和签名下载地址。
- 大模型 API 配置入口、API Key 脱敏展示，以及可选的管理员登录和 CSRF 防护。

## uv 环境

```bash
cd fishagent-next
uv python pin 3.12
uv sync --extra agent
PYTHONPATH=src uv run python -m fishagent.cli doctor
```

## 启动

开发机或单机部署先启动基础服务：

```bash
cd fishagent-next
cp .env.example .env
./scripts/bootstrap.sh
```

`.env` 默认连接本机 Docker Compose 提供的 PostgreSQL `5432`、Redis `6379`、MinIO `9000` 和 MQTT `1883`。不要把真实密码或 API Key 提交到 Git。

完整 Compose 会启动 `web`、`worker-default`、`worker-vision`、`beat`、`postgres`、`redis`、`minio` 和 `mqtt`。仅运行 Python Web 进程时可使用 `./start.sh [port]`，端口参数默认是 `3000`，也可以通过 `FISHAGENT_PORT` 配置。

```bash
./start.sh          # 监听 3000
./start.sh 3010     # 监听 3010
```

访问：

- 控制台：http://localhost:3000
- 公网控制台：`http://<服务器公网 IP>:3000`
- nginx 公共入口：http://localhost:3001
- 健康检查：http://localhost:3000/health/ready
- API 状态：http://localhost:3000/api/v1/state
- OpenAPI：http://localhost:3000/api/docs

应用进程通过 Docker 发布到 `0.0.0.0:3000`，可由公网和 Tailscale 直接访问；nginx 入口监听 `3001` 并反代到 `127.0.0.1:3000`。配置模板位于 `deploy/nginx/fishagent-3001.conf`。公网部署应启用认证并在云安全列表中只放行必要来源。

完整 Compose 启动后，`beat` 和 `worker-default` 会持续运行后台调度；每日 `23:59` 自动日报依赖这两个服务。只使用 `./start.sh` 启动 Web 进程时，页面和 API 可用，但不会单独启动 Celery Beat/Worker。

## 控制台视图

浏览器控制台当前包含以下主要入口：

- **监控总览**：四个池塘的水质卡片、监控画面、告警列表、Agent 执行中心、逐塘巡检和 Agent Demo 注入。
- **智渔 AI**：与 CrewAI Agent 对话，支持流式输出、池塘范围选择和 Agent 运行轨迹查看。
- **库存补货**：浏览库存、低库存项和待确认补货单。
- **养殖管理**：人工任务、审批和设备执行情况。
- **数据分析**：水质监控面板和按传感器切换的 12/24 小时趋势图。
- **资产录入**：管理养殖场、区域、池塘、传感器、设备和摄像头。
- **调度中心 / 系统审计 / 每日报告 / 知识库**：分别管理周期作业、审计事件、日报历史和行业知识文档。

页面右上角的大模型设置入口用于管理供应商、模型、Base URL、API Key、空响应重试次数和启用状态。API Key 只保存脱敏状态，不会在普通状态接口中返回完整值。

## 运行时基础设施

- **PostgreSQL**：业务状态的最终来源，保存养殖资产、读数、事件闭环、审批、命令和调度状态；同时记录 Outbox 事件，重启后可恢复。当前兼容快照和关系投影在同一事务内写入，领域表由 Alembic 管理。
- **Redis**：实时事件加速通道，用于发布 `fishagent.events`；Redis 不承担业务数据最终持久化，短暂不可用时健康检查会标记 degraded。
- **MQTT/Mosquitto**：本地 IoT Broker，mock 遥测通过 MQTT 上报，模型决策后的设备命令通过 MQTT 发布；默认监听 `127.0.0.1:1883`。
- **MinIO**：S3 兼容对象存储，保存证据文件和经过校验的相机视觉帧，并提供短期签名下载；它不参与业务表查询和事务。

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

## Agent、队列与接入

设置 `FISHAGENT_LLM_ENABLED=true` 和 API Key 后，用户目标和事件闭环会进入 CrewAI 主决策、传感器监控、巡查分析、视觉与病害分析和行动规划 Agent；模型只输出结构化决策，设备指令由执行边界发布到 MQTT。未配置模型时不会使用硬编码规则自动控制设备，而是转人工任务。

### 多模态案例与决策流程

监控总览右下角的 Agent Demo 浮动入口可以单独或按顺序演示以下场景：

1. 水面摄像头发现 B-01 浮头，Agent 结合低风速天气判断缺氧风险，策略门允许打开 B-01 增氧机并进入复核。
2. 水下摄像头发现 B-02 鳃部异常，Agent 检索病害知识库，因不能自动确诊或投药而提交人工任务。
3. B-03 摄像头发现摄食响应弱，Agent 结合投喂上下文提出关闭投喂机，策略门校验后通过 MQTT 执行。
4. B-04 水面摄像头发现天气锋面风险，Agent 建议关闭进排水阀，因风险等级需要人工审批后再执行。

统一流程为：摄像头/天气/知识库感知 -> CrewAI 主决策动态委派专职 Agent -> 结构化动作或人工建议 -> 确定性策略门校验能力、风险、审批、幂等和复核 -> MQTT 发布设备命令或创建人工任务 -> 回读影子状态并写入审计。案例视图会展示每个节点、证据和最终处理结果。

OpenRouter 免费路由预设写在 `.env.example`。复制为 `.env` 后，将 `FISHAGENT_LLM_API_KEY=sk-or-v1-REPLACE_WITH_YOUR_KEY` 替换为真实 Key；提供商使用 `openrouter`，模型使用 `openrouter/free`。Base URL 为 `https://openrouter.ai/api/v1`，也可以在右上角模型设置中粘贴完整的 `https://openrouter.ai/api/v1/chat/completions`，系统会自动规范化路径。

Celery Beat 每 5 秒调用 `dispatch_due_jobs`，通过 PostgreSQL 状态和业务幂等键领取到期复核/巡查作业，再交给 Worker 执行。相同调度循环会在本地时间每天 `23:59` 检查并生成当日报告；自动报告带有生成标记，同一日期不会因 Beat 的重复轮询而重复生成。Redis 只负责队列和实时加速，Outbox 事件号由 PostgreSQL 全局序列分配，支持多进程并发写入；Web 读请求会刷新最新快照但不回写，避免轮询覆盖 Worker 状态。

MQTT 主题格式为 `farms/{farm_id}/ponds/{pond_id}/sensors/{sensor_id}`，消息示例：

```json
{"metric":"AMMONIA","unit":"mg/L","value":0.18,"source_event_id":"mqtt-001"}
```

点击“立即巡查”或执行周期巡查时，系统会先向每个传感器的
`farms/{farm_id}/ponds/{pond_id}/sensors/{sensor_id}/commands` 发布
`{"action":"REPORT_NOW",...}`，再等待传感器通过标准读数主题回传本轮数据；巡查分析只使用这批主动请求产生的最新读数。演示环境中的模拟传感器也通过本地 MQTT Broker 完成请求和回传。

实时事件可通过 `WS /events?after={sequence}` 断线续传；HTTP 事件补齐接口为 `GET /api/v1/events?after={sequence}`。

## 演示接口

默认演示数据通过显式初始化或注入产生，不会在每次 Web 启动时隐式覆盖数据库状态。控制台推荐从监控总览右下角的浮动入口操作：

1. **重置演示数据**：恢复四个池塘的正常背景数据，不触发自动处置。
2. **低溶氧自动处置**：注入 B-01 低溶氧，Agent 决策开启增氧机，随后进入延迟复核。
3. **双传感器失效**：同时注入溶解氧和氨氮异常，演示异常证据聚合和人工任务转派。
4. **多模态案例**：将水面/水下图片、天气、知识库和传感器数据纳入巡塘 SOP，形成真实 Agent 运行轨迹。

Demo 注入后页面会显示注入反馈并触发逐塘巡检；告警列表按事件逐条展示，决策、执行和复核结果会随着流程推进显示。

也可以通过 API 操作：

```bash
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3000/api/v1/demo/init
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3000/api/v1/demo/success
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3000/api/v1/demo/alerts
curl --noproxy localhost,127.0.0.1 http://localhost:3000/api/v1/demo/options
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3000/api/v1/analysis-cases/case-floating-head-weather/run
```

用于回归测试的其它路径仍可通过 Demo API 调用：

```bash
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3000/api/v1/demo/failure
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3000/api/v1/demo/dedup
```

资产管理接口：

```bash
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3000/api/v1/farms \
  -H 'Content-Type: application/json' \
  -d '{"id":"farm-a","name":"东区养殖场","location":"湖州"}'

curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3000/api/v1/ponds \
  -H 'Content-Type: application/json' \
  -d '{"id":"P-01","farm_id":"farm-a","name":"P-01 精养池","species":"草鱼"}'
```

传入真实形态的遥测批量读数：

```bash
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3000/api/v1/telemetry/readings:batch \
  -H 'Content-Type: application/json' \
  -d '{"readings":[{"pond_id":"B-01","sensor_id":"ammonia-b-01","metric":"AMMONIA","unit":"mg/L","value":0.18,"source_event_id":"manual-001"}]}'
```

审批、人工任务和调度接口：

```bash
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3000/api/v1/demo/approval
curl --noproxy localhost,127.0.0.1 http://localhost:3000/api/v1/approvals
curl --noproxy localhost,127.0.0.1 http://localhost:3000/api/v1/manual-tasks
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3000/api/v1/scheduled-jobs:dispatch
```

每日报告接口：

```bash
curl --noproxy localhost,127.0.0.1 http://localhost:3000/api/v1/reports
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3000/api/v1/reports/generate \
  -H 'Content-Type: application/json' -d '{}'
curl --noproxy localhost,127.0.0.1 http://localhost:3000/api/v1/reports/<report_id>/download -o daily-report.html
curl --noproxy localhost,127.0.0.1 -X DELETE http://localhost:3000/api/v1/reports/<report_id>
```

`POST /api/v1/reports/generate` 生成当前本地日期的报告；报告正文包含真实快照趋势图、告警、自动操作、人工任务和设备操作日志，不包含知识库正文。知识文档在独立的知识库视图中管理。

`POST /api/v1/action-proposals/{id}/approve` 只允许已创建的 L2 提案进入设备执行；L3 只会创建人工任务。完整 Compose 中的 Celery Beat 每 5 秒触发一次调度，复核和巡查作业也可以通过 `scheduled-jobs:dispatch` 显式触发。

## 测试

```bash
uv run pytest -q
uv run ruff check src tests migrations
```

如需只运行报告和自动日报测试：

```bash
uv run pytest -q tests/test_reports.py
```

## 边界说明

当前是可运行的模块化单体：PostgreSQL、Redis、MinIO、FastAPI、Celery Worker/Beat、MQTT 和 CrewAI/LLM 已接入运行时；HTTP Snapshot/RTSP 抽帧、视觉上传校验和视觉 Worker 已接入。天气、摄像头、设备和传感器默认使用模拟适配器，真实厂商设备协议、模型评测与提示词版本治理、疾病/投喂专业规则和多用户持久化目录仍是后续扩展。

当前实现不在启动时隐式 seed；演示数据通过页面按钮、`/api/v1/demo/init` 或 Demo API 显式初始化。大模型配置保存到 `data/runtime_config.json`，API 响应不会回显完整密钥。不要将 `.env`、真实 API Key、运行时数据或备份文件提交到 Git。

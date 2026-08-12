# 智渔 Agent

根据上级目录 `智渔Agent-CrewAI-Python绿地开发计划.md` 落地的绿地垂直切片。

当前版本使用 `uv` 管理 Python 版本和项目元数据，并保留无外部依赖的可运行原型入口，重点覆盖：

- 养殖资产、传感器读数、设备影子状态。
- B-01 低溶氧事件闭环：感知、Agent 研判、策略门、模拟设备命令、复核、升级。
- 成功、复核失败、防重复三条演示路径。
- 只读/低风险自动/中高风险阻断的安全策略。
- HTTP API 与浏览器控制台，当前前端端口 `3008`；`3001` 保留给 nginx。

## uv 环境

```bash
cd fishagent-next
uv python pin 3.12
uv sync
PYTHONPATH=src uv run python -m fishagent.cli doctor
```

## 启动

```bash
cd fishagent-next
./start.sh
```

访问：

- 控制台：http://localhost:3008
- 健康检查：http://localhost:3008/health/ready
- API 状态：http://localhost:3008/api/v1/state

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

文档要求的 PostgreSQL、Redis、Celery、CrewAI、NiceGUI、MinIO 在本切片中以边界和事件契约预留，但尚未接入真实服务；当前运行时使用内存领域存储和进程内调度器，重启后业务状态不会恢复。当前实现不在启动时隐式 seed；演示数据通过页面按钮、`/api/v1/demo/init` 或 demo 命令显式初始化。大模型配置保存到 `data/runtime_config.json`，API 响应不会回显完整密钥。

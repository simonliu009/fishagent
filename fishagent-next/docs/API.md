# API 契约摘要

完整机器可读契约由运行中的 FastAPI OpenAPI 提供：`GET /api/openapi.json`（兼容地址 `/openapi.json`），交互文档：`/api/docs`。

## 公共状态

- `GET /health/live`
- `GET /health/ready`
- `GET /api/v1/state`
- `GET /api/v1/events?after={sequence}`
- `GET /api/v1/telemetry/snapshot`
- `GET /api/v1/telemetry/series?pond_id=B-01&metric=DO`
- `GET /api/v1/audit-events`
- `GET /api/v1/audit-events/export?format=json|csv`，导出前会留下审计记录

## 资产与接入

- `GET|POST /api/v1/farms|zones|ponds|sensors|devices|cameras`
- `GET /api/v1/sensors/{sensor_id}/health`
- `POST /api/v1/telemetry/readings:batch`
- MQTT 读数：`farms/{farm_id}/ponds/{pond_id}/sensors/{sensor_id}`
- MQTT 传感器即时上报请求：`farms/{farm_id}/ponds/{pond_id}/sensors/{sensor_id}/commands`，巡塘发布 `{"action":"REPORT_NOW",...}` 后等待传感器通过读数主题回传。
- MQTT 设备指令：`fishagent/ponds/{pond_id}/devices/{device_id}/commands`
  - payload：`command=set_state`、`target_state`、`idempotency_key`、`source=fishagent.execution-agent`
- `POST /api/v1/evidence`
- `GET /api/v1/evidence/{object_name}`
- `POST /api/v1/cameras/{camera_id}/capture`
- `POST /api/v1/cameras/{camera_id}/upload`
- `POST /api/v1/cameras/{camera_id}/analyze`

## 闭环与 Agent

- `GET /api/v1/incidents/{incident_id}/timeline`
- `POST /api/v1/incidents/{incident_id}/approve|reject|assign|verify`
- `POST /api/v1/device-commands`，支持 `Idempotency-Key`
- `GET /api/v1/device-commands/{command_id}`
- `POST /api/v1/agent-runs`、`GET /api/v1/agent-runs/{run_id}`
- `GET /api/v1/agent-runs/{run_id}/steps`
- `POST /api/v1/agent-runs/{run_id}/cancel`
- `GET|POST /api/v1/schedules`
- `POST /api/v1/schedules/{schedule_id}/pause|resume|run-now`

写请求使用 Problem Details 错误格式，并在认证开启时要求 HttpOnly Session Cookie 和 CSRF Token。

# 智渔 Agent 架构说明

## 运行边界

```text
Browser
  -> Nginx :3001
  -> FastAPI/NiceGUI :3000
       |-- PostgreSQL: domain snapshot, relational projection, outbox
       |-- Redis: Celery broker/result and live pub/sub acceleration
       |-- MinIO: evidence and camera frame objects
       |-- MQTT: sensor ingress + IoT command pub
       `-- Celery workers: default, vision, beat
```

Web、Worker 和 Beat 共享领域服务，但不通过内部 HTTP 调用彼此。PostgreSQL 是业务事实源，Redis 断开时不应丢失已提交的业务事件；MinIO 只保存二进制对象，数据库保存对象引用、哈希和尺寸。

Compose 自带本地 Mosquitto Broker，监听 `127.0.0.1:1883`：演示遥测先发布到 `farms/{farm_id}/ponds/{pond_id}/sensors/{sensor_id}`，再由订阅适配器消费；设备动作发布到 `fishagent/ponds/{pond_id}/devices/{device_id}/commands`。设备网关默认模拟 ACK，便于本地闭环验收。

## 安全边界

CrewAI 可以读取结构化证据、委派专职 Agent 和形成动作建议，但没有设备写工具。所有设备写操作必须经过 `evaluate_action`，检查设备归属、能力、证据新鲜度、阈值、目标状态、风险等级、审批和幂等键。L3 只能产生人工任务。

## 可靠性边界

- HTTP/MQTT 读数通过 `source_event_id` 去重。
- Outbox 事件与快照在一次 PostgreSQL 保存操作中落库。
- Celery 任务以业务幂等键执行，Worker 重启时恢复过期 RUNNING 作业。
- WebSocket 使用 PostgreSQL 事件序列补齐断线期间的事件。
- 视觉帧在进入视觉适配器前校验格式、大小、尺寸和 SHA-256；不可用或过期时不产生视觉结论。

## 端口

- 外部浏览器入口：`3001`，由 Nginx 反代。
- 应用进程：`3000`，只绑定本机，便于统一经过 Nginx 访问。
- 生产决策：CrewAI/LLM 输出结构化动作；MQTT 网关发布设备指令，模型不直接持有设备写工具。
- LLM 不可用：自动处置停止并创建人工任务，不回退到硬编码动作。

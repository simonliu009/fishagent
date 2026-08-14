# 大模型 API 配置入口

当前系统使用 OpenAI-compatible 模型配置驱动 CrewAI Agent。配置更新后立即刷新 Web 进程中的模型编排器。

## 环境变量

- `FISHAGENT_TIMEZONE`：聊天证据时间显示时区，默认 `Asia/Shanghai`；模型回答禁止使用 UTC 标记。
- `FISHAGENT_LLM_PROVIDER`：`zai`、`openai` 或 `compatible`。
- `FISHAGENT_LLM_BASE_URL`：模型 API Base URL。
- `FISHAGENT_LLM_MODEL`：模型名。
- `FISHAGENT_LLM_API_KEY`：API Key。
- `FISHAGENT_LLM_ENABLED`：是否启用模型调用。
- `FISHAGENT_LLM_CHAT_RETRY_COUNT`：聊天模型返回空响应时的重试次数，默认 `3`；设置为 `0` 关闭重试，最大为 `10`。
- `FISHAGENT_AGENT_DECISION_TIMEOUT_SECONDS`：单次告警 LLM 决策预算，默认 `300` 秒。

## HTTP API

- `GET /api/v1/config/llm`
- `POST /api/v1/config/llm`
- `POST /api/v1/config/llm/test`：使用已保存的 API Key 请求 OpenAI-compatible `/models`，只返回连通性和 HTTP 状态。

启用模型后，事件闭环和用户目标由 CrewAI/LLM 产生结构化决策。设备动作通过 `FISHAGENT_MQTT_COMMAND_TOPIC` 发布到 MQTT，策略门仍负责协议、风险、审批、幂等和复核约束。模型不可用时不会使用硬编码规则代替模型执行，而是转人工。

写入示例：

```bash
curl --noproxy localhost,127.0.0.1 -X POST http://localhost:3000/api/v1/config/llm \
  -H 'Content-Type: application/json' \
  -d '{"provider":"zai","base_url":"https://api.z.ai/api/paas/v4","model":"glm-4.5","api_key":"sk-***","enabled":true}'
```

API 响应不会回显完整密钥，只返回是否已配置和前缀预览。

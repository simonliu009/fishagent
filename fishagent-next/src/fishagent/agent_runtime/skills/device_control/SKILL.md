---
name: device-control
description: Execute an already validated low-risk device decision through the configured MQTT device gateway.
---

# Device Control Skill

Use this skill only after the LLM has returned a validated `IncidentDecision` and the execution agent has selected `EXECUTE`.

1. Require a device in the same pond as the incident.
2. Require `target_state` to be `on` or `off` and keep the decision risk at `L1`.
3. Run the deterministic policy gate before any write.
4. Publish the command through the configured `DeviceGateway`; in the demo deployment this publishes the MQTT IoT command.
5. Return the command acknowledgement to the execution agent so the incident can enter verification or escalate.

The skill never accepts free-form model text as an executable command and never bypasses idempotency, evidence, approval, or device-health checks.

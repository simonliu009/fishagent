---
name: device-control
description: Validate an interpreted low-risk device decision at the execution boundary and send it through the configured MQTT device gateway.
---

# Device Control Skill

Use this skill when the model output has been interpreted as an action and the execution agent has selected `EXECUTE`. This is the final feasibility boundary: model formatting and aliases are handled before the Skill, while device, state, risk, pond, evidence, policy, idempotency, and gateway checks happen here.

1. Require a device in the same pond as the incident.
2. Require `target_state` to be `on` or `off` and keep the decision risk at `L1`.
3. Run the deterministic policy gate before any write.
4. Publish the command through the configured `DeviceGateway`; in the demo deployment this publishes the MQTT IoT command.
5. Return the command acknowledgement to the execution agent so the incident can enter verification or escalate.

The skill never accepts free-form model text as an executable command and never bypasses idempotency, evidence, or approval checks. An unhealthy device is a visible execution risk, but it does not by itself block an L1 `EXECUTE` that passes the policy gate; publish the MQTT command and preserve the health warning in the result.

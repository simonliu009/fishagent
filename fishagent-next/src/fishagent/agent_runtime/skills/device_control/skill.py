from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fishagent.domain.models import AgentRun, DeviceCommand, Incident, RiskLevel

if TYPE_CHECKING:
    from fishagent.agent_runtime.contracts import IncidentDecision
    from fishagent.application.agent_service import FishAgentSystem


class DeviceControlSkill:
    """Execution-agent skill for policy-checked MQTT device control."""

    name = "device-control"
    _instructions_path = Path(__file__).with_name("SKILL.md")

    def __init__(self, system: FishAgentSystem) -> None:
        self.system = system

    @property
    def instructions(self) -> str:
        return self._instructions_path.read_text(encoding="utf-8")

    def execute(
        self,
        run: AgentRun,
        incident: Incident,
        decision: IncidentDecision,
        multimodal_evidence: bool = False,
    ) -> DeviceCommand:
        if decision.action != "EXECUTE":
            raise ValueError("device-control skill requires an EXECUTE decision")
        if RiskLevel(decision.risk) != RiskLevel.L1:
            raise ValueError("device-control skill only accepts L1 actions")
        run.step(
            "execution-agent",
            "call_skill",
            "调用 device-control Skill，通过策略门发布 MQTT 控制消息",
            details={
                "kind": "skill_call",
                "skill": self.name,
                "transport": "MQTT",
                "device_id": decision.device_id,
                "target_state": decision.target_state,
                "risk": decision.risk,
                "multimodal_evidence": multimodal_evidence,
            },
        )
        return self.system.request_action_execution(
            run,
            incident,
            device_id=decision.device_id,
            target_state=decision.target_state,
            risk=RiskLevel.L1,
            multimodal_evidence=multimodal_evidence,
        )

from datetime import timedelta
from typing import Optional

from fishagent.application.policy import evaluate_action
from fishagent.application.store import InMemoryStore
from fishagent.domain.models import (
    AgentRun,
    CommandStatus,
    DeviceCommand,
    Incident,
    IncidentStatus,
    RiskLevel,
    SensorReading,
    new_id,
    utcnow,
)


class FishAgentSystem:
    def __init__(self, store: Optional[InMemoryStore] = None) -> None:
        self.store = store or InMemoryStore()

    def initialize_demo(self) -> dict:
        self.store.reset_demo()
        return self.snapshot()

    def ingest_do(self, pond_id: str, value: float, source_event_id: Optional[str] = None, seconds_old: int = 0) -> Optional[Incident]:
        reading = SensorReading(
            pond_id=pond_id,
            sensor_id="do-%s" % pond_id.lower(),
            metric="DO",
            value=value,
            unit="mg/L",
            sampled_at=utcnow() - timedelta(seconds=seconds_old),
            source_event_id=source_event_id or new_id("reading"),
        )
        incident = self.store.add_reading(reading)
        if incident and incident.status == IncidentStatus.DETECTED:
            self.run_incident_flow(incident.id)
        return incident

    def run_incident_flow(self, incident_id: str) -> AgentRun:
        incident = self.store.incidents[incident_id]
        run = AgentRun(id=new_id("run"), goal="处理 %s" % incident.title, incident_id=incident_id, status="RUNNING")
        self.store.agent_runs[run.id] = run
        self.store.emit("agent.run.started", run.goal, {"run_id": run.id}, correlation_id=run.id)

        incident.transition(IncidentStatus.INVESTIGATING)
        run.step("supervisor-agent", "validate_trigger", "确认触发源为低溶氧传感器事件")

        latest_do = self.store.latest_reading(incident.pond_id, "DO")
        run.step("sensor-monitor-agent", "get_pond_snapshot", "读取最新溶氧、水质质量和采样时间")
        if latest_do is None or not latest_do.is_fresh():
            run.step("supervisor-agent", "stop", "核心数据过期或缺失，要求刷新数据")
            run.status = "FAILED"
            run.stop_reason = "STALE_EVIDENCE"
            self.store.emit("agent.run.failed", "证据过期，未执行设备动作", {"run_id": run.id}, correlation_id=run.id)
            return run

        device = self.store.devices["aerator-b01-1"]
        run.step("patrol-analysis-agent", "get_device_shadow_state", "%s 当前为 %s" % (device.name, device.shadow_state))

        if device.shadow_state == "on":
            run.step("supervisor-agent", "route", "设备已开启，停止重复执行并转向效果复核/故障调查")
            incident.transition(IncidentStatus.ACTION_PROPOSED)
            incident.transition(IncidentStatus.EXECUTING)
            incident.transition(IncidentStatus.VERIFY_PENDING)
            incident.verification_due_at = utcnow()
            run.status = "COMPLETED"
            run.stop_reason = "ALREADY_SATISFIED"
            self.store.emit("agent.run.completed", "设备已在目标状态，已抑制重复动作", {"run_id": run.id}, correlation_id=run.id)
            return run

        run.step("action-planning-agent", "propose_action", "建议开启 B-01 一号增氧机，风险 L1，30 秒后复核溶氧")
        incident.transition(IncidentStatus.ACTION_PROPOSED)

        command = self.request_action_execution(run, incident, device_id=device.id, target_state="on", risk=RiskLevel.L1)
        if command.status == CommandStatus.CONFIRMED:
            incident.transition(IncidentStatus.EXECUTING)
            incident.transition(IncidentStatus.VERIFY_PENDING)
            incident.verification_due_at = utcnow() + timedelta(seconds=30)
            run.status = "COMPLETED"
            run.stop_reason = "ACTION_EXECUTED"
            self.store.emit("agent.run.completed", "增氧命令已确认，等待复核", {"run_id": run.id}, correlation_id=run.id)
        elif command.policy_reason.startswith("设备影子状态"):
            incident.transition(IncidentStatus.EXECUTING)
            incident.transition(IncidentStatus.VERIFY_PENDING)
            incident.verification_due_at = utcnow()
            run.status = "COMPLETED"
            run.stop_reason = "ALREADY_SATISFIED"
        else:
            incident.transition(IncidentStatus.ACTION_FAILED)
            incident.transition(IncidentStatus.ESCALATED)
            run.status = "FAILED"
            run.stop_reason = "POLICY_REJECTED"
        return run

    def request_action_execution(
        self,
        run: AgentRun,
        incident: Incident,
        device_id: str,
        target_state: str,
        risk: RiskLevel,
    ) -> DeviceCommand:
        device = self.store.devices[device_id]
        latest_do = self.store.latest_reading(incident.pond_id, "DO")
        idempotency_key = "%s:%s:%s" % (incident.pond_id, device_id, target_state)
        policy = evaluate_action(
            actor="execution-agent",
            device=device,
            pond_id=incident.pond_id,
            target_state=target_state,
            risk=risk,
            latest_do=latest_do,
            idempotency_seen=idempotency_key in self.store.executed_idempotency_keys,
        )
        command = DeviceCommand(
            id=new_id("cmd"),
            device_id=device.id,
            pond_id=incident.pond_id,
            target_state=target_state,
            risk=risk,
            idempotency_key=idempotency_key,
            policy_reason=policy.reason,
        )
        self.store.commands[command.id] = command
        incident.command_ids.append(command.id)
        run.step("execution-agent", "request_action_execution", policy.reason)
        self.store.emit("policy.evaluated", policy.reason, {"allowed": policy.allowed, "command_id": command.id}, correlation_id=run.id)
        if not policy.allowed:
            command.status = CommandStatus.REJECTED
            return command

        command.status = CommandStatus.AUTHORIZED
        command.status = CommandStatus.QUEUED
        command.status = CommandStatus.SENT
        command.status = CommandStatus.ACKNOWLEDGED
        device.shadow_state = target_state
        command.status = CommandStatus.CONFIRMED
        self.store.executed_idempotency_keys[idempotency_key] = command.id
        self.store.emit("device.command.confirmed", "%s 已切换为 %s" % (device.name, target_state), {"command_id": command.id}, correlation_id=run.id)
        return command

    def verify_incident(self, incident_id: str) -> Incident:
        incident = self.store.incidents[incident_id]
        if incident.status != IncidentStatus.VERIFY_PENDING:
            return incident
        run = AgentRun(id=new_id("run"), goal="复核 %s" % incident.title, incident_id=incident.id, status="RUNNING")
        self.store.agent_runs[run.id] = run
        latest_do = self.store.latest_reading(incident.pond_id, "DO")
        run.step("verification-agent", "record_verification", "读取复核溶氧并判断处置效果")
        if latest_do and latest_do.is_fresh() and latest_do.value >= self.store.ponds[incident.pond_id].dissolved_oxygen_min:
            incident.transition(IncidentStatus.RESOLVED)
            run.status = "COMPLETED"
            run.stop_reason = "RESOLVED"
            self.store.emit("verification.resolved", "复核通过，事件关闭", {"incident_id": incident.id}, correlation_id=run.id)
        else:
            incident.transition(IncidentStatus.VERIFY_FAILED)
            incident.transition(IncidentStatus.ESCALATED)
            incident.assignee = "现场操作员"
            run.step("verification-agent", "create_manual_task", "复核未恢复，升级设备故障与人工处理")
            run.status = "COMPLETED"
            run.stop_reason = "ESCALATED"
            self.store.emit("verification.escalated", "复核失败，已升级人工任务", {"incident_id": incident.id}, correlation_id=run.id)
        return incident

    def run_demo(self, mode: str) -> dict:
        self.store.reset_demo()
        if mode == "dedup":
            self.store.devices["aerator-b01-1"].shadow_state = "on"
            incident = self.ingest_do("B-01", 2.1, source_event_id="demo-dedup")
            if incident:
                self.store.force_verification_due(incident.id)
                self.verify_incident(incident.id)
        elif mode == "failure":
            incident = self.ingest_do("B-01", 2.1, source_event_id="demo-failure")
            if incident:
                self.store.force_verification_due(incident.id)
                self.ingest_do("B-01", 2.3, source_event_id="demo-failure-review")
                self.verify_incident(incident.id)
        else:
            incident = self.ingest_do("B-01", 2.1, source_event_id="demo-success")
            if incident:
                self.store.force_verification_due(incident.id)
                self.ingest_do("B-01", 5.2, source_event_id="demo-success-review")
                self.verify_incident(incident.id)
        return self.snapshot()

    def snapshot(self) -> dict:
        return {
            "ponds": [pond.__dict__ for pond in self.store.ponds.values()],
            "devices": [device.__dict__ for device in self.store.devices.values()],
            "incidents": [
                {
                    "id": item.id,
                    "pond_id": item.pond_id,
                    "title": item.title,
                    "status": item.status.value,
                    "risk": item.risk.value,
                    "evidence": [e.__dict__ for e in item.evidence],
                    "command_ids": item.command_ids,
                    "verification_due_at": item.verification_due_at.isoformat() if item.verification_due_at else None,
                    "assignee": item.assignee,
                }
                for item in self.store.incidents.values()
            ],
            "commands": [
                {
                    "id": item.id,
                    "device_id": item.device_id,
                    "pond_id": item.pond_id,
                    "target_state": item.target_state,
                    "risk": item.risk.value,
                    "status": item.status.value,
                    "policy_reason": item.policy_reason,
                    "idempotency_key": item.idempotency_key,
                }
                for item in self.store.commands.values()
            ],
            "agent_runs": [
                {
                    "id": run.id,
                    "goal": run.goal,
                    "incident_id": run.incident_id,
                    "status": run.status,
                    "stop_reason": run.stop_reason,
                    "delegated_agents": run.delegated_agents,
                    "steps": [step.__dict__ for step in run.steps],
                    "budget": run.budget,
                }
                for run in self.store.agent_runs.values()
            ],
            "events": self.store.events[-80:],
        }

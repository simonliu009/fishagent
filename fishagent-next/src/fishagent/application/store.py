from datetime import timedelta
from typing import Dict, List, Optional

from fishagent.domain.models import (
    AgentRun,
    Device,
    DeviceCommand,
    Evidence,
    Incident,
    IncidentStatus,
    Pond,
    SensorReading,
    new_id,
    utcnow,
)


class InMemoryStore:
    def __init__(self) -> None:
        self.ponds: Dict[str, Pond] = {}
        self.devices: Dict[str, Device] = {}
        self.readings: List[SensorReading] = []
        self.incidents: Dict[str, Incident] = {}
        self.commands: Dict[str, DeviceCommand] = {}
        self.agent_runs: Dict[str, AgentRun] = {}
        self.events: List[dict] = []
        self.executed_idempotency_keys: Dict[str, str] = {}
        self.emit("system.started", "系统已启动，等待显式初始化资产或接入真实数据")

    def reset_demo(self) -> None:
        self.ponds.clear()
        self.devices.clear()
        self.readings.clear()
        self.incidents.clear()
        self.commands.clear()
        self.agent_runs.clear()
        self.events.clear()
        self.executed_idempotency_keys.clear()
        self.ponds["B-01"] = Pond(id="B-01", name="B-01 精养池", species="加州鲈")
        self.devices["aerator-b01-1"] = Device(
            id="aerator-b01-1",
            pond_id="B-01",
            name="B-01 一号增氧机",
            capability="aeration",
        )
        self.emit("system.demo.initialized", "演示数据已初始化：B-01、溶氧传感器、增氧机")

    def emit(self, event_type: str, summary: str, payload: Optional[dict] = None, correlation_id: Optional[str] = None) -> None:
        self.events.append(
            {
                "sequence": len(self.events) + 1,
                "event_id": new_id("evt"),
                "occurred_at": utcnow().isoformat(),
                "correlation_id": correlation_id,
                "event_type": event_type,
                "summary": summary,
                "payload": payload or {},
            }
        )

    def add_reading(self, reading: SensorReading) -> Optional[Incident]:
        if any(item.source_event_id == reading.source_event_id for item in self.readings):
            self.emit("telemetry.duplicate_ignored", "重复读数已按 source_event_id 忽略")
            return None
        self.readings.append(reading)
        self.emit(
            "telemetry.reading.received",
            "%s %s=%.2f%s" % (reading.pond_id, reading.metric, reading.value, reading.unit),
            {"pond_id": reading.pond_id, "metric": reading.metric, "value": reading.value},
        )
        pond = self.ponds.get(reading.pond_id)
        if pond and reading.metric == "DO" and reading.quality == "GOOD" and reading.value < pond.dissolved_oxygen_min:
            active = self.active_incident_for_pond(reading.pond_id)
            evidence = Evidence(
                id=new_id("evi"),
                type="sensor_snapshot",
                summary="溶氧 %.2f%s，低于安全线 %.2f%s" % (
                    reading.value,
                    reading.unit,
                    pond.dissolved_oxygen_min,
                    reading.unit,
                ),
                refs=[reading.source_event_id],
            )
            if active:
                active.evidence.append(evidence)
                self.emit("incident.evidence_merged", "低溶氧证据已合并到现有事件", {"incident_id": active.id})
                return active
            incident = Incident(
                id=new_id("inc"),
                pond_id=reading.pond_id,
                title="%s 低溶氧" % pond.name,
                evidence=[evidence],
            )
            self.incidents[incident.id] = incident
            self.emit("incident.detected", incident.title, {"incident_id": incident.id, "pond_id": reading.pond_id})
            return incident
        return None

    def latest_reading(self, pond_id: str, metric: str) -> Optional[SensorReading]:
        candidates = [r for r in self.readings if r.pond_id == pond_id and r.metric == metric]
        candidates.sort(key=lambda r: r.sampled_at, reverse=True)
        return candidates[0] if candidates else None

    def active_incident_for_pond(self, pond_id: str) -> Optional[Incident]:
        terminal = {IncidentStatus.RESOLVED, IncidentStatus.DISMISSED, IncidentStatus.ESCALATED}
        for incident in self.incidents.values():
            if incident.pond_id == pond_id and incident.status not in terminal:
                return incident
        return None

    def due_verifications(self) -> List[Incident]:
        now = utcnow()
        return [
            incident
            for incident in self.incidents.values()
            if incident.status == IncidentStatus.VERIFY_PENDING
            and incident.verification_due_at is not None
            and incident.verification_due_at <= now
        ]

    def force_verification_due(self, incident_id: str) -> None:
        self.incidents[incident_id].verification_due_at = utcnow() - timedelta(seconds=1)

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fishagent.domain.models import (
    ActionProposal,
    AgentRun,
    AgentStep,
    Approval,
    ApprovalStatus,
    AuditEvent,
    CameraSource,
    CommandStatus,
    Device,
    DeviceCommand,
    Escalation,
    Evidence,
    Farm,
    HealthStatus,
    Incident,
    IncidentStatus,
    JobStatus,
    ManualTask,
    PatrolFinding,
    Pond,
    RiskLevel,
    ScheduleDefinition,
    ScheduledJob,
    ScheduleStatus,
    Sensor,
    SensorHealth,
    SensorReading,
    TaskStatus,
    VerificationPlan,
    VerificationResult,
    VisionFrame,
    Zone,
    new_id,
    utcnow,
)


class InMemoryStore:
    def __init__(self) -> None:
        self.farms: Dict[str, Farm] = {}
        self.zones: Dict[str, Zone] = {}
        self.ponds: Dict[str, Pond] = {}
        self.sensors: Dict[str, Sensor] = {}
        self.sensor_health: Dict[str, SensorHealth] = {}
        self.devices: Dict[str, Device] = {}
        self.cameras: Dict[str, CameraSource] = {}
        self.vision_frames: Dict[str, VisionFrame] = {}
        self.readings: List[SensorReading] = []
        self.incidents: Dict[str, Incident] = {}
        self.action_proposals: Dict[str, ActionProposal] = {}
        self.approvals: Dict[str, Approval] = {}
        self.commands: Dict[str, DeviceCommand] = {}
        self.verification_plans: Dict[str, VerificationPlan] = {}
        self.verification_results: Dict[str, VerificationResult] = {}
        self.manual_tasks: Dict[str, ManualTask] = {}
        self.schedules: Dict[str, ScheduleDefinition] = {}
        self.scheduled_jobs: Dict[str, ScheduledJob] = {}
        self.agent_runs: Dict[str, AgentRun] = {}
        self.patrol_findings: Dict[str, PatrolFinding] = {}
        self.escalations: Dict[str, Escalation] = {}
        self.audit_events: List[AuditEvent] = []
        self.events: List[dict] = []
        self.executed_idempotency_keys: Dict[str, str] = {}
        self._event_sequence = 0
        self.emit("system.started", "系统已启动，等待显式初始化资产或接入真实数据")

    def reset_demo(self) -> None:
        self.farms.clear()
        self.zones.clear()
        self.ponds.clear()
        self.sensors.clear()
        self.sensor_health.clear()
        self.devices.clear()
        self.cameras.clear()
        self.vision_frames.clear()
        self.readings.clear()
        self.incidents.clear()
        self.action_proposals.clear()
        self.approvals.clear()
        self.commands.clear()
        self.verification_plans.clear()
        self.verification_results.clear()
        self.manual_tasks.clear()
        self.schedules.clear()
        self.scheduled_jobs.clear()
        self.agent_runs.clear()
        self.patrol_findings.clear()
        self.escalations.clear()
        self.audit_events.clear()
        self.events.clear()
        self.executed_idempotency_keys.clear()
        self.farms["farm-demo"] = Farm(id="farm-demo", name="青湾智慧渔场", location="浙江湖州")
        self.zones["zone-east"] = Zone(id="zone-east", farm_id="farm-demo", name="东区")
        self.zones["zone-west"] = Zone(id="zone-west", farm_id="farm-demo", name="西区")
        pond_specs = (
            ("B-01", "B-01 鲈鱼精养池", "加州鲈", 4.0, "off"),
            ("B-02", "B-02 草鱼生态池", "草鱼", 4.0, "on"),
            ("B-03", "B-03 黄颡鱼育成池", "黄颡鱼", 4.5, "off"),
            ("B-04", "B-04 对虾标粗池", "南美白对虾", 4.0, "off"),
        )
        for pond_id, name, species, do_min, device_state in pond_specs:
            pond_slug = pond_id.lower().replace("-", "")
            sensor_id = "do-%s" % pond_id.lower()
            self.ponds[pond_id] = Pond(
                id=pond_id,
                name=name,
                species=species,
                farm_id="farm-demo",
                dissolved_oxygen_min=do_min,
            )
            self.sensors[sensor_id] = Sensor(
                id=sensor_id,
                pond_id=pond_id,
                name="%s 溶氧传感器" % pond_id,
                metric="DO",
                unit="mg/L",
            )
            self.sensor_health[sensor_id] = SensorHealth(sensor_id=sensor_id, last_heartbeat_at=utcnow())
            self.devices["aerator-%s-1" % pond_slug] = Device(
                id="aerator-%s-1" % pond_slug,
                pond_id=pond_id,
                name="%s 一号增氧机" % pond_id,
                capability="aeration",
                shadow_state=device_state,
            )
            self.cameras["camera-%s" % pond_slug] = CameraSource(
                id="camera-%s" % pond_slug,
                pond_id=pond_id,
                name="%s 岸边摄像头" % pond_id,
                source_type="HTTP_SNAPSHOT",
                status="UNAVAILABLE",
            )
        self.schedules["schedule-demo-patrol"] = ScheduleDefinition(
            id="schedule-demo-patrol",
            name="五分钟全场巡查",
            job_type="patrol",
            interval_seconds=300,
            next_run_at=utcnow() + timedelta(seconds=300),
        )
        self.emit(
            "system.demo.initialized",
            "演示数据已初始化：4 个池塘及传感器、增氧机、摄像头",
            {"pond_ids": [item[0] for item in pond_specs]},
        )

    def emit(
        self,
        event_type: str,
        summary: str,
        payload: Optional[dict] = None,
        correlation_id: Optional[str] = None,
        *,
        actor_type: str = "system",
        actor_id: str = "fishagent",
        resource_type: str = "event",
        resource_id: Optional[str] = None,
    ) -> None:
        self._event_sequence += 1
        event = {
                "sequence": self._event_sequence,
                "event_id": new_id("evt"),
                "occurred_at": utcnow().isoformat(),
                "correlation_id": correlation_id,
                "event_type": event_type,
                "summary": summary,
                "payload": payload or {},
            }
        self.events.append(event)
        self.audit_events.append(
            AuditEvent(
                id=str(event["event_id"]),
                actor_type=actor_type,
                actor_id=actor_id,
                action=event_type,
                resource_type=resource_type,
                resource_id=resource_id or str(event["event_id"]),
                correlation_id=correlation_id,
                payload=payload or {},
            )
        )

    @staticmethod
    def _datetime(value: object, default: Optional[datetime] = None) -> Optional[datetime]:
        if value is None:
            return default
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    def restore_snapshot(self, payload: dict) -> None:
        """Restore a JSON-safe state snapshot without emitting seed events."""
        self.farms = {item["id"]: Farm(**item) for item in payload.get("farms", [])}
        self.zones = {item["id"]: Zone(**item) for item in payload.get("zones", [])}
        self.ponds = {item["id"]: Pond(**item) for item in payload.get("ponds", [])}
        self.sensors = {item["id"]: Sensor(**item) for item in payload.get("sensors", [])}
        self.sensor_health = {
            item["sensor_id"]: SensorHealth(
                sensor_id=item["sensor_id"],
                status=HealthStatus(item.get("status", HealthStatus.ONLINE.value)),
                last_heartbeat_at=self._datetime(item.get("last_heartbeat_at")),
                last_reading_at=self._datetime(item.get("last_reading_at")),
                error_count=int(item.get("error_count", 0)),
                drift_score=float(item.get("drift_score", 0.0)),
                message=item.get("message", ""),
            )
            for item in payload.get("sensor_health", [])
        }
        self.devices = {item["id"]: Device(**item) for item in payload.get("devices", [])}
        self.cameras = {
            item["id"]: CameraSource(
                id=item["id"],
                pond_id=item["pond_id"],
                name=item["name"],
                source_type=item["source_type"],
                status=item.get("status", "UNAVAILABLE"),
                last_frame_at=self._datetime(item.get("last_frame_at")),
                source_url=item.get("source_url", ""),
                privacy_policy=item.get("privacy_policy", "EVENT_ONLY"),
                last_frame_id=item.get("last_frame_id"),
                last_frame_hash=item.get("last_frame_hash"),
                last_frame_width=item.get("last_frame_width"),
                last_frame_height=item.get("last_frame_height"),
            )
            for item in payload.get("cameras", [])
        }
        self.vision_frames = {
            item["id"]: VisionFrame(
                id=item["id"],
                camera_id=item["camera_id"],
                source_url=item.get("source_url", ""),
                object_name=item["object_name"],
                content_type=item.get("content_type", "application/octet-stream"),
                sha256=item["sha256"],
                width=int(item["width"]),
                height=int(item["height"]),
                captured_at=self._datetime(item.get("captured_at")) or utcnow(),
            )
            for item in payload.get("vision_frames", [])
        }
        self.readings = [
            SensorReading(
                pond_id=item["pond_id"],
                sensor_id=item["sensor_id"],
                metric=item["metric"],
                value=float(item["value"]),
                unit=item["unit"],
                sampled_at=self._datetime(item["sampled_at"]) or utcnow(),
                received_at=self._datetime(item.get("received_at")) or utcnow(),
                quality=item.get("quality", "GOOD"),
                source_event_id=item["source_event_id"],
            )
            for item in payload.get("readings", [])
        ]
        self.incidents = {}
        for item in payload.get("incidents", []):
            incident = Incident(
                id=item["id"],
                pond_id=item["pond_id"],
                title=item["title"],
                status=IncidentStatus(item.get("status", IncidentStatus.DETECTED.value)),
                risk=RiskLevel(item.get("risk", RiskLevel.L1.value)),
                evidence=[
                    Evidence(
                        id=evidence["id"],
                        type=evidence["type"],
                        summary=evidence["summary"],
                        created_at=self._datetime(evidence.get("created_at")) or utcnow(),
                        refs=evidence.get("refs", []),
                    )
                    for evidence in item.get("evidence", [])
                ],
                action_proposal_ids=item.get("action_proposal_ids", []),
                command_ids=item.get("command_ids", []),
                verification_plan_id=item.get("verification_plan_id"),
                verification_result_ids=item.get("verification_result_ids", []),
                manual_task_ids=item.get("manual_task_ids", []),
                verification_due_at=self._datetime(item.get("verification_due_at")),
                assignee=item.get("assignee"),
            )
            self.incidents[incident.id] = incident
        self.action_proposals = {
            item["id"]: ActionProposal(
                id=item["id"],
                incident_id=item["incident_id"],
                device_id=item["device_id"],
                pond_id=item["pond_id"],
                target_state=item["target_state"],
                risk=RiskLevel(item["risk"]),
                rationale=item["rationale"],
                evidence_refs=item.get("evidence_refs", []),
                status=item.get("status", "PROPOSED"),
                approval_id=item.get("approval_id"),
                created_at=self._datetime(item.get("created_at")) or utcnow(),
            )
            for item in payload.get("action_proposals", [])
        }
        self.approvals = {
            item["id"]: Approval(
                id=item["id"],
                proposal_id=item["proposal_id"],
                incident_id=item["incident_id"],
                status=ApprovalStatus(item.get("status", ApprovalStatus.PENDING.value)),
                requested_by=item.get("requested_by", "execution-agent"),
                decided_by=item.get("decided_by"),
                reason=item.get("reason", ""),
                created_at=self._datetime(item.get("created_at")) or utcnow(),
                decided_at=self._datetime(item.get("decided_at")),
            )
            for item in payload.get("approvals", [])
        }
        self.commands = {
            item["id"]: DeviceCommand(
                id=item["id"],
                device_id=item["device_id"],
                pond_id=item["pond_id"],
                target_state=item["target_state"],
                risk=RiskLevel(item["risk"]),
                idempotency_key=item["idempotency_key"],
                status=CommandStatus(item.get("status", CommandStatus.PROPOSED.value)),
                policy_reason=item.get("policy_reason", ""),
                created_at=self._datetime(item.get("created_at")) or utcnow(),
            )
            for item in payload.get("commands", [])
        }
        self.verification_plans = {
            item["id"]: VerificationPlan(
                id=item["id"],
                incident_id=item["incident_id"],
                metric=item.get("metric", "DO"),
                threshold=float(item.get("threshold", 4.0)),
                earliest_at=self._datetime(item.get("earliest_at")),
                latest_at=self._datetime(item.get("latest_at")),
                status=item.get("status", "PENDING"),
            )
            for item in payload.get("verification_plans", [])
        }
        self.verification_results = {
            item["id"]: VerificationResult(
                id=item["id"],
                incident_id=item["incident_id"],
                plan_id=item["plan_id"],
                outcome=item["outcome"],
                observed_value=item.get("observed_value"),
                evidence_refs=item.get("evidence_refs", []),
                created_at=self._datetime(item.get("created_at")) or utcnow(),
            )
            for item in payload.get("verification_results", [])
        }
        self.manual_tasks = {
            item["id"]: ManualTask(
                id=item["id"],
                incident_id=item.get("incident_id"),
                title=item["title"],
                description=item["description"],
                assignee=item.get("assignee", "现场操作员"),
                priority=item.get("priority", "HIGH"),
                status=TaskStatus(item.get("status", TaskStatus.OPEN.value)),
                created_at=self._datetime(item.get("created_at")) or utcnow(),
                completed_at=self._datetime(item.get("completed_at")),
            )
            for item in payload.get("manual_tasks", [])
        }
        self.schedules = {
            item["id"]: ScheduleDefinition(
                id=item["id"],
                name=item["name"],
                job_type=item["job_type"],
                interval_seconds=int(item["interval_seconds"]),
                status=ScheduleStatus(item.get("status", ScheduleStatus.ACTIVE.value)),
                next_run_at=self._datetime(item.get("next_run_at")),
                last_run_at=self._datetime(item.get("last_run_at")),
            )
            for item in payload.get("schedules", [])
        }
        self.scheduled_jobs = {
            item["id"]: ScheduledJob(
                id=item["id"],
                job_type=item["job_type"],
                idempotency_key=item["idempotency_key"],
                due_at=self._datetime(item["due_at"]) or utcnow(),
                incident_id=item.get("incident_id"),
                schedule_id=item.get("schedule_id"),
                status=JobStatus(item.get("status", JobStatus.DUE.value)),
                attempts=int(item.get("attempts", 0)),
                created_at=self._datetime(item.get("created_at")) or utcnow(),
            )
            for item in payload.get("scheduled_jobs", [])
        }
        self.agent_runs = {}
        for item in payload.get("agent_runs", []):
            run = AgentRun(
                id=item["id"],
                goal=item["goal"],
                incident_id=item.get("incident_id"),
                status=item.get("status", "QUEUED"),
                stop_reason=item.get("stop_reason"),
                delegated_agents=item.get("delegated_agents", []),
                budget=item.get("budget", {"delegations": 8, "tool_calls": 20, "seconds": 90}),
            )
            run.steps = [
                AgentStep(
                    agent=step["agent"],
                    action=step["action"],
                    summary=step["summary"],
                    created_at=self._datetime(step.get("created_at")) or utcnow(),
                )
                for step in item.get("steps", [])
            ]
            self.agent_runs[run.id] = run
        self.patrol_findings = {
            item["id"]: PatrolFinding(
                id=item["id"],
                patrol_run_id=item["patrol_run_id"],
                pond_id=item["pond_id"],
                status=item["status"],
                summary=item["summary"],
                evidence_refs=item.get("evidence_refs", []),
                confidence=item.get("confidence"),
                created_at=self._datetime(item.get("created_at")) or utcnow(),
            )
            for item in payload.get("patrol_findings", [])
        }
        self.escalations = {
            item["id"]: Escalation(
                id=item["id"],
                incident_id=item["incident_id"],
                level=item["level"],
                reason=item["reason"],
                status=item.get("status", "OPEN"),
                manual_task_id=item.get("manual_task_id"),
                created_at=self._datetime(item.get("created_at")) or utcnow(),
            )
            for item in payload.get("escalations", [])
        }
        self.audit_events = [
            AuditEvent(
                id=item["id"],
                actor_type=item.get("actor_type", "system"),
                actor_id=item.get("actor_id", "fishagent"),
                action=item["action"],
                resource_type=item["resource_type"],
                resource_id=item.get("resource_id"),
                correlation_id=item.get("correlation_id"),
                payload=item.get("payload", {}),
                created_at=self._datetime(item.get("created_at")) or utcnow(),
            )
            for item in payload.get("audit_events", [])
        ]
        self.events = payload.get("events", [])
        self._event_sequence = int(payload.get("event_sequence", max((event.get("sequence", 0) for event in self.events), default=0)))
        self.executed_idempotency_keys = payload.get("executed_idempotency_keys", {})

    def mark_sensor_health(
        self,
        sensor_id: str,
        status: HealthStatus = HealthStatus.ONLINE,
        message: str = "",
        reading_at: Optional[datetime] = None,
    ) -> SensorHealth:
        health = self.sensor_health.setdefault(sensor_id, SensorHealth(sensor_id=sensor_id))
        health.status = status
        health.message = message
        health.last_heartbeat_at = utcnow()
        if reading_at:
            health.last_reading_at = reading_at
        if status == HealthStatus.ERROR:
            health.error_count += 1
        self.emit("sensor.health.changed", "传感器 %s 状态：%s" % (sensor_id, status.value), {"sensor_id": sensor_id, "status": status.value})
        return health

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

    def aeration_device_for_pond(self, pond_id: str) -> Optional[Device]:
        for device in self.devices.values():
            if device.pond_id == pond_id and device.capability == "aeration" and device.healthy:
                return device
        return None

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

    def due_jobs(self) -> List[ScheduledJob]:
        now = utcnow()
        return [
            job
            for job in self.scheduled_jobs.values()
            if job.status in {JobStatus.DUE, JobStatus.RETRY_WAIT} and job.due_at <= now
        ]

    def force_verification_due(self, incident_id: str) -> None:
        self.incidents[incident_id].verification_due_at = utcnow() - timedelta(seconds=1)
        for job in self.scheduled_jobs.values():
            if job.incident_id == incident_id and job.job_type == "verification":
                job.due_at = utcnow() - timedelta(seconds=1)

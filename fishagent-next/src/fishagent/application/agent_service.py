import re
import time
from datetime import datetime, timedelta
from queue import Empty, Queue
from threading import RLock, Thread
from typing import Any, Optional, cast

from fishagent.agent_runtime.contracts import IncidentDecision
from fishagent.agent_runtime.skills.device_control import DeviceControlSkill
from fishagent.application.demo_data import DEMO_SENSOR_BY_METRIC, DEMO_SENSOR_SPECS, DEMO_WATER_SERIES
from fishagent.application.policy import evaluate_action
from fishagent.application.store import WATER_QUALITY_HIGH_LIMITS, WATER_QUALITY_RANGES, InMemoryStore
from fishagent.domain.models import (
    ActionProposal,
    AgentRun,
    AnalysisCase,
    Approval,
    ApprovalStatus,
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
    Zone,
    new_id,
    utcnow,
)
from fishagent.infrastructure.gateways import DeviceGateway, SimulatorDeviceGateway
from fishagent.infrastructure.persistence import PostgresStateRepository
from fishagent.infrastructure.realtime import RedisEventPublisher


class _AnalysisCaseCancelled(RuntimeError):
    pass


class FishAgentSystem:
    def __init__(
        self,
        store: Optional[InMemoryStore] = None,
        repository: Optional[PostgresStateRepository] = None,
        event_publisher: Optional[RedisEventPublisher] = None,
        device_gateway: Optional[DeviceGateway] = None,
        agent_orchestrator: Optional[Any] = None,
        telemetry_publisher: Optional[Any] = None,
        agent_decision_timeout_seconds: int = 300,
    ) -> None:
        self.store = store or InMemoryStore()
        self.repository = repository
        self.event_publisher = event_publisher
        self.device_gateway = device_gateway or SimulatorDeviceGateway()
        self.agent_orchestrator = agent_orchestrator
        self.telemetry_publisher = telemetry_publisher
        self.agent_decision_timeout_seconds = max(1, int(agent_decision_timeout_seconds))
        self.device_control_skill = DeviceControlSkill(self)
        self._job_lock = RLock()
        self._analysis_case_lock = RLock()
        self._analysis_case_thread: Optional[Thread] = None
        self._analysis_case_generation = 0
        if self.repository:
            persisted = self.repository.load()
            if persisted:
                self.store.restore_snapshot(persisted)

    def initialize_demo(self) -> dict:
        self._cancel_analysis_case_sequence()
        self._reset_demo_with_telemetry()
        return self.snapshot()

    def _cancel_analysis_case_sequence(self) -> None:
        with self._analysis_case_lock:
            self._analysis_case_generation += 1

    def _reset_demo_with_telemetry(self) -> None:
        self.store.reset_demo()
        payloads = []
        for pond_id, series_by_metric in DEMO_WATER_SERIES.items():
            for metric, values in series_by_metric.items():
                spec = DEMO_SENSOR_BY_METRIC[metric]
                for index, value in enumerate(values):
                    is_latest_do = metric == "DO" and index == len(values) - 1
                    payloads.append(
                        {
                            "pond_id": pond_id,
                            "sensor_id": "%s-%s" % (spec["slug"], pond_id.lower()),
                            "metric": metric,
                            "unit": spec["unit"],
                            "value": value,
                            "source_event_id": "demo-seed-%s-%s-%02d"
                            % (pond_id.lower(), spec["slug"], index + 1),
                            "seconds_old": (len(values) - index - 1) * 3 * 60 * 60,
                            "quality": "SUSPECT" if pond_id == "B-04" and is_latest_do else "GOOD",
                            "auto_run": False,
                        }
                    )
        self._publish_demo_dataset(payloads)

    def _publish_demo_dataset(self, payloads: list[dict]) -> None:
        if self.telemetry_publisher is None:
            for payload in payloads:
                self.ingest_reading(**payload)
            return
        expected_ids = {str(payload["source_event_id"]) for payload in payloads}
        for payload in payloads:
            if not self.telemetry_publisher.publish_reading(**payload, defer_persist=True):
                raise RuntimeError("MQTT mock telemetry publish failed")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            consumed_ids = {item.source_event_id for item in self.store.readings}
            if expected_ids.issubset(consumed_ids):
                return
            time.sleep(0.02)
        missing = len(expected_ids - {item.source_event_id for item in self.store.readings})
        raise RuntimeError("%d MQTT mock telemetry readings were not consumed before timeout" % missing)

    def _demo_reading(
        self,
        pond_id: str,
        value: float,
        source_event_id: str,
        seconds_old: int = 0,
        quality: str = "GOOD",
        auto_run: bool = True,
        defer_persist: bool = False,
        metric: str = "DO",
        unit: Optional[str] = None,
    ) -> Optional[Incident]:
        metric = metric.upper()
        spec = DEMO_SENSOR_BY_METRIC.get(metric, {"slug": metric.lower(), "unit": unit or ""})
        resolved_unit = unit or str(spec["unit"])
        if self.telemetry_publisher is None:
            return self.ingest_reading(
                pond_id,
                value,
                metric=metric,
                unit=resolved_unit,
                source_event_id=source_event_id,
                seconds_old=seconds_old,
                quality=quality,
                auto_run=auto_run,
            )
        sensor_id = "%s-%s" % (spec["slug"], pond_id.lower())
        if not self.telemetry_publisher.publish_reading(
            pond_id=pond_id,
            sensor_id=sensor_id,
            metric=metric,
            unit=resolved_unit,
            value=value,
            source_event_id=source_event_id,
            quality=quality,
            seconds_old=seconds_old,
            auto_run=auto_run,
            defer_persist=defer_persist,
        ):
            raise RuntimeError("MQTT mock telemetry publish failed")
        deadline = time.monotonic() + (95 if auto_run else 5)
        transient_statuses = {
            IncidentStatus.DETECTED,
            IncidentStatus.INVESTIGATING,
            IncidentStatus.ACTION_PROPOSED,
            IncidentStatus.EXECUTING,
        }
        while time.monotonic() < deadline:
            if any(item.source_event_id == source_event_id for item in self.store.readings):
                matching_incident = next(
                    (
                        incident
                        for incident in reversed(list(self.store.incidents.values()))
                        if any(source_event_id in evidence.refs for evidence in incident.evidence)
                    ),
                    None,
                )
                if not auto_run or matching_incident is None or matching_incident.status not in transient_statuses:
                    return matching_incident
            time.sleep(0.02)
        if any(item.source_event_id == source_event_id for item in self.store.readings):
            return next(
                (
                    incident
                    for incident in reversed(list(self.store.incidents.values()))
                    if any(source_event_id in evidence.refs for evidence in incident.evidence)
                ),
                None,
            )
        raise RuntimeError("MQTT mock telemetry was not consumed before timeout")

    def create_farm(self, payload: dict) -> Farm:
        farm = Farm(
            id=str(payload.get("id") or new_id("farm")),
            name=str(payload.get("name") or "未命名养殖场"),
            location=str(payload.get("location") or ""),
        )
        self.store.farms[farm.id] = farm
        self.store.emit("asset.farm.created", "创建养殖场：%s" % farm.name, {"farm_id": farm.id})
        return farm

    def create_zone(self, payload: dict) -> Zone:
        farm_id = str(payload.get("farm_id") or "")
        if farm_id not in self.store.farms:
            raise ValueError("farm_id does not exist")
        zone = Zone(
            id=str(payload.get("id") or new_id("zone")),
            farm_id=farm_id,
            name=str(payload.get("name") or "未命名区域"),
            location=str(payload.get("location") or ""),
            status=str(payload.get("status") or "ACTIVE"),
        )
        self.store.zones[zone.id] = zone
        self.store.emit("asset.zone.created", "创建区域：%s" % zone.name, {"zone_id": zone.id, "farm_id": farm_id})
        return zone

    def create_pond(self, payload: dict) -> Pond:
        farm_id = str(payload.get("farm_id") or "")
        if farm_id and farm_id not in self.store.farms:
            raise ValueError("farm_id does not exist")
        pond = Pond(
            id=str(payload.get("id") or new_id("pond")),
            name=str(payload.get("name") or "未命名池塘"),
            species=str(payload.get("species") or ""),
            farm_id=farm_id,
            dissolved_oxygen_min=float(payload.get("dissolved_oxygen_min") or 4.0),
        )
        self.store.ponds[pond.id] = pond
        self.store.emit("asset.pond.created", "创建养殖单元：%s" % pond.name, {"pond_id": pond.id})
        return pond

    def create_sensor(self, payload: dict) -> Sensor:
        pond_id = str(payload.get("pond_id") or "")
        if pond_id not in self.store.ponds:
            raise ValueError("pond_id does not exist")
        sensor = Sensor(
            id=str(payload.get("id") or new_id("sensor")),
            pond_id=pond_id,
            name=str(payload.get("name") or "未命名传感器"),
            metric=str(payload.get("metric") or "DO"),
            unit=str(payload.get("unit") or "mg/L"),
            status=str(payload.get("status") or "ONLINE"),
            freshness_seconds=int(payload.get("freshness_seconds") or 120),
        )
        self.store.sensors[sensor.id] = sensor
        self.store.sensor_health.setdefault(sensor.id, SensorHealth(sensor_id=sensor.id, last_heartbeat_at=utcnow()))
        self.store.emit("asset.sensor.created", "创建传感器：%s" % sensor.name, {"sensor_id": sensor.id})
        return sensor

    def create_device(self, payload: dict) -> Device:
        pond_id = str(payload.get("pond_id") or "")
        if pond_id not in self.store.ponds:
            raise ValueError("pond_id does not exist")
        device = Device(
            id=str(payload.get("id") or new_id("device")),
            pond_id=pond_id,
            name=str(payload.get("name") or "未命名设备"),
            capability=str(payload.get("capability") or "aeration"),
            shadow_state=str(payload.get("shadow_state") or "off"),
            healthy=bool(payload.get("healthy", True)),
        )
        self.store.devices[device.id] = device
        self.store.emit("asset.device.created", "创建设备：%s" % device.name, {"device_id": device.id})
        return device

    def create_camera(self, payload: dict) -> CameraSource:
        pond_id = str(payload.get("pond_id") or "")
        if pond_id not in self.store.ponds:
            raise ValueError("pond_id does not exist")
        camera = CameraSource(
            id=str(payload.get("id") or new_id("camera")),
            pond_id=pond_id,
            name=str(payload.get("name") or "未命名摄像头"),
            source_type=str(payload.get("source_type") or "HTTP_SNAPSHOT"),
            camera_role=str(payload.get("camera_role") or "SURFACE"),
            status=str(payload.get("status") or "UNAVAILABLE"),
            source_url=str(payload.get("source_url") or ""),
            privacy_policy=str(payload.get("privacy_policy") or "EVENT_ONLY"),
        )
        self.store.cameras[camera.id] = camera
        self.store.emit("asset.camera.created", "创建摄像头：%s" % camera.name, {"camera_id": camera.id})
        return camera

    def _case_evidence_summary(self, case: AnalysisCase) -> str:
        summaries: list[str] = []
        for ref in case.evidence_refs:
            observation = self.store.camera_observations.get(ref)
            if observation:
                summaries.append(
                    "摄像头[%s] %s（置信度 %.0f%%）"
                    % (observation.camera_role, observation.summary, observation.confidence * 100)
                )
                continue
            weather = self.store.weather_observations.get(ref)
            if weather:
                summaries.append(
                    "天气[%s] %s，风向%s，风速 %.1fm/s，降雨概率 %d%%"
                    % (weather.condition, weather.forecast, weather.wind_direction, weather.wind_speed_mps, weather.rain_probability_pct)
                )
                continue
            article = self.store.disease_knowledge.get(ref)
            if article:
                summaries.append(
                    "知识库[%s] %s：%s；建议：%s"
                    % (article.name, article.species, article.signs, "、".join(article.recommended_actions))
                )
        return "；".join(summaries)

    def run_analysis_case(self, case_id: str, generation: Optional[int] = None) -> AgentRun:
        """Turn one multimodal demo case into the normal incident decision flow."""
        with self._analysis_case_lock:
            if generation is not None and generation != self._analysis_case_generation:
                raise _AnalysisCaseCancelled("案例序列已被重置")
            case = self.store.analysis_cases[case_id]
            if case.agent_run_id and case.agent_run_id in self.store.agent_runs:
                return self.store.agent_runs[case.agent_run_id]
            case.status = "RUNNING"
            case.updated_at = utcnow()
            evidence = Evidence(
                id=new_id("evi"),
                type="multimodal_analysis",
                summary=self._case_evidence_summary(case),
                refs=list(case.evidence_refs),
            )
            incident = Incident(
                id=new_id("inc"),
                pond_id=case.pond_id,
                title="%s：%s" % (self.store.ponds[case.pond_id].name, case.title),
                risk=RiskLevel.L2 if case.expected_device_id.startswith("valve-") else RiskLevel.L1,
                evidence=[evidence],
            )
            self.store.incidents[incident.id] = incident
            case.incident_id = incident.id
            self.store.emit(
                "analysis_case.detected",
                case.title,
                {"case_id": case.id, "incident_id": incident.id, "category": case.category},
            )
            if self.agent_orchestrator is None:
                incident.transition(IncidentStatus.INVESTIGATING)
                run = AgentRun(id=new_id("run"), goal="分析案例：%s" % case.title, incident_id=incident.id, status="RUNNING")
                self.store.agent_runs[run.id] = run
                run.step("supervisor-agent", "stop", "CrewAI 未配置，案例不使用硬编码动作替代模型决策")
                self._llm_manual_stop(incident, run, "LLM_REQUIRED", "案例已转人工，等待配置 CrewAI")
            else:
                run = self.run_incident_flow(incident.id)
            case.agent_run_id = run.id
            case.result_summary = self._analysis_case_result(case, incident, run)
            case.status = self._analysis_case_status(incident, run)
            case.updated_at = utcnow()
            self.snapshot()
            return run

    @staticmethod
    def _analysis_case_status(incident: Incident, run: AgentRun) -> str:
        if incident.status == IncidentStatus.WAITING_APPROVAL or run.status == "WAITING_APPROVAL":
            return "WAITING_APPROVAL"
        if incident.status == IncidentStatus.MANUAL_REQUIRED:
            return "MANUAL_REQUIRED"
        if incident.status in {IncidentStatus.RESOLVED, IncidentStatus.VERIFY_PENDING}:
            return "COMPLETED"
        if run.status == "FAILED":
            return "FAILED"
        return "COMPLETED"

    @staticmethod
    def _analysis_case_result(case: AnalysisCase, incident: Incident, run: AgentRun) -> str:
        if run.steps:
            return run.steps[-1].summary
        return "案例状态：%s，事件状态：%s" % (case.status, incident.status.value)

    def run_all_analysis_cases(self, generation: Optional[int] = None) -> list[AgentRun]:
        runs: list[AgentRun] = []
        for case in sorted(self.store.analysis_cases.values(), key=lambda item: item.sequence):
            with self._analysis_case_lock:
                if generation is not None and generation != self._analysis_case_generation:
                    break
            if case.status in {"COMPLETED", "MANUAL_REQUIRED", "WAITING_APPROVAL"}:
                continue
            try:
                runs.append(self.run_analysis_case(case.id, generation=generation))
            except _AnalysisCaseCancelled:
                break
        return runs

    def start_analysis_case_sequence(self) -> bool:
        with self._analysis_case_lock:
            if self._analysis_case_thread and self._analysis_case_thread.is_alive():
                return False
            self._analysis_case_generation += 1
            generation = self._analysis_case_generation
            self._analysis_case_thread = Thread(
                target=self.run_all_analysis_cases,
                args=(generation,),
                name="fishagent-analysis-case-sequence",
                daemon=True,
            )
            self._analysis_case_thread.start()
            return True

    def create_schedule(self, payload: dict) -> ScheduleDefinition:
        interval_seconds = int(payload.get("interval_seconds") or 300)
        if interval_seconds < 5:
            raise ValueError("interval_seconds must be at least 5")
        job_type = str(payload.get("job_type") or "patrol")
        if job_type not in {"patrol", "verification"}:
            raise ValueError("job_type must be patrol or verification")
        schedule = ScheduleDefinition(
            id=str(payload.get("id") or new_id("schedule")),
            name=str(payload.get("name") or "全场巡查"),
            job_type=job_type,
            interval_seconds=interval_seconds,
            next_run_at=utcnow() + timedelta(seconds=interval_seconds),
        )
        self.store.schedules[schedule.id] = schedule
        self.store.emit(
            "schedule.created",
            "创建调度：%s" % schedule.name,
            {"schedule_id": schedule.id, "job_type": schedule.job_type},
        )
        return schedule

    def set_schedule_status(self, schedule_id: str, status: ScheduleStatus) -> ScheduleDefinition:
        schedule = self.store.schedules[schedule_id]
        schedule.status = status
        self.store.emit(
            "schedule.status.changed",
            "%s 已%s" % (schedule.name, "暂停" if status == ScheduleStatus.PAUSED else "恢复"),
            {"schedule_id": schedule_id, "status": status.value},
        )
        return schedule

    def run_schedule_now(self, schedule_id: str) -> ScheduledJob:
        schedule = self.store.schedules[schedule_id]
        job = ScheduledJob(
            id=new_id("job"),
            job_type=schedule.job_type,
            idempotency_key="%s:%s" % (schedule.id, utcnow().isoformat()),
            due_at=utcnow(),
            schedule_id=schedule.id,
        )
        self.store.scheduled_jobs[job.id] = job
        self.store.emit("schedule.job.due", "调度已立即触发：%s" % schedule.name, {"job_id": job.id})
        return job

    def _enqueue_due_schedules(self) -> None:
        now = utcnow()
        for schedule in self.store.schedules.values():
            if schedule.status != ScheduleStatus.ACTIVE or not schedule.next_run_at:
                continue
            if schedule.next_run_at > now:
                continue
            due_at = schedule.next_run_at
            key = "schedule:%s:%s" % (schedule.id, int(due_at.timestamp()))
            if not any(job.idempotency_key == key for job in self.store.scheduled_jobs.values()):
                job = ScheduledJob(
                    id=new_id("job"),
                    job_type=schedule.job_type,
                    idempotency_key=key,
                    due_at=due_at,
                    schedule_id=schedule.id,
                )
                self.store.scheduled_jobs[job.id] = job
                self.store.emit("schedule.job.due", "周期调度已到期：%s" % schedule.name, {"job_id": job.id})
            schedule.last_run_at = due_at
            schedule.next_run_at = now + timedelta(seconds=schedule.interval_seconds)

    def run_patrol(self) -> AgentRun:
        run = AgentRun(id=new_id("run"), goal="执行全场巡查", status="RUNNING")
        self.store.agent_runs[run.id] = run
        run.step("supervisor-agent", "start_patrol", "读取全场养殖单元和活动事件")
        incidents_to_decide: list[str] = []
        for pond in self.store.ponds.values():
            latest_readings = [self.store.latest_reading(pond.id, spec["metric"]) for spec in DEMO_SENSOR_SPECS]
            available = [reading for reading in latest_readings if reading is not None]
            summary = "；".join(
                "%s %.2f%s" % (spec["name"], reading.value, reading.unit)
                for spec, reading in zip(DEMO_SENSOR_SPECS, latest_readings)
                if reading is not None
            ) or "暂无传感器读数"
            sensor_ids = {sensor.id for sensor in self.store.sensors.values() if sensor.pond_id == pond.id}
            unhealthy_sensors = [
                health
                for sensor_id, health in self.store.sensor_health.items()
                if sensor_id in sensor_ids and health.status != HealthStatus.ONLINE
            ]
            unhealthy_devices = [
                device for device in self.store.devices.values() if device.pond_id == pond.id and not device.healthy
            ]
            latest_do = next((reading for reading in available if reading.metric == "DO"), None)
            reasons = []
            if len(available) < len(DEMO_SENSOR_SPECS):
                reasons.append("传感器数据不完整")
            if latest_do is not None and latest_do.value < pond.dissolved_oxygen_min:
                reasons.append("溶氧低于安全线")
            for spec, reading in zip(DEMO_SENSOR_SPECS, latest_readings):
                if reading is None:
                    continue
                high_limit = WATER_QUALITY_HIGH_LIMITS.get(reading.metric)
                safe_range = WATER_QUALITY_RANGES.get(reading.metric)
                if high_limit is not None and reading.value > high_limit:
                    reasons.append("%s高于安全线" % spec["name"])
                elif safe_range is not None and not safe_range[0] <= reading.value <= safe_range[1]:
                    reasons.append("%s超出安全范围" % spec["name"])
            if unhealthy_sensors:
                reasons.append("%d 个传感器异常" % len(unhealthy_sensors))
            if unhealthy_devices:
                reasons.append("%d 台设备离线" % len(unhealthy_devices))
            active = self.store.active_incident_for_pond(pond.id)
            if active and not reasons:
                reasons.append("存在未关闭异常事件")
            status = "NEEDS_ATTENTION" if reasons or active else "NORMAL"
            run.step("sensor-monitor-agent", "inspect_pond", "%s：%s" % (pond.name, summary))
            finding = PatrolFinding(
                id=new_id("finding"),
                patrol_run_id=run.id,
                pond_id=pond.id,
                status=status,
                summary="%s%s" % (summary, "；异常：%s" % "、".join(reasons) if reasons else ""),
                evidence_refs=[reading.source_event_id for reading in available],
            )
            self.store.patrol_findings[finding.id] = finding
            if status == "NEEDS_ATTENTION" and active is None:
                active = Incident(
                    id=new_id("inc"),
                    pond_id=pond.id,
                    title="%s 巡查异常" % pond.name,
                    evidence=[
                        Evidence(
                            id=new_id("evi"),
                            type="patrol_finding",
                            summary=finding.summary,
                            refs=finding.evidence_refs,
                        )
                    ],
                )
                self.store.incidents[active.id] = active
                self.store.emit(
                    "incident.detected",
                    active.title,
                    {"incident_id": active.id, "pond_id": pond.id, "source": "patrol"},
                )
            if active and active.status == IncidentStatus.DETECTED:
                incidents_to_decide.append(active.id)
        run.status = "COMPLETED"
        run.stop_reason = "PATROL_COMPLETED"
        self.store.emit("patrol.completed", "全场巡查完成", {"run_id": run.id}, correlation_id=run.id)
        if self.agent_orchestrator is not None:
            for incident_id in dict.fromkeys(incidents_to_decide):
                decision_run = self.run_incident_flow(incident_id)
                run.step(
                    "supervisor-agent",
                    "dispatch_incident",
                    "异常已提交 CrewAI 决策：%s" % (decision_run.stop_reason or decision_run.status),
                )
        return run

    def run_chat(
        self,
        message: str,
        history: Optional[list[dict[str, str]]] = None,
        pond_id: Optional[str] = None,
    ) -> tuple[AgentRun, str]:
        normalized = message.strip()
        if not normalized:
            raise ValueError("message is required")
        effective_pond_id = pond_id or self._infer_chat_pond_id(normalized)
        if effective_pond_id and effective_pond_id not in self.store.ponds:
            raise ValueError("pond_id does not exist")
        run = AgentRun(id=new_id("run"), goal="对话：%s" % normalized[:80], status="RUNNING")
        self.store.agent_runs[run.id] = run
        orchestrator = self.agent_orchestrator
        if orchestrator is None or not orchestrator.available:
            run.status = "FAILED"
            run.stop_reason = "LLM_REQUIRED"
            reply = "CrewAI 未启用或模型 API Key 未配置"
            run.step("supervisor-agent", "chat.stop", reply)
            self.store.emit("agent.chat.failed", reply, {"run_id": run.id}, correlation_id=run.id)
            return run, reply
        result = orchestrator.chat(normalized, (history or [])[-12:], effective_pond_id)
        for agent, action, summary in result.steps:
            run.step(agent, action, summary)
        run.delegated_agents = sorted(set(run.delegated_agents + result.delegated_agents))
        run.status = "COMPLETED" if result.stop_reason == "CREW_CHAT_COMPLETED" else "FAILED"
        run.stop_reason = result.stop_reason
        self.store.emit(
            "agent.chat.completed" if run.status == "COMPLETED" else "agent.chat.failed",
            result.summary,
            {"run_id": run.id, "pond_id": effective_pond_id, "stop_reason": result.stop_reason},
            correlation_id=run.id,
        )
        return run, result.summary

    def _infer_chat_pond_id(self, message: str) -> Optional[str]:
        """Use an explicit pond token in a question to narrow chat evidence."""
        for candidate in self.store.ponds:
            match = re.fullmatch(r"([A-Za-z]+)[-_ ]?(\d+)", candidate)
            if match:
                prefix, number = match.groups()
                pattern = r"(?<![A-Za-z0-9])%s[-_ ]?%s(?![A-Za-z0-9])" % (
                    re.escape(prefix),
                    re.escape(number),
                )
            else:
                pattern = r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(candidate)
            if re.search(pattern, message, flags=re.IGNORECASE):
                return candidate
        return None

    def run_goal(self, goal: str, pond_id: Optional[str] = None) -> AgentRun:
        normalized = goal.strip()
        if not normalized:
            raise ValueError("goal is required")
        if pond_id and pond_id not in self.store.ponds:
            raise ValueError("pond_id does not exist")
        if self.agent_orchestrator is not None:
            active = self.store.active_incident_for_pond(pond_id) if pond_id else None
            if active and active.status == IncidentStatus.DETECTED:
                return self.run_incident_flow(active.id)
            if not self.agent_orchestrator.available:
                run = AgentRun(id=new_id("run"), goal=normalized, status="FAILED")
                self.store.agent_runs[run.id] = run
                run.stop_reason = "LLM_REQUIRED"
                run.step("supervisor-agent", "stop", "大模型未配置，系统不会使用硬编码规则替代模型决策")
                self.store.emit("agent.run.failed", "大模型未配置，无法执行模型驱动目标", {"run_id": run.id}, correlation_id=run.id)
                return run
            run = AgentRun(id=new_id("run"), goal=normalized, status="RUNNING")
            self.store.agent_runs[run.id] = run
            result = self.agent_orchestrator.run(normalized, pond_id)
            for agent, action, summary in result.steps:
                run.step(agent, action, summary)
            run.delegated_agents = sorted(set(run.delegated_agents + result.delegated_agents))
            run.status = "COMPLETED" if result.stop_reason == "CREW_COMPLETED" else "FAILED"
            run.stop_reason = result.stop_reason
            self.store.emit(
                "agent.run.completed" if run.status == "COMPLETED" else "agent.run.failed",
                result.summary,
                {"run_id": run.id, "stop_reason": result.stop_reason, "delegated_agents": run.delegated_agents},
                correlation_id=run.id,
            )
            return run
        if normalized.lower() in {"patrol", "巡查", "巡查全场", "全场巡查"}:
            return self.run_patrol()
        active = self.store.active_incident_for_pond(pond_id) if pond_id else None
        if active and active.status == IncidentStatus.DETECTED:
            return self.run_incident_flow(active.id)
        run = AgentRun(id=new_id("run"), goal=normalized, status="RUNNING")
        self.store.agent_runs[run.id] = run
        run.step("supervisor-agent", "interpret_goal", "解析用户目标并检查可用证据")
        if pond_id:
            latest = self.store.latest_reading(pond_id, "DO")
            run.step(
                "sensor-monitor-agent",
                "get_pond_snapshot",
                "%s" % ("读取到最新溶氧 %.2fmg/L" % latest.value if latest else "暂无最新溶氧读数"),
            )
        run.status = "COMPLETED"
        run.stop_reason = "NO_ACTION_NEEDED"
        self.store.emit("agent.run.completed", "用户目标已完成：%s" % normalized, {"run_id": run.id}, correlation_id=run.id)
        return run

    def _create_verification_plan(self, incident: Incident, due_at: datetime) -> VerificationPlan:
        existing = self.store.verification_plans.get(incident.verification_plan_id or "")
        if existing:
            existing.earliest_at = due_at
            existing.latest_at = due_at + timedelta(seconds=60)
            return existing
        pond = self.store.ponds[incident.pond_id]
        plan = VerificationPlan(
            id=new_id("verify-plan"),
            incident_id=incident.id,
            threshold=pond.dissolved_oxygen_min,
            earliest_at=due_at,
            latest_at=due_at + timedelta(seconds=60),
        )
        self.store.verification_plans[plan.id] = plan
        incident.verification_plan_id = plan.id
        return plan

    def _schedule_verification(self, incident: Incident, due_at: datetime) -> ScheduledJob:
        self._create_verification_plan(incident, due_at)
        incident.verification_due_at = due_at
        key = "verification:%s" % incident.id
        existing = next(
            (job for job in self.store.scheduled_jobs.values() if job.idempotency_key == key),
            None,
        )
        if existing:
            existing.due_at = due_at
            existing.status = JobStatus.DUE
            return existing
        job = ScheduledJob(
            id=new_id("job"),
            job_type="verification",
            idempotency_key=key,
            due_at=due_at,
            incident_id=incident.id,
        )
        self.store.scheduled_jobs[job.id] = job
        self.store.emit(
            "verification.scheduled",
            "已安排复核：%s" % incident.title,
            {"incident_id": incident.id, "job_id": job.id, "due_at": due_at.isoformat()},
        )
        return job

    def create_manual_task(
        self,
        title: str,
        description: str,
        incident_id: Optional[str] = None,
        assignee: str = "现场操作员",
        priority: str = "HIGH",
    ) -> ManualTask:
        task = ManualTask(
            id=new_id("task"),
            incident_id=incident_id,
            title=title,
            description=description,
            assignee=assignee,
            priority=priority,
        )
        self.store.manual_tasks[task.id] = task
        if incident_id and incident_id in self.store.incidents:
            self.store.incidents[incident_id].manual_task_ids.append(task.id)
        self.store.emit("manual_task.created", title, {"task_id": task.id, "incident_id": incident_id})
        return task

    def complete_manual_task(self, task_id: str) -> ManualTask:
        task = self.store.manual_tasks[task_id]
        task.status = TaskStatus.COMPLETED
        task.completed_at = utcnow()
        self.store.emit("manual_task.completed", task.title, {"task_id": task.id})
        return task

    def ingest_reading(
        self,
        pond_id: str,
        value: float,
        metric: str = "DO",
        unit: Optional[str] = None,
        source_event_id: Optional[str] = None,
        seconds_old: int = 0,
        sensor_id: Optional[str] = None,
        quality: str = "GOOD",
        auto_run: bool = True,
    ) -> Optional[Incident]:
        if pond_id not in self.store.ponds:
            raise ValueError("pond_id does not exist")
        if quality not in {"GOOD", "SUSPECT", "STALE", "INVALID"}:
            raise ValueError("unsupported reading quality")
        metric = metric.upper()
        spec = DEMO_SENSOR_BY_METRIC.get(metric)
        resolved_sensor_id = sensor_id or "%s-%s" % ((spec or {"slug": metric.lower()})["slug"], pond_id.lower())
        sensor = self.store.sensors.get(resolved_sensor_id)
        if sensor and (sensor.pond_id != pond_id or sensor.metric.upper() != metric):
            raise ValueError("sensor_id does not match pond_id and metric")
        resolved_unit = unit or (sensor.unit if sensor else None) or (spec["unit"] if spec else "")
        if not resolved_unit:
            raise ValueError("unit is required for unsupported metrics")
        reading = SensorReading(
            pond_id=pond_id,
            sensor_id=resolved_sensor_id,
            metric=metric,
            value=value,
            unit=resolved_unit,
            sampled_at=utcnow() - timedelta(seconds=seconds_old),
            quality=quality,
            source_event_id=source_event_id or new_id("reading"),
        )
        health = self.store.sensor_health.setdefault(resolved_sensor_id, SensorHealth(sensor_id=resolved_sensor_id))
        health.last_heartbeat_at = utcnow()
        health.last_reading_at = reading.sampled_at
        health.status = HealthStatus.ONLINE if quality == "GOOD" else HealthStatus.ERROR
        health.message = "" if quality == "GOOD" else "读数质量：%s" % quality
        incident = self.store.add_reading(reading)
        if incident is None and quality != "GOOD" and auto_run:
            incident = self.store.active_incident_for_pond(pond_id)
            evidence = Evidence(
                id=new_id("evi"),
                type="sensor_health",
                summary="%s 传感器读数质量异常：%s" % ((spec or {"name": metric})["name"], quality),
                refs=[reading.source_event_id],
            )
            if incident:
                incident.evidence.append(evidence)
            else:
                incident = Incident(
                    id=new_id("inc"),
                    pond_id=pond_id,
                    title="%s %s传感器异常" % (self.store.ponds[pond_id].name, (spec or {"name": metric})["name"]),
                    evidence=[evidence],
                )
                self.store.incidents[incident.id] = incident
                self.store.emit(
                    "incident.detected",
                    incident.title,
                    {"incident_id": incident.id, "pond_id": pond_id, "metric": metric, "quality": quality},
                )
        if incident and incident.status == IncidentStatus.DETECTED and auto_run:
            self.run_incident_flow(incident.id)
        return incident

    def ingest_do(
        self,
        pond_id: str,
        value: float,
        source_event_id: Optional[str] = None,
        seconds_old: int = 0,
        sensor_id: Optional[str] = None,
        quality: str = "GOOD",
        auto_run: bool = True,
    ) -> Optional[Incident]:
        return self.ingest_reading(
            pond_id=pond_id,
            value=value,
            metric="DO",
            unit="mg/L",
            source_event_id=source_event_id,
            seconds_old=seconds_old,
            sensor_id=sensor_id,
            quality=quality,
            auto_run=auto_run,
        )

    def run_incident_flow(self, incident_id: str, risk_override: Optional[RiskLevel] = None) -> AgentRun:
        if self.agent_orchestrator is not None:
            return self._run_llm_incident_flow(incident_id)
        return self._run_rule_incident_flow(incident_id, risk_override)

    def _incident_llm_context(self, incident_id: str) -> dict:
        snapshot = self._snapshot(persist=False)
        incident = next(item for item in snapshot["incidents"] if item["id"] == incident_id)
        pond_id = incident["pond_id"]
        analysis_case = next(
            (item for item in snapshot.get("analysis_cases", []) if item.get("incident_id") == incident_id),
            None,
        )
        return {
            "incident": incident,
            "pond": next((item for item in snapshot["ponds"] if item["id"] == pond_id), None),
            "readings": [item for item in snapshot["readings"] if item["pond_id"] == pond_id][-20:],
            "sensor_health": [item for item in snapshot["sensor_health"] if item["sensor_id"] in {sensor["id"] for sensor in snapshot["sensors"] if sensor["pond_id"] == pond_id}],
            "devices": [item for item in snapshot["devices"] if item["pond_id"] == pond_id],
            "cameras": [item for item in snapshot["cameras"] if item["pond_id"] == pond_id],
            "active_incidents": [item for item in snapshot["incidents"] if item["status"] not in {"RESOLVED", "DISMISSED"}],
            "weather_observations": [item for item in snapshot.get("weather_observations", []) if item["pond_id"] == pond_id],
            "camera_observations": [item for item in snapshot.get("camera_observations", []) if item["pond_id"] == pond_id],
            "disease_knowledge": snapshot.get("disease_knowledge", []),
            "analysis_case": analysis_case,
        }

    def _llm_manual_stop(self, incident: Incident, run: AgentRun, reason: str, summary: str) -> AgentRun:
        if incident.status == IncidentStatus.INVESTIGATING:
            incident.transition(IncidentStatus.ACTION_PROPOSED)
        if incident.status == IncidentStatus.ACTION_PROPOSED:
            incident.transition(IncidentStatus.MANUAL_REQUIRED)
        incident.assignee = "现场操作员"
        detail = str(summary or "未提供模型错误详情").strip()
        if len(detail) > 600:
            detail = detail[:600] + "..."
        manual_description = "错误码：%s；原因：%s" % (reason, detail)
        self.create_manual_task(
            title="模型驱动处置待人工确认：%s" % incident.title,
            description=manual_description,
            incident_id=incident.id,
        )
        run.step("execution-agent", "route_manual_task", manual_description)
        run.status = "COMPLETED"
        run.stop_reason = reason
        self.store.emit("agent.run.completed", summary, {"run_id": run.id, "reason": reason}, correlation_id=run.id)
        return run

    @staticmethod
    def _decide_incident_with_timeout(orchestrator: Any, context: dict, timeout_seconds: int) -> Any:
        results: Queue[tuple[bool, Any]] = Queue(maxsize=1)

        def invoke() -> None:
            try:
                results.put((True, orchestrator.decide_incident(context)))
            except Exception as exc:
                results.put((False, exc))

        Thread(target=invoke, name="crewai-incident-decision", daemon=True).start()
        try:
            succeeded, value = results.get(timeout=max(1, timeout_seconds))
        except Empty as exc:
            raise TimeoutError("CrewAI incident decision exceeded its runtime budget") from exc
        if not succeeded:
            raise cast(Exception, value)
        return value

    def _run_llm_incident_flow(self, incident_id: str) -> AgentRun:
        incident = self.store.incidents[incident_id]
        run = AgentRun(id=new_id("run"), goal="模型驱动处理 %s" % incident.title, incident_id=incident_id, status="RUNNING")
        run.budget["seconds"] = self.agent_decision_timeout_seconds
        self.store.agent_runs[run.id] = run
        self.store.emit("agent.run.started", run.goal, {"run_id": run.id, "mode": "llm"}, correlation_id=run.id)
        incident.transition(IncidentStatus.INVESTIGATING)
        orchestrator = cast(Any, self.agent_orchestrator)
        try:
            result = self._decide_incident_with_timeout(
                orchestrator,
                self._incident_llm_context(incident_id),
                run.budget.get("seconds", self.agent_decision_timeout_seconds),
            )
        except TimeoutError:
            run.step(
                "supervisor-agent",
                "incident.timeout",
                "CrewAI 超过 %s 秒运行预算，迟到结果已作废" % run.budget.get("seconds", self.agent_decision_timeout_seconds),
            )
            return self._llm_manual_stop(incident, run, "LLM_TIMEOUT", "模型决策超时，已转人工确认")
        except Exception as exc:
            run.step("supervisor-agent", "incident.failed", "模型调用失败：%s" % exc)
            return self._llm_manual_stop(incident, run, "LLM_UNAVAILABLE", "模型不可用，已转人工确认")
        for agent, action, summary in result.steps:
            run.step(agent, action, summary)
        run.delegated_agents = sorted(set(run.delegated_agents + result.delegated_agents))
        decision: Optional[IncidentDecision] = result.decision
        if decision is None:
            reason = result.stop_reason if result.stop_reason.startswith("LLM_") else "LLM_MODEL_OR_TOOL_FAILURE"
            return self._llm_manual_stop(incident, run, reason, result.summary)

        run.step("execution-agent", "validate_llm_decision", "结构化动作通过协议校验，提交安全策略门")
        incident.transition(IncidentStatus.ACTION_PROPOSED)
        try:
            risk = RiskLevel(decision.risk)
            if decision.action in {"NO_ACTION", "REFRESH_EVIDENCE", "MANUAL_REQUIRED"}:
                return self._llm_manual_stop(incident, run, "LLM_%s" % decision.action, decision.rationale)
            if risk in {RiskLevel.L2, RiskLevel.L3} or decision.action == "REQUEST_APPROVAL":
                proposal_risk = RiskLevel.L2 if risk == RiskLevel.L1 else risk
                proposal = self.propose_action(
                    incident_id=incident.id,
                    device_id=decision.device_id,
                    target_state=decision.target_state,
                    risk=proposal_risk,
                    rationale=decision.rationale,
                )
                if proposal.status in {"REJECTED", "FAILED"}:
                    return self._llm_manual_stop(
                        incident,
                        run,
                        "LLM_POLICY_REJECTED",
                        "模型建议未通过设备策略门，已转人工确认",
                    )
                run.step("execution-agent", "route_action", "模型建议已转为 %s 受控流程" % proposal_risk.value)
                run.status = "WAITING_APPROVAL" if proposal_risk == RiskLevel.L2 else "COMPLETED"
                run.stop_reason = "WAITING_APPROVAL" if proposal_risk == RiskLevel.L2 else "MANUAL_REQUIRED"
                return run
            if decision.action != "EXECUTE" or risk != RiskLevel.L1:
                return self._llm_manual_stop(incident, run, "LLM_ACTION_REQUIRES_REVIEW", decision.rationale)
            command = self.device_control_skill.execute(
                run,
                incident,
                decision,
                multimodal_evidence=bool(set(decision.evidence_refs) & {ref for evidence in incident.evidence for ref in evidence.refs}),
            )
        except (KeyError, TypeError, ValueError) as exc:
            return self._llm_manual_stop(incident, run, "LLM_ACTION_INVALID", str(exc))
        if command.status == CommandStatus.CONFIRMED:
            incident.transition(IncidentStatus.EXECUTING)
            device = self.store.devices[decision.device_id]
            if device.capability == "aeration":
                incident.transition(IncidentStatus.VERIFY_PENDING)
                self._schedule_verification(incident, utcnow() + timedelta(seconds=decision.verification_delay_seconds))
            else:
                incident.transition(IncidentStatus.RESOLVED)
            run.status = "COMPLETED"
            run.stop_reason = "LLM_ACTION_EXECUTED"
            self.store.emit("agent.run.completed", decision.rationale, {"run_id": run.id, "command_id": command.id}, correlation_id=run.id)
        else:
            incident.transition(IncidentStatus.ACTION_FAILED)
            incident.transition(IncidentStatus.ESCALATED)
            run.status = "FAILED"
            run.stop_reason = "LLM_POLICY_REJECTED"
            self.store.emit("agent.run.failed", command.policy_reason, {"run_id": run.id, "command_id": command.id}, correlation_id=run.id)
        return run

    def _run_rule_incident_flow(self, incident_id: str, risk_override: Optional[RiskLevel] = None) -> AgentRun:
        incident = self.store.incidents[incident_id]
        run = AgentRun(id=new_id("run"), goal="处理 %s" % incident.title, incident_id=incident_id, status="RUNNING")
        self.store.agent_runs[run.id] = run
        self.store.emit("agent.run.started", run.goal, {"run_id": run.id}, correlation_id=run.id)

        incident.transition(IncidentStatus.INVESTIGATING)
        run.step("supervisor-agent", "validate_trigger", "确认触发源为低溶氧传感器事件")

        evidence_refs = {ref for evidence in incident.evidence for ref in evidence.refs}
        trigger_readings = [
            reading
            for reading in self.store.readings
            if reading.pond_id == incident.pond_id and reading.source_event_id in evidence_refs
        ]
        latest_do = max(trigger_readings, key=lambda reading: reading.sampled_at) if trigger_readings else None
        run.step("sensor-monitor-agent", "get_pond_snapshot", "读取最新溶氧、水质质量和采样时间")
        if latest_do is None or not latest_do.is_fresh():
            run.step("supervisor-agent", "stop", "核心数据过期或缺失，要求刷新数据")
            run.status = "FAILED"
            run.stop_reason = "STALE_EVIDENCE"
            self.store.emit("agent.run.failed", "证据过期，未执行设备动作", {"run_id": run.id}, correlation_id=run.id)
            return run

        device = self.store.aeration_device_for_pond(incident.pond_id)
        if device is None:
            run.step("patrol-analysis-agent", "get_device_capabilities", "未找到可用增氧设备，升级人工处理")
            incident.transition(IncidentStatus.ACTION_PROPOSED)
            incident.transition(IncidentStatus.MANUAL_REQUIRED)
            incident.assignee = "现场操作员"
            self.create_manual_task(
                title="检查 %s 的增氧设备" % incident.title,
                description="没有找到健康且具备 aeration 能力的设备，请现场确认设备或手动增氧。",
                incident_id=incident.id,
            )
            run.status = "COMPLETED"
            run.stop_reason = "NO_CAPABLE_DEVICE"
            self.store.emit("agent.run.completed", "未找到可用增氧设备，已转人工", {"run_id": run.id}, correlation_id=run.id)
            return run
        run.step("patrol-analysis-agent", "get_device_shadow_state", "%s 当前为 %s" % (device.name, device.shadow_state))

        if device.shadow_state == "on":
            run.step("supervisor-agent", "route", "设备已开启，停止重复执行并转向效果复核/故障调查")
            incident.transition(IncidentStatus.ACTION_PROPOSED)
            incident.transition(IncidentStatus.EXECUTING)
            incident.transition(IncidentStatus.VERIFY_PENDING)
            self._schedule_verification(incident, utcnow())
            run.status = "COMPLETED"
            run.stop_reason = "ALREADY_SATISFIED"
            self.store.emit("agent.run.completed", "设备已在目标状态，已抑制重复动作", {"run_id": run.id}, correlation_id=run.id)
            return run

        run.step("action-planning-agent", "propose_action", "建议开启 %s，风险 L1，30 秒后复核溶氧" % device.name)
        incident.transition(IncidentStatus.ACTION_PROPOSED)

        risk = risk_override or RiskLevel.L1
        if risk != RiskLevel.L1:
            proposal = self.propose_action(
                incident_id=incident.id,
                device_id=device.id,
                target_state="on",
                risk=risk,
                rationale="低溶氧事件需要对设备执行受控动作",
            )
            run.step("execution-agent", "propose_action", proposal.rationale)
            run.status = "WAITING_APPROVAL" if risk == RiskLevel.L2 else "COMPLETED"
            run.stop_reason = "WAITING_APPROVAL" if risk == RiskLevel.L2 else "MANUAL_REQUIRED"
            return run

        command = self.request_action_execution(run, incident, device_id=device.id, target_state="on", risk=risk)
        if command.status == CommandStatus.CONFIRMED:
            incident.transition(IncidentStatus.EXECUTING)
            incident.transition(IncidentStatus.VERIFY_PENDING)
            self._schedule_verification(incident, utcnow() + timedelta(seconds=30))
            run.status = "COMPLETED"
            run.stop_reason = "ACTION_EXECUTED"
            self.store.emit("agent.run.completed", "增氧命令已确认，等待复核", {"run_id": run.id}, correlation_id=run.id)
        elif command.policy_reason.startswith("设备影子状态"):
            incident.transition(IncidentStatus.EXECUTING)
            incident.transition(IncidentStatus.VERIFY_PENDING)
            self._schedule_verification(incident, utcnow())
            run.status = "COMPLETED"
            run.stop_reason = "ALREADY_SATISFIED"
        else:
            incident.transition(IncidentStatus.ACTION_FAILED)
            incident.transition(IncidentStatus.ESCALATED)
            run.status = "FAILED"
            run.stop_reason = "POLICY_REJECTED"
        return run

    def propose_action(
        self,
        incident_id: str,
        device_id: str,
        target_state: str,
        risk: RiskLevel,
        rationale: str,
    ) -> ActionProposal:
        incident = self.store.incidents[incident_id]
        device = self.store.devices.get(device_id)
        if device is None:
            raise ValueError("device_id does not exist")
        latest_do = self.store.latest_reading(incident.pond_id, "DO")
        policy = evaluate_action(
            actor="execution-agent",
            device=device,
            pond_id=incident.pond_id,
            target_state=target_state,
            risk=risk,
            latest_do=latest_do,
            idempotency_seen=False,
            multimodal_evidence=any(evidence.type == "multimodal_analysis" for evidence in incident.evidence),
        )
        status = "PENDING_APPROVAL" if policy.status == "WAITING_APPROVAL" else policy.status
        proposal = ActionProposal(
            id=new_id("proposal"),
            incident_id=incident.id,
            device_id=device.id,
            pond_id=incident.pond_id,
            target_state=target_state,
            risk=risk,
            rationale=rationale or policy.reason,
            evidence_refs=([latest_do.source_event_id] if latest_do else [ref for evidence in incident.evidence for ref in evidence.refs])[:20],
            status=status,
        )
        self.store.action_proposals[proposal.id] = proposal
        incident.action_proposal_ids.append(proposal.id)
        self.store.emit(
            "action.proposal.created",
            proposal.rationale,
            {"proposal_id": proposal.id, "risk": risk.value, "policy_status": policy.status},
        )
        if policy.status == "WAITING_APPROVAL":
            approval = Approval(id=new_id("approval"), proposal_id=proposal.id, incident_id=incident.id)
            proposal.approval_id = approval.id
            self.store.approvals[approval.id] = approval
            if incident.status == IncidentStatus.ACTION_PROPOSED:
                incident.transition(IncidentStatus.WAITING_APPROVAL)
            self.store.emit(
                "approval.requested",
                "中风险动作等待人工审批",
                {"approval_id": approval.id, "proposal_id": proposal.id},
            )
        elif policy.status == "MANUAL_REQUIRED":
            proposal.status = "MANUAL_REQUIRED"
            if incident.status == IncidentStatus.ACTION_PROPOSED:
                incident.transition(IncidentStatus.MANUAL_REQUIRED)
            self.create_manual_task(
                title="人工执行：%s" % incident.title,
                description=proposal.rationale,
                incident_id=incident.id,
            )
        return proposal

    def approve_action(self, proposal_id: str, approver: str, reason: str = "") -> DeviceCommand:
        proposal = self.store.action_proposals[proposal_id]
        if not proposal.approval_id:
            raise ValueError("proposal does not require approval")
        approval = self.store.approvals[proposal.approval_id]
        if approval.status != ApprovalStatus.PENDING:
            raise ValueError("approval is no longer pending")
        incident = self.store.incidents[proposal.incident_id]
        approval.status = ApprovalStatus.APPROVED
        approval.decided_by = approver
        approval.reason = reason
        approval.decided_at = utcnow()
        proposal.status = "APPROVED"
        run = AgentRun(id=new_id("run"), goal="执行已批准动作", incident_id=incident.id, status="RUNNING")
        self.store.agent_runs[run.id] = run
        if incident.status == IncidentStatus.WAITING_APPROVAL:
            incident.transition(IncidentStatus.EXECUTING)
        command = self.request_action_execution(
            run,
            incident,
            device_id=proposal.device_id,
            target_state=proposal.target_state,
            risk=proposal.risk,
            approval_granted=True,
            multimodal_evidence=any(evidence.type == "multimodal_analysis" for evidence in incident.evidence),
        )
        if command.status == CommandStatus.CONFIRMED:
            if incident.status == IncidentStatus.EXECUTING:
                device = self.store.devices[proposal.device_id]
                if device.capability == "aeration":
                    incident.transition(IncidentStatus.VERIFY_PENDING)
                    self._schedule_verification(incident, utcnow() + timedelta(seconds=30))
                else:
                    incident.transition(IncidentStatus.RESOLVED)
            run.status = "COMPLETED"
            run.stop_reason = "ACTION_EXECUTED_AFTER_APPROVAL"
        else:
            if incident.status == IncidentStatus.EXECUTING:
                incident.transition(IncidentStatus.ACTION_FAILED)
                incident.transition(IncidentStatus.ESCALATED)
            run.status = "FAILED"
            run.stop_reason = "POLICY_REJECTED"
        self.store.emit(
            "approval.approved",
            "动作已由 %s 批准" % approver,
            {"approval_id": approval.id, "proposal_id": proposal.id},
            correlation_id=run.id,
        )
        return command

    def reject_action(self, proposal_id: str, approver: str, reason: str) -> Approval:
        proposal = self.store.action_proposals[proposal_id]
        if not proposal.approval_id:
            raise ValueError("proposal does not require approval")
        approval = self.store.approvals[proposal.approval_id]
        if approval.status != ApprovalStatus.PENDING:
            raise ValueError("approval is no longer pending")
        approval.status = ApprovalStatus.REJECTED
        approval.decided_by = approver
        approval.reason = reason
        approval.decided_at = utcnow()
        proposal.status = "REJECTED"
        incident = self.store.incidents[proposal.incident_id]
        if incident.status == IncidentStatus.WAITING_APPROVAL:
            incident.transition(IncidentStatus.DISMISSED)
        self.store.emit(
            "approval.rejected",
            "动作审批被拒绝：%s" % reason,
            {"approval_id": approval.id, "proposal_id": proposal.id},
        )
        return approval

    def request_action_execution(
        self,
        run: AgentRun,
        incident: Incident,
        device_id: str,
        target_state: str,
        risk: RiskLevel,
        approval_granted: bool = False,
        idempotency_key: Optional[str] = None,
        multimodal_evidence: bool = False,
    ) -> DeviceCommand:
        device = self.store.devices[device_id]
        latest_do = self.store.latest_reading(incident.pond_id, "DO")
        idempotency_key = idempotency_key or "%s:%s:%s" % (incident.pond_id, device_id, target_state)
        existing = next(
            (item for item in self.store.commands.values() if item.idempotency_key == idempotency_key),
            None,
        )
        if existing:
            if existing.device_id != device_id or existing.pond_id != incident.pond_id or existing.target_state != target_state:
                raise ValueError("idempotency key conflicts with an existing device command")
            run.step("execution-agent", "deduplicate_command", "幂等键已存在，复用已有设备命令")
            return existing
        policy = evaluate_action(
            actor="execution-agent",
            device=device,
            pond_id=incident.pond_id,
            target_state=target_state,
            risk=risk,
            latest_do=latest_do,
            idempotency_seen=idempotency_key in self.store.executed_idempotency_keys,
            approval_granted=approval_granted,
            multimodal_evidence=multimodal_evidence,
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
        try:
            result = self.device_gateway.send_command(device, target_state, idempotency_key)
        except Exception as exc:
            result = None
            command.status = CommandStatus.FAILED
            self.store.emit("device.command.failed", "设备网关调用失败：%s" % exc, {"command_id": command.id}, correlation_id=run.id)
        if result:
            command.status = CommandStatus.SENT
            if result.acknowledged:
                command.status = CommandStatus.ACKNOWLEDGED
            if result.confirmed:
                device.shadow_state = target_state
                command.status = CommandStatus.CONFIRMED
                self.store.executed_idempotency_keys[idempotency_key] = command.id
                self.store.emit("device.command.confirmed", "%s 已切换为 %s" % (device.name, target_state), {"command_id": command.id}, correlation_id=run.id)
            else:
                command.status = CommandStatus.TIMED_OUT if result.acknowledged else CommandStatus.FAILED
                self.store.emit("device.command.unconfirmed", result.detail or "设备命令未确认", {"command_id": command.id}, correlation_id=run.id)
        return command

    def verify_incident(self, incident_id: str) -> Incident:
        incident = self.store.incidents[incident_id]
        if incident.status != IncidentStatus.VERIFY_PENDING:
            return incident
        plan = self.store.verification_plans.get(incident.verification_plan_id or "")
        run = AgentRun(id=new_id("run"), goal="复核 %s" % incident.title, incident_id=incident.id, status="RUNNING")
        self.store.agent_runs[run.id] = run
        latest_do = self.store.latest_reading(incident.pond_id, "DO")
        run.step("verification-agent", "record_verification", "读取复核溶氧并判断处置效果")
        passed = bool(
            latest_do
            and latest_do.is_fresh()
            and latest_do.value >= self.store.ponds[incident.pond_id].dissolved_oxygen_min
        )
        result = VerificationResult(
            id=new_id("verify"),
            incident_id=incident.id,
            plan_id=plan.id if plan else "",
            outcome="PASSED" if passed else "FAILED",
            observed_value=latest_do.value if latest_do else None,
            evidence_refs=[latest_do.source_event_id] if latest_do else [],
        )
        self.store.verification_results[result.id] = result
        incident.verification_result_ids.append(result.id)
        if plan:
            plan.status = result.outcome
        for job in self.store.scheduled_jobs.values():
            if job.incident_id == incident.id and job.job_type == "verification":
                job.status = JobStatus.COMPLETED
                job.attempts += 1
        if passed:
            incident.transition(IncidentStatus.RESOLVED)
            run.status = "COMPLETED"
            run.stop_reason = "RESOLVED"
            self.store.emit("verification.resolved", "复核通过，事件关闭", {"incident_id": incident.id}, correlation_id=run.id)
        else:
            incident.transition(IncidentStatus.VERIFY_FAILED)
            incident.transition(IncidentStatus.ESCALATED)
            incident.assignee = "现场操作员"
            run.step("verification-agent", "create_manual_task", "复核未恢复，升级设备故障与人工处理")
            manual_task = self.create_manual_task(
                title="处理复核失败：%s" % incident.title,
                description="复核溶氧仍未达到安全线，请检查增氧设备、供电和水体状态。",
                incident_id=incident.id,
            )
            escalation = Escalation(
                id=new_id("escalation"),
                incident_id=incident.id,
                level="L2",
                reason="复核失败，设备动作效果未被新鲜传感器证据确认",
                manual_task_id=manual_task.id,
            )
            self.store.escalations[escalation.id] = escalation
            run.status = "COMPLETED"
            run.stop_reason = "ESCALATED"
            self.store.emit("verification.escalated", "复核失败，已升级人工任务", {"incident_id": incident.id}, correlation_id=run.id)
        return incident

    def run_due_jobs(self, limit: int = 50) -> list[ScheduledJob]:
        with self._job_lock:
            jobs = self.claim_due_jobs(limit)
            completed = []
            for job in jobs:
                try:
                    self.execute_scheduled_job(job.id)
                    completed.append(job)
                except Exception:
                    # execute_scheduled_job records retry/dead-letter state.
                    continue
            return completed

    def claim_due_jobs(self, limit: int = 50) -> list[ScheduledJob]:
        """Atomically move due work out of DUE before a worker executes it."""
        with self._job_lock:
            self._enqueue_due_schedules()
            self.recover_stuck_jobs()
            jobs = self.store.due_jobs()[:limit]
            for job in jobs:
                job.status = JobStatus.RUNNING
                job.attempts += 1
                self.store.emit("schedule.job.claimed", "后台作业已领取", {"job_id": job.id})
            if jobs:
                self.snapshot()
            return jobs

    def recover_stuck_jobs(self, max_age_seconds: int = 300) -> list[ScheduledJob]:
        """Return abandoned RUNNING jobs to the durable queue after a worker restart."""
        cutoff = utcnow() - timedelta(seconds=max_age_seconds)
        recovered: list[ScheduledJob] = []
        for job in self.store.scheduled_jobs.values():
            if job.status != JobStatus.RUNNING or job.created_at > cutoff:
                continue
            if job.attempts >= 3:
                job.status = JobStatus.DEAD_LETTER
                self.store.emit("schedule.job.dead_letter", "作业重启恢复次数已耗尽", {"job_id": job.id})
            else:
                job.status = JobStatus.DUE
                job.due_at = utcnow()
                recovered.append(job)
                self.store.emit("schedule.job.recovered", "Worker 重启后恢复作业", {"job_id": job.id})
        if recovered:
            self.snapshot()
        return recovered

    def execute_scheduled_job(self, job_id: str) -> ScheduledJob:
        """Execute one claimed job with retry and dead-letter semantics."""
        with self._job_lock:
            job = self.store.scheduled_jobs[job_id]
            if job.status == JobStatus.COMPLETED:
                return job
            if job.status != JobStatus.RUNNING:
                job.status = JobStatus.RUNNING
                job.attempts += 1
            try:
                if job.job_type == "verification" and job.incident_id:
                    self.verify_incident(job.incident_id)
                elif job.job_type == "patrol":
                    self.run_patrol()
                job.status = JobStatus.COMPLETED
                self.store.emit("schedule.job.completed", "后台作业已完成", {"job_id": job.id})
            except Exception as exc:
                job.status = JobStatus.RETRY_WAIT if job.attempts < 3 else JobStatus.DEAD_LETTER
                if job.status == JobStatus.RETRY_WAIT:
                    job.due_at = utcnow() + timedelta(seconds=2 ** job.attempts)
                self.store.emit(
                    "schedule.job.failed",
                    "后台作业执行失败：%s" % exc,
                    {"job_id": job.id, "status": job.status.value, "attempts": job.attempts},
                )
                self.snapshot()
                raise
            self.snapshot()
            return job

    def run_demo(self, mode: str) -> dict:
        self._cancel_analysis_case_sequence()
        self._reset_demo_with_telemetry()
        if mode == "alerts":
            self._demo_reading("B-01", 2.8, source_event_id="demo-alert-do")
            self._demo_reading(
                "B-02",
                0.82,
                source_event_id="demo-alert-ammonia",
                metric="AMMONIA",
            )
        elif mode == "approval":
            incident = self._demo_reading("B-01", 2.1, source_event_id="demo-approval", auto_run=False)
            if incident:
                self.run_incident_flow(incident.id, risk_override=RiskLevel.L2)
        elif mode == "dedup":
            self.store.devices["aerator-b01-1"].shadow_state = "on"
            incident = self._demo_reading("B-01", 2.1, source_event_id="demo-dedup")
            if incident:
                self.store.force_verification_due(incident.id)
                self.verify_incident(incident.id)
        elif mode == "failure":
            incident = self._demo_reading("B-01", 2.1, source_event_id="demo-failure")
            if incident:
                self.store.force_verification_due(incident.id)
                self._demo_reading("B-01", 2.3, source_event_id="demo-failure-review")
                self.verify_incident(incident.id)
        else:
            incident = self._demo_reading("B-01", 2.1, source_event_id="demo-success")
            if incident:
                self.store.force_verification_due(incident.id)
                self._demo_reading("B-01", 5.2, source_event_id="demo-success-review")
                self.verify_incident(incident.id)
        return self.snapshot()

    def snapshot(self) -> dict:
        return self._snapshot(persist=True)

    def read_snapshot(self) -> dict:
        """Refresh a read-only process view without overwriting newer writes."""
        if self.repository:
            persisted = self.repository.load()
            if persisted:
                self.store.restore_snapshot(persisted)
        return self._snapshot(persist=False)

    def _snapshot(self, persist: bool = True) -> dict:
        is_demo_dataset = any(reading.source_event_id.startswith("demo-seed-") for reading in self.store.readings)
        data = {
            "dataset": {
                "dataset_id": "four_pond_water_quality_demo_v2",
                "source_classification": "simulated_persistent",
                "description": "四池塘七指标水质演示数据",
            }
            if is_demo_dataset
            else None,
            "farms": [farm.__dict__ for farm in self.store.farms.values()],
            "zones": [zone.__dict__ for zone in self.store.zones.values()],
            "ponds": [pond.__dict__ for pond in self.store.ponds.values()],
            "sensors": [sensor.__dict__ for sensor in self.store.sensors.values()],
            "sensor_health": [
                {
                    "sensor_id": health.sensor_id,
                    "status": health.status.value,
                    "last_heartbeat_at": health.last_heartbeat_at.isoformat() if health.last_heartbeat_at else None,
                    "last_reading_at": health.last_reading_at.isoformat() if health.last_reading_at else None,
                    "error_count": health.error_count,
                    "drift_score": health.drift_score,
                    "message": health.message,
                }
                for health in self.store.sensor_health.values()
            ],
            "devices": [device.__dict__ for device in self.store.devices.values()],
            "readings": [
                {
                    "pond_id": reading.pond_id,
                    "sensor_id": reading.sensor_id,
                    "metric": reading.metric,
                    "value": reading.value,
                    "unit": reading.unit,
                    "sampled_at": reading.sampled_at.isoformat(),
                    "received_at": reading.received_at.isoformat(),
                    "quality": reading.quality,
                    "source_event_id": reading.source_event_id,
                }
                for reading in self.store.readings[-500:]
            ],
            "cameras": [
                {
                    "id": camera.id,
                    "pond_id": camera.pond_id,
                    "name": camera.name,
                    "source_type": camera.source_type,
                    "camera_role": camera.camera_role,
                    "status": camera.status,
                    "last_frame_at": camera.last_frame_at.isoformat() if camera.last_frame_at else None,
                    "source_url": camera.source_url,
                    "privacy_policy": camera.privacy_policy,
                    "last_frame_id": camera.last_frame_id,
                    "last_frame_hash": camera.last_frame_hash,
                    "last_frame_width": camera.last_frame_width,
                    "last_frame_height": camera.last_frame_height,
                }
                for camera in self.store.cameras.values()
            ],
            "vision_frames": [
                {
                    "id": frame.id,
                    "camera_id": frame.camera_id,
                    "source_url": frame.source_url,
                    "object_name": frame.object_name,
                    "content_type": frame.content_type,
                    "sha256": frame.sha256,
                    "width": frame.width,
                    "height": frame.height,
                    "captured_at": frame.captured_at.isoformat(),
                }
                for frame in self.store.vision_frames.values()
            ],
            "weather_observations": [
                {
                    "id": weather.id,
                    "pond_id": weather.pond_id,
                    "condition": weather.condition,
                    "temperature_c": weather.temperature_c,
                    "wind_speed_mps": weather.wind_speed_mps,
                    "wind_direction": weather.wind_direction,
                    "humidity_pct": weather.humidity_pct,
                    "rain_probability_pct": weather.rain_probability_pct,
                    "pressure_hpa": weather.pressure_hpa,
                    "forecast": weather.forecast,
                    "observed_at": weather.observed_at.isoformat(),
                }
                for weather in self.store.weather_observations.values()
            ],
            "camera_observations": [
                {
                    "id": observation.id,
                    "camera_id": observation.camera_id,
                    "pond_id": observation.pond_id,
                    "camera_role": observation.camera_role,
                    "observation_type": observation.observation_type,
                    "status": observation.status,
                    "summary": observation.summary,
                    "labels": observation.labels,
                    "confidence": observation.confidence,
                    "captured_at": observation.captured_at.isoformat(),
                    "evidence_refs": observation.evidence_refs,
                }
                for observation in self.store.camera_observations.values()
            ],
            "disease_knowledge": [
                {
                    "id": article.id,
                    "name": article.name,
                    "species": article.species,
                    "signs": article.signs,
                    "visual_cues": article.visual_cues,
                    "recommended_actions": article.recommended_actions,
                    "severity": article.severity,
                }
                for article in self.store.disease_knowledge.values()
            ],
            "analysis_cases": [
                {
                    "id": case.id,
                    "sequence": case.sequence,
                    "title": case.title,
                    "category": case.category,
                    "pond_id": case.pond_id,
                    "trigger": case.trigger,
                    "description": case.description,
                    "evidence_refs": case.evidence_refs,
                    "expected_path": case.expected_path,
                    "expected_device_id": case.expected_device_id,
                    "expected_target_state": case.expected_target_state,
                    "expected_result": case.expected_result,
                    "status": case.status,
                    "incident_id": case.incident_id,
                    "agent_run_id": case.agent_run_id,
                    "result_summary": case.result_summary,
                    "updated_at": case.updated_at.isoformat(),
                }
                for case in sorted(self.store.analysis_cases.values(), key=lambda item: item.sequence)
            ],
            "incidents": [
                {
                    "id": item.id,
                    "pond_id": item.pond_id,
                    "title": item.title,
                    "status": item.status.value,
                    "risk": item.risk.value,
                    "evidence": [
                        {
                            "id": evidence.id,
                            "type": evidence.type,
                            "summary": evidence.summary,
                            "created_at": evidence.created_at.isoformat(),
                            "refs": evidence.refs,
                        }
                        for evidence in item.evidence
                    ],
                    "action_proposal_ids": item.action_proposal_ids,
                    "command_ids": item.command_ids,
                    "verification_plan_id": item.verification_plan_id,
                    "verification_result_ids": item.verification_result_ids,
                    "manual_task_ids": item.manual_task_ids,
                    "verification_due_at": item.verification_due_at.isoformat() if item.verification_due_at else None,
                    "assignee": item.assignee,
                }
                for item in self.store.incidents.values()
            ],
            "action_proposals": [
                {
                    "id": item.id,
                    "incident_id": item.incident_id,
                    "device_id": item.device_id,
                    "pond_id": item.pond_id,
                    "target_state": item.target_state,
                    "risk": item.risk.value,
                    "rationale": item.rationale,
                    "evidence_refs": item.evidence_refs,
                    "status": item.status,
                    "approval_id": item.approval_id,
                    "created_at": item.created_at.isoformat(),
                }
                for item in self.store.action_proposals.values()
            ],
            "approvals": [
                {
                    "id": item.id,
                    "proposal_id": item.proposal_id,
                    "incident_id": item.incident_id,
                    "status": item.status.value,
                    "requested_by": item.requested_by,
                    "decided_by": item.decided_by,
                    "reason": item.reason,
                    "created_at": item.created_at.isoformat(),
                    "decided_at": item.decided_at.isoformat() if item.decided_at else None,
                }
                for item in self.store.approvals.values()
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
                    "created_at": item.created_at.isoformat(),
                }
                for item in self.store.commands.values()
            ],
            "verification_plans": [
                {
                    "id": item.id,
                    "incident_id": item.incident_id,
                    "metric": item.metric,
                    "threshold": item.threshold,
                    "earliest_at": item.earliest_at.isoformat() if item.earliest_at else None,
                    "latest_at": item.latest_at.isoformat() if item.latest_at else None,
                    "status": item.status,
                }
                for item in self.store.verification_plans.values()
            ],
            "verification_results": [
                {
                    "id": item.id,
                    "incident_id": item.incident_id,
                    "plan_id": item.plan_id,
                    "outcome": item.outcome,
                    "observed_value": item.observed_value,
                    "evidence_refs": item.evidence_refs,
                    "created_at": item.created_at.isoformat(),
                }
                for item in self.store.verification_results.values()
            ],
            "manual_tasks": [
                {
                    "id": item.id,
                    "incident_id": item.incident_id,
                    "title": item.title,
                    "description": item.description,
                    "assignee": item.assignee,
                    "priority": item.priority,
                    "status": item.status.value,
                    "created_at": item.created_at.isoformat(),
                    "completed_at": item.completed_at.isoformat() if item.completed_at else None,
                }
                for item in self.store.manual_tasks.values()
            ],
            "schedules": [
                {
                    "id": item.id,
                    "name": item.name,
                    "job_type": item.job_type,
                    "interval_seconds": item.interval_seconds,
                    "status": item.status.value,
                    "next_run_at": item.next_run_at.isoformat() if item.next_run_at else None,
                    "last_run_at": item.last_run_at.isoformat() if item.last_run_at else None,
                }
                for item in self.store.schedules.values()
            ],
            "scheduled_jobs": [
                {
                    "id": item.id,
                    "job_type": item.job_type,
                    "idempotency_key": item.idempotency_key,
                    "due_at": item.due_at.isoformat(),
                    "incident_id": item.incident_id,
                    "schedule_id": item.schedule_id,
                    "status": item.status.value,
                    "attempts": item.attempts,
                    "created_at": item.created_at.isoformat(),
                }
                for item in self.store.scheduled_jobs.values()
            ],
            "agent_runs": [
                {
                    "id": run.id,
                    "goal": run.goal,
                    "incident_id": run.incident_id,
                    "status": run.status,
                    "stop_reason": run.stop_reason,
                    "delegated_agents": run.delegated_agents,
                    "steps": [
                        {
                            "agent": step.agent,
                            "action": step.action,
                            "summary": step.summary,
                            "created_at": step.created_at.isoformat(),
                        }
                        for step in run.steps
                    ],
                    "budget": run.budget,
                }
                for run in self.store.agent_runs.values()
            ],
            "patrol_findings": [
                {
                    "id": finding.id,
                    "patrol_run_id": finding.patrol_run_id,
                    "pond_id": finding.pond_id,
                    "status": finding.status,
                    "summary": finding.summary,
                    "evidence_refs": finding.evidence_refs,
                    "confidence": finding.confidence,
                    "created_at": finding.created_at.isoformat(),
                }
                for finding in self.store.patrol_findings.values()
            ],
            "escalations": [
                {
                    "id": escalation.id,
                    "incident_id": escalation.incident_id,
                    "level": escalation.level,
                    "reason": escalation.reason,
                    "status": escalation.status,
                    "manual_task_id": escalation.manual_task_id,
                    "created_at": escalation.created_at.isoformat(),
                }
                for escalation in self.store.escalations.values()
            ],
            "audit_events": [
                {
                    "id": event.id,
                    "actor_type": event.actor_type,
                    "actor_id": event.actor_id,
                    "action": event.action,
                    "resource_type": event.resource_type,
                    "resource_id": event.resource_id,
                    "correlation_id": event.correlation_id,
                    "payload": event.payload,
                    "created_at": event.created_at.isoformat(),
                }
                for event in self.store.audit_events[-500:]
            ],
            "events": self.store.events[-500:],
            "event_sequence": self.store._event_sequence,
            "executed_idempotency_keys": self.store.executed_idempotency_keys,
        }
        if persist and self.repository:
            persisted_sequence = self.repository.save(data)
            if persisted_sequence is not None:
                data["event_sequence"] = persisted_sequence
                self.store._event_sequence = max(self.store._event_sequence, persisted_sequence)
        if persist and self.event_publisher:
            try:
                self.event_publisher.publish(cast(list[dict], data["events"]))
            except Exception as exc:  # Redis is live acceleration, not the source of truth.
                self.event_publisher.last_error = str(exc)
        return data

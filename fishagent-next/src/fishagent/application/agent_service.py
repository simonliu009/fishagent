import json
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
from fishagent.application.reporting import build_daily_report, report_timezone
from fishagent.application.store import (
    READING_QUALITY_LABELS,
    WATER_QUALITY_HIGH_LIMITS,
    WATER_QUALITY_RANGES,
    InMemoryStore,
)
from fishagent.domain.models import (
    ActionProposal,
    AgentRun,
    AnalysisCase,
    Approval,
    ApprovalStatus,
    CameraSource,
    CommandStatus,
    DailyReport,
    Device,
    DeviceCommand,
    Escalation,
    Evidence,
    Farm,
    HealthStatus,
    Incident,
    IncidentStatus,
    JobStatus,
    KnowledgeDocument,
    ManualTask,
    PatrolFinding,
    Pond,
    RestockOrder,
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
from fishagent.infrastructure.weather import MockWeatherApi


class _AnalysisCaseCancelled(RuntimeError):
    pass


# DO safety alarms trigger below the pond safety line, but recovery must be
# confirmed above it so a marginal reading does not immediately stop aeration.
DO_RECOVERY_MARGIN = 0.8
VERIFICATION_RETRY_SECONDS = 300

DEMO_MODE_LABELS = {
    "success": "低溶氧自动处置",
    "alerts": "双传感器告警",
    "failure": "复核失败升级",
    "dedup": "防重复动作",
    "approval": "L2 审批转人工",
    "multimodal": "多模态案例序列",
    "health": "传感器与设备故障",
}
AUTO_RESPONSE_DEMO_MODES = frozenset(DEMO_MODE_LABELS)


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
        self.weather_api = MockWeatherApi()
        self.device_control_skill = DeviceControlSkill(self)
        self._job_lock = RLock()
        self._demo_lock = RLock()
        self._report_lock = RLock()
        self._analysis_case_lock = RLock()
        self._analysis_case_thread: Optional[Thread] = None
        self._analysis_case_generation = 0
        if self.repository:
            persisted = self.repository.load()
            if persisted:
                persisted_camera_urls = [str(item.get("source_url") or "") for item in persisted.get("cameras", [])]
                persisted_observation_count = len(persisted.get("camera_observations", []))
                self.store.restore_snapshot(persisted)
                if (
                    len(self.store.camera_observations) > persisted_observation_count
                    or any(url.startswith("mock://") for url in persisted_camera_urls)
                ):
                    self.snapshot()

    def initialize_demo(self) -> dict:
        with self._demo_lock:
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
                            "quality": "GOOD",
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

    def _request_sensor_reports(self, run: AgentRun, pond_id: Optional[str] = None) -> None:
        """Actively request fresh MQTT reports before inspection or case analysis."""
        sensors = [item for item in self.store.sensors.values() if pond_id is None or item.pond_id == pond_id]
        if not sensors:
            run.step(
                "sensor-monitor-agent",
                "sensor_report.skipped",
                "没有可请求的传感器",
                details={"kind": "mqtt_sensor_report", "pond_id": pond_id, "requested": 0},
            )
            return
        requester = getattr(self.telemetry_publisher, "request_sensor_report", None)
        if self.telemetry_publisher is None or not callable(requester):
            run.step(
                "sensor-monitor-agent",
                "sensor_report.fallback",
                "未接入 MQTT 请求端点，使用当前传感器快照",
                details={
                    "kind": "mqtt_sensor_report",
                    "pond_id": pond_id,
                    "requested": len(sensors),
                    "received": len(sensors),
                    "transport": "snapshot_fallback",
                },
            )
            return

        expected_ids: set[str] = set()
        requested = 0
        for sensor in sensors:
            latest = self.store.latest_reading(sensor.pond_id, sensor.metric)
            if latest is None:
                continue
            value = latest.value
            active = self.store.active_incident_for_pond(sensor.pond_id)
            device = self._aerator_device(sensor.pond_id)
            plan = self.store.verification_plans.get(active.verification_plan_id or "") if active else None
            # The MQTT mock models a gradual DO recovery after aeration starts.
            # Each patrol still publishes a real report through the broker.
            if (
                sensor.metric == "DO"
                and latest.quality == "GOOD"
                and active
                and active.status == IncidentStatus.VERIFY_PENDING
                and device
                and device.shadow_state == "on"
            ):
                recovery_threshold = plan.threshold if plan else self._do_recovery_threshold(sensor.pond_id)
                value = min(recovery_threshold, latest.value + 0.5)
            request_id = "%s-%s" % (run.id, sensor.id)
            source_event_id = "patrol-report-%s-%s" % (run.id, sensor.id)
            if requester(
                pond_id=sensor.pond_id,
                sensor_id=sensor.id,
                metric=sensor.metric,
                unit=sensor.unit,
                value=value,
                request_id=request_id,
                source_event_id=source_event_id,
                quality=latest.quality,
                auto_run=False,
                defer_persist=False,
            ):
                requested += 1
                expected_ids.add(source_event_id)
        run.step(
            "sensor-monitor-agent",
            "sensor_report.requested",
            "已通过 MQTT 向 %s 个传感器主动请求即时上报" % requested,
            details={
                "kind": "mqtt_sensor_report",
                "pond_id": pond_id,
                "requested": requested,
                "expected_source_event_ids": sorted(expected_ids),
                "transport": "mqtt",
            },
        )
        deadline = time.monotonic() + 30
        while expected_ids and time.monotonic() < deadline:
            received_ids = {item.source_event_id for item in self.store.readings}
            if expected_ids.issubset(received_ids):
                run.step(
                    "sensor-monitor-agent",
                    "sensor_report.received",
                    "本轮传感器上报已全部通过 MQTT 入库",
                    details={
                        "kind": "mqtt_sensor_report",
                        "pond_id": pond_id,
                        "requested": len(expected_ids),
                        "received": len(expected_ids),
                        "transport": "mqtt",
                    },
                )
                return
            time.sleep(0.02)
        received_ids = {item.source_event_id for item in self.store.readings}
        missing = expected_ids - received_ids
        run.step(
            "sensor-monitor-agent",
            "sensor_report.timeout" if missing else "sensor_report.received",
            "MQTT 传感器上报完成：收到 %s/%s，缺少 %s" % (len(received_ids & expected_ids), len(expected_ids), len(missing)),
            details={
                "kind": "mqtt_sensor_report",
                "pond_id": pond_id,
                "requested": len(expected_ids),
                "received": len(received_ids & expected_ids),
                "missing_source_event_ids": sorted(missing),
                "transport": "mqtt",
            },
        )

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
        try:
            return self._run_analysis_case(case_id, generation=generation)
        except _AnalysisCaseCancelled:
            raise
        except Exception as exc:
            case = self.store.analysis_cases.get(case_id)
            if case is None:
                raise
            run = AgentRun(id=new_id("run"), goal="分析案例：%s" % case.title, status="FAILED")
            run.stop_reason = "ANALYSIS_CASE_FAILED"
            run.step("supervisor-agent", "analysis_case.failed", "多模态案例执行失败：%s" % exc)
            self.store.agent_runs[run.id] = run
            case.agent_run_id = run.id
            case.status = "FAILED"
            case.result_summary = "案例执行失败：%s" % exc
            case.updated_at = utcnow()
            self.store.emit(
                "analysis_case.failed",
                case.result_summary,
                {"case_id": case.id, "run_id": run.id, "error": str(exc)},
            )
            self.snapshot()
            return run

    def start_analysis_case(self, case_id: str) -> bool:
        """Start one case asynchronously so HTTP/UI callers never wait on the LLM."""
        with self._analysis_case_lock:
            case = self.store.analysis_cases.get(case_id)
            if case is None:
                raise KeyError(case_id)
            if case.agent_run_id and case.agent_run_id in self.store.agent_runs:
                return False
            if case.status == "RUNNING":
                return False
            if self._analysis_case_thread and self._analysis_case_thread.is_alive():
                return False
            self._analysis_case_generation += 1
            generation = self._analysis_case_generation
            case.status = "RUNNING"
            case.updated_at = utcnow()
            self._analysis_case_thread = Thread(
                target=self.run_analysis_case,
                args=(case_id, generation),
                name="fishagent-analysis-case",
                daemon=True,
            )
            self._analysis_case_thread.start()
            return True

    def _run_analysis_case(self, case_id: str, generation: Optional[int] = None) -> AgentRun:
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
                run.step(
                    "supervisor-agent",
                    "patrol_sop.entered",
                    "多模态案例已纳入巡塘 SOP，先获取池塘现场数据再进入模型研判",
                    details={"kind": "patrol_sop", "stage": "entered", "case_id": case.id, "pond_id": case.pond_id},
                )
                self._request_sensor_reports(run, pond_id=case.pond_id)
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
        run.step("supervisor-agent", "start_patrol", "主动请求全场传感器即时上报后再开始巡查")
        self._request_sensor_reports(run)
        self._verify_due_incidents_from_patrol(run)
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
            unhealthy_sensor_details = []
            for health in unhealthy_sensors:
                sensor = self.store.sensors.get(health.sensor_id)
                health_label = {
                    HealthStatus.OFFLINE: "离线",
                    HealthStatus.DRIFTING: "漂移",
                    HealthStatus.ERROR: "错误",
                }.get(health.status, health.status.value)
                if sensor is None:
                    unhealthy_sensor_details.append("传感器 %s（状态：%s）" % (health.sensor_id, health_label))
                    continue
                detail = "%s（%s，指标：%s，状态：%s" % (
                    sensor.name,
                    sensor.id,
                    DEMO_SENSOR_BY_METRIC.get(sensor.metric, {"name": sensor.metric}).get("name", sensor.metric),
                    health_label,
                )
                if health.message:
                    detail += "，说明：%s" % health.message
                unhealthy_sensor_details.append(detail + "）")
            unhealthy_device_details = [
                "%s（%s，能力：%s，状态：离线）" % (
                    device.name,
                    device.id,
                    {"aeration": "增氧", "valve": "阀门"}.get(device.capability, device.capability),
                )
                for device in unhealthy_devices
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
                reasons.append("传感器异常：%s" % "、".join(unhealthy_sensor_details))
            if unhealthy_devices:
                reasons.append("设备离线：%s" % "、".join(unhealthy_device_details))
            active = self.store.active_incident_for_pond(pond.id)
            if active and not reasons:
                reasons.append("存在未关闭异常事件")
            status = "NEEDS_ATTENTION" if reasons or active else "NORMAL"
            recommendations = self._patrol_recommendations(
                pond,
                latest_readings,
                available,
                reasons,
                unhealthy_sensors,
                unhealthy_devices,
            )
            run.step("sensor-monitor-agent", "inspect_pond", "%s：%s" % (pond.name, summary))
            finding = PatrolFinding(
                id=new_id("finding"),
                patrol_run_id=run.id,
                pond_id=pond.id,
                status=status,
                summary="池塘 %s %s；%s%s" % (
                    pond.id,
                    pond.name,
                    summary,
                    "；异常：%s" % "、".join(reasons) if reasons else "",
                ),
                evidence_refs=[reading.source_event_id for reading in available],
                recommendations=recommendations,
            )
            self.store.patrol_findings[finding.id] = finding
            run.step(
                "patrol-analysis-agent",
                "patrol.advice",
                "已形成巡查建议：%s" % "；".join(recommendations),
                details={"kind": "patrol_advice", "pond_id": pond.id, "recommendations": recommendations},
            )
            if status == "NEEDS_ATTENTION" and active is None:
                active = Incident(
                    id=new_id("inc"),
                    pond_id=pond.id,
                    title=self._patrol_incident_title(pond.id),
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
            elif active and active.title.endswith("巡查异常"):
                active.title = self._patrol_incident_title(pond.id)
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

    def _verify_due_incidents_from_patrol(self, patrol_run: AgentRun) -> None:
        """Run due verification only after the patrol has requested fresh MQTT data."""
        for incident in list(self.store.due_verifications()):
            verified = self.verify_incident(incident.id)
            patrol_run.step(
                "supervisor-agent",
                "dispatch_verification",
                "巡塘已触发 %s：%s" % (incident.title, self._incident_status_label(verified.status)),
            )

    @staticmethod
    def _incident_status_label(status: IncidentStatus) -> str:
        return {
            IncidentStatus.VERIFY_PENDING: "继续等待下一次复核",
            IncidentStatus.RESOLVED: "复核通过并关闭告警",
            IncidentStatus.ESCALATED: "升级人工处理",
        }.get(status, status.value)

    def _patrol_alert_reason_labels(self, pond_id: str) -> list[str]:
        pond = self.store.ponds.get(pond_id)
        if pond is None:
            return []
        labels: list[str] = []
        latest_readings = [self.store.latest_reading(pond_id, spec["metric"]) for spec in DEMO_SENSOR_SPECS]
        if len([reading for reading in latest_readings if reading is not None]) < len(DEMO_SENSOR_SPECS):
            labels.append("传感器数据不完整")
        for spec, reading in zip(DEMO_SENSOR_SPECS, latest_readings):
            if reading is None:
                continue
            sensor = self.store.sensors.get(reading.sensor_id)
            source_name = sensor.name if sensor else spec["name"]
            if reading.quality != "GOOD":
                labels.append("%s读数质量%s" % (source_name, READING_QUALITY_LABELS.get(reading.quality, reading.quality)))
                continue
            if reading.metric == "DO" and reading.value < pond.dissolved_oxygen_min:
                labels.append("溶氧低于安全线")
                continue
            high_limit = WATER_QUALITY_HIGH_LIMITS.get(reading.metric)
            if high_limit is not None and reading.value > high_limit:
                labels.append("%s高于安全线" % spec["name"])
                continue
            safe_range = WATER_QUALITY_RANGES.get(reading.metric)
            if safe_range and not safe_range[0] <= reading.value <= safe_range[1]:
                labels.append("%s超出安全范围" % spec["name"])

        sensor_ids = {sensor.id for sensor in self.store.sensors.values() if sensor.pond_id == pond_id}
        latest_by_sensor = {reading.sensor_id: reading for reading in latest_readings if reading is not None}
        for sensor_id, health in self.store.sensor_health.items():
            if sensor_id not in sensor_ids or health.status == HealthStatus.ONLINE:
                continue
            if sensor_id in latest_by_sensor and latest_by_sensor[sensor_id].quality != "GOOD":
                continue
            sensor = self.store.sensors.get(sensor_id)
            sensor_name = sensor.name if sensor else sensor_id
            labels.append("%s%s" % (
                sensor_name,
                {
                    HealthStatus.OFFLINE: "离线",
                    HealthStatus.DRIFTING: "漂移",
                    HealthStatus.ERROR: "错误",
                }.get(health.status, health.status.value),
            ))
        for device in self.store.devices.values():
            if device.pond_id == pond_id and not device.healthy:
                labels.append("%s离线" % device.name)
        return list(dict.fromkeys(labels))

    def _patrol_incident_title(self, pond_id: str) -> str:
        pond = self.store.ponds.get(pond_id)
        if pond is None:
            return "%s 巡查异常" % pond_id
        labels = self._patrol_alert_reason_labels(pond_id)
        suffix = "：%s" % "；".join(labels[:4]) if labels else ""
        return "%s 巡查异常%s" % (pond.name, suffix)

    def _do_recovery_threshold(self, pond_id: str) -> float:
        pond = self.store.ponds[pond_id]
        return round(pond.dissolved_oxygen_min + DO_RECOVERY_MARGIN, 2)

    def _aerator_device(self, pond_id: str) -> Optional[Device]:
        return next(
            (device for device in self.store.devices.values() if device.pond_id == pond_id and device.capability == "aeration"),
            None,
        )

    def _patrol_recommendations(
        self,
        pond: Pond,
        latest_readings: list[Optional[SensorReading]],
        available: list[SensorReading],
        reasons: list[str],
        unhealthy_sensors: list[SensorHealth],
        unhealthy_devices: list[Device],
    ) -> list[str]:
        """Give every patrol finding an actionable next step, including normal ones."""
        latest_do = next((reading for reading in available if reading.metric == "DO"), None)
        if reasons:
            recommendations: list[str] = []
            if latest_do and latest_do.value < pond.dissolved_oxygen_min:
                recommendations.append("优先复核溶氧读数和增氧机状态，按处置流程执行后等待下一次巡塘复核。")
            if any("高于安全线" in reason or "超出安全范围" in reason for reason in reasons):
                recommendations.append("对超限指标安排现场复测，结合投喂、换水和天气记录研判，复测前不要直接投药。")
            if unhealthy_sensors:
                recommendations.append("现场检查异常传感器的供电、通讯和校准状态，恢复可信读数后再作进一步调整。")
            if unhealthy_devices:
                recommendations.append("现场检查离线设备的供电、网络和运行状态，设备恢复前保留人工处置边界。")
            if not recommendations:
                recommendations.append("保持当前事件跟踪，补充现场证据后再决定是否调整设备或作业。")
            return recommendations

        recommendations = ["当前各项指标均在安全范围内，保持现有设备和养殖策略，不额外下发控制指令。"]
        if latest_do:
            do_margin = latest_do.value - pond.dissolved_oxygen_min
            if do_margin <= 0.8:
                recommendations.append("溶氧距安全线 %.2f mg/L，建议重点观察夜间和清晨趋势，下一次巡塘优先复核。" % do_margin)
            else:
                recommendations.append("建议继续观察溶氧、pH 和水温趋势，重点关注天气与投喂变化带来的波动。")
        if len(available) == len(latest_readings):
            recommendations.append("建议记录本轮天气、投喂和换水情况，供下一轮 Agent 巡查进行趋势对比。")
        return recommendations

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
            reply = "智渔AI 未启用或模型 API Key 未配置"
            run.step("supervisor-agent", "chat.stop", reply, details={"kind": "llm_error", "error_code": "LLM_REQUIRED"})
            self.store.emit("agent.chat.failed", reply, {"run_id": run.id}, correlation_id=run.id)
            return run, reply
        run.step(
            "supervisor-agent",
            "chat.request",
            "Agent 向大模型发送聊天消息",
            details={
                "kind": "llm_request",
                "message": {
                    "role": "user",
                    "from": "智渔AI 助手",
                    "to": "大模型",
                    "content": normalized,
                    "pond_id": effective_pond_id,
                    "history": (history or [])[-12:],
                },
            },
        )
        result = orchestrator.chat(normalized, (history or [])[-12:], effective_pond_id)
        for agent, action, summary in result.steps:
            run.step(agent, action, summary)
        run.step(
            "supervisor-agent",
            "chat.response",
            "大模型返回聊天答复",
            details={
                "kind": "llm_response",
                "valid": result.stop_reason == "CREW_CHAT_COMPLETED",
                "response": result.summary,
            },
        )
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
        plan = VerificationPlan(
            id=new_id("verify-plan"),
            incident_id=incident.id,
            threshold=self._do_recovery_threshold(incident.pond_id),
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
        description = self._manual_task_description(incident_id, title, description)
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

    @staticmethod
    def manual_task_checklist(description: str) -> list[str]:
        lines = str(description or "").splitlines()
        start = next((index for index, line in enumerate(lines) if "【人工执行清单】" in line), None)
        if start is None:
            return []
        end = next((index for index, line in enumerate(lines[start + 1:], start + 1) if "【完成回报】" in line), len(lines))
        checklist = []
        for line in lines[start + 1:end]:
            match = re.match(r"^\s*\d+[.、]\s*(.+?)\s*$", line)
            if match:
                checklist.append(match.group(1))
        return checklist

    def submit_manual_task_report(self, task_id: str, report: dict[str, Any], reporter: str = "现场操作员") -> ManualTask:
        task = self.store.manual_tasks[task_id]
        if task.status == TaskStatus.COMPLETED:
            raise ValueError("任务已经完成，不能重复上报")

        required_fields = {
            "retest_data": "复测数据",
            "device_status": "设备实际状态",
            "actions_taken": "已执行动作",
            "executed_at": "执行时间",
            "photo_evidence": "现场照片或记录",
        }
        missing = [label for key, label in required_fields.items() if not str(report.get(key) or "").strip()]
        if missing:
            raise ValueError("请完整填写：%s" % "、".join(missing))

        checklist = self.manual_task_checklist(task.description)
        raw_results = report.get("checklist_results")
        if not isinstance(raw_results, list) or len(raw_results) != len(checklist):
            raise ValueError("请完整填写人工执行清单")
        checklist_results = []
        for index, instruction in enumerate(checklist):
            item = raw_results[index]
            result = item.get("result") if isinstance(item, dict) else item
            if not str(result or "").strip():
                raise ValueError("请填写第%d项人工执行清单" % (index + 1))
            checklist_results.append({"index": index + 1, "instruction": instruction, "result": str(result).strip()})

        task.completion_report = {
            "checklist_results": checklist_results,
            "retest_data": str(report["retest_data"]).strip(),
            "device_status": str(report["device_status"]).strip(),
            "actions_taken": str(report["actions_taken"]).strip(),
            "executed_at": str(report["executed_at"]).strip(),
            "photo_evidence": str(report["photo_evidence"]).strip(),
            "notes": str(report.get("notes") or "").strip(),
        }
        task.reported_at = utcnow()
        task.reported_by = str(reporter or "现场操作员").strip() or "现场操作员"
        task.status = TaskStatus.COMPLETED
        task.completed_at = task.reported_at
        self.store.emit(
            "manual_task.report_submitted",
            "人工任务已上报处理结果：%s" % task.title,
            {"task_id": task.id, "reported_by": task.reported_by},
        )
        self.store.emit("manual_task.completed", task.title, {"task_id": task.id, "source": "completion_report"})
        return task

    def complete_manual_task(self, task_id: str) -> ManualTask:
        task = self.store.manual_tasks[task_id]
        if not task.completion_report:
            raise ValueError("请先提交完整处理结果")
        task.status = TaskStatus.COMPLETED
        task.completed_at = utcnow()
        self.store.emit("manual_task.completed", task.title, {"task_id": task.id})
        return task

    def _escalate_command_failure(self, incident: Incident, run: AgentRun, command: DeviceCommand, stop_reason: str) -> ManualTask:
        if incident.status in {IncidentStatus.ACTION_PROPOSED, IncidentStatus.EXECUTING}:
            incident.transition(IncidentStatus.ACTION_FAILED)
        if incident.status == IncidentStatus.ACTION_FAILED:
            incident.transition(IncidentStatus.ESCALATED)
        incident.assignee = "现场操作员"
        device = self.store.devices.get(command.device_id)
        device_name = device.name if device else command.device_id
        target_label = "开启" if command.target_state == "on" else "关闭"
        feedback = command.policy_reason or "设备网关未返回确认"
        for step in reversed(run.steps):
            if step.action != "device.command_result":
                continue
            details = step.details if isinstance(step.details, dict) else {}
            feedback = str(details.get("detail") or details.get("reason") or step.summary or feedback)
            break
        if device and device.capability == "aeration" and command.target_state == "on":
            description = (
                "自动开启设备“%s”的命令未获得设备确认；命令状态：%s；系统反馈：%s。"
                "请现场检查电源、断路器、控制箱、线路、叶轮或曝气盘，确认增氧机实际开关状态；"
                "若仍未启动，启用备用增氧设备或现场手动开启，并记录启动时间、设备状态和现场照片。"
                % (device_name, command.status.value, feedback)
            )
        else:
            description = (
                "自动%s%s命令未获得设备确认；命令状态：%s；系统反馈：%s。"
                "请现场检查设备电源、网络、控制箱和实际状态，必要时按审批要求人工执行并记录结果。"
                % (target_label, device_name, command.status.value, feedback)
            )
        task = self.create_manual_task(
            title="处理设备动作失败：%s" % incident.title,
            description=description,
            incident_id=incident.id,
        )
        run.step(
            "execution-agent",
            "route_manual_task",
            "设备动作未确认，已升级人工处理：%s" % task.title,
            details={
                "kind": "manual_task",
                "task_id": task.id,
                "command_id": command.id,
                "status": command.status.value,
                "reason": feedback,
            },
        )
        run.status = "FAILED"
        run.stop_reason = stop_reason
        self.store.emit(
            "agent.run.failed",
            "设备动作未确认，已转人工：%s" % task.title,
            {"run_id": run.id, "command_id": command.id, "task_id": task.id},
            correlation_id=run.id,
        )
        return task

    @staticmethod
    def _localize_manual_task_text(value: str) -> str:
        for quality, label in READING_QUALITY_LABELS.items():
            value = value.replace(quality, label)
        return value

    def _manual_task_description(self, incident_id: Optional[str], title: str, reason: str) -> str:
        """Turn a terse escalation into an operator-ready field checklist."""
        original = self._localize_manual_task_text(str(reason or "未提供转人工原因").strip())
        if not incident_id or incident_id not in self.store.incidents:
            return original
        if "【人工执行清单】" in original:
            return original

        incident = self.store.incidents[incident_id]
        pond = self.store.ponds.get(incident.pond_id)
        pond_label = pond.name if pond else incident.pond_id
        latest_by_metric: dict[str, SensorReading] = {}
        for reading in self.store.readings:
            if reading.pond_id != incident.pond_id:
                continue
            previous = latest_by_metric.get(reading.metric)
            if previous is None or reading.sampled_at > previous.sampled_at:
                latest_by_metric[reading.metric] = reading

        evidence = incident.evidence[-1] if incident.evidence else None
        evidence_summary = self._localize_manual_task_text(str(evidence.summary if evidence else "暂无现场证据").strip())
        if len(evidence_summary) > 360:
            evidence_summary = evidence_summary[:360] + "..."

        abnormal_metrics: list[str] = []
        abnormal_metric_keys: set[str] = set()
        for metric, reading in latest_by_metric.items():
            metric_name = DEMO_SENSOR_BY_METRIC.get(metric, {"name": metric}).get("name", metric)
            if reading.quality != "GOOD":
                abnormal_metric_keys.add(metric)
                abnormal_metrics.append("%s %.2f%s（读数质量：%s）" % (metric_name, reading.value, reading.unit, READING_QUALITY_LABELS.get(reading.quality, reading.quality)))
                continue
            if metric == "DO" and pond and reading.value < pond.dissolved_oxygen_min:
                abnormal_metric_keys.add(metric)
                abnormal_metrics.append("%s %.2f%s（低于安全线 %.2f%s）" % (metric_name, reading.value, reading.unit, pond.dissolved_oxygen_min, reading.unit))
                continue
            high_limit = WATER_QUALITY_HIGH_LIMITS.get(metric)
            if high_limit is not None and reading.value > high_limit:
                abnormal_metric_keys.add(metric)
                abnormal_metrics.append("%s %.2f%s（高于安全线 %.2f%s）" % (metric_name, reading.value, reading.unit, high_limit, reading.unit))
                continue
            safe_range = WATER_QUALITY_RANGES.get(metric)
            if safe_range and not safe_range[0] <= reading.value <= safe_range[1]:
                abnormal_metric_keys.add(metric)
                abnormal_metrics.append("%s %.2f（超出安全范围 %.2f-%.2f）" % (metric_name, reading.value, safe_range[0], safe_range[1]))

        sensor_health = []
        for sensor in self.store.sensors.values():
            if sensor.pond_id != incident.pond_id:
                continue
            health = self.store.sensor_health.get(sensor.id)
            if health and health.status != HealthStatus.ONLINE:
                state_label = {
                    HealthStatus.OFFLINE: "离线",
                    HealthStatus.DRIFTING: "漂移",
                    HealthStatus.ERROR: "错误",
                }.get(health.status, health.status.value)
                detail = "%s（%s，%s" % (sensor.name, sensor.id, state_label)
                if health.message:
                    detail += "，%s" % health.message
                sensor_health.append(detail + "）")

        devices = [item for item in self.store.devices.values() if item.pond_id == incident.pond_id]
        proposed_devices = []
        for proposal_id in incident.action_proposal_ids:
            proposal = self.store.action_proposals.get(proposal_id)
            device = self.store.devices.get(proposal.device_id) if proposal else None
            if device and device not in proposed_devices:
                proposed_devices.append(device)
        relevant_capabilities: set[str] = set()
        if "DO" in abnormal_metric_keys or "溶氧" in incident.title or "低氧" in evidence_summary:
            relevant_capabilities.add("aeration")
        if "投喂" in incident.title or "投喂" in evidence_summary:
            relevant_capabilities.add("feeding")
        if "天气" in evidence_summary or "强降雨" in evidence_summary:
            relevant_capabilities.add("valve_control")
        relevant_devices = [item for item in devices if not item.healthy or item.capability in relevant_capabilities]
        for item in proposed_devices:
            if item not in relevant_devices:
                relevant_devices.append(item)
        device_details = [
            "%s（能力：%s，%s，当前%s）" % (
                item.name,
                {"aeration": "增氧", "valve_control": "阀门", "feeding": "投喂"}.get(item.capability, item.capability),
                "在线" if item.healthy else "离线",
                "开启" if item.shadow_state == "on" else "关闭",
            )
            for item in relevant_devices
        ]

        metric_names = abnormal_metric_keys
        checklist: list[str] = []
        if "DO" in metric_names or "溶氧" in incident.title or "低氧" in evidence_summary:
            safety_line = pond.dissolved_oxygen_min if pond else 4.0
            recovery_threshold = safety_line + DO_RECOVERY_MARGIN
            plan = self.store.verification_plans.get(incident.verification_plan_id or "")
            if plan:
                recovery_threshold = plan.threshold
            checklist.extend([
                "使用已校准的便携式溶氧仪，在池塘表层、中层、底层各复测 1 次，记录测量位置、时间和值；不要只依据异常传感器读数。",
                "检查增氧设备的电源、断路器、控制箱、线路、叶轮或曝气盘，以及现场气泡和水流；设备离线时先确认能否现场手动启动。",
                "若复测仍低于安全线 %.2f mg/L，立即启动可用增氧设备或备用设备，并记录启动时间；设备故障时联系维修并持续人工观察虾群活动。" % safety_line,
                "增氧启动后不要立即停机；下一次复测达到 %.2f mg/L 且设备运行稳定后，才评估停机，停机后再次确认设备状态。" % recovery_threshold,
            ])
        if "AMMONIA" in metric_names or "氨氮" in incident.title:
            checklist.extend([
                "现场取水样复测氨氮，至少记录池中心和进水口两个点位的结果，并拍照留存试剂或仪器读数。",
                "核对最近 24 小时投喂量、残饵、死亡个体和换水记录，检查底部有机物和进排水是否正常。",
                "在复测和养殖负责人确认前，不得自行投药或大幅改变换水量；将复测结果和建议处理方案回填任务。",
            ])
        if "NITRITE" in metric_names or "亚硝酸" in incident.title:
            checklist.extend([
                "现场取水样复测亚硝酸根离子，记录池中心、进水口和底层水样的结果。",
                "检查增氧、循环水和换水设备运行情况，核对近期投喂、残饵及底泥变化；异常设备拍照并记录。",
                "复测结果未确认前，不自行投药；将水样结果、设备状态和处理建议提交养殖负责人审核。",
            ])
        if "TURBIDITY" in metric_names or "浊度" in incident.title:
            checklist.extend([
                "现场观察并记录水色、悬浮物和底部扰动，在池中心和进排水口各取样复测浊度。",
                "检查进水、排水、循环泵和池边施工或降雨影响，确认是否存在持续泥沙或泡沫来源。",
            ])
        if "CHLOROPHYLL" in metric_names or "叶绿素" in incident.title:
            checklist.extend([
                "复测叶绿素并观察水色、藻团和水面分布，记录池中心与边缘差异。",
                "核对近期光照、投喂和换水情况；未经负责人确认，不直接采取杀藻或投药措施。",
            ])
        if "PH" in metric_names or "pH" in incident.title:
            checklist.extend([
                "使用校准 pH 仪在池中心、进水口和底层各复测 1 次，并记录校准时间和读数。",
                "检查传感器探头清洁、线缆和安装位置；复测异常时先更换或校准探头，再判断水体处置。",
            ])
        if "TEMPERATURE" in metric_names or "水温" in incident.title:
            checklist.extend([
                "用独立温度计在表层、中层和底层复测水温，记录各点读数和测量时间。",
                "检查遮阳、进排水和循环设备状态，确认温差来源后再决定是否调整运行方案。",
            ])
        if sensor_health:
            checklist.append("处理异常传感器：检查探头清洁、供电、线缆、网关和 MQTT 上报；与便携仪结果对比后校准或更换，并记录恢复时间。")
        if evidence and evidence.type == "multimodal_analysis":
            checklist.append("复核对应水面或水下摄像头画面，确认模型指出的现象是否仍存在；记录发现的位置、数量、持续时间并留存现场照片或视频。")
            checklist.append("涉及病害、死亡或明显行为异常时，按养殖负责人要求采样或隔离；未完成复核前不得自行投药或跨池转运。")
        if not checklist:
            checklist.extend([
                "按告警证据到现场复核池塘、水体、摄像头观察到的现象及相关设备状态，记录时间、位置、数值和照片。",
                "对涉及设备先确认电源、网络、控制箱和当前状态；未经人工审批不得执行高风险动作。",
            ])
        if any(item.capability == "valve_control" for item in relevant_devices) or "天气" in evidence_summary or "强降雨" in evidence_summary:
            checklist.append("检查进排水口、阀门位置、漂浮物和防雨固定；需要改变阀门状态时，先按审批要求确认后再现场执行。")
        if any(item.capability == "feeding" for item in relevant_devices) or "投喂" in evidence_summary:
            checklist.append("核对投喂机当前状态、下一轮计划和残饵；未完成现场确认前暂停下一轮投喂，不要继续增加水体负荷。")

        lines = [
            "处理背景：%s" % original,
            "告警位置：%s" % pond_label,
            "告警证据：%s" % evidence_summary,
        ]
        if abnormal_metrics:
            lines.append("异常指标：%s" % "；".join(abnormal_metrics))
        if sensor_health:
            lines.append("异常传感器：%s" % "；".join(sensor_health))
        if device_details:
            lines.append("相关设备：%s" % "；".join(device_details))
        lines.append("【人工执行清单】")
        lines.extend("%d. %s" % (index, item) for index, item in enumerate(dict.fromkeys(checklist), 1))
        lines.extend([
            "【完成回报】记录复测数据、设备实际状态、已执行动作、执行时间和现场照片；告警条件未消失时不要仅将任务标记为完成。",
            "【任务来源】%s" % title,
        ])
        return "\n".join(lines)

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
        quality_label = READING_QUALITY_LABELS[quality]
        health.message = "" if quality == "GOOD" else "读数质量：%s" % quality_label
        incident = self.store.add_reading(reading)
        if incident is None and quality != "GOOD" and auto_run:
            incident = self.store.active_incident_for_pond(pond_id)
            evidence = Evidence(
                id=new_id("evi"),
                type="sensor_health",
                summary="%s 传感器读数质量异常：%s" % ((spec or {"name": metric})["name"], quality_label),
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

    def search_knowledge(self, query: str = "", species: str = "", metric: str = "") -> list[dict[str, Any]]:
        """Perform deterministic keyword retrieval over the demo knowledge base."""
        snapshot = self._snapshot(persist=False)
        query_text = str(query or "").strip().lower()
        terms = [term for term in re.split(r"[\s,，。；;、/]+", query_text) if term]
        documents: list[dict[str, Any]] = []
        for item in snapshot.get("knowledge_documents", []):
            haystack = json.dumps(item, ensure_ascii=False).lower()
            score = sum(2 for term in terms if term in haystack)
            score += sum(1 for keyword in item.get("keywords", []) if keyword.lower() in query_text)
            if metric and item.get("metric") == metric:
                score += 3
            if species and species in item.get("species", ""):
                score += 1
            documents.append({**item, "match_score": score})
        documents.sort(key=lambda item: (-int(item.get("match_score", 0)), item.get("title", "")))
        matched = [item for item in documents if item.get("match_score", 0) > 0]
        selected = matched or documents
        return selected[:8]

    def create_knowledge_document(self, payload: dict[str, Any]) -> KnowledgeDocument:
        title = str(payload.get("title") or "").strip()
        content = str(payload.get("content") or "").strip()
        if not title or not content:
            raise ValueError("知识文档标题和正文不能为空")
        keywords = payload.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [item.strip() for item in re.split(r"[,，、;；]+", keywords) if item.strip()]
        if not isinstance(keywords, list):
            raise ValueError("keywords must be a list or comma-separated string")
        document = KnowledgeDocument(
            id=str(payload.get("id") or new_id("knowledge")),
            title=title,
            source=str(payload.get("source") or "养殖运营知识库").strip(),
            version=str(payload.get("version") or "1.0").strip(),
            section=str(payload.get("section") or "").strip(),
            content=content,
            keywords=[str(item).strip() for item in keywords if str(item).strip()],
            species=str(payload.get("species") or "").strip(),
            metric=str(payload.get("metric") or "").strip(),
            reference_dose=str(payload.get("reference_dose") or "").strip(),
            risk_notes=str(payload.get("risk_notes") or "").strip(),
            withdrawal_period=str(payload.get("withdrawal_period") or "").strip(),
        )
        if document.id in self.store.knowledge_documents:
            raise ValueError("知识文档 ID 已存在")
        self.store.knowledge_documents[document.id] = document
        self.store.emit("knowledge.document.created", "知识文档已添加", {"document_id": document.id})
        self.snapshot()
        return document

    def delete_knowledge_document(self, document_id: str) -> KnowledgeDocument:
        document = self.store.knowledge_documents.pop(document_id, None)
        if document is None:
            raise KeyError("knowledge document does not exist")
        self.store.emit("knowledge.document.deleted", "知识文档已删除", {"document_id": document_id})
        self.snapshot()
        return document

    def inventory_snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item.id,
                "name": item.name,
                "category": item.category,
                "unit": item.unit,
                "stock_quantity": item.stock_quantity,
                "minimum_quantity": item.minimum_quantity,
                "reorder_quantity": item.reorder_quantity,
                "supplier": item.supplier,
                "pond_id": item.pond_id,
                "updated_at": item.updated_at.isoformat(),
                "low_stock": item.stock_quantity <= item.minimum_quantity,
            }
            for item in self.store.inventory.values()
        ]

    def draft_restock_order(self, inventory_id: str, quantity: float, rationale: str, created_by: str = "action-planning-agent") -> RestockOrder:
        item = self.store.inventory.get(inventory_id)
        if item is None:
            raise KeyError("inventory item does not exist")
        if quantity <= 0:
            raise ValueError("restock quantity must be positive")
        order = RestockOrder(
            id=new_id("restock"),
            status="PENDING_CONFIRMATION",
            supplier=item.supplier,
            items=[
                {
                    "inventory_id": item.id,
                    "name": item.name,
                    "quantity": quantity,
                    "unit": item.unit,
                    "pond_id": item.pond_id,
                }
            ],
            rationale=rationale,
            created_by=created_by,
        )
        self.store.restock_orders[order.id] = order
        self.store.emit(
            "procurement.restock_draft.created",
            "已生成补货草案，等待人工确认",
            {"order_id": order.id, "inventory_id": inventory_id, "quantity": quantity},
        )
        self.snapshot()
        return order

    def approve_restock_order(self, order_id: str, approver: str) -> RestockOrder:
        order = self.store.restock_orders[order_id]
        if order.status != "PENDING_CONFIRMATION":
            raise ValueError("补货单不在待确认状态")
        order.status = "SUBMITTED"
        order.approved_by = approver
        order.approved_at = utcnow()
        self.store.emit(
            "procurement.restock_order.submitted",
            "补货单已提交模拟采购接口",
            {"order_id": order.id, "approver": approver},
        )
        self.snapshot()
        return order

    def generate_daily_report(self, report_date: Optional[str] = None, automatic: bool = False) -> DailyReport:
        generated_at = utcnow()
        selected_date = datetime.now(report_timezone()).date()
        if report_date:
            try:
                selected_date = datetime.fromisoformat(report_date).date()
            except ValueError as exc:
                raise ValueError("report_date must use YYYY-MM-DD") from exc
        summary, data, html_content = build_daily_report(self._snapshot(persist=False), selected_date, generated_at)
        data = dict(data)
        data["generation_mode"] = "automatic" if automatic else "manual"
        report = DailyReport(
            id=new_id("report"),
            report_date=selected_date.isoformat(),
            title="今日渔场巡检与操作建议报告 · %s" % selected_date.isoformat(),
            generated_at=generated_at,
            summary=summary,
            html_content=html_content,
            data=data,
        )
        self.store.daily_reports[report.id] = report
        self.store.emit(
            "daily_report.generated",
            report.title,
            {
                "report_id": report.id,
                "report_date": report.report_date,
                "generation_mode": data["generation_mode"],
            },
        )
        self.snapshot()
        return report

    def generate_daily_report_if_due(self, now: Optional[datetime] = None) -> Optional[DailyReport]:
        """Generate one automatic report during the local 23:59 minute.

        Celery Beat polls every five seconds, so the report is guarded by the
        local report date and an automatic-generation marker instead of a
        transient scheduler task ID. This keeps the operation idempotent
        across repeated polls and worker restarts.
        """
        current = now or utcnow()
        if current.tzinfo is None:
            current = current.replace(tzinfo=report_timezone())
        local_now = current.astimezone(report_timezone())
        if local_now.hour != 23 or local_now.minute < 59:
            return None

        report_date = local_now.date().isoformat()
        with self._report_lock:
            existing = next(
                (
                    report
                    for report in self.store.daily_reports.values()
                    if report.report_date == report_date
                    and report.data.get("generation_mode") == "automatic"
                ),
                None,
            )
            if existing is not None:
                return existing
            try:
                report = self.generate_daily_report(report_date, automatic=True)
            except Exception as exc:
                self.store.emit(
                    "daily_report.auto_generation_failed",
                    "每日23:59自动生成日报失败：%s" % exc,
                    {"report_date": report_date},
                )
                self.snapshot()
                return None
            self.store.emit(
                "daily_report.auto_generated",
                "每日23:59已自动生成当日报告",
                {"report_id": report.id, "report_date": report.report_date},
            )
            self.snapshot()
            return report

    def delete_daily_report(self, report_id: str) -> DailyReport:
        report = self.store.daily_reports.pop(report_id, None)
        if report is None:
            raise KeyError(report_id)
        self.store.emit(
            "daily_report.deleted",
            "已删除每日报告：%s" % report.title,
            {"report_id": report.id, "report_date": report.report_date},
        )
        self.snapshot()
        return report

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

    @staticmethod
    def _llm_trace_context(context: dict) -> dict[str, Any]:
        """Keep the operator-facing request trace useful while bounding its size."""
        incident = context.get("incident") or {}
        case = context.get("analysis_case") or {}
        evidence = incident.get("evidence") or []
        return {
            "incident": {
                key: incident.get(key)
                for key in ("id", "title", "pond_id", "status", "risk")
                if incident.get(key) is not None
            },
            "analysis_case": {
                key: case.get(key)
                for key in ("id", "title", "category", "trigger", "evidence_refs")
                if case.get(key) is not None
            },
            "evidence": [
                {"type": item.get("type"), "summary": item.get("summary"), "refs": item.get("refs", [])}
                for item in evidence
            ],
            "readings": [
                {
                    key: reading.get(key)
                    for key in ("pond_id", "metric", "unit", "value", "quality", "sampled_at", "source_event_id")
                    if reading.get(key) is not None
                }
                for reading in (context.get("readings") or [])[-20:]
            ],
            "sensor_health": [
                {
                    key: item.get(key)
                    for key in ("sensor_id", "status", "message", "last_seen_at")
                    if item.get(key) is not None
                }
                for item in context.get("sensor_health") or []
            ],
            "devices": [
                {
                    key: item.get(key)
                    for key in ("id", "name", "capability", "shadow_state", "healthy", "pond_id")
                    if item.get(key) is not None
                }
                for item in context.get("devices") or []
            ],
            "weather": [
                {
                    key: item.get(key)
                    for key in ("id", "condition", "forecast", "wind_speed_mps", "rain_probability_pct", "observed_at")
                    if item.get(key) is not None
                }
                for item in context.get("weather_observations") or []
            ],
            "camera_observations": [
                {
                    key: item.get(key)
                    for key in (
                        "id",
                        "camera_role",
                        "observation_type",
                        "summary",
                        "labels",
                        "confidence",
                        "captured_at",
                        "image_url",
                    )
                    if item.get(key) is not None
                }
                for item in context.get("camera_observations") or []
            ],
            "knowledge_refs": [
                {"id": item.get("id"), "name": item.get("name")}
                for item in context.get("disease_knowledge") or []
            ],
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
        task = self.create_manual_task(
            title="模型驱动处置待人工确认：%s" % incident.title,
            description=manual_description,
            incident_id=incident.id,
        )
        run.step(
            "execution-agent",
            "route_manual_task",
            manual_description,
            details={
                "kind": "manual_task",
                "task_id": task.id,
                "status": task.status.value,
                "reason_code": reason,
            },
        )
        run.status = "COMPLETED"
        run.stop_reason = reason
        if run.plan:
            run.plan[-1]["status"] = "WAITING_HUMAN"
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
        run.plan = [
            {"id": "diagnose", "title": "诊断水质异常原因", "status": "RUNNING"},
            {"id": "knowledge", "title": "检索行业知识并形成参考建议", "status": "PENDING"},
            {"id": "confirm", "title": "高风险动作人工确认", "status": "PENDING"},
        ]
        self.store.agent_runs[run.id] = run
        self.store.emit("agent.run.started", run.goal, {"run_id": run.id, "mode": "llm"}, correlation_id=run.id)
        incident.transition(IncidentStatus.INVESTIGATING)
        orchestrator = cast(Any, self.agent_orchestrator)
        analysis_case = next(
            (item for item in self.store.analysis_cases.values() if item.incident_id == incident.id),
            None,
        )
        if analysis_case is not None:
            run.step(
                "supervisor-agent",
                "patrol_sop.entered",
                "多模态案例已纳入巡塘 SOP，先主动请求现场传感器上报",
                details={
                    "kind": "patrol_sop",
                    "stage": "entered",
                    "case_id": analysis_case.id,
                    "pond_id": incident.pond_id,
                    "case_category": analysis_case.category,
                },
            )
            self._request_sensor_reports(run, pond_id=incident.pond_id)
            run.step(
                "patrol-analysis-agent",
                "patrol_sop.evidence_ready",
                "巡塘 SOP 已完成现场数据采集，进入摄像头、天气和知识库交叉研判",
                details={
                    "kind": "patrol_sop",
                    "stage": "evidence_ready",
                    "case_id": analysis_case.id,
                    "evidence_refs": list(analysis_case.evidence_refs),
                },
            )
        context = self._incident_llm_context(incident_id)
        run.plan[0]["status"] = "COMPLETED"
        run.plan[1]["status"] = "RUNNING"
        run.step(
            "supervisor-agent",
            "llm.request",
            "向模型提交事件、现场传感器和多模态证据上下文",
            details={
                "kind": "llm_request",
                "timeout_seconds": run.budget.get("seconds", self.agent_decision_timeout_seconds),
                "message": {
                    "role": "user",
                    "from": "supervisor-agent",
                    "to": "大模型",
                    "content": (
                        "请处理事件 %s。根据下面的巡塘现场上下文、多模态文字观察和图片附件，动态委派必要的 Agent，"
                        "最终只返回 action、device_id、target_state、risk、rationale、"
                        "verification_delay_seconds、evidence_refs 组成的 JSON；禁止输出思考过程，"
                        "禁止直接调用设备写接口。"
                    ) % context.get("incident", {}).get("id", "unknown"),
                    "image_attachments": [
                        {
                            "observation_id": item.get("id"),
                            "camera_role": item.get("camera_role"),
                            "image_url": item.get("image_url"),
                            "attached": bool(item.get("image_url")),
                        }
                        for item in context.get("camera_observations") or []
                        if item.get("image_url")
                    ],
                    "context": self._llm_trace_context(context),
                },
            },
        )
        try:
            result = self._decide_incident_with_timeout(
                orchestrator,
                context,
                run.budget.get("seconds", self.agent_decision_timeout_seconds),
            )
        except TimeoutError:
            run.step(
                "supervisor-agent",
                "incident.timeout",
                "CrewAI 超过 %s 秒运行预算，迟到结果已作废" % run.budget.get("seconds", self.agent_decision_timeout_seconds),
                details={
                    "kind": "llm_error",
                    "error_code": "LLM_TIMEOUT",
                    "timeout_seconds": run.budget.get("seconds", self.agent_decision_timeout_seconds),
                },
            )
            return self._llm_manual_stop(incident, run, "LLM_TIMEOUT", "模型决策超时，已转人工确认")
        except Exception as exc:
            run.step(
                "supervisor-agent",
                "incident.failed",
                "模型调用失败：%s" % exc,
                details={"kind": "llm_error", "error_code": "LLM_UNAVAILABLE", "error": str(exc)},
            )
            return self._llm_manual_stop(incident, run, "LLM_UNAVAILABLE", "模型不可用，已转人工确认")
        for agent, action, summary in result.steps:
            run.step(agent, action, summary)
        run.plan[1]["status"] = "COMPLETED"
        run.plan[2]["status"] = "RUNNING"
        for trace in getattr(result, "trace", []) or []:
            if not isinstance(trace, dict):
                continue
            run.step(
                str(trace.get("agent") or "supervisor-agent"),
                str(trace.get("action") or "llm.trace"),
                str(trace.get("summary") or "模型数据流事件"),
                details=trace.get("details") if isinstance(trace.get("details"), dict) else {},
            )
        run.delegated_agents = sorted(set(run.delegated_agents + result.delegated_agents))
        decision: Optional[IncidentDecision] = result.decision
        if decision is None:
            reason = result.stop_reason if result.stop_reason.startswith("LLM_") else "LLM_MODEL_OR_TOOL_FAILURE"
            return self._llm_manual_stop(incident, run, reason, result.summary)

        if not getattr(result, "trace", None):
            run.step(
                "supervisor-agent",
                "llm.response",
                "模型响应已进入处置意图理解阶段",
                details={
                    "kind": "llm_response",
                    "valid": not decision.requires_manual_review,
                    "understood": True,
                    "requires_manual_review": decision.requires_manual_review,
                    "decision": {
                        "action": decision.action,
                        "device_id": decision.device_id,
                        "target_state": decision.target_state,
                        "risk": decision.risk,
                        "rationale": decision.rationale,
                        "verification_delay_seconds": decision.verification_delay_seconds,
                        "evidence_refs": decision.evidence_refs,
                        "requires_manual_review": decision.requires_manual_review,
                    },
                },
            )

        run.step(
            "execution-agent",
            "validate_llm_decision",
            "模型处置意图已理解，提交 device-control Skill 和安全策略门做执行校验",
            details={
                "kind": "decision_interpretation",
                "decision": {
                    "action": decision.action,
                    "device_id": decision.device_id,
                    "target_state": decision.target_state,
                    "risk": decision.risk,
                    "verification_delay_seconds": decision.verification_delay_seconds,
                    "evidence_refs": decision.evidence_refs,
                    "requires_manual_review": decision.requires_manual_review,
                },
            },
        )
        incident.transition(IncidentStatus.ACTION_PROPOSED)
        try:
            risk = RiskLevel(decision.risk)
            if decision.requires_manual_review:
                return self._llm_manual_stop(incident, run, result.stop_reason, decision.rationale)
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
                run.step(
                    "execution-agent",
                    "route_action",
                    "模型建议已转为 %s 受控流程" % proposal_risk.value,
                    details={
                        "kind": "action_route",
                        "proposal_id": proposal.id,
                        "proposal_status": proposal.status,
                        "risk": proposal_risk.value,
                    },
                )
                run.status = "WAITING_APPROVAL" if proposal_risk == RiskLevel.L2 else "COMPLETED"
                run.stop_reason = "WAITING_APPROVAL" if proposal_risk == RiskLevel.L2 else "MANUAL_REQUIRED"
                run.plan[-1]["status"] = "WAITING_HUMAN"
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
            if run.plan:
                run.plan[-1]["status"] = "COMPLETED"
            self.store.emit("agent.run.completed", decision.rationale, {"run_id": run.id, "command_id": command.id}, correlation_id=run.id)
        else:
            self._escalate_command_failure(incident, run, command, "LLM_ACTION_EXECUTION_FAILED")
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
            run.step("patrol-analysis-agent", "get_device_capabilities", "未找到具备增氧能力的设备，升级人工处理")
            incident.transition(IncidentStatus.ACTION_PROPOSED)
            incident.transition(IncidentStatus.MANUAL_REQUIRED)
            incident.assignee = "现场操作员"
            self.create_manual_task(
                title="检查 %s 的增氧设备" % incident.title,
                description="没有找到具备增氧能力的设备，请现场确认设备或手动增氧。",
                incident_id=incident.id,
            )
            run.status = "COMPLETED"
            run.stop_reason = "NO_CAPABLE_DEVICE"
            self.store.emit("agent.run.completed", "未找到可用增氧设备，已转人工", {"run_id": run.id}, correlation_id=run.id)
            return run
        run.step("patrol-analysis-agent", "get_device_shadow_state", "%s 当前为 %s" % (device.name, device.shadow_state))

        if device.shadow_state == "on":
            run.step("supervisor-agent", "route", "设备已开启，停止重复执行并转向效果复核/故障调查")
            run.step("execution-agent", "hold_current_state", "设备已处于开启状态，不重复下发命令，转入效果复核")
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
            self._escalate_command_failure(incident, run, command, "ACTION_EXECUTION_FAILED")
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
            self._escalate_command_failure(incident, run, command, "ACTION_EXECUTION_FAILED")
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
            if existing.id not in incident.command_ids:
                incident.command_ids.append(existing.id)
            run.step(
                "execution-agent",
                "deduplicate_command",
                "已确认设备控制：%s%s，当前状态为%s" % (
                    device.name,
                    "已开启" if existing.target_state == "on" else "已关闭",
                    existing.status.value,
                ),
                details={
                    "kind": "execution_result",
                    "command_id": existing.id,
                    "device_id": existing.device_id,
                    "device_name": device.name,
                    "pond_id": existing.pond_id,
                    "target_state": existing.target_state,
                    "status": existing.status.value,
                    "risk": existing.risk.value,
                    "policy_reason": existing.policy_reason,
                    "transport": "MQTT",
                    "success": existing.status == CommandStatus.CONFIRMED,
                    "idempotency_key": idempotency_key,
                },
            )
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
        run.step(
            "execution-agent",
            "request_action_execution",
            policy.reason,
            details={
                "kind": "policy_gate",
                "policy_status": policy.status,
                "allowed": policy.allowed,
                "reason": policy.reason,
                "device_id": device.id,
                "device_name": device.name,
                "pond_id": incident.pond_id,
                "target_state": target_state,
                "risk": risk.value,
                "multimodal_evidence": multimodal_evidence,
            },
        )
        self.store.emit("policy.evaluated", policy.reason, {"allowed": policy.allowed, "command_id": command.id}, correlation_id=run.id)
        if not policy.allowed:
            command.status = CommandStatus.REJECTED
            run.step(
                "execution-agent",
                "device.command_result",
                "设备动作被策略门拒绝：%s" % policy.reason,
                details={
                    "kind": "execution_result",
                    "command_id": command.id,
                    "status": command.status.value,
                    "device_id": device.id,
                    "target_state": target_state,
                    "transport": "MQTT",
                    "success": False,
                    "reason": policy.reason,
                },
            )
            return command

        command.status = CommandStatus.AUTHORIZED
        command.status = CommandStatus.QUEUED
        try:
            result = self.device_gateway.send_command(device, target_state, idempotency_key)
        except Exception as exc:
            result = None
            command.status = CommandStatus.FAILED
            self.store.emit("device.command.failed", "设备网关调用失败：%s" % exc, {"command_id": command.id}, correlation_id=run.id)
            run.step(
                "execution-agent",
                "device.command_result",
                "设备网关调用失败：%s" % exc,
                details={
                    "kind": "execution_result",
                    "command_id": command.id,
                    "status": command.status.value,
                    "device_id": device.id,
                    "target_state": target_state,
                    "transport": "MQTT",
                    "success": False,
                    "error": str(exc),
                },
            )
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
            run.step(
                "execution-agent",
                "device.command_result",
                "设备命令%s：%s" % ("执行成功" if command.status == CommandStatus.CONFIRMED else "未确认", result.detail or "无返回说明"),
                details={
                    "kind": "execution_result",
                    "command_id": command.id,
                    "status": command.status.value,
                    "device_id": device.id,
                    "target_state": target_state,
                    "transport": "MQTT" if "MQTT" in (result.detail or "") else "gateway",
                    "accepted": result.accepted,
                    "acknowledged": result.acknowledged,
                    "confirmed": result.confirmed,
                    "success": command.status == CommandStatus.CONFIRMED,
                    "detail": result.detail,
                },
            )
        return command

    def verify_incident(self, incident_id: str, force_escalation: bool = False) -> Incident:
        incident = self.store.incidents[incident_id]
        if incident.status != IncidentStatus.VERIFY_PENDING:
            return incident
        plan = self.store.verification_plans.get(incident.verification_plan_id or "")
        run = AgentRun(id=new_id("run"), goal="复核 %s" % incident.title, incident_id=incident.id, status="RUNNING")
        self.store.agent_runs[run.id] = run
        latest_do = self.store.latest_reading(incident.pond_id, "DO")
        threshold = plan.threshold if plan else self._do_recovery_threshold(incident.pond_id)
        run.step("verification-agent", "record_verification", "读取新鲜溶氧并按恢复阈值 %.2fmg/L 判断处置效果" % threshold)
        has_fresh_do = bool(latest_do and latest_do.is_fresh())
        passed = bool(has_fresh_do and latest_do.value >= threshold)
        outcome = "PASSED" if passed else "FAILED" if has_fresh_do else "WAITING_FOR_DATA"
        result = VerificationResult(
            id=new_id("verify"),
            incident_id=incident.id,
            plan_id=plan.id if plan else "",
            outcome=outcome,
            observed_value=latest_do.value if latest_do else None,
            evidence_refs=[latest_do.source_event_id] if latest_do else [],
        )
        self.store.verification_results[result.id] = result
        incident.verification_result_ids.append(result.id)
        if plan:
            plan.status = "PASSED" if passed else "PENDING"
        for job in self.store.scheduled_jobs.values():
            if job.incident_id == incident.id and job.job_type == "verification":
                job.status = JobStatus.COMPLETED
                job.attempts += 1
        if passed:
            device = self._aerator_device(incident.pond_id)
            stop_ok = not device or device.shadow_state == "off"
            if device and device.shadow_state == "on":
                stop_command = self.request_action_execution(
                    run,
                    incident,
                    device_id=device.id,
                    target_state="off",
                    risk=RiskLevel.L1,
                    idempotency_key="%s:%s:off" % (incident.pond_id, device.id),
                )
                stop_ok = stop_command.status == CommandStatus.CONFIRMED
            if stop_ok:
                incident.transition(IncidentStatus.RESOLVED)
                run.status = "COMPLETED"
                run.stop_reason = "RESOLVED"
                self.store.emit(
                    "verification.resolved",
                    "DO 达到恢复阈值，增氧机已停机，事件关闭",
                    {"incident_id": incident.id, "threshold": threshold},
                    correlation_id=run.id,
                )
            else:
                incident.transition(IncidentStatus.VERIFY_FAILED)
                incident.transition(IncidentStatus.ESCALATED)
                incident.assignee = "现场操作员"
                run.step("verification-agent", "create_manual_task", "复核达标但增氧机停机失败，升级人工处理")
                manual_task = self.create_manual_task(
                    title="处理增氧机停机失败：%s" % incident.title,
                    description="溶氧已达到恢复阈值，但自动停机命令未确认，请现场检查增氧机。",
                    incident_id=incident.id,
                )
                escalation = Escalation(
                    id=new_id("escalation"),
                    incident_id=incident.id,
                    level="L2",
                    reason="DO 已达到恢复阈值，但增氧机停机未确认",
                    manual_task_id=manual_task.id,
                )
                self.store.escalations[escalation.id] = escalation
                run.status = "COMPLETED"
                run.stop_reason = "ESCALATED"
                self.store.emit("verification.escalated", "停机失败，已升级人工任务", {"incident_id": incident.id}, correlation_id=run.id)
        elif force_escalation:
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
        else:
            next_due_at = utcnow() + timedelta(seconds=VERIFICATION_RETRY_SECONDS)
            self._schedule_verification(incident, next_due_at)
            run.step(
                "verification-agent",
                "schedule_reverification",
                "DO %s，未达到恢复阈值 %.2fmg/L；保持告警活跃，下次随自动或手动巡塘复核：%s"
                % (
                    "未上报新鲜数据" if outcome == "WAITING_FOR_DATA" else "仍低于恢复阈值",
                    threshold,
                    next_due_at.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            run.status = "COMPLETED"
            run.stop_reason = "VERIFY_WAITING_FOR_NEXT_PATROL"
            self.store.emit(
                "verification.retry_scheduled",
                "复核未完成，保持告警活跃并等待下一次巡塘",
                {"incident_id": incident.id, "next_due_at": next_due_at.isoformat(), "outcome": outcome},
                correlation_id=run.id,
            )
        return incident

    def dismiss_incident(self, incident_id: str, reason: str = "用户手动消除告警") -> Incident:
        incident = self.store.incidents[incident_id]
        if incident.status not in {IncidentStatus.RESOLVED, IncidentStatus.DISMISSED}:
            previous_status = incident.status.value
            incident.transition(IncidentStatus.DISMISSED)
            self.store.emit(
                "incident.dismissed",
                reason,
                {"incident_id": incident.id, "previous_status": previous_status},
                resource_type="incident",
                resource_id=incident.id,
            )
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
            self.generate_daily_report_if_due()
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
                    # Verification requires fresh telemetry; use the same MQTT
                    # request path as scheduled patrols before evaluating DO.
                    self.run_patrol()
                elif job.job_type == "patrol":
                    self.run_patrol()
                if job.status == JobStatus.RUNNING:
                    job.status = JobStatus.COMPLETED
                    self.store.emit("schedule.job.completed", "后台作业已完成", {"job_id": job.id})
                else:
                    self.store.emit("schedule.job.rescheduled", "复核未达标，已安排下一次巡塘复核", {"job_id": job.id, "due_at": job.due_at.isoformat()})
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
        with self._demo_lock:
            return self._run_demo(mode)

    def _run_demo(self, mode: str) -> dict:
        if mode not in AUTO_RESPONSE_DEMO_MODES:
            raise ValueError("unknown demo mode: %s" % mode)
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
            self._demo_reading("B-01", 2.1, source_event_id="demo-dedup")
        elif mode == "failure":
            incident = self._demo_reading("B-01", 2.1, source_event_id="demo-failure")
            if incident:
                self.store.force_verification_due(incident.id)
                self._demo_reading("B-01", 2.3, source_event_id="demo-failure-review")
                self.verify_incident(incident.id, force_escalation=True)
        elif mode == "multimodal":
            self.store.activate_multimodal_demo_data()
            self.start_analysis_case_sequence()
        elif mode == "health":
            device = self.store.devices["aerator-b04-1"]
            device.healthy = False
            self._demo_reading(
                "B-04",
                4.9,
                source_event_id="demo-health-suspect-do",
                quality="SUSPECT",
            )
        elif mode == "success":
            incident = self._demo_reading("B-01", 2.1, source_event_id="demo-success")
            # Leave the incident in VERIFY_PENDING. DO recovers gradually and
            # the next automatic patrol supplies the later verification data.
        return self.snapshot()

    def inject_demo(self, mode: str) -> dict:
        """Inject an operator-selected scenario and let the normal flow respond."""
        with self._demo_lock:
            if mode == "init":
                return self.initialize_demo()
            if mode not in AUTO_RESPONSE_DEMO_MODES:
                raise ValueError("unknown demo mode: %s" % mode)
            state = self.run_demo(mode)
            incident_ids = [item["id"] for item in state.get("incidents", [])]
            self.store.emit(
                "demo.injected",
                "已注入自动响应 Demo：%s" % DEMO_MODE_LABELS[mode],
                {
                    "mode": mode,
                    "label": DEMO_MODE_LABELS[mode],
                    "auto_response": True,
                    "transport": "mqtt",
                    "incident_ids": incident_ids,
                },
                actor_type="user",
                resource_type="demo",
                resource_id=mode,
            )
            return self.snapshot()

    def snapshot(self) -> dict:
        return self._snapshot(persist=True)

    def read_snapshot(self) -> dict:
        """Refresh a read-only process view without overwriting newer writes."""
        local_run_in_progress = any(
            run.status in {"QUEUED", "RUNNING"}
            for run in self.store.agent_runs.values()
        )
        if self.repository and not local_run_in_progress:
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
                    "image_url": self.store.cameras[observation.camera_id].source_url
                    if observation.camera_id in self.store.cameras
                    else "",
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
            "knowledge_documents": [
                {
                    "id": document.id,
                    "title": document.title,
                    "source": document.source,
                    "version": document.version,
                    "section": document.section,
                    "content": document.content,
                    "keywords": document.keywords,
                    "species": document.species,
                    "metric": document.metric,
                    "reference_dose": document.reference_dose,
                    "risk_notes": document.risk_notes,
                    "withdrawal_period": document.withdrawal_period,
                    "updated_at": document.updated_at.isoformat(),
                }
                for document in self.store.knowledge_documents.values()
            ],
            "inventory": self.inventory_snapshot(),
            "restock_orders": [
                {
                    "id": order.id,
                    "status": order.status,
                    "supplier": order.supplier,
                    "items": order.items,
                    "rationale": order.rationale,
                    "created_by": order.created_by,
                    "created_at": order.created_at.isoformat(),
                    "approved_by": order.approved_by,
                    "approved_at": order.approved_at.isoformat() if order.approved_at else None,
                }
                for order in self.store.restock_orders.values()
            ],
            "daily_reports": [
                {
                    "id": report.id,
                    "report_date": report.report_date,
                    "title": report.title,
                    "generated_at": report.generated_at.isoformat(),
                    "summary": report.summary,
                    "html_content": report.html_content,
                    "data": report.data,
                }
                for report in list(self.store.daily_reports.values())[-30:]
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
                    "title": self._patrol_incident_title(item.pond_id) if item.title.endswith("巡查异常") else item.title,
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
                    "description": self._manual_task_description(item.incident_id, item.title, item.description),
                    "assignee": item.assignee,
                    "priority": item.priority,
                    "status": item.status.value,
                    "created_at": item.created_at.isoformat(),
                    "completed_at": item.completed_at.isoformat() if item.completed_at else None,
                    "completion_report": item.completion_report,
                    "reported_at": item.reported_at.isoformat() if item.reported_at else None,
                    "reported_by": item.reported_by,
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
                            "details": step.details,
                        }
                        for step in run.steps
                    ],
                    "budget": run.budget,
                    "plan": run.plan,
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
                    "recommendations": finding.recommendations,
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

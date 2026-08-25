from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, cast

from fishagent.application.demo_data import (
    DEMO_ANALYSIS_CASES,
    DEMO_BASELINE_CAMERA_OBSERVATIONS,
    DEMO_BASELINE_WEATHER,
    DEMO_CAMERA_OBSERVATIONS,
    DEMO_DISEASE_KNOWLEDGE,
    DEMO_INVENTORY,
    DEMO_KNOWLEDGE_DOCUMENTS,
    DEMO_SENSOR_SPECS,
    DEMO_WEATHER,
)
from fishagent.domain.models import (
    ActionProposal,
    AgentRun,
    AgentStep,
    AnalysisCase,
    Approval,
    ApprovalStatus,
    AuditEvent,
    CameraObservation,
    CameraSource,
    CommandStatus,
    DailyReport,
    Device,
    DeviceCommand,
    DiseaseKnowledgeArticle,
    Escalation,
    Evidence,
    Farm,
    HealthStatus,
    Incident,
    IncidentStatus,
    InventoryItem,
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
    VisionFrame,
    WeatherObservation,
    Zone,
    new_id,
    utcnow,
)

SENSOR_NAME_BY_METRIC = {item["metric"]: item["name"] for item in DEMO_SENSOR_SPECS}
READING_QUALITY_LABELS = {
    "GOOD": "正常",
    "SUSPECT": "不可信",
    "STALE": "过期",
    "INVALID": "无效",
}
WATER_QUALITY_HIGH_LIMITS = {
    "AMMONIA": 0.50,
    "NITRITE": 0.20,
    "TURBIDITY": 50.0,
    "CHLOROPHYLL": 30.0,
    "TEMPERATURE": 32.0,
}
WATER_QUALITY_RANGES = {"PH": (6.5, 8.5)}


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
        self.weather_observations: Dict[str, WeatherObservation] = {}
        self.camera_observations: Dict[str, CameraObservation] = {}
        self.disease_knowledge: Dict[str, DiseaseKnowledgeArticle] = {}
        self.knowledge_documents: Dict[str, KnowledgeDocument] = {}
        self.inventory: Dict[str, InventoryItem] = {}
        self.restock_orders: Dict[str, RestockOrder] = {}
        self.daily_reports: Dict[str, DailyReport] = {}
        self.analysis_cases: Dict[str, AnalysisCase] = {}
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
        self.weather_observations.clear()
        self.camera_observations.clear()
        self.disease_knowledge.clear()
        self.knowledge_documents.clear()
        self.inventory.clear()
        self.restock_orders.clear()
        self.daily_reports.clear()
        self.analysis_cases.clear()
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
        device_specs = (
            ("aerator", "一号增氧机", "aeration"),
            ("feeder", "自动投饵机", "feeding"),
            ("circulation-pump", "循环水泵", "circulation"),
            ("intake-pump", "进水泵", "water_intake"),
            ("drainage-pump", "排水泵", "drainage"),
            ("dosing-pump", "加药泵", "dosing"),
            ("valve", "电动阀门", "valve_control"),
        )
        for pond_id, name, species, do_min, device_state in pond_specs:
            pond_slug = pond_id.lower().replace("-", "")
            self.ponds[pond_id] = Pond(
                id=pond_id,
                name=name,
                species=species,
                farm_id="farm-demo",
                dissolved_oxygen_min=do_min,
            )
            for spec in DEMO_SENSOR_SPECS:
                sensor_id = "%s-%s" % (spec["slug"], pond_id.lower())
                self.sensors[sensor_id] = Sensor(
                    id=sensor_id,
                    pond_id=pond_id,
                    name="%s %s传感器" % (pond_id, spec["name"]),
                    metric=spec["metric"],
                    unit=spec["unit"],
                )
                self.sensor_health[sensor_id] = SensorHealth(sensor_id=sensor_id, last_heartbeat_at=utcnow())
            for device_slug, device_name, capability in device_specs:
                device_id = "%s-%s-1" % (device_slug, pond_slug)
                self.devices[device_id] = Device(
                    id=device_id,
                    pond_id=pond_id,
                    name="%s %s" % (pond_id, device_name),
                    capability=capability,
                    shadow_state=(
                        device_state
                        if capability == "aeration"
                        else "on"
                        if (pond_id == "B-03" and capability == "feeding") or (pond_id == "B-04" and capability == "valve_control")
                        else "off"
                    ),
                    healthy=True,
                )
            self.cameras["camera-surface-%s" % pond_slug] = CameraSource(
                id="camera-surface-%s" % pond_slug,
                pond_id=pond_id,
                name="%s 水面摄像头" % pond_id,
                source_type="STATIC_IMAGE",
                camera_role="SURFACE",
                status="ONLINE",
                source_url="/static/camera-images/%s-surface.png" % pond_slug,
                last_frame_at=utcnow(),
                last_frame_id="frame-surface-%s" % pond_slug,
                last_frame_hash="generated-surface-%s" % pond_slug,
                last_frame_width=1672,
                last_frame_height=941,
            )
            self.cameras["camera-underwater-%s" % pond_slug] = CameraSource(
                id="camera-underwater-%s" % pond_slug,
                pond_id=pond_id,
                name="%s 水下摄像头" % pond_id,
                source_type="STATIC_IMAGE",
                camera_role="UNDERWATER",
                status="ONLINE",
                source_url="/static/camera-images/%s-underwater.png" % pond_slug,
                last_frame_at=utcnow(),
                last_frame_id="frame-underwater-%s" % pond_slug,
                last_frame_hash="generated-underwater-%s" % pond_slug,
                last_frame_width=1672,
                last_frame_height=941,
            )
        for pond_id, weather in DEMO_BASELINE_WEATHER.items():
            weather_data = cast(dict[str, Any], weather)
            self.weather_observations["weather-%s" % pond_id] = WeatherObservation(
                id="weather-%s" % pond_id,
                pond_id=pond_id,
                observed_at=utcnow(),
                condition=str(weather_data["condition"]),
                temperature_c=float(weather_data["temperature_c"]),
                wind_speed_mps=float(weather_data["wind_speed_mps"]),
                wind_direction=str(weather_data["wind_direction"]),
                humidity_pct=int(weather_data["humidity_pct"]),
                rain_probability_pct=int(weather_data["rain_probability_pct"]),
                pressure_hpa=float(weather_data["pressure_hpa"]),
                forecast=str(weather_data["forecast"]),
            )
        for article in DEMO_DISEASE_KNOWLEDGE:
            article_data = cast(dict[str, Any], article)
            self.disease_knowledge[str(article_data["id"])] = DiseaseKnowledgeArticle(
                id=str(article_data["id"]),
                name=str(article_data["name"]),
                species=str(article_data["species"]),
                signs=str(article_data["signs"]),
                visual_cues=list(article_data["visual_cues"]),
                recommended_actions=list(article_data["recommended_actions"]),
                severity=str(article_data["severity"]),
            )
        for document in DEMO_KNOWLEDGE_DOCUMENTS:
            document_data = cast(dict[str, Any], document)
            self.knowledge_documents[str(document_data["id"])] = KnowledgeDocument(
                id=str(document_data["id"]),
                title=str(document_data["title"]),
                source=str(document_data["source"]),
                version=str(document_data["version"]),
                section=str(document_data["section"]),
                content=str(document_data["content"]),
                keywords=list(document_data.get("keywords", [])),
                species=str(document_data.get("species", "")),
                metric=str(document_data.get("metric", "")),
                reference_dose=str(document_data.get("reference_dose", "")),
                risk_notes=str(document_data.get("risk_notes", "")),
                withdrawal_period=str(document_data.get("withdrawal_period", "")),
            )
        for inventory in DEMO_INVENTORY:
            inventory_data = cast(dict[str, Any], inventory)
            inventory_id = str(inventory_data["id"])
            self.inventory[inventory_id] = InventoryItem(
                id=inventory_id,
                name=str(inventory_data["name"]),
                category=str(inventory_data["category"]),
                unit=str(inventory_data["unit"]),
                stock_quantity=float(inventory_data["stock_quantity"]),
                minimum_quantity=float(inventory_data["minimum_quantity"]),
                reorder_quantity=float(inventory_data["reorder_quantity"]),
                supplier=str(inventory_data["supplier"]),
                pond_id=inventory_data.get("pond_id"),
            )
        for observation in DEMO_BASELINE_CAMERA_OBSERVATIONS:
            observation_data = cast(dict[str, Any], observation)
            captured_at = utcnow()
            observation_id = str(observation_data["id"])
            self.camera_observations[observation_id] = CameraObservation(
                id=observation_id,
                camera_id=str(observation_data["camera_id"]),
                pond_id=str(observation_data["pond_id"]),
                camera_role=str(observation_data["camera_role"]),
                observation_type=str(observation_data["observation_type"]),
                status=str(observation_data["status"]),
                summary=str(observation_data["summary"]),
                labels=list(observation_data["labels"]),
                confidence=float(observation_data["confidence"]),
                captured_at=captured_at,
                evidence_refs=[observation_id],
            )
            camera = self.cameras[str(observation_data["camera_id"])]
            camera.last_frame_at = captured_at
            self.vision_frames["frame-%s" % observation_id] = VisionFrame(
                id="frame-%s" % observation_id,
                camera_id=camera.id,
                source_url=camera.source_url,
                object_name=str(observation_data["observation_type"]),
                content_type="image/png",
                sha256="generated-%s" % observation["id"],
                width=camera.last_frame_width or 1672,
                height=camera.last_frame_height or 941,
                captured_at=captured_at,
            )
        for case in DEMO_ANALYSIS_CASES:
            case_data = cast(dict[str, Any], case)
            case_id = str(case_data["id"])
            self.analysis_cases[case_id] = AnalysisCase(
                id=case_id,
                sequence=int(case_data["sequence"]),
                title=str(case_data["title"]),
                category=str(case_data["category"]),
                pond_id=str(case_data["pond_id"]),
                trigger=str(case_data["trigger"]),
                description=str(case_data["description"]),
                evidence_refs=list(case_data["evidence_refs"]),
                expected_path=str(case_data["expected_path"]),
                expected_device_id=str(case_data["expected_device_id"]),
                expected_target_state=str(case_data["expected_target_state"]),
                expected_result=str(case_data["expected_result"]),
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
            "演示数据已初始化：4 个池塘、28 个传感器、28 台设备、8 路摄像头及 4 个 Agent 分析案例",
            {
                "pond_ids": [item[0] for item in pond_specs],
                "sensor_metrics": [item["metric"] for item in DEMO_SENSOR_SPECS],
                "device_count": len(self.devices),
                "camera_count": len(self.cameras),
                "analysis_case_count": len(self.analysis_cases),
            },
        )

    def activate_multimodal_demo_data(self) -> None:
        """Replace the healthy camera/weather baseline with case evidence."""
        for pond_id, weather in DEMO_WEATHER.items():
            weather_data = cast(dict[str, Any], weather)
            observation = self.weather_observations.get("weather-%s" % pond_id)
            if observation is None:
                continue
            observation.observed_at = utcnow()
            observation.condition = str(weather_data["condition"])
            observation.temperature_c = float(weather_data["temperature_c"])
            observation.wind_speed_mps = float(weather_data["wind_speed_mps"])
            observation.wind_direction = str(weather_data["wind_direction"])
            observation.humidity_pct = int(weather_data["humidity_pct"])
            observation.rain_probability_pct = int(weather_data["rain_probability_pct"])
            observation.pressure_hpa = float(weather_data["pressure_hpa"])
            observation.forecast = str(weather_data["forecast"])

        for observation_data in DEMO_CAMERA_OBSERVATIONS:
            data = cast(dict[str, Any], observation_data)
            observation = self.camera_observations.get(str(data["id"]))
            if observation is None:
                continue
            observation.observation_type = str(data["observation_type"])
            observation.status = str(data["status"])
            observation.summary = str(data["summary"])
            observation.labels = list(data["labels"])
            observation.confidence = float(data["confidence"])
            observation.captured_at = utcnow()

        self.emit(
            "demo.multimodal.activated",
            "已注入多模态摄像头与天气异常证据",
            {"camera_observation_count": len(DEMO_CAMERA_OBSERVATIONS), "weather_observation_count": len(DEMO_WEATHER)},
            actor_type="user",
            resource_type="demo",
            resource_id="multimodal",
        )

    def _upgrade_demo_camera_assets(self) -> bool:
        """Backfill generated camera frames into snapshots created before image assets existed."""
        expected_camera_ids = {str(item["camera_id"]) for item in DEMO_CAMERA_OBSERVATIONS}
        if not expected_camera_ids.issubset(self.cameras):
            return False
        changed = False
        for camera in self.cameras.values():
            if camera.id not in expected_camera_ids:
                continue
            pond_slug = camera.pond_id.lower().replace("-", "")
            role_slug = "underwater" if camera.camera_role == "UNDERWATER" else "surface"
            source_url = "/static/camera-images/%s-%s.png" % (pond_slug, role_slug)
            if camera.source_type != "STATIC_IMAGE":
                camera.source_type = "STATIC_IMAGE"
                changed = True
            if camera.source_url != source_url:
                camera.source_url = source_url
                changed = True
            if camera.last_frame_hash != "generated-%s-%s" % (role_slug, pond_slug):
                camera.last_frame_hash = "generated-%s-%s" % (role_slug, pond_slug)
                changed = True
            if camera.last_frame_width != 1672 or camera.last_frame_height != 941:
                camera.last_frame_width = 1672
                camera.last_frame_height = 941
                changed = True

        for observation_data in DEMO_CAMERA_OBSERVATIONS:
            observation_id = str(observation_data["id"])
            camera_id = str(observation_data["camera_id"])
            camera = self.cameras[camera_id]
            observation = self.camera_observations.get(observation_id)
            captured_at = observation.captured_at if observation else camera.last_frame_at or utcnow()
            if observation is None:
                self.camera_observations[observation_id] = CameraObservation(
                    id=observation_id,
                    camera_id=camera_id,
                    pond_id=str(observation_data["pond_id"]),
                    camera_role=str(observation_data["camera_role"]),
                    observation_type=str(observation_data["observation_type"]),
                    status=str(observation_data["status"]),
                    summary=str(observation_data["summary"]),
                    labels=list(observation_data["labels"]),
                    confidence=float(observation_data["confidence"]),
                    captured_at=captured_at,
                    evidence_refs=[observation_id],
                )
                observation = self.camera_observations[observation_id]
                changed = True
            frame_id = "frame-%s" % observation_id
            frame = self.vision_frames.get(frame_id)
            if frame is None:
                self.vision_frames[frame_id] = VisionFrame(
                    id=frame_id,
                    camera_id=camera_id,
                    source_url=camera.source_url,
                    object_name=str(observation_data["observation_type"]),
                    content_type="image/png",
                    sha256="generated-%s" % observation_id,
                    width=1672,
                    height=941,
                    captured_at=captured_at,
                )
                changed = True
            elif frame.source_url != camera.source_url or frame.content_type != "image/png":
                frame.source_url = camera.source_url
                frame.content_type = "image/png"
                frame.sha256 = "generated-%s" % observation_id
                frame.width = 1672
                frame.height = 941
                changed = True
        return changed

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

    @staticmethod
    def _localize_quality_message(message: object) -> str:
        text = str(message or "")
        for code, label in READING_QUALITY_LABELS.items():
            text = text.replace(code, label)
        return text

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
                message=self._localize_quality_message(item.get("message", "")),
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
                camera_role=item.get("camera_role", "SURFACE"),
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
        self.weather_observations = {
            item["id"]: WeatherObservation(
                id=item["id"],
                pond_id=item["pond_id"],
                condition=item["condition"],
                temperature_c=float(item["temperature_c"]),
                wind_speed_mps=float(item["wind_speed_mps"]),
                wind_direction=item.get("wind_direction", ""),
                humidity_pct=int(item.get("humidity_pct", 0)),
                rain_probability_pct=int(item.get("rain_probability_pct", 0)),
                pressure_hpa=float(item.get("pressure_hpa", 0)),
                forecast=item.get("forecast", ""),
                observed_at=self._datetime(item.get("observed_at")) or utcnow(),
            )
            for item in payload.get("weather_observations", [])
        }
        self.camera_observations = {
            item["id"]: CameraObservation(
                id=item["id"],
                camera_id=item["camera_id"],
                pond_id=item["pond_id"],
                camera_role=item.get("camera_role", "SURFACE"),
                observation_type=item["observation_type"],
                status=item.get("status", "READY"),
                summary=item["summary"],
                labels=item.get("labels", []),
                confidence=float(item.get("confidence", 0.0)),
                captured_at=self._datetime(item.get("captured_at")) or utcnow(),
                evidence_refs=item.get("evidence_refs", []),
            )
            for item in payload.get("camera_observations", [])
        }
        self.disease_knowledge = {
            item["id"]: DiseaseKnowledgeArticle(
                id=item["id"],
                name=item["name"],
                species=item.get("species", ""),
                signs=item.get("signs", ""),
                visual_cues=item.get("visual_cues", []),
                recommended_actions=item.get("recommended_actions", []),
                severity=item.get("severity", "MEDIUM"),
            )
            for item in payload.get("disease_knowledge", [])
        }
        self.knowledge_documents = {
            item["id"]: KnowledgeDocument(
                id=item["id"],
                title=item["title"],
                source=item.get("source", ""),
                version=item.get("version", ""),
                section=item.get("section", ""),
                content=item.get("content", ""),
                keywords=item.get("keywords", []),
                species=item.get("species", ""),
                metric=item.get("metric", ""),
                reference_dose=item.get("reference_dose", ""),
                risk_notes=item.get("risk_notes", ""),
                withdrawal_period=item.get("withdrawal_period", ""),
                updated_at=self._datetime(item.get("updated_at")) or utcnow(),
            )
            for item in payload.get("knowledge_documents", [])
        }
        self.inventory = {
            item["id"]: InventoryItem(
                id=item["id"],
                name=item["name"],
                category=item.get("category", ""),
                unit=item.get("unit", ""),
                stock_quantity=float(item.get("stock_quantity", 0)),
                minimum_quantity=float(item.get("minimum_quantity", 0)),
                reorder_quantity=float(item.get("reorder_quantity", 0)),
                supplier=item.get("supplier", ""),
                pond_id=item.get("pond_id"),
                updated_at=self._datetime(item.get("updated_at")) or utcnow(),
            )
            for item in payload.get("inventory", [])
        }
        self.restock_orders = {
            item["id"]: RestockOrder(
                id=item["id"],
                status=item.get("status", "PENDING_CONFIRMATION"),
                supplier=item.get("supplier", ""),
                items=item.get("items", []),
                rationale=item.get("rationale", ""),
                created_by=item.get("created_by", "action-planning-agent"),
                created_at=self._datetime(item.get("created_at")) or utcnow(),
                approved_by=item.get("approved_by"),
                approved_at=self._datetime(item.get("approved_at")),
            )
            for item in payload.get("restock_orders", [])
        }
        self.daily_reports = {
            item["id"]: DailyReport(
                id=item["id"],
                report_date=item["report_date"],
                title=item.get("title", "每日报告"),
                generated_at=self._datetime(item.get("generated_at")) or utcnow(),
                summary=item.get("summary", ""),
                html_content=item.get("html_content", ""),
                data=item.get("data", {}),
            )
            for item in payload.get("daily_reports", [])
        }
        self.analysis_cases = {
            item["id"]: AnalysisCase(
                id=item["id"],
                sequence=int(item["sequence"]),
                title=item["title"],
                category=item["category"],
                pond_id=item["pond_id"],
                trigger=item["trigger"],
                description=item["description"],
                evidence_refs=item.get("evidence_refs", []),
                expected_path=item.get("expected_path", ""),
                expected_device_id=item.get("expected_device_id", ""),
                expected_target_state=item.get("expected_target_state", ""),
                expected_result=item.get("expected_result", ""),
                status=item.get("status", "READY"),
                incident_id=item.get("incident_id"),
                agent_run_id=item.get("agent_run_id"),
                result_summary=item.get("result_summary", ""),
                updated_at=self._datetime(item.get("updated_at")) or utcnow(),
            )
            for item in payload.get("analysis_cases", [])
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
                completion_report=item.get("completion_report") if isinstance(item.get("completion_report"), dict) else None,
                reported_at=self._datetime(item.get("reported_at")),
                reported_by=item.get("reported_by"),
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
                budget=item.get("budget", {"delegations": 8, "tool_calls": 20, "seconds": 300}),
                plan=item.get("plan", []),
            )
            run.steps = [
                AgentStep(
                    agent=step["agent"],
                    action=step["action"],
                    summary=step["summary"],
                    created_at=self._datetime(step.get("created_at")) or utcnow(),
                    details=step.get("details", {}) if isinstance(step.get("details", {}), dict) else {},
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
                recommendations=item.get("recommendations", []),
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
        self._upgrade_demo_camera_assets()

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
        anomaly: Optional[tuple[str, str]] = None
        metric_name = SENSOR_NAME_BY_METRIC.get(reading.metric, reading.metric)
        if pond and reading.quality == "GOOD":
            if reading.metric == "DO" and reading.value < pond.dissolved_oxygen_min:
                anomaly = (
                    "%s 低溶氧" % pond.name,
                    "溶氧 %.2f%s，低于安全线 %.2f%s"
                    % (reading.value, reading.unit, pond.dissolved_oxygen_min, reading.unit),
                )
            elif reading.metric in WATER_QUALITY_HIGH_LIMITS:
                limit = WATER_QUALITY_HIGH_LIMITS[reading.metric]
                if reading.value > limit:
                    anomaly = (
                        "%s %s超标" % (pond.name, metric_name),
                        "%s %.2f%s，高于安全线 %.2f%s"
                        % (metric_name, reading.value, reading.unit, limit, reading.unit),
                    )
            elif reading.metric in WATER_QUALITY_RANGES:
                lower, upper = WATER_QUALITY_RANGES[reading.metric]
                if reading.value < lower or reading.value > upper:
                    anomaly = (
                        "%s %s异常" % (pond.name, metric_name),
                        "%s %.2f，超出安全范围 %.2f-%.2f" % (metric_name, reading.value, lower, upper),
                    )
        if anomaly:
            active = self.active_incident_for_pond(reading.pond_id)
            evidence = Evidence(
                id=new_id("evi"),
                type="sensor_snapshot",
                summary=anomaly[1],
                refs=[reading.source_event_id],
            )
            if active:
                active.evidence.append(evidence)
                self.emit(
                    "incident.evidence_merged",
                    "%s证据已合并到现有事件" % metric_name,
                    {"incident_id": active.id, "metric": reading.metric},
                )
                return active
            incident = Incident(
                id=new_id("inc"),
                pond_id=reading.pond_id,
                title=anomaly[0],
                evidence=[evidence],
            )
            self.incidents[incident.id] = incident
            self.emit(
                "incident.detected",
                incident.title,
                {"incident_id": incident.id, "pond_id": reading.pond_id, "metric": reading.metric},
            )
            return incident
        return None

    def latest_reading(self, pond_id: str, metric: str) -> Optional[SensorReading]:
        candidates = [r for r in self.readings if r.pond_id == pond_id and r.metric == metric]
        candidates.sort(key=lambda r: r.sampled_at, reverse=True)
        return candidates[0] if candidates else None

    def aeration_device_for_pond(self, pond_id: str) -> Optional[Device]:
        fallback = None
        for device in self.devices.values():
            if device.pond_id != pond_id or device.capability != "aeration":
                continue
            if device.healthy:
                return device
            fallback = fallback or device
        return fallback

    def active_incident_for_pond(self, pond_id: str) -> Optional[Incident]:
        # Escalated incidents remain active until the condition is recovered or
        # an operator explicitly dismisses them.
        terminal = {IncidentStatus.RESOLVED, IncidentStatus.DISMISSED}
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

import json
import unittest
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

from fishagent.agent_runtime.contracts import IncidentDecision
from fishagent.agent_runtime.crewai_runtime import CrewRunResult
from fishagent.application.agent_service import FishAgentSystem
from fishagent.application.policy import evaluate_action
from fishagent.core import LLMConfig, RuntimeConfigStore
from fishagent.domain.models import Device, IncidentStatus, JobStatus, RiskLevel, SensorReading, utcnow
from fishagent.infrastructure.auth import AuthManager
from fishagent.infrastructure.gateways import MqttDeviceGateway


class B01FlowTest(unittest.TestCase):
    def test_llm_decision_drives_incident_execution(self) -> None:
        class FakeOrchestrator:
            available = True

            def decide_incident(self, context):
                self.context = context
                return SimpleNamespace(
                    decision=IncidentDecision(
                        action="EXECUTE",
                        device_id="aerator-b01-1",
                        target_state="on",
                        risk="L1",
                        rationale="模型确认低溶氧证据新鲜，建议开启增氧机并复核。",
                    ),
                    summary="model decision",
                    stop_reason="LLM_DECISION_READY",
                    delegated_agents=["sensor-monitor-agent", "action-planning-agent"],
                    steps=[("supervisor-agent", "incident.decided", "model output")],
                )

        system = FishAgentSystem(agent_orchestrator=FakeOrchestrator())
        system.initialize_demo()
        with patch.object(system.device_control_skill, "execute", wraps=system.device_control_skill.execute) as skill:
            incident = system.ingest_do("B-01", 2.0, source_event_id="llm-low-do")
        self.assertIsNotNone(incident)
        skill.assert_called_once()
        state = system.snapshot()
        self.assertEqual(state["commands"][0]["status"], "CONFIRMED")
        self.assertEqual(state["agent_runs"][0]["stop_reason"], "LLM_ACTION_EXECUTED")
        self.assertEqual(state["incidents"][0]["status"], "VERIFY_PENDING")

    def test_device_control_skill_loads_contract_and_keeps_mqtt_gateway_boundary(self) -> None:
        system = FishAgentSystem()

        self.assertEqual(system.device_control_skill.name, "device-control")
        self.assertIn("MQTT", system.device_control_skill.instructions)
        self.assertIn("policy gate", system.device_control_skill.instructions)

    def test_llm_decision_skill_publishes_mqtt_device_control_command(self) -> None:
        class FakeOrchestrator:
            available = True

            def decide_incident(self, context):
                return CrewRunResult(
                    summary="低溶氧自动处置",
                    stop_reason="LLM_DECISION_READY",
                    steps=[("supervisor-agent", "incident.decided", "EXECUTE")],
                    decision=IncidentDecision(
                        action="EXECUTE",
                        device_id="aerator-b01-1",
                        target_state="on",
                        risk="L1",
                        rationale="低溶氧证据充分，开启增氧机。",
                    ),
                )

        class PublishResult:
            rc = 0

        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.published = []

            def connect(self, host, port, keepalive):
                self.connection = (host, port, keepalive)

            def loop_start(self):
                pass

            def publish(self, topic, payload, qos, retain):
                self.published.append((topic, json.loads(payload), qos, retain))
                return PublishResult()

            def loop_stop(self):
                pass

            def disconnect(self):
                pass

        client = None

        def factory(**kwargs):
            nonlocal client
            client = FakeClient(**kwargs)
            return client

        gateway = MqttDeviceGateway("mqtt.test", 1883, client_factory=factory, simulate_ack=True)
        system = FishAgentSystem(agent_orchestrator=FakeOrchestrator(), device_gateway=gateway)
        system.initialize_demo()
        system.ingest_do("B-01", 2.0, source_event_id="skill-mqtt-low-do")
        state = system.snapshot()

        self.assertEqual(state["commands"][0]["status"], "CONFIRMED")
        self.assertEqual(state["agent_runs"][0]["stop_reason"], "LLM_ACTION_EXECUTED")
        self.assertIsNotNone(client)
        self.assertEqual(client.published[0][0], "fishagent/ponds/B-01/devices/aerator-b01-1/commands")
        self.assertEqual(client.published[0][1]["source"], "fishagent.execution-agent")
        gateway.close()

    def test_production_orchestrator_without_llm_never_uses_rule_execution(self) -> None:
        class UnavailableOrchestrator:
            available = False

        system = FishAgentSystem(agent_orchestrator=UnavailableOrchestrator())
        system.initialize_demo()
        incident = system.ingest_do("B-01", 2.0, source_event_id="llm-missing")
        self.assertIsNotNone(incident)
        state = system.snapshot()
        self.assertEqual(state["commands"], [])
        self.assertEqual(state["incidents"][0]["status"], "MANUAL_REQUIRED")
        self.assertEqual(state["agent_runs"][0]["stop_reason"], "LLM_UNAVAILABLE")

    def test_llm_runtime_budget_timeout_creates_manual_task(self) -> None:
        class SlowOrchestrator:
            available = True

        system = FishAgentSystem(agent_orchestrator=SlowOrchestrator(), agent_decision_timeout_seconds=17)
        system.initialize_demo()
        with patch.object(
            system,
            "_decide_incident_with_timeout",
            side_effect=TimeoutError("budget exceeded"),
        ):
            incident = system.ingest_do("B-01", 2.0, source_event_id="llm-timeout")

        self.assertIsNotNone(incident)
        state = system.snapshot()
        self.assertEqual(state["incidents"][0]["status"], "MANUAL_REQUIRED")
        self.assertEqual(state["agent_runs"][0]["stop_reason"], "LLM_TIMEOUT")
        self.assertEqual(state["agent_runs"][0]["budget"]["seconds"], 17)
        self.assertEqual(len(state["manual_tasks"]), 1)

    def test_invalid_llm_action_reports_value_and_creates_manual_task(self) -> None:
        class FakeOrchestrator:
            available = True

            def decide_incident(self, context):
                return CrewRunResult(
                    summary="模型返回非法 action TURN_ON；允许值：EXECUTE、REQUEST_APPROVAL、MANUAL_REQUIRED、NO_ACTION、REFRESH_EVIDENCE",
                    stop_reason="LLM_DECISION_INVALID",
                    steps=[
                        (
                            "supervisor-agent",
                            "incident.invalid",
                            "unsupported LLM decision action: TURN_ON",
                        )
                    ],
                )

        system = FishAgentSystem(agent_orchestrator=FakeOrchestrator())
        system.initialize_demo()
        incident = system.ingest_do("B-01", 2.0, source_event_id="llm-invalid-action")
        self.assertIsNotNone(incident)
        state = system.snapshot()
        self.assertEqual(state["incidents"][0]["status"], "MANUAL_REQUIRED")
        self.assertEqual(state["agent_runs"][0]["stop_reason"], "LLM_DECISION_INVALID")
        self.assertIn("TURN_ON", state["manual_tasks"][0]["description"])
        self.assertIn("允许值", state["manual_tasks"][0]["description"])

    def test_llm_policy_rejection_stops_without_waiting_for_missing_approval(self) -> None:
        class FakeOrchestrator:
            available = True

            def decide_incident(self, context):
                return SimpleNamespace(
                    decision=IncidentDecision(
                        action="REQUEST_APPROVAL",
                        device_id="aerator-b01-1",
                        target_state="on",
                        risk="L2",
                        rationale="证据已过期，需要人工确认。",
                    ),
                    summary="stale evidence",
                    stop_reason="LLM_DECISION_READY",
                    delegated_agents=[],
                    steps=[],
                )

        system = FishAgentSystem(agent_orchestrator=FakeOrchestrator())
        system.initialize_demo()
        incident = system.ingest_do("B-01", 2.0, source_event_id="llm-stale", seconds_old=3600)
        self.assertIsNotNone(incident)
        state = system.snapshot()
        self.assertEqual(state["incidents"][0]["status"], "MANUAL_REQUIRED")
        self.assertEqual(state["agent_runs"][0]["stop_reason"], "LLM_POLICY_REJECTED")
        self.assertEqual(state["approvals"], [])

    def test_mqtt_device_gateway_publishes_iot_command(self) -> None:
        class PublishResult:
            rc = 0

        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.published = []

            def connect(self, host, port, keepalive):
                self.connection = (host, port, keepalive)

            def loop_start(self):
                pass

            def publish(self, topic, payload, qos, retain):
                self.published.append((topic, json.loads(payload), qos, retain))
                return PublishResult()

            def loop_stop(self):
                pass

            def disconnect(self):
                pass

        client = None

        def factory(**kwargs):
            nonlocal client
            client = FakeClient(**kwargs)
            return client

        gateway = MqttDeviceGateway("mqtt.test", 1883, client_factory=factory, simulate_ack=True)
        device = Device(id="aerator-b01-1", pond_id="B-01", name="增氧机", capability="aeration")
        result = gateway.send_command(device, "on", "llm-command-1")
        self.assertTrue(result.confirmed)
        self.assertEqual(client.published[0][0], "fishagent/ponds/B-01/devices/aerator-b01-1/commands")
        self.assertEqual(client.published[0][1]["source"], "fishagent.execution-agent")
        gateway.close()
    def test_success_demo_resolves_incident(self) -> None:
        system = FishAgentSystem()
        state = system.run_demo("success")
        self.assertEqual(state["incidents"][0]["status"], "RESOLVED")
        self.assertEqual(state["commands"][0]["status"], "CONFIRMED")
        self.assertEqual(state["devices"][0]["shadow_state"], "on")
        self.assertEqual(state["agent_runs"][0]["stop_reason"], "ACTION_EXECUTED")

    def test_failure_demo_escalates_manual_task(self) -> None:
        system = FishAgentSystem()
        state = system.run_demo("failure")
        self.assertEqual(state["incidents"][0]["status"], "ESCALATED")
        self.assertEqual(state["incidents"][0]["assignee"], "现场操作员")
        self.assertTrue(any(run["stop_reason"] == "ESCALATED" for run in state["agent_runs"]))

    def test_dedup_demo_does_not_create_new_command(self) -> None:
        system = FishAgentSystem()
        state = system.run_demo("dedup")
        self.assertEqual(state["commands"], [])
        self.assertEqual(state["agent_runs"][0]["stop_reason"], "ALREADY_SATISFIED")
        self.assertIn("patrol-analysis-agent", state["agent_runs"][0]["delegated_agents"])

    def test_normal_do_does_not_create_incident(self) -> None:
        system = FishAgentSystem()
        system.initialize_demo()
        incident = system.ingest_do("B-01", 5.6, source_event_id="normal")
        self.assertIsNone(incident)
        self.assertEqual(system.snapshot()["incidents"], [])

    def test_stale_evidence_stops_without_command(self) -> None:
        system = FishAgentSystem()
        system.initialize_demo()
        incident = system.ingest_do("B-01", 2.1, source_event_id="stale", seconds_old=3600)
        self.assertIsNotNone(incident)
        state = system.snapshot()
        self.assertEqual(state["commands"], [])
        self.assertEqual(state["agent_runs"][0]["stop_reason"], "STALE_EVIDENCE")

    def test_policy_rejects_cross_pond_device(self) -> None:
        system = FishAgentSystem()
        system.initialize_demo()
        device = system.store.devices["aerator-b01-1"]
        reading = SensorReading(
            pond_id="B-01",
            sensor_id="do-b01",
            metric="DO",
            value=2.1,
            unit="mg/L",
            sampled_at=utcnow(),
        )
        result = evaluate_action(
            actor="execution-agent",
            device=device,
            pond_id="B-02",
            target_state="on",
            risk=RiskLevel.L1,
            latest_do=reading,
            idempotency_seen=False,
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.status, "REJECTED")

    def test_policy_rejects_missing_evidence(self) -> None:
        system = FishAgentSystem()
        system.initialize_demo()
        result = evaluate_action(
            actor="execution-agent",
            device=system.store.devices["aerator-b01-1"],
            pond_id="B-01",
            target_state="on",
            risk=RiskLevel.L1,
            latest_do=None,
            idempotency_seen=False,
        )
        self.assertFalse(result.allowed)
        self.assertIn("证据缺失", result.reason)

    def test_llm_config_public_dict_masks_secret(self) -> None:
        config = LLMConfig(api_key="sk-secret-value", enabled=True)
        public = config.public_dict()
        self.assertTrue(public["api_key_configured"])
        self.assertEqual(public["api_key_preview"], "sk-sec...")
        self.assertNotIn("sk-secret-value", str(public))

    def test_llm_config_normalizes_chat_completions_endpoint(self) -> None:
        config = LLMConfig()
        config.update_from_payload(
            {
                "provider": "openrouter",
                "base_url": "https://openrouter.ai/api/v1/chat/completions",
                "model": "openrouter/free",
            }
        )
        self.assertEqual(config.provider, "openrouter")
        self.assertEqual(config.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(config.model, "openrouter/free")

    def test_llm_config_does_not_treat_placeholder_as_a_key(self) -> None:
        config = LLMConfig(api_key="sk-or-v1-REPLACE_WITH_YOUR_KEY", enabled=True)
        self.assertFalse(config.has_api_key())
        self.assertFalse(config.public_dict()["api_key_configured"])

    def test_system_does_not_seed_demo_assets_on_startup(self) -> None:
        system = FishAgentSystem()
        state = system.snapshot()
        self.assertEqual(state["ponds"], [])
        self.assertEqual(state["devices"], [])
        self.assertEqual(state["events"][0]["event_type"], "system.started")

    def test_demo_init_is_explicit(self) -> None:
        system = FishAgentSystem()
        state = system.initialize_demo()
        self.assertEqual(state["farms"][0]["id"], "farm-demo")
        self.assertEqual({item["id"] for item in state["ponds"]}, {"B-01", "B-02", "B-03", "B-04"})
        self.assertEqual(len(state["sensors"]), 28)
        self.assertEqual(len(state["devices"]), 28)
        self.assertEqual(sum(1 for item in state["devices"] if item["healthy"]), 27)
        self.assertEqual(sum(1 for item in state["devices"] if not item["healthy"]), 1)
        offline = next(item for item in state["devices"] if not item["healthy"])
        self.assertEqual(offline["id"], "aerator-b04-1")
        self.assertEqual(offline["pond_id"], "B-04")
        self.assertEqual(len(state["cameras"]), 8)
        self.assertEqual(len(state["readings"]), 252)
        self.assertEqual(len(state["schedules"]), 1)
        self.assertEqual(state["incidents"], [])
        self.assertEqual(state["dataset"]["dataset_id"], "four_pond_water_quality_demo_v2")
        self.assertEqual(state["dataset"]["source_classification"], "simulated_persistent")
        self.assertEqual(
            {item["metric"] for item in state["sensors"]},
            {"AMMONIA", "NITRITE", "TURBIDITY", "CHLOROPHYLL", "DO", "PH", "TEMPERATURE"},
        )

    def test_demo_initializes_multimodal_cases_weather_knowledge_and_cameras(self) -> None:
        system = FishAgentSystem()
        state = system.initialize_demo()

        self.assertEqual(len(state["cameras"]), 8)
        self.assertEqual({item["camera_role"] for item in state["cameras"]}, {"SURFACE", "UNDERWATER"})
        self.assertEqual(len(state["weather_observations"]), 4)
        self.assertGreaterEqual(len(state["disease_knowledge"]), 3)
        self.assertEqual(len(state["analysis_cases"]), 4)
        self.assertEqual(len(state["camera_observations"]), 4)

    def test_multimodal_cases_use_device_policy_and_manual_boundaries(self) -> None:
        class MultimodalOrchestrator:
            available = True

            def decide_incident(self, context):
                case = context["analysis_case"]
                decisions = {
                    "case-floating-head-weather": ("EXECUTE", "aerator-b01-1", "on", "L1"),
                    "case-underwater-disease": ("MANUAL_REQUIRED", "", "", "L3"),
                    "case-weak-feeding-response": ("EXECUTE", "feeder-b03-1", "off", "L1"),
                    "case-weather-front-protection": ("REQUEST_APPROVAL", "valve-b04-1", "off", "L2"),
                }
                action, device_id, target_state, risk = decisions[case["id"]]
                return SimpleNamespace(
                    decision=IncidentDecision(
                        action=action,
                        device_id=device_id,
                        target_state=target_state,
                        risk=risk,
                        rationale="多模态证据已交叉验证",
                        evidence_refs=case["evidence_refs"],
                    ),
                    summary="multimodal decision",
                    delegated_agents=["camera-analysis-agent"],
                    steps=[("camera-analysis-agent", "inspect_multimodal_evidence", "已读取摄像头、天气和知识库")],
                )

        system = FishAgentSystem(agent_orchestrator=MultimodalOrchestrator())
        system.initialize_demo()
        for case in sorted(system.store.analysis_cases.values(), key=lambda item: item.sequence):
            system.run_analysis_case(case.id)

        state = system.snapshot()
        self.assertEqual(state["commands"][0]["device_id"], "aerator-b01-1")
        self.assertEqual(state["commands"][1]["device_id"], "feeder-b03-1")
        self.assertEqual(len(state["manual_tasks"]), 1)
        self.assertEqual(len(state["approvals"]), 1)
        self.assertEqual({item["status"] for item in state["analysis_cases"]}, {"COMPLETED", "MANUAL_REQUIRED", "WAITING_APPROVAL"})
        self.assertEqual({item["metric"] for item in state["readings"]}, {item["metric"] for item in state["sensors"]})
        health = {item["sensor_id"]: item["status"] for item in state["sensor_health"]}
        self.assertEqual(health["do-b-01"], "ONLINE")
        self.assertEqual(health["do-b-04"], "ERROR")
        self.assertEqual(state["events"][0]["event_type"], "system.demo.initialized")

    def test_reset_cancels_case_sequence_before_next_case(self) -> None:
        started = Event()
        release = Event()

        class SlowOrchestrator:
            available = True

            def decide_incident(self, context):
                started.set()
                release.wait(timeout=2)
                return SimpleNamespace(
                    decision=IncidentDecision(
                        action="MANUAL_REQUIRED",
                        risk="L3",
                        rationale="需要人工复核",
                        evidence_refs=context["incident"]["evidence"][0]["refs"],
                    ),
                    summary="manual review",
                    delegated_agents=["vision-disease-agent"],
                    steps=[],
                )

        system = FishAgentSystem(agent_orchestrator=SlowOrchestrator())
        system.initialize_demo()
        self.assertTrue(system.start_analysis_case_sequence())
        self.assertTrue(started.wait(timeout=2))

        reset = Thread(target=system.initialize_demo)
        reset.start()
        release.set()
        reset.join(timeout=5)
        self.assertFalse(reset.is_alive())
        self.assertEqual({item.status for item in system.store.analysis_cases.values()}, {"READY"})
        self.assertEqual(system.store.incidents, {})

    def test_non_do_anomaly_is_routed_to_agent_and_manual_task(self) -> None:
        class ManualOrchestrator:
            available = True

            def decide_incident(self, context):
                return SimpleNamespace(
                    decision=IncidentDecision(
                        action="MANUAL_REQUIRED",
                        risk="L3",
                        rationale="氨氮异常需要现场复测并检查投喂。",
                        evidence_refs=context["incident"]["evidence"][0]["refs"],
                    ),
                    summary="manual review",
                    stop_reason="LLM_DECISION_READY",
                    delegated_agents=["sensor-monitor-agent"],
                    steps=[("supervisor-agent", "incident.decided", "氨氮异常转人工")],
                )

        system = FishAgentSystem(agent_orchestrator=ManualOrchestrator())
        system.initialize_demo()
        incident = system.ingest_reading(
            "B-02",
            0.82,
            metric="AMMONIA",
            unit="mg/L",
            source_event_id="ammonia-high",
        )
        self.assertIsNotNone(incident)
        state = system.snapshot()
        self.assertIn("氨氮超标", state["incidents"][0]["title"])
        self.assertEqual(state["incidents"][0]["status"], "MANUAL_REQUIRED")
        self.assertEqual(state["agent_runs"][0]["stop_reason"], "LLM_MANUAL_REQUIRED")
        self.assertEqual(state["manual_tasks"][0]["incident_id"], state["incidents"][0]["id"])

    def test_bad_sensor_quality_is_routed_to_agent(self) -> None:
        class ManualOrchestrator:
            available = True

            def decide_incident(self, context):
                return SimpleNamespace(
                    decision=IncidentDecision(
                        action="MANUAL_REQUIRED",
                        risk="L3",
                        rationale="传感器质量异常，要求现场校准。",
                    ),
                    summary="manual review",
                    stop_reason="LLM_DECISION_READY",
                    delegated_agents=[],
                    steps=[],
                )

        system = FishAgentSystem(agent_orchestrator=ManualOrchestrator())
        system.initialize_demo()
        incident = system.ingest_reading(
            "B-03",
            7.2,
            metric="PH",
            unit="pH",
            source_event_id="ph-suspect",
            quality="SUSPECT",
        )
        self.assertIsNotNone(incident)
        state = system.snapshot()
        self.assertIn("pH传感器异常", state["incidents"][0]["title"])
        self.assertEqual(state["incidents"][0]["status"], "MANUAL_REQUIRED")
        self.assertEqual(len(state["manual_tasks"]), 1)

    def test_patrol_records_all_sensor_metrics_and_dispatches_anomaly(self) -> None:
        class ManualOrchestrator:
            available = True

            def __init__(self) -> None:
                self.contexts = []

            def decide_incident(self, context):
                self.contexts.append(context)
                return SimpleNamespace(
                    decision=IncidentDecision(
                        action="MANUAL_REQUIRED",
                        risk="L3",
                        rationale="设备离线且传感器状态异常，需要现场处理。",
                    ),
                    summary="manual review",
                    stop_reason="LLM_DECISION_READY",
                    delegated_agents=["patrol-analysis-agent"],
                    steps=[("supervisor-agent", "incident.decided", "巡查异常转人工")],
                )

        orchestrator = ManualOrchestrator()
        system = FishAgentSystem(agent_orchestrator=orchestrator)
        system.initialize_demo()
        patrol = system.run_patrol()
        state = system.snapshot()
        b04 = next(item for item in state["patrol_findings"] if item["pond_id"] == "B-04")
        for label in ("氨氮", "亚硝酸根离子", "浊度", "叶绿素", "溶解氧", "pH", "水温"):
            self.assertIn(label, b04["summary"])
        self.assertEqual(len(b04["evidence_refs"]), 7)
        self.assertEqual(b04["status"], "NEEDS_ATTENTION")
        self.assertEqual(len(orchestrator.contexts), 1)
        self.assertTrue(any(step.action == "dispatch_incident" for step in patrol.steps))
        self.assertEqual(state["incidents"][0]["status"], "MANUAL_REQUIRED")
        self.assertEqual(len(state["manual_tasks"]), 1)

    def test_mixed_alert_demo_uses_do_and_ammonia_and_processes_both(self) -> None:
        class ManualOrchestrator:
            available = True

            def decide_incident(self, context):
                return SimpleNamespace(
                    decision=IncidentDecision(
                        action="MANUAL_REQUIRED",
                        risk="L3",
                        rationale="异常已由 CrewAI 研判并提交人工任务。",
                    ),
                    summary="manual review",
                    stop_reason="LLM_DECISION_READY",
                    delegated_agents=[],
                    steps=[],
                )

        system = FishAgentSystem(agent_orchestrator=ManualOrchestrator())
        state = system.run_demo("alerts")
        titles = [item["title"] for item in state["incidents"]]
        self.assertTrue(any("低溶氧" in title for title in titles))
        self.assertTrue(any("氨氮超标" in title for title in titles))
        self.assertEqual(len(state["agent_runs"]), 2)
        self.assertEqual(len(state["manual_tasks"]), 2)

    def test_mixed_alert_demo_executes_llm_low_do_and_routes_ammonia_to_manual(self) -> None:
        class AlertOrchestrator:
            available = True

            def __init__(self) -> None:
                self.contexts = []

            def decide_incident(self, context):
                self.contexts.append(context)
                if context["incident"]["pond_id"] == "B-01":
                    return CrewRunResult(
                        summary="低溶氧证据充分，开启增氧机并等待复核。",
                        stop_reason="LLM_DECISION_READY",
                        delegated_agents=["sensor-monitor-agent", "action-planning-agent"],
                        steps=[("supervisor-agent", "incident.decided", "低溶氧自动处置")],
                        decision=IncidentDecision(
                            action="EXECUTE",
                            device_id="aerator-b01-1",
                            target_state="on",
                            risk="L1",
                            rationale="低溶氧读数新鲜且设备健康，自动开启增氧机。",
                            verification_delay_seconds=30,
                        ),
                    )
                return CrewRunResult(
                    summary="氨氮异常需要现场复测和检查投喂记录。",
                    stop_reason="LLM_DECISION_READY",
                    delegated_agents=["sensor-monitor-agent"],
                    steps=[("supervisor-agent", "incident.decided", "氨氮异常转人工")],
                    decision=IncidentDecision(
                        action="MANUAL_REQUIRED",
                        risk="L3",
                        rationale="氨氮异常需要现场复测，禁止模型直接投药。",
                    ),
                )

        orchestrator = AlertOrchestrator()
        system = FishAgentSystem(agent_orchestrator=orchestrator)
        state = system.run_demo("alerts")

        incidents = {item["pond_id"]: item for item in state["incidents"]}
        runs = {item["incident_id"]: item for item in state["agent_runs"] if item["incident_id"]}
        self.assertEqual(len(orchestrator.contexts), 2)
        self.assertEqual(state["commands"][0]["status"], "CONFIRMED")
        self.assertEqual(state["commands"][0]["device_id"], "aerator-b01-1")
        self.assertEqual(incidents["B-01"]["status"], "VERIFY_PENDING")
        self.assertEqual(runs[incidents["B-01"]["id"]]["stop_reason"], "LLM_ACTION_EXECUTED")
        self.assertEqual(incidents["B-02"]["status"], "MANUAL_REQUIRED")
        self.assertEqual(runs[incidents["B-02"]["id"]]["stop_reason"], "LLM_MANUAL_REQUIRED")
        self.assertEqual(len(state["manual_tasks"]), 1)
        self.assertEqual(state["manual_tasks"][0]["incident_id"], incidents["B-02"]["id"])

    def test_crewai_chat_turn_is_audited_as_agent_run(self) -> None:
        class ChatOrchestrator:
            available = True

            def chat(self, message, history, pond_id):
                self.input = (message, history, pond_id)
                return CrewRunResult(
                    summary="B-02 氨氮偏高，建议复测并检查投喂记录。",
                    stop_reason="CREW_CHAT_COMPLETED",
                    delegated_agents=["sensor-monitor-agent"],
                    steps=[("supervisor-agent", "chat.completed", "已读取最新水质")],
                )

        orchestrator = ChatOrchestrator()
        system = FishAgentSystem(agent_orchestrator=orchestrator)
        system.initialize_demo()
        run, reply = system.run_chat(
            "B-02 水质怎么样？",
            [{"role": "user", "content": "先看异常"}],
            "B-02",
        )
        self.assertEqual(reply, "B-02 氨氮偏高，建议复测并检查投喂记录。")
        self.assertEqual(run.status, "COMPLETED")
        self.assertEqual(run.stop_reason, "CREW_CHAT_COMPLETED")
        self.assertEqual(orchestrator.input[2], "B-02")
        self.assertEqual(system.snapshot()["agent_runs"][0]["goal"], "对话：B-02 水质怎么样？")

    def test_demo_mock_telemetry_uses_publisher_boundary(self) -> None:
        published = []

        class LoopbackPublisher:
            def __init__(self) -> None:
                self.system = None

            def publish_reading(self, **payload):
                published.append(payload.copy())
                payload.pop("defer_persist", None)
                self.system.ingest_reading(**payload)
                return True

        publisher = LoopbackPublisher()
        system = FishAgentSystem(telemetry_publisher=publisher)
        publisher.system = system
        state = system.initialize_demo()

        self.assertEqual(len(published), 252)
        self.assertEqual({item["pond_id"] for item in published}, {"B-01", "B-02", "B-03", "B-04"})
        self.assertTrue(all(item["auto_run"] is False for item in published))
        self.assertTrue(all(item["defer_persist"] is True for item in published))
        self.assertEqual(len(state["readings"]), 252)

    def test_demo_telemetry_timeout_returns_consumed_incident(self) -> None:
        class AcceptedPublisher:
            def __init__(self, system):
                self.system = system

            def publish_reading(self, **payload):
                payload.pop("defer_persist", None)
                payload["auto_run"] = False
                self.system.ingest_reading(**payload)
                return True

        system = FishAgentSystem()
        system.initialize_demo()
        system.telemetry_publisher = AcceptedPublisher(system)

        with patch(
            "fishagent.application.agent_service.time.monotonic",
            side_effect=[0, 96],
        ):
            incident = system._demo_reading("B-02", 0.82, "accepted-ammonia", metric="AMMONIA")

        self.assertIsNotNone(incident)
        self.assertEqual(incident.status, IncidentStatus.DETECTED)

    def test_asset_creation_validates_relationships(self) -> None:
        system = FishAgentSystem()
        farm = system.create_farm({"id": "farm-a", "name": "东区养殖场", "location": "湖州"})
        pond = system.create_pond({"id": "P-01", "farm_id": farm.id, "name": "P-01 池", "species": "草鱼"})
        sensor = system.create_sensor({"id": "sensor-p01-do", "pond_id": pond.id, "name": "P-01 DO", "metric": "DO"})
        device = system.create_device({"id": "aerator-p01", "pond_id": pond.id, "name": "P-01 增氧机"})
        camera = system.create_camera({"id": "camera-p01", "pond_id": pond.id, "name": "P-01 摄像头"})
        state = system.snapshot()
        self.assertEqual(state["farms"][0]["id"], "farm-a")
        self.assertEqual(sensor.pond_id, "P-01")
        self.assertEqual(device.capability, "aeration")
        self.assertEqual(camera.status, "UNAVAILABLE")

    def test_custom_pond_uses_its_own_aeration_device(self) -> None:
        system = FishAgentSystem()
        system.create_farm({"id": "farm-a", "name": "东区养殖场"})
        system.create_pond({"id": "P-01", "farm_id": "farm-a", "name": "P-01 池", "species": "草鱼"})
        system.create_device({"id": "aerator-p01", "pond_id": "P-01", "name": "P-01 增氧机"})
        incident = system.ingest_do("P-01", 2.0, source_event_id="p01-low-do")
        self.assertIsNotNone(incident)
        state = system.snapshot()
        self.assertEqual(state["commands"][0]["device_id"], "aerator-p01")
        self.assertEqual(state["agent_runs"][0]["stop_reason"], "ACTION_EXECUTED")
        summaries = [step["summary"] for step in state["agent_runs"][0]["steps"]]
        self.assertTrue(any("P-01 增氧机" in summary for summary in summaries))

    def test_low_do_without_capable_device_requires_manual_work(self) -> None:
        system = FishAgentSystem()
        system.create_farm({"id": "farm-a", "name": "东区养殖场"})
        system.create_pond({"id": "P-02", "farm_id": "farm-a", "name": "P-02 池", "species": "草鱼"})
        incident = system.ingest_do("P-02", 2.0, source_event_id="p02-low-do")
        self.assertIsNotNone(incident)
        state = system.snapshot()
        self.assertEqual(state["incidents"][0]["status"], "MANUAL_REQUIRED")
        self.assertEqual(state["agent_runs"][0]["stop_reason"], "NO_CAPABLE_DEVICE")

    def test_runtime_config_store_persists_llm_config(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = RuntimeConfigStore(temp_dir)
            original = LLMConfig(
                provider="compatible",
                base_url="https://example.test/v1",
                model="fish-ops-model",
                api_key="sk-persisted",
                enabled=True,
            )
            store.save_llm(original)
            restored = store.load_llm(LLMConfig())
            self.assertEqual(restored.provider, "compatible")
            self.assertEqual(restored.base_url, "https://example.test/v1")
            self.assertEqual(restored.model, "fish-ops-model")
            self.assertEqual(restored.api_key, "sk-persisted")
            self.assertTrue(restored.enabled)

    def test_runtime_config_store_persists_named_llm_profiles(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = RuntimeConfigStore(temp_dir)
            active = LLMConfig(profile_id="custom-deepseek", name="DeepSeek 生产", provider="compatible", enabled=True)
            profile = LLMConfig(
                profile_id="custom-deepseek",
                name="DeepSeek 生产",
                provider="compatible",
                base_url="https://api.deepseek.com/v1",
                model="deepseek-chat",
                api_key="sk-deepseek",
                enabled=True,
            )
            store.save_llm(active, [profile])
            restored, profiles = store.load_llm_bundle(LLMConfig())
            self.assertEqual(restored.name, "DeepSeek 生产")
            self.assertEqual(len(profiles), 1)
            self.assertEqual(profiles[0].profile_id, "custom-deepseek")
            self.assertEqual(profiles[0].base_url, "https://api.deepseek.com/v1")
            self.assertEqual(profiles[0].api_key, "sk-deepseek")

    def test_l2_action_waits_for_approval_and_executes_once_approved(self) -> None:
        system = FishAgentSystem()
        system.initialize_demo()
        incident = system.ingest_do("B-01", 2.1, source_event_id="l2-low-do", auto_run=False)
        self.assertIsNotNone(incident)
        run = system.run_incident_flow(incident.id, risk_override=RiskLevel.L2)
        state = system.snapshot()
        self.assertEqual(run.stop_reason, "WAITING_APPROVAL")
        self.assertEqual(state["incidents"][0]["status"], IncidentStatus.WAITING_APPROVAL.value)
        self.assertEqual(state["approvals"][0]["status"], "PENDING")
        self.assertEqual(state["commands"], [])

        command = system.approve_action(state["action_proposals"][0]["id"], "manager", "确认现场低氧")
        self.assertEqual(command.status.value, "CONFIRMED")
        state = system.snapshot()
        self.assertEqual(state["approvals"][0]["status"], "APPROVED")
        self.assertEqual(state["incidents"][0]["status"], IncidentStatus.VERIFY_PENDING.value)
        self.assertEqual(state["scheduled_jobs"][0]["status"], JobStatus.DUE.value)

    def test_failed_verification_creates_manual_task_and_result(self) -> None:
        system = FishAgentSystem()
        state = system.run_demo("failure")
        self.assertEqual(state["verification_results"][0]["outcome"], "FAILED")
        self.assertTrue(state["manual_tasks"])
        self.assertEqual(state["manual_tasks"][0]["status"], "OPEN")

    def test_due_job_dispatch_runs_verification(self) -> None:
        system = FishAgentSystem()
        system.initialize_demo()
        incident = system.ingest_do("B-01", 2.1, source_event_id="scheduled-low")
        self.assertIsNotNone(incident)
        system.store.force_verification_due(incident.id)
        system.ingest_do("B-01", 5.0, source_event_id="scheduled-review", auto_run=False)
        jobs = system.run_due_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(system.snapshot()["incidents"][0]["status"], IncidentStatus.RESOLVED.value)

    def test_due_patrol_schedule_creates_job_and_agent_run(self) -> None:
        system = FishAgentSystem()
        system.initialize_demo()
        schedule = system.create_schedule({"id": "patrol-1", "name": "每五秒巡查", "interval_seconds": 5})
        schedule.next_run_at = utcnow()
        jobs = system.run_due_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status.value, "COMPLETED")
        self.assertTrue(any(run["stop_reason"] == "PATROL_COMPLETED" for run in system.snapshot()["agent_runs"]))

    def test_user_goal_can_start_patrol_without_demo_seed_side_effects(self) -> None:
        system = FishAgentSystem()
        system.initialize_demo()
        run = system.run_goal("巡查全场")
        self.assertEqual(run.status, "COMPLETED")
        self.assertEqual(run.stop_reason, "PATROL_COMPLETED")
        self.assertIn("sensor-monitor-agent", run.delegated_agents)

    def test_state_snapshot_restores_after_process_restart(self) -> None:
        class SnapshotRepository:
            def __init__(self) -> None:
                self.payload = None

            def load(self):
                return json.loads(json.dumps(self.payload)) if self.payload else None

            def save(self, payload):
                self.payload = json.loads(json.dumps(payload))

        repository = SnapshotRepository()
        original = FishAgentSystem(repository=repository)
        state = original.run_demo("failure")
        restored = FishAgentSystem(repository=repository)
        recovered = restored.snapshot()
        self.assertEqual(recovered["incidents"][0]["status"], "ESCALATED")
        self.assertEqual(recovered["commands"][0]["status"], "CONFIRMED")
        self.assertEqual(recovered["event_sequence"], state["event_sequence"])
        self.assertTrue(recovered["manual_tasks"])

    def test_demo_reset_keeps_event_sequence_monotonic(self) -> None:
        system = FishAgentSystem()
        first = system.initialize_demo()
        second = system.initialize_demo()
        self.assertGreater(second["event_sequence"], first["event_sequence"])
        self.assertEqual(len(second["ponds"]), 4)
        self.assertEqual(len(second["readings"]), 252)

    def test_auth_session_requires_password_and_expires(self) -> None:
        auth = AuthManager(enabled=True, username="admin", password="secret")
        self.assertIsNone(auth.login("admin", "wrong"))
        session = auth.login("admin", "secret", ttl_seconds=60)
        self.assertIsNotNone(session)
        self.assertEqual(auth.authenticate("fishagent_session=%s" % session.token).role, "admin")
        auth.logout("fishagent_session=%s" % session.token)
        self.assertIsNone(auth.authenticate("fishagent_session=%s" % session.token))


if __name__ == "__main__":
    unittest.main()

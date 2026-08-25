import json
import unittest
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from types import SimpleNamespace
from unittest.mock import patch

from fishagent.agent_runtime.contracts import IncidentDecision
from fishagent.agent_runtime.crewai_runtime import CrewRunResult
from fishagent.application.agent_service import FishAgentSystem
from fishagent.application.policy import evaluate_action
from fishagent.core import LLMConfig, RuntimeConfigStore
from fishagent.domain.models import AgentRun, Device, IncidentStatus, JobStatus, RiskLevel, SensorReading, utcnow
from fishagent.infrastructure.auth import AuthManager
from fishagent.infrastructure.gateways import GatewayResult, MqttDeviceGateway


class B01FlowTest(unittest.TestCase):
    def test_auto_response_demo_serializes_concurrent_runs(self) -> None:
        system = FishAgentSystem()
        entered = Event()
        release = Event()
        counter_lock = Lock()
        active = 0
        max_active = 0

        def fake_run_demo(mode):
            nonlocal active, max_active
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            entered.set()
            release.wait(timeout=2)
            with counter_lock:
                active -= 1
            return {"incidents": []}

        with patch.object(system, "_run_demo", side_effect=fake_run_demo):
            first = Thread(target=system.run_demo, args=("success",))
            second = Thread(target=system.run_demo, args=("success",))
            first.start()
            self.assertTrue(entered.wait(timeout=1))
            second.start()
            second.join(timeout=0.05)
            self.assertTrue(second.is_alive())
            release.set()
            first.join(timeout=1)
            second.join(timeout=1)

        self.assertEqual(max_active, 1)

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

    def test_llm_execute_can_target_unhealthy_device(self) -> None:
        class FakeOrchestrator:
            available = True

            def decide_incident(self, context):
                return SimpleNamespace(
                    decision=IncidentDecision(
                        action="EXECUTE",
                        device_id="aerator-b04-1",
                        target_state="on",
                        risk="L1",
                        rationale="低溶氧需要立即尝试开启现场增氧设备，并保留设备健康风险提示。",
                    ),
                    summary="执行不健康设备控制",
                    stop_reason="LLM_DECISION_READY",
                    delegated_agents=["action-planning-agent"],
                    steps=[],
                )

        system = FishAgentSystem(agent_orchestrator=FakeOrchestrator())
        system.initialize_demo()
        system.store.devices["aerator-b04-1"].healthy = False
        system.ingest_do("B-04", 2.0, source_event_id="llm-unhealthy-device")
        state = system.snapshot()
        command = next(item for item in state["commands"] if item["device_id"] == "aerator-b04-1")
        self.assertEqual(command["status"], "CONFIRMED")
        self.assertEqual(next(item for item in state["devices"] if item["id"] == "aerator-b04-1")["shadow_state"], "on")
        policy_step = next(step for step in state["agent_runs"][0]["steps"] if step["action"] == "request_action_execution")
        self.assertTrue(policy_step["details"]["allowed"])
        self.assertIn("健康状态异常", policy_step["details"]["reason"])

    def test_llm_aeration_confirmation_failure_creates_manual_task(self) -> None:
        class FakeOrchestrator:
            available = True

            def decide_incident(self, context):
                return SimpleNamespace(
                    decision=IncidentDecision(
                        action="EXECUTE",
                        device_id="aerator-b01-1",
                        target_state="on",
                        risk="L1",
                        rationale="可信低溶氧读数需要立即开启增氧机。",
                    ),
                    summary="设备确认失败演示",
                    stop_reason="LLM_DECISION_READY",
                    delegated_agents=["action-planning-agent"],
                    steps=[],
                )

        class FailingGateway:
            def send_command(self, device, target_state, idempotency_key):
                return GatewayResult(True, True, False, "模拟设备未确认目标状态")

        system = FishAgentSystem(agent_orchestrator=FakeOrchestrator(), device_gateway=FailingGateway())
        system.initialize_demo()
        system.ingest_do("B-01", 2.0, source_event_id="llm-aeration-confirmation-failure")
        state = system.snapshot()

        self.assertEqual(state["commands"][0]["status"], "TIMED_OUT")
        self.assertEqual(state["incidents"][0]["status"], "ESCALATED")
        self.assertEqual(state["agent_runs"][0]["stop_reason"], "LLM_ACTION_EXECUTION_FAILED")
        self.assertEqual(len(state["manual_tasks"]), 1)
        self.assertIn("自动开启设备“B-01 一号增氧机”的命令未获得设备确认", state["manual_tasks"][0]["description"])
        self.assertIn("现场检查电源", state["manual_tasks"][0]["description"])

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

    def test_interpreted_action_is_still_checked_by_device_control_skill(self) -> None:
        class FakeOrchestrator:
            available = True

            def decide_incident(self, context):
                del context
                return CrewRunResult(
                    summary="模型要求开启增氧机，但没有给出设备编号",
                    stop_reason="LLM_DECISION_READY",
                    steps=[("supervisor-agent", "incident.decided", "TURN_ON")],
                    decision=IncidentDecision.from_payload(
                        {
                            "action": "TURN_ON",
                            "risk": "L1",
                            "rationale": "低溶氧，需要开启增氧机。",
                        }
                    ),
                )

        system = FishAgentSystem(agent_orchestrator=FakeOrchestrator())
        system.initialize_demo()
        with patch.object(system.device_control_skill, "execute", wraps=system.device_control_skill.execute) as skill:
            system.ingest_do("B-01", 2.0, source_event_id="interpreted-action-missing-device")

        skill.assert_called_once()
        state = system.snapshot()
        self.assertEqual(state["incidents"][0]["status"], "MANUAL_REQUIRED")
        self.assertEqual(state["agent_runs"][0]["stop_reason"], "LLM_ACTION_INVALID")
        self.assertIn("device-control skill requires a device_id", state["manual_tasks"][0]["description"])

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
    def test_success_demo_waits_for_patrol_verification_then_stops_aerator(self) -> None:
        system = FishAgentSystem()
        state = system.run_demo("success")
        self.assertEqual(state["incidents"][0]["status"], "VERIFY_PENDING")
        self.assertEqual(state["commands"][0]["status"], "CONFIRMED")
        self.assertEqual(state["devices"][0]["shadow_state"], "on")
        self.assertEqual(state["verification_results"], [])
        self.assertGreater(state["verification_plans"][0]["threshold"], state["ponds"][0]["dissolved_oxygen_min"])
        self.assertEqual(state["agent_runs"][0]["stop_reason"], "ACTION_EXECUTED")

        incident_id = state["incidents"][0]["id"]
        system.store.force_verification_due(incident_id)
        system.ingest_do("B-01", 4.9, source_event_id="manual-patrol-recovery", auto_run=False)
        system.run_patrol()
        state = system.snapshot()
        self.assertEqual(state["incidents"][0]["status"], "RESOLVED")
        self.assertEqual(state["verification_results"][0]["outcome"], "PASSED")
        self.assertEqual(state["devices"][0]["shadow_state"], "off")
        self.assertEqual(len(state["commands"]), 2)
        self.assertEqual(state["commands"][-1]["target_state"], "off")

    def test_patrol_verification_below_recovery_threshold_stays_active(self) -> None:
        system = FishAgentSystem()
        state = system.run_demo("success")
        incident_id = state["incidents"][0]["id"]
        system.store.force_verification_due(incident_id)
        system.run_patrol()
        state = system.snapshot()
        self.assertEqual(state["incidents"][0]["status"], "VERIFY_PENDING")
        self.assertEqual(state["verification_results"][0]["outcome"], "FAILED")
        self.assertGreater(state["incidents"][0]["verification_due_at"], state["verification_results"][0]["created_at"])
        self.assertEqual(state["verification_plans"][0]["status"], "PENDING")

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
        self.assertEqual(state["incidents"][0]["status"], "VERIFY_PENDING")
        self.assertEqual(state["verification_results"], [])
        self.assertIn("patrol-analysis-agent", state["agent_runs"][0]["delegated_agents"])

    def test_reused_device_control_remains_linked_to_current_incident(self) -> None:
        system = FishAgentSystem()
        system.initialize_demo()
        incident = system.ingest_do("B-01", 2.1, source_event_id="dedup-visible", auto_run=False)
        self.assertIsNotNone(incident)

        first_run = AgentRun(id="run-first-command", goal="执行设备控制", incident_id=incident.id, status="RUNNING")
        first_command = system.request_action_execution(
            first_run,
            incident,
            device_id="aerator-b01-1",
            target_state="on",
            risk=RiskLevel.L1,
        )
        second_run = AgentRun(id="run-reused-command", goal="再次确认设备控制", incident_id=incident.id, status="RUNNING")
        reused_command = system.request_action_execution(
            second_run,
            incident,
            device_id="aerator-b01-1",
            target_state="on",
            risk=RiskLevel.L1,
        )

        self.assertEqual(reused_command.id, first_command.id)
        self.assertEqual(incident.command_ids, [first_command.id])
        self.assertEqual(second_run.steps[-1].action, "deduplicate_command")
        self.assertEqual(second_run.steps[-1].details["transport"], "MQTT")
        self.assertEqual(second_run.steps[-1].details["device_id"], "aerator-b01-1")
        self.assertEqual(second_run.steps[-1].details["target_state"], "on")

    def test_normal_do_does_not_create_incident(self) -> None:
        system = FishAgentSystem()
        system.initialize_demo()
        incident = system.ingest_do("B-01", 5.6, source_event_id="normal")
        self.assertIsNone(incident)
        self.assertEqual(system.snapshot()["incidents"], [])

    def test_manual_dismiss_closes_active_incident(self) -> None:
        system = FishAgentSystem()
        system.initialize_demo()
        incident = system.ingest_do("B-01", 2.1, source_event_id="manual-dismiss", auto_run=False)
        self.assertIsNotNone(incident)

        dismissed = system.dismiss_incident(incident.id)

        self.assertEqual(dismissed.status, IncidentStatus.DISMISSED)
        self.assertEqual(system.snapshot()["incidents"][0]["status"], "DISMISSED")

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

    def test_llm_config_persists_chat_retry_count(self) -> None:
        config = LLMConfig()
        config.update_from_payload({"chat_retry_count": 3})
        self.assertEqual(config.chat_retry_count, 3)
        config.update_from_payload({"chat_retry_count": -2})
        self.assertEqual(config.chat_retry_count, 0)
        config.update_from_payload({"chat_retry_count": 99})
        self.assertEqual(config.chat_retry_count, 10)

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
        self.assertEqual(sum(1 for item in state["devices"] if item["healthy"]), 28)
        self.assertEqual(sum(1 for item in state["devices"] if not item["healthy"]), 0)
        self.assertTrue(all(item["status"] == "ONLINE" for item in state["sensor_health"]))
        self.assertTrue(all(item["quality"] == "GOOD" for item in state["readings"]))
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
        self.assertEqual(len(state["camera_observations"]), 8)
        self.assertTrue(all(item["observation_type"].startswith("NORMAL") or item["observation_type"] == "SURFACE_WATER_STABLE" for item in state["camera_observations"]))
        self.assertTrue(all(item["rain_probability_pct"] <= 25 for item in state["weather_observations"]))
        self.assertEqual(
            {(item["pond_id"], item["camera_role"]) for item in state["camera_observations"]},
            {
                ("B-01", "SURFACE"),
                ("B-01", "UNDERWATER"),
                ("B-02", "SURFACE"),
                ("B-02", "UNDERWATER"),
                ("B-03", "SURFACE"),
                ("B-03", "UNDERWATER"),
                ("B-04", "SURFACE"),
                ("B-04", "UNDERWATER"),
            },
        )
        self.assertTrue(all(item["image_url"].startswith("/static/camera-images/") for item in state["camera_observations"]))

    def test_multimodal_demo_injects_abnormal_camera_and_weather_evidence(self) -> None:
        system = FishAgentSystem()
        system.initialize_demo()

        system.store.activate_multimodal_demo_data()

        observations = {item.id: item for item in system.store.camera_observations.values()}
        self.assertEqual(observations["obs-surface-b01-floating-head"].observation_type, "FLOATING_HEAD_GATHERING")
        self.assertEqual(observations["obs-underwater-b02-disease"].observation_type, "DISEASE_SUSPECT")
        self.assertEqual(system.store.weather_observations["weather-B-04"].rain_probability_pct, 86)

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
        self.assertEqual(health["do-b-04"], "ONLINE")
        self.assertEqual(state["events"][0]["event_type"], "system.demo.initialized")

    def test_multimodal_case_run_records_real_agent_data_stream(self) -> None:
        class MultimodalOrchestrator:
            available = True

            def decide_incident(self, context):
                case = context["analysis_case"]
                return SimpleNamespace(
                    decision=IncidentDecision(
                        action="EXECUTE",
                        device_id=case["expected_device_id"] or "aerator-b01-1",
                        target_state=case["expected_target_state"] or "on",
                        risk="L1",
                        rationale="已核对巡塘证据，执行低风险动作。",
                        evidence_refs=case["evidence_refs"],
                    ),
                    summary="case decision",
                    stop_reason="LLM_DECISION_READY",
                    delegated_agents=["vision-analysis-agent"],
                    steps=[("vision-analysis-agent", "inspect_multimodal_evidence", "已读取案例证据")],
                )

        system = FishAgentSystem(agent_orchestrator=MultimodalOrchestrator())
        system.initialize_demo()
        run = system.run_analysis_case("case-floating-head-weather")
        actions = [step.action for step in run.steps]
        details_by_action = {step.action: step.details for step in run.steps if step.details}

        self.assertIn("patrol_sop.entered", actions)
        self.assertIn("sensor_report.fallback", actions)
        self.assertIn("llm.request", actions)
        self.assertIn("llm.response", actions)
        self.assertIn("validate_llm_decision", actions)
        self.assertIn("call_skill", actions)
        self.assertIn("request_action_execution", actions)
        self.assertIn("device.command_result", actions)
        self.assertEqual(details_by_action["llm.request"]["message"]["from"], "supervisor-agent")
        self.assertEqual(details_by_action["llm.request"]["message"]["context"]["analysis_case"]["id"], "case-floating-head-weather")
        attachments = details_by_action["llm.request"]["message"]["image_attachments"]
        self.assertEqual({item["camera_role"] for item in attachments}, {"SURFACE", "UNDERWATER"})
        self.assertTrue(all(item["attached"] for item in attachments))
        self.assertEqual(details_by_action["llm.response"]["decision"]["action"], "EXECUTE")
        self.assertTrue(details_by_action["device.command_result"]["success"])

    def test_multimodal_case_can_start_without_waiting_for_model(self) -> None:
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
                        rationale="多模态证据需要人工确认",
                        evidence_refs=context["incident"]["evidence"][0]["refs"],
                    ),
                    summary="manual review",
                    delegated_agents=["vision-analysis-agent"],
                    steps=[],
                )

        system = FishAgentSystem(agent_orchestrator=SlowOrchestrator())
        system.initialize_demo()
        self.assertTrue(system.start_analysis_case("case-floating-head-weather"))
        self.assertTrue(started.wait(timeout=2))
        self.assertEqual(system.store.analysis_cases["case-floating-head-weather"].status, "RUNNING")
        release.set()
        worker = system._analysis_case_thread
        self.assertIsNotNone(worker)
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(system.store.analysis_cases["case-floating-head-weather"].status, "MANUAL_REQUIRED")

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
        self.assertIn("现场取水样复测氨氮", state["manual_tasks"][0]["description"])
        self.assertIn("核对最近 24 小时投喂量", state["manual_tasks"][0]["description"])
        self.assertIn("不得自行投药", state["manual_tasks"][0]["description"])

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
        self.assertIn("不可信", state["incidents"][0]["evidence"][0]["summary"])
        self.assertNotIn("SUSPECT", state["incidents"][0]["evidence"][0]["summary"])
        self.assertIn("处理异常传感器", state["manual_tasks"][0]["description"])
        self.assertIn("校准或更换", state["manual_tasks"][0]["description"])

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
        system.store.devices["aerator-b04-1"].healthy = False
        system.ingest_reading(
            "B-04",
            4.9,
            source_event_id="patrol-health-suspect-do",
            quality="SUSPECT",
            auto_run=False,
        )
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
        self.assertIn("巡查异常：", state["incidents"][0]["title"])
        self.assertIn("溶解氧传感器读数质量不可信", state["incidents"][0]["title"])
        self.assertIn("一号增氧机离线", state["incidents"][0]["title"])
        self.assertEqual(len(state["manual_tasks"]), 1)
        task_description = state["manual_tasks"][0]["description"]
        self.assertIn("【人工执行清单】", task_description)
        self.assertIn("B-04 一号增氧机", task_description)
        self.assertIn("离线", task_description)
        self.assertIn("便携式溶氧仪", task_description)
        self.assertIn("读数质量：不可信", task_description)
        self.assertNotIn("SUSPECT", task_description)

    def test_normal_patrol_produces_recommendations_for_each_pond(self) -> None:
        system = FishAgentSystem()
        system.initialize_demo()

        system.run_patrol()

        state = system.snapshot()
        findings = [item for item in state["patrol_findings"] if item["patrol_run_id"] == state["agent_runs"][-1]["id"]]
        self.assertEqual(len(findings), 4)
        self.assertTrue(all(item["status"] == "NORMAL" for item in findings))
        self.assertTrue(all(item["recommendations"] for item in findings))
        self.assertTrue(any("保持现有设备和养殖策略" in item for item in findings[0]["recommendations"]))
        self.assertTrue(any(step["action"] == "patrol.advice" for step in state["agent_runs"][-1]["steps"]))

    def test_patrol_requests_fresh_sensor_reports_before_inspection(self) -> None:
        class LoopbackPublisher:
            def __init__(self) -> None:
                self.system = None
                self.initial_reports = []
                self.requests = []

            def publish_reading(self, **payload):
                self.initial_reports.append(payload.copy())
                report = payload.copy()
                report.pop("defer_persist", None)
                self.system.ingest_reading(**report)
                return True

            def request_sensor_report(self, **payload):
                self.requests.append(payload.copy())
                report = payload.copy()
                report.pop("request_id", None)
                report.pop("defer_persist", None)
                self.system.ingest_reading(**report)
                return True

        publisher = LoopbackPublisher()
        system = FishAgentSystem(telemetry_publisher=publisher)
        publisher.system = system
        system.initialize_demo()
        publisher.requests.clear()

        patrol = system.run_patrol()

        self.assertEqual(len(publisher.requests), 28)
        self.assertTrue(all(item["auto_run"] is False for item in publisher.requests))
        self.assertTrue(all(item["request_id"].startswith("run-") for item in publisher.requests))
        self.assertTrue(any(step.action == "sensor_report.requested" for step in patrol.steps))
        self.assertTrue(any(step.action == "sensor_report.received" for step in patrol.steps))

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

    def test_auto_response_demo_injection_records_operator_trigger(self) -> None:
        class ManualOrchestrator:
            available = True

            def decide_incident(self, context):
                return SimpleNamespace(
                    decision=IncidentDecision(
                        action="MANUAL_REQUIRED",
                        risk="L3",
                        rationale="演示异常需要人工复核。",
                    ),
                    summary="演示转人工",
                    stop_reason="LLM_DECISION_READY",
                    delegated_agents=["sensor-monitor-agent"],
                    steps=[],
                )

        system = FishAgentSystem(agent_orchestrator=ManualOrchestrator())
        state = system.inject_demo("alerts")

        self.assertEqual(len(state["incidents"]), 2)
        self.assertTrue(any(item.source_event_id == "demo-alert-do" for item in system.store.readings))
        event = next(item for item in reversed(state["events"]) if item["event_type"] == "demo.injected")
        self.assertEqual(event["payload"]["mode"], "alerts")
        self.assertTrue(event["payload"]["auto_response"])
        self.assertEqual(event["payload"]["transport"], "mqtt")

    def test_auto_response_demo_rejects_unknown_mode(self) -> None:
        with self.assertRaises(ValueError):
            FishAgentSystem().inject_demo("unknown")

    def test_health_demo_injects_sensor_and_device_faults(self) -> None:
        state = FishAgentSystem().inject_demo("health")

        device = next(item for item in state["devices"] if item["id"] == "aerator-b04-1")
        sensor = next(item for item in state["sensor_health"] if item["sensor_id"] == "do-b-04")
        self.assertFalse(device["healthy"])
        self.assertEqual(sensor["status"], "ERROR")
        self.assertTrue(any(item["event_type"] == "demo.injected" for item in state["events"]))

    def test_auto_response_demo_uses_mqtt_publisher_for_injected_alerts(self) -> None:
        class ManualOrchestrator:
            available = True

            def decide_incident(self, context):
                return SimpleNamespace(
                    decision=IncidentDecision(
                        action="MANUAL_REQUIRED",
                        risk="L3",
                        rationale="演示异常需要人工复核。",
                    ),
                    summary="演示转人工",
                    stop_reason="LLM_DECISION_READY",
                    delegated_agents=["sensor-monitor-agent"],
                    steps=[],
                )

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
        system = FishAgentSystem(agent_orchestrator=ManualOrchestrator(), telemetry_publisher=publisher)
        publisher.system = system
        state = system.inject_demo("alerts")

        self.assertEqual(len(published), 254)
        self.assertTrue(any(item["source_event_id"] == "demo-alert-do" and item["auto_run"] for item in published))
        self.assertTrue(any(item["source_event_id"] == "demo-alert-ammonia" and item["auto_run"] for item in published))
        self.assertEqual(len(state["agent_runs"]), 2)
        self.assertTrue(any(item["event_type"] == "demo.injected" for item in state["events"]))

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
                chat_retry_count=3,
            )
            store.save_llm(original)
            restored = store.load_llm(LLMConfig())
            self.assertEqual(restored.provider, "compatible")
            self.assertEqual(restored.base_url, "https://example.test/v1")
            self.assertEqual(restored.model, "fish-ops-model")
            self.assertEqual(restored.api_key, "sk-persisted")
            self.assertTrue(restored.enabled)
            self.assertEqual(restored.chat_retry_count, 3)

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

    def test_manual_task_requires_complete_report_before_completion(self) -> None:
        system = FishAgentSystem()
        task = system.create_manual_task(
            "模型驱动处置待人工确认：B-02 氨氮超标",
            "处理背景：需要现场复核\n告警位置：B-02 草鱼生态池\n异常指标：氨氮 0.82mg/L（高于安全线 0.50mg/L）\n【人工执行清单】\n1. 复测氨氮并记录两个点位\n2. 核对投喂和换水记录\n【完成回报】记录现场结果",
        )

        with self.assertRaisesRegex(ValueError, "请先提交完整处理结果"):
            system.complete_manual_task(task.id)

        report = {
            "checklist_results": [
                {"result": "池中心 0.70mg/L，进水口 0.66mg/L，已拍照"},
                {"result": "已核对投喂、残饵和换水记录，未发现漏记"},
            ],
            "retest_data": "池中心 0.70mg/L；进水口 0.66mg/L",
            "device_status": "增氧机在线，关闭",
            "actions_taken": "暂停下一轮投喂，已通知养殖负责人",
            "executed_at": "2026-08-25T18:10",
            "photo_evidence": "IMG-20260825-001",
            "notes": "等待负责人确认后决定后续换水",
        }
        submitted = system.submit_manual_task_report(task.id, report, "operator")
        self.assertEqual(submitted.status.value, "COMPLETED")
        self.assertEqual(len(submitted.completion_report["checklist_results"]), 2)
        self.assertIsNotNone(submitted.completed_at)

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

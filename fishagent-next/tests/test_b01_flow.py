import json
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from fishagent.agent_runtime.contracts import IncidentDecision
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
        incident = system.ingest_do("B-01", 2.0, source_event_id="llm-low-do")
        self.assertIsNotNone(incident)
        state = system.snapshot()
        self.assertEqual(state["commands"][0]["status"], "CONFIRMED")
        self.assertEqual(state["agent_runs"][0]["stop_reason"], "LLM_ACTION_EXECUTED")
        self.assertEqual(state["incidents"][0]["status"], "VERIFY_PENDING")

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
        self.assertEqual(len(state["sensors"]), 4)
        self.assertEqual(len(state["devices"]), 4)
        self.assertEqual(len(state["cameras"]), 4)
        self.assertEqual(len(state["readings"]), 36)
        self.assertEqual(len(state["schedules"]), 1)
        self.assertEqual(state["incidents"], [])
        self.assertEqual(state["dataset"]["dataset_id"], "four_pond_demo_v1")
        self.assertEqual(state["dataset"]["source_classification"], "simulated_persistent")
        health = {item["sensor_id"]: item["status"] for item in state["sensor_health"]}
        self.assertEqual(health["do-b-01"], "ONLINE")
        self.assertEqual(health["do-b-04"], "ERROR")
        self.assertEqual(state["events"][0]["event_type"], "system.demo.initialized")

    def test_demo_mock_telemetry_uses_publisher_boundary(self) -> None:
        published = []

        class LoopbackPublisher:
            def __init__(self) -> None:
                self.system = None

            def publish_reading(self, **payload):
                published.append(payload.copy())
                payload.pop("defer_persist", None)
                self.system.ingest_do(**payload)
                return True

        publisher = LoopbackPublisher()
        system = FishAgentSystem(telemetry_publisher=publisher)
        publisher.system = system
        state = system.initialize_demo()

        self.assertEqual(len(published), 36)
        self.assertEqual({item["pond_id"] for item in published}, {"B-01", "B-02", "B-03", "B-04"})
        self.assertTrue(all(item["auto_run"] is False for item in published))
        self.assertTrue(all(item["defer_persist"] is True for item in published))
        self.assertEqual(len(state["readings"]), 36)

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
        self.assertEqual(len(second["readings"]), 36)

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

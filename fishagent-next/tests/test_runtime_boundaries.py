import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from fishagent.agent_runtime.contracts import IncidentDecision
from fishagent.agent_runtime.crewai_runtime import CrewAIOrchestrator
from fishagent.core import LLMConfig
from fishagent.domain.models import AgentRun, Device
from fishagent.infrastructure.gateways import SimulatorDeviceGateway
from fishagent.infrastructure.mqtt import MqttTelemetryAdapter
from fishagent.infrastructure.queue.celery_app import celery_app
from fishagent.web.app import SYSTEM, app
from fishagent.web.server import test_llm_connection as check_llm_connection


class RuntimeBoundaryTests(unittest.TestCase):
    def test_crewai_uses_litellm_openrouter_model_prefix(self) -> None:
        orchestrator = object.__new__(CrewAIOrchestrator)
        orchestrator.available = True
        orchestrator.llm_config = LLMConfig(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model="openrouter/free",
            api_key="test-key",
            enabled=True,
        )
        orchestrator._LLM = lambda **kwargs: kwargs

        llm = orchestrator._llm()

        self.assertEqual(llm["model"], "openrouter/openrouter/free")
        self.assertEqual(llm["base_url"], "https://openrouter.ai/api/v1")

    def test_crewai_uses_openai_prefix_for_compatible_endpoints(self) -> None:
        orchestrator = object.__new__(CrewAIOrchestrator)
        orchestrator.available = True
        orchestrator.llm_config = LLMConfig(
            provider="compatible",
            base_url="https://ark.cn-beijing.volces.com/api/plan/v3",
            model="ark-code-latest",
            api_key="test-key",
            enabled=True,
        )
        orchestrator._LLM = lambda **kwargs: kwargs

        llm = orchestrator._llm()

        self.assertEqual(llm["model"], "openai/ark-code-latest")
        self.assertEqual(llm["base_url"], "https://ark.cn-beijing.volces.com/api/plan/v3")

    def test_crewai_hierarchical_manager_is_not_duplicated_in_member_agents(self) -> None:
        orchestrator = object.__new__(CrewAIOrchestrator)
        orchestrator.system = SimpleNamespace(
            snapshot=lambda: {"ponds": [], "readings": [], "devices": [], "incidents": []}
        )
        orchestrator._llm = lambda: "llm"
        orchestrator._tool = lambda _name: lambda function: function
        orchestrator._Agent = lambda **kwargs: SimpleNamespace(**kwargs)
        orchestrator._Task = lambda **kwargs: SimpleNamespace(**kwargs)
        orchestrator._Process = SimpleNamespace(hierarchical="hierarchical")
        captured = {}

        def crew_factory(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(kickoff=lambda inputs: inputs)

        orchestrator._Crew = crew_factory
        orchestrator._kickoff_crew("检查全场", None, [])

        self.assertEqual(captured["manager_agent"].role, "主决策 Agent")
        self.assertEqual(captured["manager_agent"].tools, [])
        self.assertNotIn(captured["manager_agent"], captured["agents"])
        self.assertIsNone(captured["tasks"][0].agent)
        self.assertEqual(
            [agent.role for agent in captured["agents"]],
            ["传感器监控 Agent", "巡查分析 Agent", "视觉与病害分析 Agent", "行动规划 Agent"],
        )

    def test_crewai_chat_only_exposes_final_answer(self) -> None:
        raw = "Here's a thinking process:\ninternal details\n结论：B-02 水质稳定。"

        answer = CrewAIOrchestrator._extract_public_answer(raw)

        self.assertEqual(answer, "结论：B-02 水质稳定。")

    def test_crewai_chat_rejects_reasoning_without_final_answer(self) -> None:
        with self.assertRaisesRegex(ValueError, "safe final chat answer"):
            CrewAIOrchestrator._extract_public_answer("Here's a thinking process: internal details")

    def test_crewai_classifies_provider_rate_limits_separately_from_invalid_json(self) -> None:
        self.assertEqual(
            CrewAIOrchestrator._decision_failure_reason(RuntimeError("429 free-models-per-day")),
            "LLM_RATE_LIMITED",
        )
        self.assertEqual(
            CrewAIOrchestrator._decision_failure_reason(RuntimeError("connection reset")),
            "LLM_MODEL_OR_TOOL_FAILURE",
        )
        self.assertEqual(
            CrewAIOrchestrator._decision_failure_reason(RuntimeError("LLM Provider NOT provided")),
            "LLM_PROVIDER_CONFIG_INVALID",
        )

    def test_crewai_decision_invalid_only_means_invalid_structured_output(self) -> None:
        orchestrator = object.__new__(CrewAIOrchestrator)
        orchestrator.available = True
        orchestrator.last_error = None
        orchestrator._kickoff_crew = lambda *args, **kwargs: SimpleNamespace(raw="not json")

        result = orchestrator.decide_incident({"incident": {"id": "inc-test", "pond_id": "B-01"}})

        self.assertIsNone(result.decision)
        self.assertEqual(result.stop_reason, "LLM_DECISION_INVALID")

    def test_invalid_llm_action_reports_actual_and_allowed_actions(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            r"unsupported LLM decision action: TURN_ON; allowed actions: .*EXECUTE.*",
        ):
            IncidentDecision.from_payload(
                {
                    "action": "TURN_ON",
                    "risk": "L1",
                    "rationale": "test",
                }
            )

    def test_crewai_decision_rate_limit_is_not_reported_as_invalid(self) -> None:
        orchestrator = object.__new__(CrewAIOrchestrator)
        orchestrator.available = True
        orchestrator.last_error = None

        def fail(*args, **kwargs):
            raise RuntimeError("429 free-models-per-day")

        orchestrator._kickoff_crew = fail
        result = orchestrator.decide_incident({"incident": {"id": "inc-test", "pond_id": "B-01"}})

        self.assertIsNone(result.decision)
        self.assertEqual(result.stop_reason, "LLM_RATE_LIMITED")

    def test_console_keeps_core_views_and_adds_operational_views(self) -> None:
        with TestClient(app) as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        for view in ("monitor", "assistant", "management", "analytics"):
            self.assertIn(f'id="view_{view}"', response.text)
        for view in ("assets", "work", "schedules", "audit"):
            self.assertIn(f'id="view_{view}"', response.text)
        self.assertIn('onclick="openLlmDialog()"', response.text)
        self.assertIn('<option value="openrouter">OpenRouter</option>', response.text)
        for metric in ("AMMONIA", "NITRITE", "TURBIDITY", "CHLOROPHYLL", "PH", "TEMPERATURE"):
            self.assertIn(f'id="sensor_chart_{metric}"', response.text)
        self.assertIn('id="llm_profile"', response.text)
        self.assertIn('onclick="newLlmProvider()"', response.text)
        self.assertIn("id:'preset:volcengine'", response.text)
        self.assertIn("name:'火山引擎'", response.text)
        self.assertIn("base_url:'https://ark.cn-beijing.volces.com/api/plan/v3'", response.text)
        self.assertIn("model:'ark-code-latest'", response.text)
        self.assertNotIn('id="monitor_chart"', response.text)
        self.assertIn('position: sticky', response.text)
        self.assertIn('id="llm_layer"', response.text)
        self.assertIn('id="alert_capsule"', response.text)
        self.assertIn('id="alert_capsule_toggle"', response.text)
        self.assertIn('id="alert_capsule_list"', response.text)
        self.assertIn('class="alert-capsule-track"', response.text)
        self.assertIn('id="alert_panel"', response.text)
        self.assertIn('onclick="toggleAlertCapsule(event)"', response.text)
        self.assertIn('onclick="openAlertView(event)"', response.text)
        self.assertIn("function advanceCountdownTarget", response.text)
        self.assertIn("采样 ${fmtDate(sampledAt)}", response.text)
        self.assertIn('id="assistant_chat"', response.text)
        self.assertIn('id="assistant_chat_input"', response.text)
        self.assertIn("api('/api/v1/agent-chat'", response.text)
        self.assertIn("全场设备 · 在线率", response.text)
        self.assertIn("<b>处理结果：</b>", response.text)
        self.assertIn("waterMetrics.map", response.text)

    def test_agent_chat_endpoint_returns_audited_crewai_reply(self) -> None:
        run = AgentRun(id="run-chat-test", goal="对话：检查 B-02", status="COMPLETED", stop_reason="CREW_CHAT_COMPLETED")
        run_data = {
            "id": run.id,
            "goal": run.goal,
            "status": run.status,
            "stop_reason": run.stop_reason,
            "steps": [],
            "delegated_agents": [],
            "budget": run.budget,
        }
        with (
            patch.object(SYSTEM, "run_chat", return_value=(run, "B-02 水质总体稳定。")) as mocked_chat,
            patch.object(SYSTEM, "snapshot", return_value={"agent_runs": [run_data]}),
            TestClient(app) as client,
        ):
            response = client.post(
                "/api/v1/agent-chat",
                json={
                    "message": "检查 B-02",
                    "pond_id": "B-02",
                    "history": [{"role": "user", "content": "先看传感器"}],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], "B-02 水质总体稳定。")
        mocked_chat.assert_called_once_with(
            "检查 B-02",
            [{"role": "user", "content": "先看传感器"}],
            "B-02",
        )

    def test_patrol_endpoint_persists_run_before_building_response(self) -> None:
        run = AgentRun(id="run-patrol-test", goal="执行全场巡查", status="COMPLETED", stop_reason="PATROL_COMPLETED")
        run_data = {
            "id": run.id,
            "goal": run.goal,
            "status": run.status,
            "stop_reason": run.stop_reason,
            "steps": [],
            "delegated_agents": [],
            "budget": run.budget,
        }
        state = {"agent_runs": [run_data]}
        with (
            patch.object(SYSTEM, "run_patrol", return_value=run),
            patch.object(SYSTEM, "snapshot", return_value=state) as snapshot,
            TestClient(app) as client,
        ):
            response = client.post("/api/v1/patrol-runs", json={})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["run"]["id"], run.id)
        snapshot.assert_called_once_with()

    def test_llm_connection_posts_selected_model_to_chat_completions(self) -> None:
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return b'{"model":"openrouter/free","choices":[{"message":{"content":"OK"}}]}'

        config = LLMConfig(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model="openrouter/free",
            api_key="test-key",
            enabled=True,
        )
        with patch("fishagent.web.server.urlopen", return_value=Response()) as mocked:
            result = check_llm_connection(config)

        request = mocked.call_args.args[0]
        self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data)["model"], "openrouter/free")
        self.assertTrue(result["ok"])

    def test_fastapi_openapi_and_websocket_replay(self) -> None:
        with TestClient(app) as client:
            self.assertEqual(client.get("/health/live").status_code, 200)
            self.assertEqual(client.get("/openapi.json").status_code, 200)
            self.assertEqual(client.get("/api/openapi.json").status_code, 200)
            client.post("/api/v1/demo/init")
            with client.websocket_connect("/events?after=0") as websocket:
                event = websocket.receive_json()
                self.assertIn(event["event_type"], {"system.started", "system.demo.initialized"})

    def test_request_correlation_and_prometheus_metrics(self) -> None:
        with TestClient(app) as client:
            response = client.get("/health/live", headers={"X-Correlation-ID": "test-correlation"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["X-Correlation-ID"], "test-correlation")
            metrics = client.get("/metrics")
            self.assertEqual(metrics.status_code, 200)
            self.assertIn("fishagent_http_requests_total", metrics.text)

    def test_audit_event_keeps_actor_and_resource(self) -> None:
        from fishagent.application.agent_service import FishAgentSystem

        system = FishAgentSystem()
        system.store.emit(
            "test.approval",
            "测试审批",
            actor_type="user",
            actor_id="manager-1",
            resource_type="approval",
            resource_id="approval-1",
        )
        audit = system.snapshot()["audit_events"][-1]
        self.assertEqual(audit["actor_type"], "user")
        self.assertEqual(audit["actor_id"], "manager-1")
        self.assertEqual(audit["resource_type"], "approval")
        self.assertEqual(audit["resource_id"], "approval-1")

    def test_simulator_gateway_is_safe_for_unhealthy_device(self) -> None:
        device = Device(id="d-1", pond_id="p-1", name="增氧机", capability="aeration", healthy=False)
        result = SimulatorDeviceGateway().send_command(device, "on", "p-1:d-1:on")
        self.assertFalse(result.accepted)
        self.assertFalse(result.confirmed)

    def test_mqtt_adapter_maps_topic_and_payload(self) -> None:
        received = []
        adapter = MqttTelemetryAdapter("127.0.0.1", 1883, "farms/+/ponds/+/sensors/+", lambda **data: received.append(data))

        class Message:
            topic = "farms/f-1/ponds/B-01/sensors/s-1"
            payload = json.dumps(
                {"metric": "AMMONIA", "unit": "mg/L", "value": 0.18, "source_event_id": "mqtt-1"}
            ).encode()

        adapter._on_message(None, None, Message())
        self.assertEqual(received[0]["pond_id"], "B-01")
        self.assertEqual(received[0]["sensor_id"], "s-1")
        self.assertEqual(received[0]["metric"], "AMMONIA")
        self.assertEqual(received[0]["unit"], "mg/L")
        self.assertEqual(received[0]["value"], 0.18)

    def test_mqtt_network_callback_queues_slow_ingest(self) -> None:
        ingest_started = threading.Event()
        allow_ingest = threading.Event()

        def slow_ingest(**data) -> None:
            del data
            ingest_started.set()
            allow_ingest.wait(timeout=1)

        adapter = MqttTelemetryAdapter("127.0.0.1", 1883, "farms/+/ponds/+/sensors/+", slow_ingest)
        adapter._ingest_worker = threading.Thread(target=adapter._run_ingest_worker, daemon=True)
        adapter._ingest_worker.start()

        class Message:
            topic = "farms/f-1/ponds/B-01/sensors/s-1"
            payload = b'{"metric":"DO","unit":"mg/L","value":2.8,"source_event_id":"slow-1"}'

        adapter._on_message(None, None, Message())
        self.assertTrue(ingest_started.wait(timeout=1))
        self.assertTrue(adapter._ingest_worker.is_alive())
        allow_ingest.set()
        adapter._ingest_queue.join()
        adapter._ingest_queue.put(None)
        adapter._ingest_worker.join(timeout=1)

    def test_celery_has_default_queue_and_beat_tick(self) -> None:
        self.assertEqual(celery_app.conf.task_default_queue, "default")
        self.assertIn("dispatch-due-jobs-every-five-seconds", celery_app.conf.beat_schedule)

    def test_resource_contract_and_device_command_idempotency(self) -> None:
        with TestClient(app) as client:
            client.post("/api/v1/demo/init")
            zone = client.post("/api/v1/zones", json={"id": "zone-api", "farm_id": "farm-demo", "name": "西区"})
            self.assertEqual(zone.status_code, 201)
            self.assertEqual(client.get("/api/v1/sensors/do-b-01/health").status_code, 200)
            state = client.post(
                "/api/v1/telemetry/readings:batch",
                json={"readings": [{"pond_id": "B-01", "value": 2.0, "source_event_id": "api-command-reading", "auto_run": False}]},
            ).json()["state"]
            incident_id = state["incidents"][0]["id"]
            headers = {"Idempotency-Key": "api-command-1"}
            first = client.post(
                "/api/v1/device-commands",
                headers=headers,
                json={"incident_id": incident_id, "device_id": "aerator-b01-1", "target_state": "on", "risk": "L1"},
            )
            second = client.post(
                "/api/v1/device-commands",
                headers=headers,
                json={"incident_id": incident_id, "device_id": "aerator-b01-1", "target_state": "on", "risk": "L1"},
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(first.json()["device_command"]["id"], second.json()["device_command"]["id"])

    def test_device_command_cannot_forge_l2_approval(self) -> None:
        with TestClient(app) as client:
            client.post("/api/v1/demo/init")
            state = client.post(
                "/api/v1/telemetry/readings:batch",
                json={"readings": [{"pond_id": "B-01", "value": 2.0, "source_event_id": "api-l2-reading", "auto_run": False}]},
            ).json()["state"]
            incident_id = state["incidents"][0]["id"]
            response = client.post(
                "/api/v1/device-commands",
                json={
                    "incident_id": incident_id,
                    "device_id": "aerator-b01-1",
                    "target_state": "on",
                    "risk": "L2",
                    "approval_granted": True,
                },
            )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["title"], "Approval required")

    def test_audit_export_is_available_in_json_and_csv(self) -> None:
        with TestClient(app) as client:
            client.post("/api/v1/demo/init")
            json_export = client.get("/api/v1/audit-events/export?limit=5")
            csv_export = client.get("/api/v1/audit-events/export?limit=5&format=csv")
            self.assertEqual(json_export.status_code, 200)
            self.assertLessEqual(len(json_export.json()["audit_events"]), 5)
            self.assertEqual(csv_export.status_code, 200)
            self.assertIn("actor_type", csv_export.text.splitlines()[0])
            self.assertIn("attachment", csv_export.headers.get("content-disposition", ""))


if __name__ == "__main__":
    unittest.main()

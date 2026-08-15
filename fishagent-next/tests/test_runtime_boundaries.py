import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from fishagent.agent_runtime.contracts import IncidentDecision
from fishagent.agent_runtime.crewai_runtime import (
    CrewAIOrchestrator,
    CrewRunResult,
    _MultimodalCrewLLM,
)
from fishagent.application.agent_service import FishAgentSystem
from fishagent.core import LLMConfig
from fishagent.domain.models import AgentRun, Device
from fishagent.infrastructure.gateways import SimulatorDeviceGateway
from fishagent.infrastructure.mqtt import MqttTelemetryAdapter, MqttTelemetryPublisher
from fishagent.infrastructure.queue.celery_app import celery_app
from fishagent.web.app import SYSTEM, app
from fishagent.web.server import test_llm_connection as check_llm_connection


class RuntimeBoundaryTests(unittest.TestCase):
    def test_multimodal_proxy_attaches_camera_image_as_vision_content(self) -> None:
        class FakeLLM:
            stop = None

            def supports_stop_words(self):
                return True

            def call(self, messages, **kwargs):
                del kwargs
                self.messages = messages
                return "ok"

        base = FakeLLM()
        image_path = Path(__file__).parents[1] / "src/fishagent/web/static/camera-images/b01-surface.png"
        proxy = _MultimodalCrewLLM(base, [image_path])

        self.assertEqual(proxy.call([{"role": "user", "content": "请查看图片"}]), "ok")
        content = base.messages[-1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "请查看图片"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

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
        self.assertIn("必须返回 EXECUTE", captured["tasks"][0].description)
        self.assertIsNone(getattr(captured["tasks"][0], "output_pydantic", None))
        self.assertIsNone(getattr(captured["tasks"][0], "output_json", None))

    def test_crewai_chat_only_exposes_final_answer(self) -> None:
        raw = "Here's a thinking process:\ninternal details\n结论：B-02 水质稳定。"

        answer = CrewAIOrchestrator._extract_public_answer(raw)

        self.assertEqual(answer, "结论：B-02 水质稳定。")

    def test_crewai_chat_recovers_when_only_reasoning_is_returned(self) -> None:
        self.assertEqual(
            CrewAIOrchestrator._extract_public_answer("Here's a thinking process: internal details"),
            "结论：模型未给出可展示结论，请稍后重试。",
        )

    def test_crewai_chat_localizes_snapshot_times_for_operator(self) -> None:
        orchestrator = object.__new__(CrewAIOrchestrator)
        orchestrator.available = True
        orchestrator.last_error = None
        orchestrator.llm_config = LLMConfig(chat_retry_count=0)
        orchestrator.system = SimpleNamespace(
            snapshot=lambda: {
                "ponds": [{"id": "B-02", "name": "B-02 草鱼生态池"}],
                "readings": [
                    {
                        "pond_id": "B-02",
                        "metric": "PH",
                        "value": 7.9,
                        "quality": "GOOD",
                        "sampled_at": "2026-08-14T07:24:00+00:00",
                    }
                ],
                "sensors": [{"id": "ph-b02", "pond_id": "B-02"}],
                "sensor_health": [{"sensor_id": "ph-b02", "status": "ONLINE"}],
                "devices": [{"id": "aerator-b02-1", "pond_id": "B-02", "healthy": True}],
                "incidents": [],
            }
        )
        captured = {}

        def kickoff(*args, **kwargs):
            del args
            captured.update(kwargs)
            return SimpleNamespace(raw="结论：B-02 水质正常。")

        orchestrator._kickoff_crew = kickoff
        result = orchestrator.chat("B-02 水质是否正常", [], "B-02")

        self.assertEqual(result.stop_reason, "CREW_CHAT_COMPLETED")
        live_state = captured["context"]["live_state"]
        self.assertEqual(live_state["latest_readings"][0]["sampled_at"], "2026-08-14 15:24:00")
        self.assertEqual(live_state["timezone"], "Asia/Shanghai")
        self.assertEqual(live_state["current_status"]["label"], "正常")
        self.assertEqual(live_state["current_status"]["active_incident_count"], 0)
        self.assertNotIn("UTC", str(live_state))

    def test_crewai_chat_retries_an_empty_provider_response(self) -> None:
        orchestrator = object.__new__(CrewAIOrchestrator)
        orchestrator.available = True
        orchestrator.last_error = None
        orchestrator.llm_config = LLMConfig(chat_retry_count=3)
        orchestrator.system = SimpleNamespace(
            snapshot=lambda: {"ponds": [], "readings": [], "devices": [], "incidents": []}
        )
        responses = [
            RuntimeError("Invalid response from LLM call - None or empty."),
            SimpleNamespace(raw="结论：B-01 水质正常。"),
        ]

        def kickoff(*args, **kwargs):
            del args, kwargs
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        orchestrator._kickoff_crew = kickoff

        self.assertTrue(CrewAIOrchestrator._chat_failure_retryable(RuntimeError("None or empty")))
        self.assertFalse(CrewAIOrchestrator._chat_failure_retryable(RuntimeError("provider not provided")))
        result = orchestrator.chat("检查 B-01", [], "B-01")

        self.assertEqual(result.stop_reason, "CREW_CHAT_COMPLETED")
        self.assertEqual(result.summary, "结论：B-01 水质正常。")
        self.assertEqual(len(responses), 0)
        self.assertTrue(any(action == "chat.retry" for _, action, _ in result.steps))

    def test_crewai_chat_uses_three_configured_retries_for_empty_raw(self) -> None:
        orchestrator = object.__new__(CrewAIOrchestrator)
        orchestrator.available = True
        orchestrator.last_error = None
        orchestrator.llm_config = LLMConfig(chat_retry_count=3)
        orchestrator.system = SimpleNamespace(
            snapshot=lambda: {"ponds": [], "readings": [], "devices": [], "incidents": []}
        )
        responses = [
            SimpleNamespace(raw=""),
            SimpleNamespace(raw=None),
            SimpleNamespace(raw="None"),
            SimpleNamespace(raw="结论：B-01 水质正常。"),
        ]

        orchestrator._kickoff_crew = lambda *args, **kwargs: responses.pop(0)

        result = orchestrator.chat("检查 B-01", [], "B-01")

        self.assertEqual(result.stop_reason, "CREW_CHAT_COMPLETED")
        self.assertEqual(result.summary, "结论：B-01 水质正常。")
        self.assertEqual(len(responses), 0)
        self.assertEqual(sum(action == "chat.retry" for _, action, _ in result.steps), 3)

    def test_chat_infers_pond_from_user_message(self) -> None:
        system = FishAgentSystem()
        system.initialize_demo()
        self.assertEqual(system._infer_chat_pond_id("请检查 B01 水塘水质"), "B-01")
        self.assertEqual(system._infer_chat_pond_id("B-02水质是否正常"), "B-02")
        self.assertIsNone(system._infer_chat_pond_id("全场水质是否正常"))

    def test_chat_passes_inferred_pond_to_orchestrator(self) -> None:
        calls = []

        class FakeOrchestrator:
            available = True

            def chat(self, message, history, pond_id):
                calls.append((message, history, pond_id))
                return CrewRunResult(summary="结论：B-01 水质正常。", stop_reason="CREW_CHAT_COMPLETED")

        system = FishAgentSystem(agent_orchestrator=FakeOrchestrator())
        system.initialize_demo()
        run, reply = system.run_chat("B-01水塘水质是否正常")

        self.assertEqual(run.status, "COMPLETED")
        self.assertEqual(reply, "结论：B-01 水质正常。")
        self.assertEqual(calls[0][2], "B-01")

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

    def test_crewai_decision_keeps_unstructured_output_for_manual_understanding(self) -> None:
        orchestrator = object.__new__(CrewAIOrchestrator)
        orchestrator.available = True
        orchestrator.last_error = None
        orchestrator._kickoff_crew = lambda *args, **kwargs: SimpleNamespace(raw="not json")

        result = orchestrator.decide_incident({"incident": {"id": "inc-test", "pond_id": "B-01"}})

        self.assertIsNotNone(result.decision)
        self.assertEqual(result.decision.action, "MANUAL_REQUIRED")
        self.assertTrue(result.decision.requires_manual_review)
        self.assertEqual(result.stop_reason, "LLM_DECISION_NEEDS_REVIEW")
        self.assertTrue(any(action == "incident.understood" for _, action, _ in result.steps))

    def test_crewai_decision_accepts_structured_task_output(self) -> None:
        orchestrator = object.__new__(CrewAIOrchestrator)
        orchestrator.available = True
        orchestrator.last_error = None
        orchestrator._kickoff_crew = lambda *args, **kwargs: SimpleNamespace(
            raw="模型最终说明",
            json_dict={
                "action": "EXECUTE",
                "device_id": "aerator-b01-1",
                "target_state": "on",
                "risk": "L1",
                "rationale": "低溶氧证据充分，开启增氧机。",
                "verification_delay_seconds": 30,
                "evidence_refs": [],
            },
        )

        result = orchestrator.decide_incident({"incident": {"id": "inc-test", "pond_id": "B-01"}})

        self.assertIsNotNone(result.decision)
        self.assertEqual(result.decision.action, "EXECUTE")
        self.assertEqual(result.stop_reason, "LLM_DECISION_READY")

    def test_crewai_decision_accepts_json_string_in_structured_task_output(self) -> None:
        orchestrator = object.__new__(CrewAIOrchestrator)
        orchestrator.available = True
        orchestrator.last_error = None
        orchestrator._kickoff_crew = lambda *args, **kwargs: SimpleNamespace(
            raw='{"action":"EXECUTE","device_id":"aerator-b01-1","target_state":"on","risk":"L1","rationale":"低溶氧证据充分，开启增氧机。","verification_delay_seconds":30,"evidence_refs":[]}',
            json_dict='{"action":"EXECUTE","device_id":"aerator-b01-1","target_state":"on","risk":"L1","rationale":"低溶氧证据充分，开启增氧机。","verification_delay_seconds":30,"evidence_refs":[]}',
        )

        result = orchestrator.decide_incident({"incident": {"id": "inc-test", "pond_id": "B-01"}})

        self.assertIsNotNone(result.decision)
        self.assertEqual(result.decision.action, "EXECUTE")

    def test_model_action_alias_is_understood_before_execution(self) -> None:
        decision = IncidentDecision.from_payload(
            {
                "action": "TURN_ON",
                "risk": "L1",
                "rationale": "低溶氧，开启增氧机",
            }
        )

        self.assertEqual(decision.action, "EXECUTE")
        self.assertEqual(decision.target_state, "on")
        self.assertFalse(decision.requires_manual_review)
        self.assertIn("缺少设备标识", decision.rationale)

    def test_unknown_model_action_is_kept_for_manual_understanding(self) -> None:
        decision = IncidentDecision.from_payload(
            {
                "action": "TELEPORT",
                "risk": "L1",
                "rationale": "模型给出了未知动作",
            }
        )

        self.assertEqual(decision.action, "MANUAL_REQUIRED")
        self.assertTrue(decision.requires_manual_review)
        self.assertIn("模型未提供可识别的动作", decision.rationale)

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
        self.assertIn('id="sensor_trend_chart"', response.text)
        self.assertIn('id="sensor_trend_tabs"', response.text)
        self.assertIn('onclick="setTrendWindow(12)"', response.text)
        self.assertIn('onclick="setTrendWindow(24)"', response.text)
        self.assertIn('function setTrendMetric', response.text)
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
        self.assertLess(response.text.index('id="alert_panel"'), response.text.index('id="camera_observation_grid"'))
        self.assertGreater(response.text.index('id="pond_grid"'), response.text.index('id="view_analytics"'))
        self.assertLess(response.text.index('id="pond_grid"'), response.text.index('id="sensor_trend_tabs"'))
        self.assertIn('id="demo_launcher"', response.text)
        self.assertIn('function setDemoInjectionBusy', response.text)
        self.assertIn('if (demoInjectionBusy) return;', response.text)
        self.assertIn('id="demo_feedback"', response.text)
        self.assertIn('function showDemoFeedback', response.text)
        self.assertIn('正在处理，请稍候', response.text)
        self.assertIn('正在触发逐塘巡检', response.text)
        self.assertIn('const ALERT_REVEAL_INTERVAL_MS = 2000', response.text)
        self.assertIn('function startAlertReveal', response.text)
        self.assertIn('function streamAlertNode', response.text)
        self.assertIn('data-stream-text=', response.text)
        self.assertIn("scrollIntoView({behavior:'smooth', block:'center'})", response.text)
        self.assertIn("onclick=\"injectDemo('alerts')\"", response.text)
        self.assertIn('双传感器失效', response.text)
        self.assertIn("runMultimodalCase('case-floating-head-weather'", response.text)
        self.assertIn("runMultimodalCase('case-underwater-disease'", response.text)
        self.assertIn("runMultimodalCase('case-weak-feeding-response'", response.text)
        self.assertIn("runMultimodalCase('case-weather-front-protection'", response.text)
        self.assertIn('function keepDemoLauncherOpen()', response.text)
        self.assertIn('}, 1200);', response.text)
        self.assertNotIn("injectDemo('failure')", response.text)
        self.assertNotIn("injectDemo('dedup')", response.text)
        self.assertNotIn("injectDemo('approval')", response.text)
        self.assertNotIn("injectDemo('multimodal')", response.text)
        self.assertNotIn("injectDemo('health')", response.text)
        self.assertNotIn('data-view="cases"', response.text)
        self.assertNotIn('id="view_cases"', response.text)
        self.assertGreater(response.text.index('data-view="knowledge"'), response.text.index('data-view="reports"'))
        self.assertIn("function resetAssistantChatContext()", response.text)
        self.assertIn('onclick="toggleAlertCapsule(event)"', response.text)
        self.assertIn('onclick="openAlertView(event)"', response.text)
        self.assertIn("function advanceCountdownTarget", response.text)
        self.assertIn("巡检 ${fmtDate(markedAt)}", response.text)
        self.assertIn("lastRun?.stop_reason ? stopReasonLabel(lastRun.stop_reason)", response.text)
        self.assertIn("PATROL_COMPLETED: '巡查已完成'", response.text)
        self.assertIn("class=\"patrol-advice\"", response.text)
        self.assertIn("巡查建议", response.text)
        self.assertIn('class="patrol-loading"', response.text)
        self.assertIn('.patrol-loading { grid-column: 1 / -1;', response.text)
        self.assertIn('class="patrol-loading-robot"', response.text)
        self.assertIn('@keyframes patrol-robot-walk', response.text)
        self.assertIn('@keyframes patrol-dot-bounce', response.text)
        self.assertIn("24 * 60 * 60 * 1000", response.text)
        self.assertIn('class="alert-item ${closed ? \'resolved\' : \'active\'}${revealClass}"', response.text)
        self.assertIn("VERIFY_PENDING: '处置中 · 待复核'", response.text)
        self.assertIn('id="assistant_chat"', response.text)
        self.assertIn('id="assistant_chat_input"', response.text)
        self.assertIn('id="assistant_launcher"', response.text)
        self.assertIn('id="assistant_floating_panel"', response.text)
        self.assertIn('id="assistant_floating_chat"', response.text)
        self.assertIn('function toggleFloatingAssistant', response.text)
        self.assertIn('function syncAssistantPond', response.text)
        self.assertIn("/api/v1/agent-chat/stream", response.text)
        self.assertIn("全场设备 · 在线率", response.text)
        self.assertIn('class="alert-flow"', response.text)
        self.assertIn('id="alert_text_tooltip"', response.text)
        self.assertIn('function showAlertTextTooltip', response.text)
        self.assertNotIn('.alert-flow-summary:hover { z-index: 10; display: block;', response.text)
        self.assertIn('function dismissIncident', response.text)
        self.assertIn("waterMetrics.map", response.text)

    def test_demo_options_endpoint_exposes_auto_response_scenarios(self) -> None:
        with TestClient(app) as client:
            response = client.get("/api/v1/demo/options")

        self.assertEqual(response.status_code, 200)
        options = response.json()["options"]
        modes = {item["mode"] for item in options}
        self.assertEqual(
            modes,
            {
                "success",
                "alerts",
                "analysis_case:case-floating-head-weather",
                "analysis_case:case-underwater-disease",
                "analysis_case:case-weak-feeding-response",
                "analysis_case:case-weather-front-protection",
                "init",
            },
        )
        self.assertTrue(next(item for item in options if item["mode"] == "alerts")["auto_response"])
        self.assertFalse(next(item for item in options if item["mode"] == "init")["auto_response"])

    def test_demo_endpoint_dispatches_to_injection_service(self) -> None:
        with patch.object(SYSTEM, "inject_demo", return_value={"demo": "alerts"}) as inject, TestClient(app) as client:
            response = client.post("/api/v1/demo/alerts")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"demo": "alerts"})
        inject.assert_called_once_with("alerts")

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

    def test_agent_chat_endpoint_falls_back_when_snapshot_lags_run(self) -> None:
        run = AgentRun(id="run-chat-fallback", goal="对话：检查 B-02", status="COMPLETED", stop_reason="CREW_CHAT_COMPLETED")
        with (
            patch.object(SYSTEM, "run_chat", return_value=(run, "B-02 水质总体稳定。")),
            patch.object(SYSTEM, "snapshot", return_value={"agent_runs": []}),
            TestClient(app) as client,
        ):
            response = client.post("/api/v1/agent-chat", json={"message": "检查 B-02", "history": []})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["run"]["id"], run.id)

    def test_agent_chat_stream_emits_progressive_events(self) -> None:
        run = AgentRun(id="run-chat-stream", goal="对话：检查 B-02", status="COMPLETED", stop_reason="CREW_CHAT_COMPLETED")
        with (
            patch.object(SYSTEM, "run_chat", return_value=(run, "B-02 水质总体稳定。")),
            patch.object(SYSTEM, "snapshot", return_value={"agent_runs": []}),
            TestClient(app) as client,
        ):
            response = client.post("/api/v1/agent-chat/stream", json={"message": "检查 B-02", "history": []})

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: start", response.text)
        self.assertIn("event: delta", response.text)
        self.assertIn("event: done", response.text)
        self.assertIn("B-02 水质总体稳定。", response.text)

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

    def test_simulator_gateway_can_execute_unhealthy_device_with_warning(self) -> None:
        device = Device(id="d-1", pond_id="p-1", name="增氧机", capability="aeration", healthy=False)
        result = SimulatorDeviceGateway().send_command(device, "on", "p-1:d-1:on")
        self.assertTrue(result.accepted)
        self.assertTrue(result.confirmed)
        self.assertIn("健康状态异常", result.detail)

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

    def test_mqtt_sensor_report_publishes_request_and_response(self) -> None:
        class FakeResult:
            rc = 0

        class FakeClient:
            def __init__(self) -> None:
                self.published = []

            def publish(self, topic, payload, qos, retain):
                self.published.append((topic, json.loads(payload), qos, retain))
                return FakeResult()

        publisher = MqttTelemetryPublisher("mqtt.test", 1883)
        client = FakeClient()
        publisher._ensure_client = lambda: client

        self.assertTrue(
            publisher.request_sensor_report(
                pond_id="B-01",
                sensor_id="do-b-01",
                metric="DO",
                unit="mg/L",
                value=5.2,
                request_id="patrol-request-1",
                source_event_id="patrol-report-1",
            )
        )
        self.assertEqual(client.published[0][0], "farms/farm-demo/ponds/B-01/sensors/do-b-01/commands")
        self.assertEqual(client.published[0][1]["action"], "REPORT_NOW")
        self.assertEqual(client.published[0][1]["request_id"], "patrol-request-1")
        self.assertEqual(client.published[1][0], "farms/farm-demo/ponds/B-01/sensors/do-b-01")
        self.assertEqual(client.published[1][1]["source_event_id"], "patrol-report-1")

    def test_mqtt_mock_sensor_supports_passive_telemetry_upload(self) -> None:
        class FakeResult:
            rc = 0

        class FakeClient:
            def __init__(self) -> None:
                self.published = []

            def publish(self, topic, payload, qos, retain):
                self.published.append((topic, json.loads(payload), qos, retain))
                return FakeResult()

        publisher = MqttTelemetryPublisher("mqtt.test", 1883)
        client = FakeClient()
        publisher._ensure_client = lambda: client

        self.assertTrue(
            publisher.publish_reading(
                pond_id="B-02",
                sensor_id="ph-b-02",
                metric="PH",
                unit="pH",
                value=7.9,
                source_event_id="passive-report-1",
            )
        )
        self.assertEqual(client.published[0][0], "farms/farm-demo/ponds/B-02/sensors/ph-b-02")
        self.assertEqual(client.published[0][1]["source_event_id"], "passive-report-1")

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

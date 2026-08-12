import unittest
from tempfile import TemporaryDirectory

from fishagent.application.agent_service import FishAgentSystem
from fishagent.application.policy import evaluate_action
from fishagent.core import LLMConfig, RuntimeConfigStore
from fishagent.domain.models import RiskLevel, SensorReading, utcnow


class B01FlowTest(unittest.TestCase):
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
        self.assertEqual(state["ponds"][0]["id"], "B-01")
        self.assertEqual(state["devices"][0]["id"], "aerator-b01-1")
        self.assertEqual(state["events"][0]["event_type"], "system.demo.initialized")

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


if __name__ == "__main__":
    unittest.main()

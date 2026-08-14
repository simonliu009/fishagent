"""Opt-in live regressions for the configured CrewAI provider.

Run inside the configured deployment with FISHAGENT_LIVE_LLM_TEST=1. The
system uses an in-memory repository, so the alert test does not change the
running application's durable state.
"""

import os
import unittest

from fishagent.agent_runtime.crewai_runtime import CrewAIOrchestrator
from fishagent.application.agent_service import FishAgentSystem
from fishagent.core import AppConfig, RuntimeConfigStore


@unittest.skipUnless(
    os.environ.get("FISHAGENT_LIVE_LLM_TEST") == "1",
    "set FISHAGENT_LIVE_LLM_TEST=1 to run live provider regressions",
)
class LiveLLMRegressionTests(unittest.TestCase):
    def build_system(self) -> FishAgentSystem:
        config = AppConfig.from_env()
        config.llm, _ = RuntimeConfigStore().load_llm_bundle(config.llm)
        self.assertTrue(config.llm.enabled, "live LLM test requires an enabled provider")
        self.assertTrue(config.llm.has_api_key(), "live LLM test requires an API key")
        system = FishAgentSystem()
        system.initialize_demo()
        system.agent_orchestrator = CrewAIOrchestrator(system, config.llm)
        self.assertTrue(system.agent_orchestrator.available, "CrewAI runtime is unavailable")
        return system

    def test_live_chat_returns_normal_public_text(self) -> None:
        system = self.build_system()

        run, reply = system.run_chat("请只用一句中文回答：连接测试。", [], None)

        self.assertEqual(run.status, "COMPLETED")
        self.assertTrue(reply.strip())
        self.assertTrue(reply.startswith(("结论：", "结论:")))
        self.assertNotIn("<think>", reply.lower())

    def test_live_low_do_alert_executes_confirmed_device_command(self) -> None:
        system = self.build_system()

        incident = system.ingest_do("B-01", 2.0, source_event_id="live-regression-low-do")
        state = system.snapshot()
        run = next(item for item in state["agent_runs"] if item.get("incident_id") == incident.id)

        self.assertEqual(run["stop_reason"], "LLM_ACTION_EXECUTED")
        self.assertEqual(state["incidents"][0]["status"], "VERIFY_PENDING")
        self.assertEqual(state["commands"][0]["status"], "CONFIRMED")
        self.assertEqual(state["commands"][0]["device_id"], "aerator-b01-1")
        self.assertEqual(state["manual_tasks"], [])


if __name__ == "__main__":
    unittest.main()

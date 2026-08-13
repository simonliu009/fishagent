import json
import unittest

from fastapi.testclient import TestClient

from fishagent.domain.models import Device
from fishagent.infrastructure.gateways import SimulatorDeviceGateway
from fishagent.infrastructure.mqtt import MqttTelemetryAdapter
from fishagent.infrastructure.queue.celery_app import celery_app
from fishagent.web.app import app


class RuntimeBoundaryTests(unittest.TestCase):
    def test_console_keeps_core_views_and_adds_operational_views(self) -> None:
        with TestClient(app) as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        for view in ("monitor", "assistant", "management", "analytics"):
            self.assertIn(f'id="view_{view}"', response.text)
        for view in ("assets", "work", "schedules", "audit"):
            self.assertIn(f'id="view_{view}"', response.text)
        self.assertIn('onclick="openLlmDialog()"', response.text)
        self.assertIn('id="llm_layer"', response.text)
        self.assertIn('id="alert_capsule"', response.text)
        self.assertIn('id="alert_capsule_list"', response.text)

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
            payload = json.dumps({"metric": "DO", "value": 2.1, "source_event_id": "mqtt-1"}).encode()

        adapter._on_message(None, None, Message())
        self.assertEqual(received[0]["pond_id"], "B-01")
        self.assertEqual(received[0]["sensor_id"], "s-1")
        self.assertEqual(received[0]["value"], 2.1)

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

import json
import unittest

from fastapi.testclient import TestClient

from fishagent.domain.models import Device
from fishagent.infrastructure.gateways import SimulatorDeviceGateway
from fishagent.infrastructure.mqtt import MqttTelemetryAdapter
from fishagent.infrastructure.queue.celery_app import celery_app
from fishagent.web.app import app


class RuntimeBoundaryTests(unittest.TestCase):
    def test_fastapi_openapi_and_websocket_replay(self) -> None:
        with TestClient(app) as client:
            self.assertEqual(client.get("/health/live").status_code, 200)
            self.assertEqual(client.get("/openapi.json").status_code, 200)
            client.post("/api/v1/demo/init")
            with client.websocket_connect("/events?after=0") as websocket:
                event = websocket.receive_json()
                self.assertIn(event["event_type"], {"system.started", "system.demo.initialized"})

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


if __name__ == "__main__":
    unittest.main()

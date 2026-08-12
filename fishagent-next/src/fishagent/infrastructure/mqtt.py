"""MQTT telemetry adapter.

Topic: farms/{farm_id}/ponds/{pond_id}/sensors/{sensor_id}
Payload: {"metric":"DO", "value":2.1, "source_event_id":"..."}
"""

import json
import re
from typing import Callable, Optional


TOPIC_PATTERN = re.compile(r"^farms/(?P<farm_id>[^/]+)/ponds/(?P<pond_id>[^/]+)/sensors/(?P<sensor_id>[^/]+)$")


class MqttTelemetryAdapter:
    def __init__(self, host: str, port: int, topic: str, ingest: Callable[..., object]) -> None:
        self.host = host
        self.port = port
        self.topic = topic
        self.ingest = ingest
        self.client = None
        self.last_error: Optional[str] = None

    def start(self) -> None:
        try:
            import paho.mqtt.client as mqtt

            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="fishagent-telemetry")
            self.client.on_connect = self._on_connect
            self.client.on_message = self._on_message
            self.client.connect(self.host, self.port, keepalive=30)
            self.client.loop_start()
        except Exception as exc:  # network failures are surfaced in health/events, not startup crashes
            self.last_error = str(exc)

    def stop(self) -> None:
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code == 0 or not getattr(reason_code, "is_failure", False):
            client.subscribe(self.topic, qos=1)
            self.last_error = None
        else:
            self.last_error = "MQTT connection refused: %s" % reason_code

    def _on_message(self, client, userdata, message) -> None:
        match = TOPIC_PATTERN.match(message.topic)
        if not match:
            self.last_error = "invalid telemetry topic"
            return
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            metric = str(payload.get("metric") or "")
            if metric != "DO":
                return
            self.ingest(
                pond_id=match.group("pond_id"),
                value=float(payload["value"]),
                source_event_id=payload.get("source_event_id"),
                sensor_id=match.group("sensor_id"),
                quality=str(payload.get("quality") or "GOOD"),
                seconds_old=int(payload.get("seconds_old", 0)),
            )
            self.last_error = None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.last_error = "invalid MQTT telemetry payload: %s" % exc

"""MQTT telemetry adapter.

Topic: farms/{farm_id}/ponds/{pond_id}/sensors/{sensor_id}
Payload: {"metric":"DO", "unit":"mg/L", "value":2.1, "source_event_id":"..."}
"""

import json
import re
import threading
import uuid
from queue import Queue
from typing import Any, Callable, Optional

TOPIC_PATTERN = re.compile(r"^farms/(?P<farm_id>[^/]+)/ponds/(?P<pond_id>[^/]+)/sensors/(?P<sensor_id>[^/]+)$")


class MqttTelemetryAdapter:
    def __init__(self, host: str, port: int, topic: str, ingest: Callable[..., object]) -> None:
        self.host = host
        self.port = port
        self.topic = topic
        self.ingest = ingest
        self.client: Any = None
        self._ingest_queue: Queue[Optional[dict[str, Any]]] = Queue()
        self._ingest_worker: Optional[threading.Thread] = None
        self.last_error: Optional[str] = None

    def start(self) -> None:
        try:
            import paho.mqtt.client as mqtt

            self._ingest_worker = threading.Thread(
                target=self._run_ingest_worker,
                name="fishagent-mqtt-ingest",
                daemon=True,
            )
            self._ingest_worker.start()
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
        if self._ingest_worker:
            self._ingest_queue.put(None)
            self._ingest_worker.join(timeout=2)
            self._ingest_worker = None

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code == 0 or not getattr(reason_code, "is_failure", False):
            client.subscribe(self.topic, qos=1)
            self.last_error = None
        else:
            self.last_error = "MQTT connection refused: %s" % reason_code

    def _on_message(self, client, userdata, message) -> None:
        del client, userdata
        match = TOPIC_PATTERN.match(message.topic)
        if not match:
            self.last_error = "invalid telemetry topic"
            return
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            metric = str(payload.get("metric") or "").upper()
            if not metric:
                raise ValueError("metric is required")
            ingest_payload = {
                "pond_id": match.group("pond_id"),
                "value": float(payload["value"]),
                "metric": metric,
                "unit": str(payload["unit"]) if payload.get("unit") else None,
                "source_event_id": payload.get("source_event_id"),
                "sensor_id": match.group("sensor_id"),
                "quality": str(payload.get("quality") or "GOOD"),
                "seconds_old": int(payload.get("seconds_old", 0)),
                "auto_run": bool(payload.get("auto_run", True)),
                "defer_persist": bool(payload.get("defer_persist", False)),
            }
            if self._ingest_worker is None:
                self.ingest(**ingest_payload)
            else:
                self._ingest_queue.put(ingest_payload)
            self.last_error = None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.last_error = "invalid MQTT telemetry payload: %s" % exc

    def _run_ingest_worker(self) -> None:
        while True:
            payload = self._ingest_queue.get()
            try:
                if payload is None:
                    return
                self.ingest(**payload)
                self.last_error = None
            except Exception as exc:
                self.last_error = "MQTT telemetry ingest failed: %s" % exc
            finally:
                self._ingest_queue.task_done()


class MqttTelemetryPublisher:
    """Publish mock telemetry and sensor report responses through MQTT."""

    def __init__(self, host: str, port: int, farm_id: str = "farm-demo") -> None:
        self.host = host
        self.port = port
        self.farm_id = farm_id
        self.client: Any = None
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None

    def _ensure_client(self) -> Any:
        if self.client is not None:
            return self.client
        with self._lock:
            if self.client is not None:
                return self.client
            import paho.mqtt.client as mqtt

            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="fishagent-mock-%s" % uuid.uuid4().hex[:10])
            self.client.connect(self.host, self.port, keepalive=30)
            self.client.loop_start()
            return self.client

    def _publish(self, topic: str, payload: str) -> None:
        result = self._ensure_client().publish(topic, payload, qos=1, retain=False)
        if int(getattr(result, "rc", 0)) != 0:
            raise RuntimeError("MQTT publish failed with rc=%s" % getattr(result, "rc", "unknown"))
        wait_for_publish = getattr(result, "wait_for_publish", None)
        if callable(wait_for_publish):
            wait_for_publish(timeout=5)
        is_published = getattr(result, "is_published", None)
        if callable(is_published) and not is_published():
            raise RuntimeError("MQTT publish acknowledgement timed out")

    def publish_reading(
        self,
        pond_id: str,
        sensor_id: str,
        metric: str,
        unit: str,
        value: float,
        source_event_id: str,
        quality: str = "GOOD",
        seconds_old: int = 0,
        auto_run: bool = True,
        defer_persist: bool = False,
    ) -> bool:
        topic = "farms/%s/ponds/%s/sensors/%s" % (self.farm_id, pond_id, sensor_id)
        payload = json.dumps(
            {
                "metric": metric,
                "unit": unit,
                "value": value,
                "source_event_id": source_event_id,
                "quality": quality,
                "seconds_old": seconds_old,
                "auto_run": auto_run,
                "defer_persist": defer_persist,
                "source": "fishagent.mock-telemetry",
            },
            ensure_ascii=False,
        )
        try:
            self._publish(topic, payload)
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def request_sensor_report(
        self,
        pond_id: str,
        sensor_id: str,
        metric: str,
        unit: str,
        value: float,
        request_id: str,
        source_event_id: str,
        quality: str = "GOOD",
        auto_run: bool = False,
        defer_persist: bool = False,
    ) -> bool:
        """Request one sensor report, then publish the mock sensor response.

        The request and response both cross the broker. The response path uses
        the normal telemetry topic so the application cannot bypass MQTT.
        """
        command_topic = "farms/%s/ponds/%s/sensors/%s/commands" % (self.farm_id, pond_id, sensor_id)
        command = json.dumps(
            {
                "action": "REPORT_NOW",
                "request_id": request_id,
                "sensor_id": sensor_id,
                "requested_at": uuid.uuid1().time,
                "source": "fishagent.patrol-agent",
            },
            ensure_ascii=False,
        )
        try:
            self._publish(command_topic, command)
        except Exception as exc:
            self.last_error = str(exc)
            return False
        return self.publish_reading(
            pond_id=pond_id,
            sensor_id=sensor_id,
            metric=metric,
            unit=unit,
            value=value,
            source_event_id=source_event_id,
            quality=quality,
            seconds_old=0,
            auto_run=auto_run,
            defer_persist=defer_persist,
        )

    def close(self) -> None:
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()
            self.client = None

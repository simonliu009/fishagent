"""Device gateway ports and safe simulator/HTTP adapters."""

import json
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol
from urllib.request import Request, urlopen

from fishagent.domain.models import Device


@dataclass
class GatewayResult:
    accepted: bool
    acknowledged: bool
    confirmed: bool
    detail: str = ""


class DeviceGateway(Protocol):
    def get_capabilities(self, device: Device) -> dict: ...

    def get_shadow_state(self, device: Device) -> str: ...

    def send_command(self, device: Device, target_state: str, idempotency_key: str) -> GatewayResult: ...


class SimulatorDeviceGateway:
    """Deterministic gateway used by demos, CI and the safety evaluation."""

    def get_capabilities(self, device: Device) -> dict:
        return {"device_id": device.id, "capability": device.capability, "states": ["on", "off"]}

    def get_shadow_state(self, device: Device) -> str:
        return device.shadow_state

    def send_command(self, device: Device, target_state: str, idempotency_key: str) -> GatewayResult:
        if not device.healthy:
            return GatewayResult(False, False, False, "设备网关报告设备不健康")
        return GatewayResult(True, True, True, "模拟设备已确认目标状态")


class MqttDeviceGateway:
    """Publish device commands as MQTT IoT messages with a local ACK simulator."""

    def __init__(
        self,
        host: str,
        port: int,
        topic_template: str = "fishagent/ponds/{pond_id}/devices/{device_id}/commands",
        simulate_ack: bool = True,
        client_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.topic_template = topic_template
        self.simulate_ack = simulate_ack
        self.client_factory = client_factory
        self.client: Any = None
        self._lock = threading.Lock()
        self.last_error = ""

    def get_capabilities(self, device: Device) -> dict:
        return {"device_id": device.id, "capability": device.capability, "states": ["on", "off"]}

    def get_shadow_state(self, device: Device) -> str:
        return device.shadow_state

    def _ensure_client(self) -> Any:
        if self.client is not None:
            return self.client
        with self._lock:
            if self.client is not None:
                return self.client
            factory = self.client_factory
            if factory is None:
                import paho.mqtt.client as mqtt

                factory = mqtt.Client
            self.client = factory(client_id="fishagent-device-%s" % uuid.uuid4().hex[:10])
            self.client.connect(self.host, self.port, keepalive=30)
            self.client.loop_start()
            return self.client

    def send_command(self, device: Device, target_state: str, idempotency_key: str) -> GatewayResult:
        if not device.healthy:
            return GatewayResult(False, False, False, "设备网关报告设备不健康")
        topic = self.topic_template.format(pond_id=device.pond_id, device_id=device.id)
        payload = json.dumps(
            {
                "command": "set_state",
                "device_id": device.id,
                "pond_id": device.pond_id,
                "target_state": target_state,
                "idempotency_key": idempotency_key,
                "source": "fishagent.execution-agent",
            },
            ensure_ascii=False,
        )
        try:
            client = self._ensure_client()
            result = client.publish(topic, payload, qos=1, retain=False)
            rc = int(getattr(result, "rc", 0))
            if rc != 0:
                raise RuntimeError("MQTT publish failed with rc=%s" % rc)
            self.last_error = ""
            detail = "MQTT IoT command published to %s" % topic
            return GatewayResult(True, True, self.simulate_ack, detail if self.simulate_ack else detail + "; waiting for device ACK")
        except Exception as exc:
            self.last_error = str(exc)
            return GatewayResult(False, False, False, "MQTT IoT command publish failed: %s" % exc)

    def close(self) -> None:
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()
            self.client = None


def mqtt_gateway_from_config(enabled: bool, host: str, port: int, topic_template: str) -> Optional[MqttDeviceGateway]:
    if not enabled:
        return None
    return MqttDeviceGateway(host, port, topic_template)


class HttpDeviceGateway:
    """OpenAPI-style gateway for a real device service."""

    def __init__(self, base_url: str, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _get(self, path: str) -> dict:
        with urlopen(Request(self.base_url + path, headers={"Accept": "application/json"}), timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_capabilities(self, device: Device) -> dict:
        return self._get("/devices/%s/capabilities" % device.id)

    def get_shadow_state(self, device: Device) -> str:
        return str(self._get("/devices/%s/shadow" % device.id).get("state", "unknown"))

    def send_command(self, device: Device, target_state: str, idempotency_key: str) -> GatewayResult:
        payload = json.dumps({"device_id": device.id, "target_state": target_state, "idempotency_key": idempotency_key}).encode()
        request = Request(
            self.base_url + "/devices/%s/commands" % device.id,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json", "Idempotency-Key": idempotency_key},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            result = json.loads(response.read().decode("utf-8"))
        return GatewayResult(
            accepted=bool(result.get("accepted", False)),
            acknowledged=bool(result.get("acknowledged", False)),
            confirmed=bool(result.get("confirmed", False)),
            detail=str(result.get("detail", "")),
        )

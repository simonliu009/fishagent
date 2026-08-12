"""Device gateway ports and safe simulator/HTTP adapters."""

import json
from dataclasses import dataclass
from typing import Protocol
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

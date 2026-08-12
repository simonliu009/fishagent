"""Vision worker boundary with explicit stale/unavailable handling.

The adapter deliberately does not invent a detection when a camera has no
fresh frame. A deployment can provide an OpenCV/RTSP implementation behind
the same protocol without changing the queue contract.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol


@dataclass(frozen=True)
class VisionObservation:
    camera_id: str
    status: str
    summary: str
    frame_age_seconds: int | None = None
    labels: tuple[str, ...] = ()


class CameraVisionAdapter(Protocol):
    def analyze(self, camera: Any) -> VisionObservation:
        ...


class FreshFrameVisionAdapter:
    """Safe default adapter for cameras whose frame pipeline is external."""

    def __init__(self, max_age_seconds: int = 30) -> None:
        self.max_age_seconds = max_age_seconds

    def analyze(self, camera: Any) -> VisionObservation:
        last_frame_at = getattr(camera, "last_frame_at", None)
        if str(getattr(camera, "status", "UNAVAILABLE")) != "ONLINE" or last_frame_at is None:
            return VisionObservation(
                camera_id=camera.id,
                status="UNAVAILABLE",
                summary="摄像头没有可用帧，未生成视觉结论",
            )
        age = max(0, int((datetime.now(timezone.utc) - last_frame_at).total_seconds()))
        if age > self.max_age_seconds:
            return VisionObservation(
                camera_id=camera.id,
                status="STALE",
                summary="最近一帧已过期，未生成视觉结论",
                frame_age_seconds=age,
            )
        return VisionObservation(
            camera_id=camera.id,
            status="READY",
            summary="已接收到新鲜视频帧，等待视觉模型分析",
            frame_age_seconds=age,
        )

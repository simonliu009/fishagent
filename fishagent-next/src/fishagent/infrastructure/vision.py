"""Camera capture and vision-worker boundaries.

Capture is deliberately separate from model inference. A bad, stale or
unreachable source produces an explicit unavailable result and never a made-up
finding.
"""

import hashlib
import io
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.request import Request, urlopen


class FrameCaptureError(RuntimeError):
    """Raised when a camera frame cannot be fetched or validated."""


@dataclass(frozen=True)
class FrameRecord:
    data: bytes
    source_url: str
    content_type: str
    sha256: str
    width: int
    height: int
    captured_at: datetime


@dataclass(frozen=True)
class VisionObservation:
    camera_id: str
    status: str
    summary: str
    frame_age_seconds: int | None = None
    frame_id: str | None = None
    labels: tuple[str, ...] = ()


class CameraCaptureAdapter(Protocol):
    def capture(self, camera: Any) -> FrameRecord:
        ...


def validate_frame(data: bytes, source_url: str, max_bytes: int = 10 * 1024 * 1024) -> FrameRecord:
    if not data:
        raise FrameCaptureError("empty camera response")
    if len(data) > max_bytes:
        raise FrameCaptureError("camera frame exceeds the configured size limit")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            image_format = (image.format or "").upper()
    except Exception as exc:
        raise FrameCaptureError("camera response is not a valid image") from exc
    content_type = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
        "GIF": "image/gif",
    }.get(image_format)
    if not content_type or width <= 0 or height <= 0:
        raise FrameCaptureError("unsupported or invalid image metadata")
    return FrameRecord(
        data=data,
        source_url=source_url,
        content_type=content_type,
        sha256=hashlib.sha256(data).hexdigest(),
        width=width,
        height=height,
        captured_at=datetime.now(timezone.utc),
    )


class HttpSnapshotCameraGateway:
    def __init__(self, timeout_seconds: float = 5.0, max_bytes: int = 10 * 1024 * 1024) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes

    def capture(self, camera: Any) -> FrameRecord:
        source_url = str(getattr(camera, "source_url", ""))
        if not source_url.startswith(("http://", "https://")):
            raise FrameCaptureError("HTTP snapshot camera requires an http(s) source_url")
        request = Request(source_url, headers={"User-Agent": "fishagent-vision/1.0"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > self.max_bytes:
                    raise FrameCaptureError("camera frame exceeds the configured size limit")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(min(1024 * 1024, self.max_bytes - total + 1))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise FrameCaptureError("camera frame exceeds the configured size limit")
        except FrameCaptureError:
            raise
        except Exception as exc:
            raise FrameCaptureError("HTTP snapshot request failed: %s" % exc) from exc
        return validate_frame(b"".join(chunks), source_url, self.max_bytes)


class FfmpegRtspCameraGateway:
    def __init__(self, timeout_seconds: float = 10.0, max_bytes: int = 10 * 1024 * 1024, ffmpeg_binary: str = "ffmpeg") -> None:
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.ffmpeg_binary = ffmpeg_binary

    def capture(self, camera: Any) -> FrameRecord:
        source_url = str(getattr(camera, "source_url", ""))
        if not source_url.startswith("rtsp://"):
            raise FrameCaptureError("RTSP camera requires an rtsp:// source_url")
        command = [
            self.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-i",
            source_url,
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]
        try:
            result = subprocess.run(command, capture_output=True, timeout=self.timeout_seconds, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FrameCaptureError("RTSP frame extraction failed: %s" % exc) from exc
        if result.returncode != 0 or not result.stdout:
            detail = result.stderr.decode("utf-8", errors="replace")[-300:]
            raise FrameCaptureError("RTSP frame extraction failed: %s" % detail)
        return validate_frame(result.stdout, source_url, self.max_bytes)


class FreshFrameVisionAdapter:
    """Safe analysis boundary with explicit unavailable and stale states."""

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
        frame_id = getattr(camera, "last_frame_id", None)
        if age > self.max_age_seconds:
            return VisionObservation(
                camera_id=camera.id,
                status="STALE",
                summary="最近一帧已过期，未生成视觉结论",
                frame_age_seconds=age,
                frame_id=frame_id,
            )
        return VisionObservation(
            camera_id=camera.id,
            status="READY",
            summary="已接收到新鲜视频帧，等待视觉模型分析",
            frame_age_seconds=age,
            frame_id=frame_id,
        )

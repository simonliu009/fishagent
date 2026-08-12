"""Celery Beat/Worker boundary.

Beat only claims due jobs and enqueues IDs. Workers load the durable snapshot
again before executing, so task IDs are transport details rather than business
idempotency keys.
"""

from typing import Any

from celery import Celery

from fishagent.agent_runtime.crewai_runtime import CrewAIOrchestrator
from fishagent.application.agent_service import FishAgentSystem
from fishagent.core import AppConfig, RuntimeConfigStore
from fishagent.domain.models import VisionFrame, new_id
from fishagent.infrastructure.object_store import object_store_from_config
from fishagent.infrastructure.persistence import repository_from_config
from fishagent.infrastructure.realtime import publisher_from_config
from fishagent.infrastructure.vision import (
    FfmpegRtspCameraGateway,
    FrameCaptureError,
    FreshFrameVisionAdapter,
    HttpSnapshotCameraGateway,
)

CONFIG = AppConfig.from_env()
CONFIG.llm = RuntimeConfigStore().load_llm(CONFIG.llm)
BROKER_URL = CONFIG.celery_broker_url or CONFIG.redis_url or "redis://127.0.0.1:6379/1"
RESULT_BACKEND = CONFIG.celery_result_backend or CONFIG.redis_url or "redis://127.0.0.1:6379/2"

celery_app = Celery("fishagent", broker=BROKER_URL, backend=RESULT_BACKEND)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_default_queue="default",
    task_default_exchange="default",
    task_default_routing_key="default",
    task_routes={
        "fishagent.analyze_camera": {"queue": "vision", "routing_key": "vision"},
        "fishagent.capture_camera_frame": {"queue": "vision", "routing_key": "vision"},
    },
    beat_schedule={
        "dispatch-due-jobs-every-five-seconds": {
            "task": "fishagent.dispatch_due_jobs",
            "schedule": 5.0,
        }
    },
)


def system() -> FishAgentSystem:
    llm_config = RuntimeConfigStore().load_llm(CONFIG.llm)
    app = FishAgentSystem(
        repository=repository_from_config(CONFIG.database_url),
        event_publisher=publisher_from_config(CONFIG.redis_url),
    )
    app.agent_orchestrator = CrewAIOrchestrator(app, llm_config)
    return app


@celery_app.task(name="fishagent.dispatch_due_jobs", bind=True, acks_late=True)
def dispatch_due_jobs(self, limit: int = 50) -> dict:
    app = system()
    jobs = app.claim_due_jobs(limit)
    queued = []
    for job in jobs:
        execute_scheduled_job.apply_async(args=[job.id], queue="default")
        queued.append(job.id)
    return {"queued_job_ids": queued}


@celery_app.task(
    name="fishagent.execute_scheduled_job",
    bind=True,
    acks_late=True,
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=2,
)
def execute_scheduled_job(self, job_id: str) -> dict:
    job = system().execute_scheduled_job(job_id)
    return {"job_id": job.id, "status": job.status.value, "attempts": job.attempts}


@celery_app.task(name="fishagent.run_agent_goal", bind=True, acks_late=True)
def run_agent_goal(self, goal: str, pond_id: str | None = None) -> dict:
    app = system()
    run = app.run_goal(goal, pond_id)
    snapshot = app.snapshot()
    return next(item for item in snapshot["agent_runs"] if item["id"] == run.id)


@celery_app.task(name="fishagent.analyze_camera", bind=True, acks_late=True)
def analyze_camera(self, camera_id: str) -> dict:
    app = system()
    camera = app.store.cameras.get(camera_id)
    if camera is None:
        return {"camera_id": camera_id, "status": "NOT_FOUND", "summary": "摄像头不存在"}
    observation = FreshFrameVisionAdapter().analyze(camera)
    app.store.emit(
        "vision.analysis.completed",
        observation.summary,
        {
            "camera_id": observation.camera_id,
            "status": observation.status,
            "frame_age_seconds": observation.frame_age_seconds,
            "frame_id": observation.frame_id,
        },
        correlation_id=observation.camera_id,
    )
    app.snapshot()
    return {
        "camera_id": observation.camera_id,
        "status": observation.status,
        "summary": observation.summary,
        "frame_age_seconds": observation.frame_age_seconds,
        "frame_id": observation.frame_id,
        "labels": list(observation.labels),
    }


@celery_app.task(name="fishagent.capture_camera_frame", bind=True, acks_late=True)
def capture_camera_frame(self, camera_id: str) -> dict:
    app = system()
    camera = app.store.cameras.get(camera_id)
    if camera is None:
        return {"camera_id": camera_id, "status": "NOT_FOUND", "summary": "摄像头不存在"}
    gateway: Any = {
        "HTTP_SNAPSHOT": HttpSnapshotCameraGateway(),
        "RTSP": FfmpegRtspCameraGateway(),
    }.get(camera.source_type.upper())
    if gateway is None:
        camera.status = "UNAVAILABLE"
        app.store.emit("vision.capture.unavailable", "不支持的摄像头来源类型", {"camera_id": camera_id})
        app.snapshot()
        return {"camera_id": camera_id, "status": "UNAVAILABLE", "summary": "不支持的摄像头来源类型"}
    try:
        frame = gateway.capture(camera)
        object_store = object_store_from_config(
            CONFIG.minio_endpoint,
            CONFIG.minio_access_key,
            CONFIG.minio_secret_key,
            CONFIG.minio_bucket,
        )
        if object_store is None:
            raise FrameCaptureError("MinIO object storage is not configured")
        object_info = object_store.put_bytes(frame.data, frame.content_type, prefix="frames/%s" % camera_id)
        vision_frame = VisionFrame(
            id=new_id("frame"),
            camera_id=camera_id,
            source_url=frame.source_url,
            object_name=object_info["object_name"],
            content_type=frame.content_type,
            sha256=frame.sha256,
            width=frame.width,
            height=frame.height,
            captured_at=frame.captured_at,
        )
        app.store.vision_frames[vision_frame.id] = vision_frame
        camera.status = "ONLINE"
        camera.last_frame_at = frame.captured_at
        camera.last_frame_id = vision_frame.id
        camera.last_frame_hash = frame.sha256
        camera.last_frame_width = frame.width
        camera.last_frame_height = frame.height
        app.store.emit(
            "vision.frame.captured",
            "摄像头帧已采集并保存",
            {
                "camera_id": camera_id,
                "frame_id": vision_frame.id,
                "object_name": vision_frame.object_name,
                "sha256": vision_frame.sha256,
                "width": vision_frame.width,
                "height": vision_frame.height,
            },
            correlation_id=camera_id,
        )
        app.snapshot()
        return {
            "camera_id": camera_id,
            "frame_id": vision_frame.id,
            "object_name": vision_frame.object_name,
            "sha256": vision_frame.sha256,
            "width": vision_frame.width,
            "height": vision_frame.height,
            "status": "ONLINE",
        }
    except FrameCaptureError as exc:
        camera.status = "UNAVAILABLE"
        app.store.emit("vision.capture.unavailable", str(exc), {"camera_id": camera_id}, correlation_id=camera_id)
        app.snapshot()
        return {"camera_id": camera_id, "status": "UNAVAILABLE", "summary": str(exc)}

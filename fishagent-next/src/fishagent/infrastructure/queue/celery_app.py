"""Celery Beat/Worker boundary.

Beat only claims due jobs and enqueues IDs. Workers load the durable snapshot
again before executing, so task IDs are transport details rather than business
idempotency keys.
"""

from celery import Celery

from fishagent.application.agent_service import FishAgentSystem
from fishagent.agent_runtime.crewai_runtime import CrewAIOrchestrator
from fishagent.core import AppConfig, RuntimeConfigStore
from fishagent.infrastructure.persistence import repository_from_config
from fishagent.infrastructure.realtime import publisher_from_config
from fishagent.infrastructure.vision import FreshFrameVisionAdapter


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
    task_routes={"fishagent.analyze_camera": {"queue": "vision", "routing_key": "vision"}},
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
        },
        correlation_id=observation.camera_id,
    )
    app.snapshot()
    return {
        "camera_id": observation.camera_id,
        "status": observation.status,
        "summary": observation.summary,
        "frame_age_seconds": observation.frame_age_seconds,
        "labels": list(observation.labels),
    }

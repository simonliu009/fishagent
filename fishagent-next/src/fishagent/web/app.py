"""FastAPI/NiceGUI web process for the fishery operations console.

The domain service remains shared with the legacy compatibility server, while
this module owns typed HTTP, WebSocket and NiceGUI process boundaries.
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from fishagent.agent_runtime.crewai_runtime import CrewAIOrchestrator
from fishagent.application.agent_service import FishAgentSystem
from fishagent.core import AppConfig, RuntimeConfigStore
from fishagent.domain.models import RiskLevel, ScheduleStatus, VisionFrame, new_id, utcnow
from fishagent.infrastructure.auth import auth_from_config
from fishagent.infrastructure.mqtt import MqttTelemetryAdapter
from fishagent.infrastructure.object_store import object_store_from_config
from fishagent.infrastructure.persistence import PersistenceError, repository_from_config
from fishagent.infrastructure.realtime import publisher_from_config
from fishagent.web.server import page, test_llm_connection

CONFIG = AppConfig.from_env()
REPOSITORY = repository_from_config(CONFIG.database_url)
EVENT_PUBLISHER = publisher_from_config(CONFIG.redis_url)
OBJECT_STORE = object_store_from_config(
    CONFIG.minio_endpoint,
    CONFIG.minio_access_key,
    CONFIG.minio_secret_key,
    CONFIG.minio_bucket,
)
SYSTEM = FishAgentSystem(repository=REPOSITORY, event_publisher=EVENT_PUBLISHER)
AUTH = auth_from_config(CONFIG.auth_enabled, CONFIG.auth_username, CONFIG.auth_password)
CONFIG_STORE = RuntimeConfigStore()
CONFIG.llm = CONFIG_STORE.load_llm(CONFIG.llm)
SYSTEM.agent_orchestrator = CrewAIOrchestrator(SYSTEM, CONFIG.llm)
STATIC_DIR = Path(__file__).parent / "static"
MQTT_ADAPTER: MqttTelemetryAdapter | None = None


def ingest_mqtt_and_persist(**payload: Any) -> Any:
    result = SYSTEM.ingest_do(**payload)
    SYSTEM.snapshot()
    return result


@asynccontextmanager
async def lifespan(_application: FastAPI):
    global MQTT_ADAPTER
    if CONFIG.mqtt_enabled:
        MQTT_ADAPTER = MqttTelemetryAdapter(
            CONFIG.mqtt_host,
            CONFIG.mqtt_port,
            CONFIG.mqtt_topic,
            ingest_mqtt_and_persist,
        )
        MQTT_ADAPTER.start()
    try:
        yield
    finally:
        if MQTT_ADAPTER:
            MQTT_ADAPTER.stop()

app = FastAPI(
    title="智渔 Agent API",
    version="0.2.0",
    description="养殖感知、研判、执行、复核与升级 API",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan,
)


class AssetPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    name: str = ""
    location: str = ""
    farm_id: str = ""
    zone_id: str = ""
    pond_id: str = ""
    species: str = ""
    metric: str = "DO"
    unit: str = "mg/L"
    source_type: str = "HTTP_SNAPSHOT"
    source_url: str = ""
    privacy_policy: str = "EVENT_ONLY"


class TelemetryReadingPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    pond_id: str
    metric: str = "DO"
    value: float
    source_event_id: Optional[str] = None
    sensor_id: Optional[str] = None
    seconds_old: int = Field(default=0, ge=0)
    quality: str = "GOOD"
    auto_run: bool = True


class TelemetryBatchPayload(BaseModel):
    readings: list[TelemetryReadingPayload] = Field(default_factory=list, max_length=1000)


class LoginPayload(BaseModel):
    username: str
    password: str


class JsonPayload(BaseModel):
    model_config = ConfigDict(extra="allow")


def problem(status: int, title: str, detail: str = "") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


def encoded_response(status_code: int, payload: dict) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=jsonable_encoder(payload))


def state_item(collection: str, item_id: str) -> dict | None:
    return next((item for item in SYSTEM.read_snapshot().get(collection, []) if item.get("id") == item_id), None)


def auth_roles_for_path(path: str) -> set[str]:
    if "/approve" in path or "/reject" in path or path.startswith("/api/v1/config"):
        return {"admin", "manager"}
    if any(path.startswith("/api/v1/" + name) for name in ("farms", "zones", "ponds", "sensors", "devices", "cameras")):
        return {"admin", "manager"}
    return {"admin", "manager", "operator", "viewer"}


def authenticate(request: Request, path: str, write: bool = False):
    if not AUTH.enabled:
        return AUTH.authenticate("")
    session = AUTH.authenticate(request.headers.get("cookie", ""))
    if session is None:
        raise HTTPException(status_code=401, detail="请先登录")
    if session.role not in auth_roles_for_path(path):
        raise HTTPException(status_code=403, detail="当前角色没有此操作权限")
    if write and session.role == "viewer":
        raise HTTPException(status_code=403, detail="viewer 角色只允许读取")
    if write and request.headers.get("x-csrf-token") != session.csrf_token:
        raise HTTPException(status_code=403, detail="缺少有效 CSRF Token")
    return session


def authenticate_websocket(websocket: WebSocket) -> bool:
    if not AUTH.enabled:
        return True
    session = AUTH.authenticate(websocket.headers.get("cookie", ""))
    return session is not None and session.role in auth_roles_for_path("/api/v1/events")


@app.exception_handler(PersistenceError)
async def persistence_error_handler(_: Request, exc: PersistenceError) -> JSONResponse:
    return problem(503, "Persistence unavailable", str(exc))


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def console() -> str:
    return page()


@app.get("/static/fish.svg", include_in_schema=False)
async def fish_icon() -> Response:
    return Response((STATIC_DIR / "fish.svg").read_text(encoding="utf-8"), media_type="image/svg+xml")


@app.get("/health/live")
async def health_live() -> dict:
    return {"status": "ok", "port": CONFIG.port, "public_port": CONFIG.public_port}


@app.get("/health/ready")
@app.get("/healthz", include_in_schema=False)
async def health_ready() -> Any:
    if AUTH.enabled and not CONFIG.auth_password:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "port": CONFIG.port, "detail": "管理员密码未配置"},
        )
    try:
        persistence = REPOSITORY.health() if REPOSITORY else {"status": "ok", "backend": "memory"}
    except PersistenceError as exc:
        return JSONResponse(status_code=503, content={"status": "not_ready", "detail": str(exc)})
    realtime = EVENT_PUBLISHER.health() if EVENT_PUBLISHER else {"status": "disabled", "backend": "redis"}
    media = OBJECT_STORE.health() if OBJECT_STORE else {"status": "disabled", "backend": "minio"}
    return {"status": "ok", "port": CONFIG.port, "public_port": CONFIG.public_port, "persistence": persistence, "realtime": realtime, "media": media}


@app.get("/api/v1/auth/config")
@app.get("/auth/config", include_in_schema=False)
async def auth_config() -> dict:
    return {"enabled": AUTH.enabled}


@app.post("/api/v1/auth/login")
@app.post("/auth/login", include_in_schema=False)
async def login(payload: LoginPayload) -> JSONResponse:
    session = AUTH.login(payload.username, payload.password)
    if session is None:
        return problem(401, "Invalid credentials", "用户名或密码错误")
    response = JSONResponse(
        {"user": {"username": session.username, "role": session.role}, "csrf_token": session.csrf_token}
    )
    response.set_cookie(
        "fishagent_session",
        session.token,
        httponly=True,
        samesite="strict",
        secure=AUTH.cookie_secure,
        path="/",
    )
    return response


@app.post("/api/v1/auth/logout")
@app.post("/auth/logout", include_in_schema=False)
async def logout(request: Request) -> JSONResponse:
    AUTH.logout(request.headers.get("cookie", ""))
    response = JSONResponse({"logged_out": True})
    response.delete_cookie("fishagent_session", path="/")
    return response


@app.get("/api/v1/me")
@app.get("/me", include_in_schema=False)
async def me(request: Request) -> dict:
    session = authenticate(request, "/api/v1/me")
    return {"username": session.username, "role": session.role}


@app.get("/api/v1/state")
async def state(request: Request) -> dict:
    authenticate(request, "/api/v1/state")
    return SYSTEM.read_snapshot()


@app.get("/api/v1/config/llm")
async def get_llm_config(request: Request) -> dict:
    authenticate(request, "/api/v1/config/llm")
    return {"llm": CONFIG.llm.public_dict()}


@app.post("/api/v1/config/llm")
async def update_llm_config(request: Request, payload: JsonPayload) -> dict:
    authenticate(request, "/api/v1/config/llm", write=True)
    data = payload.model_dump(exclude_none=True)
    if not data.get("api_key"):
        data.pop("api_key", None)
    CONFIG.llm.update_from_payload(data)
    SYSTEM.store.emit("config.llm.updated", "大模型 API 配置已更新：%s / %s" % (CONFIG.llm.provider, CONFIG.llm.model))
    CONFIG_STORE.save_llm(CONFIG.llm)
    return {"llm": CONFIG.llm.public_dict()}


@app.post("/api/v1/config/llm/test")
async def test_llm(request: Request) -> Any:
    authenticate(request, "/api/v1/config/llm/test", write=True)
    try:
        result = test_llm_connection(CONFIG.llm)
    except ValueError as exc:
        return problem(400, "LLM connection test unavailable", str(exc))
    if not result["ok"]:
        return JSONResponse(status_code=502, content=result)
    return result


@app.post("/api/v1/demo/{mode}")
async def demo(request: Request, mode: str) -> Any:
    authenticate(request, "/api/v1/demo/%s" % mode, write=True)
    if mode == "init":
        return SYSTEM.initialize_demo()
    if mode not in {"success", "failure", "dedup", "approval"}:
        return problem(400, "Unknown demo mode")
    return SYSTEM.run_demo(mode)


@app.get("/api/v1/incidents")
async def incidents(request: Request) -> dict:
    authenticate(request, "/api/v1/incidents")
    return {"incidents": SYSTEM.read_snapshot()["incidents"]}


@app.get("/api/v1/incidents/{incident_id}")
async def incident(request: Request, incident_id: str) -> Any:
    authenticate(request, request.url.path)
    item = state_item("incidents", incident_id)
    return {"incident": item} if item else problem(404, "Incident not found")


@app.get("/api/v1/incidents/{incident_id}/timeline")
async def incident_timeline(request: Request, incident_id: str) -> Any:
    authenticate(request, request.url.path)
    if state_item("incidents", incident_id) is None:
        return problem(404, "Incident not found")
    events = [
        event
        for event in SYSTEM.read_snapshot()["events"]
        if event.get("payload", {}).get("incident_id") == incident_id
        or event.get("correlation_id") == incident_id
    ]
    return {"events": events}


@app.get("/api/v1/action-proposals")
async def proposals(request: Request) -> dict:
    authenticate(request, "/api/v1/action-proposals")
    return {"action_proposals": SYSTEM.read_snapshot()["action_proposals"]}


@app.get("/api/v1/approvals")
async def approvals(request: Request) -> dict:
    authenticate(request, "/api/v1/approvals")
    return {"approvals": SYSTEM.read_snapshot()["approvals"]}


@app.get("/api/v1/manual-tasks")
async def manual_tasks(request: Request) -> dict:
    authenticate(request, "/api/v1/manual-tasks")
    return {"manual_tasks": SYSTEM.read_snapshot()["manual_tasks"]}


@app.get("/api/v1/agent-runs")
async def agent_runs(request: Request) -> dict:
    authenticate(request, "/api/v1/agent-runs")
    return {"agent_runs": SYSTEM.read_snapshot()["agent_runs"]}


@app.get("/api/v1/agent-runs/{run_id}/steps")
async def agent_run_steps(request: Request, run_id: str) -> Any:
    authenticate(request, request.url.path)
    item = state_item("agent_runs", run_id)
    return {"steps": item["steps"]} if item else problem(404, "Agent run not found")


@app.get("/api/v1/agent-runs/{run_id}")
async def agent_run(request: Request, run_id: str) -> Any:
    authenticate(request, request.url.path)
    item = state_item("agent_runs", run_id)
    return {"run": item} if item else problem(404, "Agent run not found")


@app.post("/api/v1/agent-runs/{run_id}/cancel")
async def cancel_agent_run(request: Request, run_id: str) -> Any:
    authenticate(request, request.url.path, write=True)
    run = SYSTEM.store.agent_runs.get(run_id)
    if run is None:
        return problem(404, "Agent run not found")
    run.status = "CANCELLED"
    run.stop_reason = "USER_CANCELLED"
    SYSTEM.store.emit("agent.run.cancelled", "Agent Run 已取消", {"run_id": run.id}, correlation_id=run.id)
    state = SYSTEM.snapshot()
    return {"run": next(item for item in state["agent_runs"] if item["id"] == run.id), "state": state}


@app.get("/api/v1/patrol-runs")
async def patrol_runs(request: Request) -> dict:
    authenticate(request, request.url.path)
    runs = [run for run in SYSTEM.read_snapshot()["agent_runs"] if run["goal"] == "执行全场巡查"]
    return {"patrol_runs": runs}


@app.post("/api/v1/patrol-runs")
async def create_patrol_run(request: Request) -> Any:
    authenticate(request, request.url.path, write=True)
    run = SYSTEM.run_patrol()
    return encoded_response(202, {"run": state_item("agent_runs", run.id), "state": SYSTEM.snapshot()})


@app.get("/api/v1/patrol-runs/{run_id}")
async def patrol_run(request: Request, run_id: str) -> Any:
    authenticate(request, request.url.path)
    item = state_item("agent_runs", run_id)
    if item is None or item.get("goal") != "执行全场巡查":
        return problem(404, "Patrol run not found")
    findings = [finding for finding in SYSTEM.read_snapshot()["patrol_findings"] if finding["patrol_run_id"] == run_id]
    return {"run": item, "findings": findings}


@app.get("/api/v1/device-commands")
async def device_commands(request: Request) -> dict:
    authenticate(request, request.url.path)
    return {"device_commands": SYSTEM.read_snapshot()["commands"]}


@app.get("/api/v1/device-commands/{command_id}")
async def device_command(request: Request, command_id: str) -> Any:
    authenticate(request, request.url.path)
    item = next((command for command in SYSTEM.read_snapshot()["commands"] if command["id"] == command_id), None)
    return {"device_command": item} if item else problem(404, "Device command not found")


@app.post("/api/v1/device-commands")
async def create_device_command(
    request: Request,
    payload: JsonPayload,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    authenticate(request, request.url.path, write=True)
    data = payload.model_dump()
    incident_id = str(data.get("incident_id") or "")
    incident = SYSTEM.store.incidents.get(incident_id)
    if incident is None:
        return problem(400, "Invalid device command", "incident_id is required")
    device_id = str(data.get("device_id") or "")
    target_state = str(data.get("target_state") or "on")
    try:
        risk = RiskLevel(str(data.get("risk") or "L1").upper())
        run = SYSTEM.store.agent_runs.get(str(data.get("run_id") or ""))
        if run is None:
            from fishagent.domain.models import AgentRun, new_id

            run = AgentRun(id=new_id("run"), goal="执行设备命令", incident_id=incident.id, status="RUNNING")
            SYSTEM.store.agent_runs[run.id] = run
        command = SYSTEM.request_action_execution(
            run,
            incident,
            device_id=device_id,
            target_state=target_state,
            risk=risk,
            approval_granted=bool(data.get("approval_granted", False)),
            idempotency_key=idempotency_key or None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return problem(400, "Invalid device command", str(exc))
    status = 200 if command.idempotency_key in SYSTEM.store.executed_idempotency_keys else 202
    return encoded_response(status, {"device_command": command.__dict__, "state": SYSTEM.snapshot()})


@app.post("/api/v1/action-proposals")
async def create_action_proposal(request: Request, payload: JsonPayload) -> Any:
    authenticate(request, request.url.path, write=True)
    data = payload.model_dump()
    try:
        proposal = SYSTEM.propose_action(
            incident_id=str(data.get("incident_id") or ""),
            device_id=str(data.get("device_id") or ""),
            target_state=str(data.get("target_state") or "on"),
            risk=RiskLevel(str(data.get("risk") or "L2").upper()),
            rationale=str(data.get("rationale") or ""),
        )
    except (KeyError, ValueError) as exc:
        return problem(400, "Invalid action proposal", str(exc))
    return encoded_response(201, {"proposal": state_item("action_proposals", proposal.id), "state": SYSTEM.snapshot()})


@app.post("/api/v1/schedules")
async def create_schedule(request: Request, payload: JsonPayload) -> Any:
    authenticate(request, request.url.path, write=True)
    try:
        schedule = SYSTEM.create_schedule(payload.model_dump())
    except (TypeError, ValueError) as exc:
        return problem(400, "Invalid schedule", str(exc))
    return encoded_response(201, {"schedule": schedule.__dict__, "state": SYSTEM.snapshot()})


@app.post("/api/v1/schedules/{schedule_id}/{action}")
async def schedule_action(request: Request, schedule_id: str, action: str) -> Any:
    authenticate(request, request.url.path, write=True)
    try:
        if action == "run-now":
            job = SYSTEM.run_schedule_now(schedule_id)
            return encoded_response(202, {"job": job.__dict__, "state": SYSTEM.snapshot()})
        if action in {"pause", "resume"}:
            status = ScheduleStatus.PAUSED if action == "pause" else ScheduleStatus.ACTIVE
            schedule = SYSTEM.set_schedule_status(schedule_id, status)
            return {"schedule": schedule.__dict__, "state": SYSTEM.snapshot()}
    except KeyError as exc:
        return problem(404, "Schedule not found", str(exc))
    return problem(404, "Unknown schedule action")


@app.get("/api/v1/schedules/{schedule_id}")
async def schedule(request: Request, schedule_id: str) -> Any:
    authenticate(request, request.url.path)
    item = state_item("schedules", schedule_id)
    return {"schedule": item} if item else problem(404, "Schedule not found")


def assets(collection: str) -> dict:
    return {collection: SYSTEM.read_snapshot().get(collection, [])}


@app.post("/api/v1/evidence")
async def upload_evidence(request: Request, file: UploadFile = File(...)) -> Any:
    authenticate(request, request.url.path, write=True)
    if OBJECT_STORE is None:
        return problem(503, "Object storage unavailable", "MinIO 未配置")
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        return problem(413, "Evidence too large", "单个证据文件不能超过 20MB")
    try:
        OBJECT_STORE.health()
        result = OBJECT_STORE.put_bytes(data, file.content_type or "application/octet-stream")
    except Exception as exc:
        return problem(503, "Evidence upload failed", str(exc))
    SYSTEM.store.emit("evidence.uploaded", "证据文件已上传", {"object_name": result["object_name"], "content_type": file.content_type})
    return {"evidence": result, "state": SYSTEM.snapshot()}


@app.post("/api/v1/cameras/{camera_id}/analyze")
async def analyze_camera(request: Request, camera_id: str) -> Any:
    authenticate(request, request.url.path, write=True)
    camera = state_item("cameras", camera_id)
    if camera is None:
        return problem(404, "Camera not found")
    try:
        from fishagent.infrastructure.queue.celery_app import analyze_camera as analyze_camera_task

        task = analyze_camera_task.delay(camera_id)
        return encoded_response(202, {"task_id": task.id, "camera_id": camera_id, "status": "QUEUED"})
    except Exception as exc:
        return problem(503, "Vision queue unavailable", str(exc))


@app.post("/api/v1/cameras/{camera_id}/capture")
async def capture_camera(request: Request, camera_id: str) -> Any:
    authenticate(request, request.url.path, write=True)
    if state_item("cameras", camera_id) is None:
        return problem(404, "Camera not found")
    try:
        from fishagent.infrastructure.queue.celery_app import capture_camera_frame

        task = capture_camera_frame.delay(camera_id)
        return encoded_response(202, {"task_id": task.id, "camera_id": camera_id, "status": "QUEUED"})
    except Exception as exc:
        return problem(503, "Vision queue unavailable", str(exc))


@app.post("/api/v1/cameras/{camera_id}/upload")
async def upload_camera_frame(request: Request, camera_id: str, file: UploadFile = File(...)) -> Any:
    authenticate(request, request.url.path, write=True)
    camera = SYSTEM.store.cameras.get(camera_id)
    if camera is None:
        return problem(404, "Camera not found")
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        return problem(413, "Frame too large", "单个摄像头帧不能超过 10MB")
    try:
        from fishagent.infrastructure.vision import FrameCaptureError, validate_frame

        frame = validate_frame(data, "upload://%s" % (file.filename or camera_id))
        if OBJECT_STORE is None:
            return problem(503, "Object storage unavailable", "MinIO 未配置")
        object_info = OBJECT_STORE.put_bytes(data, frame.content_type, prefix="frames/%s" % camera_id)
        vision_frame = VisionFrame(
            id=new_id("frame"),
            camera_id=camera_id,
            source_url=frame.source_url,
            object_name=object_info["object_name"],
            content_type=frame.content_type,
            sha256=frame.sha256,
            width=frame.width,
            height=frame.height,
            captured_at=frame.captured_at or utcnow(),
        )
    except (ValueError, FrameCaptureError) as exc:
        return problem(400, "Invalid camera frame", str(exc))
    except Exception as exc:
        return problem(503, "Camera frame upload failed", str(exc))
    SYSTEM.store.vision_frames[vision_frame.id] = vision_frame
    camera.status = "ONLINE"
    camera.last_frame_at = vision_frame.captured_at
    camera.last_frame_id = vision_frame.id
    camera.last_frame_hash = vision_frame.sha256
    camera.last_frame_width = vision_frame.width
    camera.last_frame_height = vision_frame.height
    SYSTEM.store.emit(
        "vision.frame.uploaded",
        "摄像头帧已上传",
        {"camera_id": camera_id, "frame_id": vision_frame.id, "object_name": vision_frame.object_name},
        correlation_id=camera_id,
    )
    return encoded_response(201, {"frame": vision_frame.__dict__, "state": SYSTEM.snapshot()})


@app.get("/api/v1/evidence/{object_name:path}")
async def evidence_url(request: Request, object_name: str) -> Any:
    authenticate(request, request.url.path)
    if OBJECT_STORE is None:
        return problem(503, "Object storage unavailable", "MinIO 未配置")
    try:
        return {"url": OBJECT_STORE.presigned_get(object_name)}
    except Exception as exc:
        return problem(404, "Evidence not found", str(exc))


@app.get("/api/v1/{collection}")
async def list_assets(request: Request, collection: str) -> Any:
    authenticate(request, request.url.path)
    if collection == "events":
        return {"events": SYSTEM.read_snapshot()["events"]}
    if collection not in {"farms", "zones", "ponds", "sensors", "devices", "cameras", "schedules", "scheduled-jobs", "sensor-health", "patrol-findings", "escalations", "audit-events"}:
        return problem(404, "Not Found")
    return assets(collection.replace("-", "_"))


@app.post("/api/v1/{collection}")
async def create_collection(request: Request, collection: str, payload: AssetPayload | JsonPayload) -> Any:
    authenticate(request, request.url.path, write=True)
    if collection not in {"farms", "zones", "ponds", "sensors", "devices", "cameras"}:
        return problem(404, "Not Found")
    try:
        asset = {"farms": SYSTEM.create_farm, "zones": SYSTEM.create_zone, "ponds": SYSTEM.create_pond, "sensors": SYSTEM.create_sensor, "devices": SYSTEM.create_device, "cameras": SYSTEM.create_camera}[collection](payload.model_dump(exclude_none=True))
    except (KeyError, TypeError, ValueError) as exc:
        return problem(400, "Invalid asset payload", str(exc))
    return encoded_response(201, {"asset": asset.__dict__, "state": SYSTEM.snapshot()})


@app.get("/api/v1/sensors/{sensor_id}/health")
async def sensor_health(request: Request, sensor_id: str) -> Any:
    authenticate(request, request.url.path)
    item = next((health for health in SYSTEM.read_snapshot()["sensor_health"] if health["sensor_id"] == sensor_id), None)
    return {"sensor_health": item} if item else problem(404, "Sensor health not found")


@app.get("/api/v1/audit-events")
async def audit_events(request: Request, resource_type: str = "", resource_id: str = "", limit: int = 100) -> dict:
    authenticate(request, request.url.path)
    limit = min(max(limit, 1), 1000)
    items = SYSTEM.read_snapshot()["audit_events"]
    if resource_type:
        items = [item for item in items if item["resource_type"] == resource_type]
    if resource_id:
        items = [item for item in items if item.get("resource_id") == resource_id]
    return {"audit_events": items[-limit:]}


@app.post("/api/v1/telemetry/readings:batch")
async def telemetry_batch(request: Request, payload: TelemetryBatchPayload, idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key")) -> Any:
    authenticate(request, "/api/v1/telemetry/readings:batch", write=True)
    accepted = 0
    incident_ids = []
    try:
        for index, item in enumerate(payload.readings):
            if item.metric != "DO":
                continue
            incident = SYSTEM.ingest_do(
                pond_id=item.pond_id,
                value=item.value,
                source_event_id=item.source_event_id or (f"{idempotency_key}:{index}" if idempotency_key else None),
                seconds_old=item.seconds_old,
                sensor_id=item.sensor_id,
                quality=item.quality,
                auto_run=item.auto_run,
            )
            accepted += 1
            if incident:
                incident_ids.append(incident.id)
    except (KeyError, TypeError, ValueError) as exc:
        return problem(400, "Invalid telemetry payload", str(exc))
    return encoded_response(202, {"accepted": accepted, "incident_ids": incident_ids, "state": SYSTEM.snapshot()})


@app.get("/api/v1/telemetry/snapshot")
async def telemetry_snapshot(request: Request) -> dict:
    authenticate(request, request.url.path)
    readings = SYSTEM.read_snapshot()["readings"]
    latest = {"%s:%s" % (reading["pond_id"], reading["metric"]): reading for reading in readings}
    return {"readings": list(latest.values())}


@app.get("/api/v1/telemetry/series")
async def telemetry_series(request: Request, pond_id: str = "", metric: str = "DO", limit: int = 100) -> dict:
    authenticate(request, request.url.path)
    limit = min(max(limit, 1), 1000)
    readings = [r for r in SYSTEM.read_snapshot()["readings"] if r["pond_id"] == pond_id and r["metric"] == metric]
    return {"readings": readings[-limit:]}


@app.post("/api/v1/agent-runs")
async def create_agent_run(request: Request, payload: JsonPayload) -> Any:
    authenticate(request, request.url.path, write=True)
    try:
        run = SYSTEM.run_goal(str(payload.model_dump().get("goal") or ""), payload.model_dump().get("pond_id"))
    except ValueError as exc:
        return problem(400, "Invalid agent goal", str(exc))
    return encoded_response(202, {"run": state_item("agent_runs", run.id), "state": SYSTEM.snapshot()})


@app.post("/api/v1/action-proposals/{proposal_id}/{decision}")
async def decide_proposal(request: Request, proposal_id: str, decision: str, payload: JsonPayload) -> Any:
    authenticate(request, request.url.path, write=True)
    data = payload.model_dump()
    try:
        if decision == "approve":
            command = SYSTEM.approve_action(proposal_id, str(data.get("approver") or "现场负责人"), str(data.get("reason") or ""))
            return {"command": command.__dict__, "state": SYSTEM.snapshot()}
        if decision == "reject":
            approval = SYSTEM.reject_action(proposal_id, str(data.get("approver") or "现场负责人"), str(data.get("reason") or ""))
            return {"approval": approval.__dict__, "state": SYSTEM.snapshot()}
    except (KeyError, ValueError) as exc:
        return problem(400, "Invalid approval decision", str(exc))
    return problem(404, "Unknown approval decision")


def proposal_for_incident(incident_id: str) -> Optional[str]:
    incident = SYSTEM.store.incidents.get(incident_id)
    if not incident:
        return None
    for proposal_id in reversed(incident.action_proposal_ids):
        proposal = SYSTEM.store.action_proposals.get(proposal_id)
        if proposal and proposal.status in {"PENDING_APPROVAL", "PROPOSED"}:
            return proposal_id
    return None


@app.post("/api/v1/incidents/{incident_id}/approve")
async def approve_incident(request: Request, incident_id: str, payload: JsonPayload) -> Any:
    authenticate(request, request.url.path, write=True)
    proposal_id = proposal_for_incident(incident_id)
    if proposal_id is None:
        return problem(409, "No pending approval", "事件没有待审批动作")
    data = payload.model_dump()
    try:
        command = SYSTEM.approve_action(
            proposal_id,
            str(data.get("approver") or "现场负责人"),
            str(data.get("reason") or ""),
        )
    except (KeyError, ValueError) as exc:
        return problem(409, "Approval unavailable", str(exc))
    return encoded_response(200, {"command": command.__dict__, "state": SYSTEM.snapshot()})


@app.post("/api/v1/incidents/{incident_id}/reject")
async def reject_incident(request: Request, incident_id: str, payload: JsonPayload) -> Any:
    authenticate(request, request.url.path, write=True)
    proposal_id = proposal_for_incident(incident_id)
    if proposal_id is None:
        return problem(409, "No pending approval", "事件没有待审批动作")
    data = payload.model_dump()
    try:
        approval = SYSTEM.reject_action(
            proposal_id,
            str(data.get("approver") or "现场负责人"),
            str(data.get("reason") or ""),
        )
    except (KeyError, ValueError) as exc:
        return problem(409, "Rejection unavailable", str(exc))
    return encoded_response(200, {"approval": approval.__dict__, "state": SYSTEM.snapshot()})


@app.post("/api/v1/incidents/{incident_id}/assign")
async def assign_incident(request: Request, incident_id: str, payload: JsonPayload) -> Any:
    authenticate(request, request.url.path, write=True)
    incident = SYSTEM.store.incidents.get(incident_id)
    if incident is None:
        return problem(404, "Incident not found")
    assignee = str(payload.model_dump().get("assignee") or "").strip()
    if not assignee:
        return problem(400, "Invalid assignee", "assignee is required")
    incident.assignee = assignee
    SYSTEM.store.emit("incident.assigned", "事件已分派给 %s" % assignee, {"incident_id": incident_id, "assignee": assignee})
    state = SYSTEM.snapshot()
    return {"incident": next(item for item in state["incidents"] if item["id"] == incident_id), "state": state}


@app.post("/api/v1/incidents/{incident_id}/verify")
async def verify(request: Request, incident_id: str) -> Any:
    authenticate(request, request.url.path, write=True)
    try:
        verified = SYSTEM.verify_incident(incident_id)
    except KeyError as exc:
        return problem(404, "Incident not found", str(exc))
    return {"incident": state_item("incidents", verified.id), "state": SYSTEM.snapshot()}


@app.post("/api/v1/manual-tasks/{task_id}/complete")
async def complete_task(request: Request, task_id: str) -> Any:
    authenticate(request, request.url.path, write=True)
    try:
        task = SYSTEM.complete_manual_task(task_id)
    except KeyError as exc:
        return problem(404, "Manual task not found", str(exc))
    return {"task": task.__dict__, "state": SYSTEM.snapshot()}


@app.post("/api/v1/scheduled-jobs:dispatch")
async def dispatch_jobs(request: Request) -> dict:
    authenticate(request, request.url.path, write=True)
    jobs = SYSTEM.run_due_jobs()
    return {"completed_job_ids": [job.id for job in jobs], "state": SYSTEM.snapshot()}


@app.get("/api/v1/events")
async def events(request: Request, after: int = 0) -> dict:
    authenticate(request, request.url.path)
    snapshot = SYSTEM.read_snapshot()
    return {"events": [event for event in snapshot["events"] if event["sequence"] > after]}


@app.websocket("/events")
@app.websocket("/api/v1/events/ws")
async def event_stream(websocket: WebSocket) -> None:
    if not authenticate_websocket(websocket):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    try:
        after = int(websocket.query_params.get("after", "0"))
        initial_snapshot = SYSTEM.read_snapshot()
        for event in initial_snapshot["events"]:
            if event["sequence"] > after:
                await websocket.send_json(event)
                after = event["sequence"]
        if EVENT_PUBLISHER:
            pubsub = EVENT_PUBLISHER._get_client().pubsub()
            pubsub.subscribe(EVENT_PUBLISHER.channel)
            try:
                while True:
                    message = await asyncio.to_thread(pubsub.get_message, True, 1.0)
                    if message and message.get("type") == "message":
                        import json

                        event = json.loads(message["data"])
                        if int(event.get("sequence", 0)) > after:
                            await websocket.send_json(event)
                            after = int(event["sequence"])
                    else:
                        await websocket.send_json({"type": "heartbeat", "after": after})
            finally:
                pubsub.close()
        else:
            while True:
                await asyncio.sleep(1)
    except WebSocketDisconnect:
        return


try:
    from nicegui import ui

    @ui.page("/nicegui", title="智渔 Agent · NiceGUI")
    def nicegui_console() -> None:
        ui.colors(primary="#0f766e", secondary="#2563eb", accent="#38bdf8")
        ui.label("智渔 Agent / NiceGUI 控制室").classes("text-2xl font-bold")
        ui.label("FastAPI + NiceGUI 运行时已连接，详细运营控制台位于首页。").classes("text-gray-600")
        status = ui.label("正在读取状态…")

        async def refresh_status() -> None:
            snapshot = SYSTEM.read_snapshot()
            status.text = "事件 %s · 养殖场 %s · 池塘 %s · Agent Run %s" % (
                snapshot["event_sequence"],
                len(snapshot["farms"]),
                len(snapshot["ponds"]),
                len(snapshot["agent_runs"]),
            )

        ui.timer(5, refresh_status)

    ui.run_with(
        app,
        title="智渔 Agent",
        language="zh-CN",
        favicon=str(STATIC_DIR / "fish.svg"),
        storage_secret="fishagent-local-ui",
        show_welcome_message=False,
    )
except ImportError:  # pragma: no cover - only used in minimal unit-test environments
    pass

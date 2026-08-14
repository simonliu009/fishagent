"""FastAPI/NiceGUI web process for the fishery operations console.

The domain service remains shared with the legacy compatibility server, while
this module owns typed HTTP, WebSocket and NiceGUI process boundaries.
"""

import asyncio
import csv
import io
import json
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from fishagent.agent_runtime.crewai_runtime import CrewAIOrchestrator
from fishagent.application.agent_service import FishAgentSystem
from fishagent.core import AppConfig, LLMConfig, RuntimeConfigStore, new_llm_profile_id
from fishagent.domain.models import RiskLevel, ScheduleStatus, VisionFrame, new_id, utcnow
from fishagent.infrastructure.auth import auth_from_config
from fishagent.infrastructure.gateways import mqtt_gateway_from_config
from fishagent.infrastructure.mqtt import MqttTelemetryAdapter, MqttTelemetryPublisher
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
DEVICE_GATEWAY = mqtt_gateway_from_config(
    CONFIG.mqtt_enabled,
    CONFIG.mqtt_host,
    CONFIG.mqtt_port,
    CONFIG.mqtt_command_topic,
)
TELEMETRY_PUBLISHER = MqttTelemetryPublisher(CONFIG.mqtt_host, CONFIG.mqtt_port) if CONFIG.mqtt_enabled else None
SYSTEM = FishAgentSystem(
    repository=REPOSITORY,
    event_publisher=EVENT_PUBLISHER,
    device_gateway=DEVICE_GATEWAY,
    telemetry_publisher=TELEMETRY_PUBLISHER,
    agent_decision_timeout_seconds=CONFIG.agent_decision_timeout_seconds,
)
AUTH = auth_from_config(CONFIG.auth_enabled, CONFIG.auth_username, CONFIG.auth_password)
CONFIG_STORE = RuntimeConfigStore()
CONFIG.llm, LLM_PROFILES = CONFIG_STORE.load_llm_bundle(CONFIG.llm)
SYSTEM.agent_orchestrator = CrewAIOrchestrator(SYSTEM, CONFIG.llm)
STATIC_DIR = Path(__file__).parent / "static"
MQTT_ADAPTER: MqttTelemetryAdapter | None = None
REQUEST_COUNTS: Counter[tuple[str, str, int]] = Counter()
REQUEST_LATENCY_MS: Counter[str] = Counter()


def ingest_mqtt_and_persist(**payload: Any) -> Any:
    defer_persist = bool(payload.pop("defer_persist", False)) and str(payload.get("source_event_id", "")).startswith(
        "demo-seed-"
    )
    result = SYSTEM.ingest_reading(**payload)
    if not defer_persist:
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
        if DEVICE_GATEWAY:
            DEVICE_GATEWAY.close()
        if TELEMETRY_PUBLISHER:
            TELEMETRY_PUBLISHER.close()

app = FastAPI(
    title="智渔 Agent API",
    version="0.2.0",
    description="养殖感知、研判、执行、复核与升级 API",
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan,
)


@app.get("/openapi.json", include_in_schema=False)
async def legacy_openapi() -> dict:
    return app.openapi()


@app.middleware("http")
async def request_observability(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID") or new_id("req")
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        REQUEST_COUNTS[(request.method, request.url.path, 500)] += 1
        REQUEST_LATENCY_MS[request.url.path] += elapsed_ms
        raise
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    REQUEST_COUNTS[(request.method, request.url.path, response.status_code)] += 1
    REQUEST_LATENCY_MS[request.url.path] += elapsed_ms
    response.headers["X-Correlation-ID"] = correlation_id
    return response


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    lines = [
        "# HELP fishagent_http_requests_total HTTP requests handled by status.",
        "# TYPE fishagent_http_requests_total counter",
    ]
    for (method, path, status), count in sorted(REQUEST_COUNTS.items()):
        lines.append(
            'fishagent_http_requests_total{method="%s",path="%s",status="%s"} %s'
            % (method, path.replace('"', '\\"'), status, count)
        )
    lines.extend(
        [
            "# HELP fishagent_http_request_latency_ms_total Cumulative request latency in milliseconds.",
            "# TYPE fishagent_http_request_latency_ms_total counter",
        ]
    )
    for path, total in sorted(REQUEST_LATENCY_MS.items()):
        lines.append('fishagent_http_request_latency_ms_total{path="%s"} %s' % (path.replace('"', '\\"'), total))
    if MQTT_ADAPTER:
        mqtt_status = "ok" if not MQTT_ADAPTER.last_error else "degraded"
        lines.extend(
            [
                "# HELP fishagent_mqtt_adapter_up MQTT adapter availability.",
                "# TYPE fishagent_mqtt_adapter_up gauge",
                'fishagent_mqtt_adapter_up{status="%s"} %s' % (mqtt_status, 1 if mqtt_status == "ok" else 0),
            ]
        )
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


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
    unit: Optional[str] = None
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


class ChatTurnPayload(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class AgentChatPayload(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    pond_id: Optional[str] = None
    history: list[ChatTurnPayload] = Field(default_factory=list, max_length=12)


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


def record_user_audit(
    session: Any,
    action: str,
    summary: str,
    payload: dict,
    resource_type: str,
    resource_id: Optional[str],
    correlation_id: Optional[str] = None,
) -> None:
    """Record the authenticated actor alongside the domain event stream."""
    if session is None:
        return
    SYSTEM.store.emit(
        action,
        summary,
        payload,
        correlation_id=correlation_id,
        actor_type="user",
        actor_id=session.username,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    SYSTEM.snapshot()


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
    persistence_ready = persistence.get("status") == "ok"
    degraded = any(item.get("status") == "degraded" for item in (realtime, media))
    response = {
        "status": "degraded" if degraded and persistence_ready else "ok",
        "port": CONFIG.port,
        "public_port": CONFIG.public_port,
        "persistence": persistence,
        "realtime": realtime,
        "media": media,
    }
    return JSONResponse(status_code=200 if persistence_ready else 503, content=response)


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
    SYSTEM.store.emit(
        "auth.login",
        "用户登录成功",
        {"username": session.username, "role": session.role},
        actor_type="user",
        actor_id=session.username,
        resource_type="user",
        resource_id=session.username,
    )
    SYSTEM.snapshot()
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
    session = AUTH.authenticate(request.headers.get("cookie", ""))
    AUTH.logout(request.headers.get("cookie", ""))
    if session and session.token != "disabled":
        SYSTEM.store.emit(
            "auth.logout",
            "用户退出登录",
            {"username": session.username},
            actor_type="user",
            actor_id=session.username,
            resource_type="user",
            resource_id=session.username,
        )
        SYSTEM.snapshot()
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
    return {
        "llm": CONFIG.llm.public_dict(),
        "profiles": [profile.public_dict() for profile in LLM_PROFILES],
    }


@app.post("/api/v1/config/llm")
async def update_llm_config(request: Request, payload: JsonPayload) -> Any:
    global LLM_PROFILES
    session = authenticate(request, "/api/v1/config/llm", write=True)
    data = payload.model_dump(exclude_none=True)
    if not data.get("api_key"):
        data.pop("api_key", None)
    requested_profile_id = str(data.pop("profile_id", "") or "").strip()
    save_as_profile = bool(data.pop("save_as_profile", False))
    if save_as_profile:
        name = str(data.get("name", "") or "").strip()
        if not name:
            return problem(400, "Invalid LLM provider", "自定义供应商必须填写名称")
        profile_id = requested_profile_id if requested_profile_id and requested_profile_id != "__new__" else new_llm_profile_id()
        data["profile_id"] = profile_id
    elif requested_profile_id:
        data["profile_id"] = requested_profile_id
    CONFIG.llm.update_from_payload(data)
    if save_as_profile:
        saved_profile = LLMConfig()
        saved_profile.update_from_payload(CONFIG.llm.private_dict())
        for index, profile in enumerate(LLM_PROFILES):
            if profile.profile_id == saved_profile.profile_id:
                LLM_PROFILES[index] = saved_profile
                break
        else:
            LLM_PROFILES.append(saved_profile)
    SYSTEM.store.emit(
        "config.llm.updated",
        "大模型 API 配置已更新：%s / %s" % (CONFIG.llm.provider, CONFIG.llm.model),
        actor_type="user",
        actor_id=session.username,
        resource_type="config",
        resource_id="llm",
    )
    CONFIG_STORE.save_llm(CONFIG.llm, LLM_PROFILES)
    SYSTEM.agent_orchestrator = CrewAIOrchestrator(SYSTEM, CONFIG.llm)
    SYSTEM.snapshot()
    return {
        "llm": CONFIG.llm.public_dict(),
        "profiles": [profile.public_dict() for profile in LLM_PROFILES],
    }


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
    if mode not in {"success", "failure", "dedup", "approval", "alerts"}:
        return problem(400, "Unknown demo mode")
    return SYSTEM.run_demo(mode)


@app.get("/api/v1/analysis-cases")
async def analysis_cases(request: Request) -> dict:
    authenticate(request, request.url.path)
    state = SYSTEM.read_snapshot()
    return {
        "analysis_cases": state.get("analysis_cases", []),
        "agent_runs": [run for run in state.get("agent_runs", []) if run.get("incident_id")],
        "camera_observations": state.get("camera_observations", []),
        "weather_observations": state.get("weather_observations", []),
        "disease_knowledge": state.get("disease_knowledge", []),
    }


@app.post("/api/v1/analysis-cases/run-all")
async def run_all_analysis_cases(request: Request) -> Any:
    authenticate(request, request.url.path, write=True)
    started = SYSTEM.start_analysis_case_sequence()
    return encoded_response(202, {"started": started, "state": SYSTEM.snapshot()})


@app.post("/api/v1/analysis-cases/{case_id}/run")
async def run_analysis_case(request: Request, case_id: str) -> Any:
    authenticate(request, request.url.path, write=True)
    if case_id not in SYSTEM.store.analysis_cases:
        return problem(404, "Analysis case not found")
    run = await asyncio.to_thread(SYSTEM.run_analysis_case, case_id)
    state = SYSTEM.snapshot()
    return encoded_response(202, {"run": next(item for item in state["agent_runs"] if item["id"] == run.id), "state": state})


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
    state = SYSTEM.snapshot()
    return encoded_response(202, {"run": next(item for item in state["agent_runs"] if item["id"] == run.id), "state": state})


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
    session = authenticate(request, request.url.path, write=True)
    data = payload.model_dump()
    incident_id = str(data.get("incident_id") or "")
    incident = SYSTEM.store.incidents.get(incident_id)
    if incident is None:
        return problem(400, "Invalid device command", "incident_id is required")
    device_id = str(data.get("device_id") or "")
    target_state = str(data.get("target_state") or "on")
    try:
        risk = RiskLevel(str(data.get("risk") or "L1").upper())
        approval_granted = False
        if risk == RiskLevel.L2:
            approval_id = str(data.get("approval_id") or "")
            approval = SYSTEM.store.approvals.get(approval_id)
            approval_granted = bool(
                approval
                and approval.incident_id == incident.id
                and approval.status.value == "APPROVED"
            )
            if not approval_granted:
                return problem(409, "Approval required", "L2 设备命令必须引用已批准的 approval_id")
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
            approval_granted=approval_granted,
            idempotency_key=idempotency_key or None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return problem(400, "Invalid device command", str(exc))
    status = 200 if command.idempotency_key in SYSTEM.store.executed_idempotency_keys else 202
    record_user_audit(
        session,
        "device.command.requested",
        "用户请求设备命令",
        {"command_id": command.id, "incident_id": incident.id, "device_id": device_id, "target_state": target_state},
        "device_command",
        command.id,
        request.headers.get("X-Correlation-ID"),
    )
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
    session = authenticate(request, request.url.path, write=True)
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
    SYSTEM.store.emit(
        "evidence.uploaded",
        "证据文件已上传",
        {"object_name": result["object_name"], "content_type": file.content_type},
        actor_type="user",
        actor_id=session.username,
        resource_type="evidence",
        resource_id=result["object_name"],
    )
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
    session = authenticate(request, request.url.path, write=True)
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
    record_user_audit(
        session,
        "vision.frame.upload.requested",
        "用户上传摄像头帧",
        {"camera_id": camera_id, "frame_id": vision_frame.id},
        "vision_frame",
        vision_frame.id,
        request.headers.get("X-Correlation-ID"),
    )
    return encoded_response(201, {"frame": vision_frame.__dict__, "state": SYSTEM.snapshot()})


@app.get("/api/v1/evidence/{object_name:path}")
async def evidence_url(request: Request, object_name: str) -> Any:
    session = authenticate(request, request.url.path)
    if OBJECT_STORE is None:
        return problem(503, "Object storage unavailable", "MinIO 未配置")
    try:
        url = OBJECT_STORE.presigned_get(object_name)
        SYSTEM.store.emit(
            "evidence.accessed",
            "证据访问地址已签发",
            {"object_name": object_name},
            actor_type="user",
            actor_id=session.username,
            resource_type="evidence",
            resource_id=object_name,
        )
        SYSTEM.snapshot()
        return {"url": url}
    except Exception as exc:
        return problem(404, "Evidence not found", str(exc))


@app.post("/api/v1/agent-runs")
async def create_agent_run(request: Request, payload: JsonPayload) -> Any:
    authenticate(request, request.url.path, write=True)
    try:
        run = SYSTEM.run_goal(str(payload.model_dump().get("goal") or ""), payload.model_dump().get("pond_id"))
    except ValueError as exc:
        return problem(400, "Invalid agent goal", str(exc))
    state = SYSTEM.snapshot()
    return encoded_response(202, {"run": next(item for item in state["agent_runs"] if item["id"] == run.id), "state": state})


def agent_run_payload(run: Any) -> dict[str, Any]:
    """Serialize the in-memory run when a concurrent snapshot has not caught up yet."""
    return {
        "id": run.id,
        "goal": run.goal,
        "incident_id": run.incident_id,
        "status": run.status,
        "stop_reason": run.stop_reason,
        "delegated_agents": list(run.delegated_agents),
        "steps": [
            {
                "agent": step.agent,
                "action": step.action,
                "summary": step.summary,
                "created_at": step.created_at.isoformat(),
            }
            for step in run.steps
        ],
        "budget": dict(run.budget),
    }


def sse_event(event: str, payload: dict[str, Any]) -> str:
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(payload, ensure_ascii=False, default=str))


def chat_run_data(run: Any, state: dict[str, Any]) -> dict[str, Any]:
    return next((item for item in state.get("agent_runs", []) if item["id"] == run.id), agent_run_payload(run))


@app.post("/api/v1/agent-chat")
async def agent_chat(request: Request, payload: AgentChatPayload) -> Any:
    authenticate(request, request.url.path, write=True)

    def execute_turn() -> tuple[Any, str, dict]:
        run, reply = SYSTEM.run_chat(
            payload.message,
            [turn.model_dump() for turn in payload.history],
            payload.pond_id,
        )
        # A read-only snapshot can restore an older durable state while the model is running.
        # Re-register the completed object before taking the response snapshot.
        SYSTEM.store.agent_runs[run.id] = run
        return run, reply, SYSTEM.snapshot()

    try:
        run, reply, state = await asyncio.to_thread(execute_turn)
    except ValueError as exc:
        return problem(400, "Invalid chat message", str(exc))
    run_data = chat_run_data(run, state)
    response = {"reply": reply, "run": run_data}
    if run.status != "COMPLETED":
        return encoded_response(503, {**response, "detail": reply})
    return response


@app.post("/api/v1/agent-chat/stream")
async def agent_chat_stream(request: Request, payload: AgentChatPayload) -> StreamingResponse:
    authenticate(request, request.url.path, write=True)

    def execute_turn() -> tuple[Any, str, dict]:
        run, reply = SYSTEM.run_chat(
            payload.message,
            [turn.model_dump() for turn in payload.history],
            payload.pond_id,
        )
        SYSTEM.store.agent_runs[run.id] = run
        return run, reply, SYSTEM.snapshot()

    async def event_stream():
        turn_task = asyncio.create_task(asyncio.to_thread(execute_turn))
        yield sse_event("start", {})
        try:
            while True:
                try:
                    run, reply, state = await asyncio.wait_for(asyncio.shield(turn_task), timeout=8)
                    break
                except asyncio.TimeoutError:
                    # Keep reverse proxies and browsers aware that the model turn is still alive.
                    yield sse_event("progress", {"message": "智渔AI 正在调用 Agent 和只读工具..."})
            run_data = chat_run_data(run, state)
            if run.status != "COMPLETED":
                yield sse_event("error", {"status": 503, "detail": reply, "run": run_data})
                return
            # CrewAI currently exposes an audited final answer rather than token callbacks.
            # Keep the HTTP contract streaming and release that final answer progressively.
            for index in range(0, len(reply), 24):
                yield sse_event("delta", {"text": reply[index : index + 24]})
                await asyncio.sleep(0.015)
            yield sse_event("done", {"reply": reply, "run": run_data})
        except ValueError as exc:
            yield sse_event("error", {"status": 400, "detail": str(exc)})
        except Exception as exc:  # pragma: no cover - protects the already-open SSE connection
            yield sse_event("error", {"status": 500, "detail": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


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


@app.get("/api/v1/audit-events/export")
async def export_audit_events(
    request: Request,
    resource_type: str = "",
    resource_id: str = "",
    limit: int = 1000,
    format: str = "json",
) -> Any:
    session = authenticate(request, request.url.path)
    limit = min(max(limit, 1), 5000)
    items = SYSTEM.read_snapshot()["audit_events"]
    if resource_type:
        items = [item for item in items if item["resource_type"] == resource_type]
    if resource_id:
        items = [item for item in items if item.get("resource_id") == resource_id]
    items = items[-limit:]
    record_user_audit(
        session,
        "audit.exported",
        "用户导出审计记录",
        {"resource_type": resource_type, "resource_id": resource_id, "limit": limit, "format": format},
        "audit_export",
        None,
        request.headers.get("X-Correlation-ID"),
    )
    if format.lower() != "csv":
        return {"audit_events": items}
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["id", "actor_type", "actor_id", "action", "resource_type", "resource_id", "correlation_id", "created_at", "payload"],
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(items)
    response = Response(output.getvalue(), media_type="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = 'attachment; filename="fishagent-audit-events.csv"'
    return response


@app.post("/api/v1/telemetry/readings:batch")
async def telemetry_batch(request: Request, payload: TelemetryBatchPayload, idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key")) -> Any:
    authenticate(request, "/api/v1/telemetry/readings:batch", write=True)
    accepted = 0
    incident_ids = []
    try:
        for index, item in enumerate(payload.readings):
            incident = SYSTEM.ingest_reading(
                pond_id=item.pond_id,
                value=item.value,
                metric=item.metric,
                unit=item.unit,
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
    session = authenticate(request, request.url.path, write=True)
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
    record_user_audit(
        session,
        "approval.approved.by_user",
        "用户批准事件动作",
        {"incident_id": incident_id, "command_id": command.id},
        "incident",
        incident_id,
        request.headers.get("X-Correlation-ID"),
    )
    return encoded_response(200, {"command": command.__dict__, "state": SYSTEM.snapshot()})


@app.post("/api/v1/incidents/{incident_id}/reject")
async def reject_incident(request: Request, incident_id: str, payload: JsonPayload) -> Any:
    session = authenticate(request, request.url.path, write=True)
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
    record_user_audit(
        session,
        "approval.rejected.by_user",
        "用户拒绝事件动作",
        {"incident_id": incident_id, "approval_id": approval.id},
        "incident",
        incident_id,
        request.headers.get("X-Correlation-ID"),
    )
    return encoded_response(200, {"approval": approval.__dict__, "state": SYSTEM.snapshot()})


@app.post("/api/v1/incidents/{incident_id}/assign")
async def assign_incident(request: Request, incident_id: str, payload: JsonPayload) -> Any:
    session = authenticate(request, request.url.path, write=True)
    incident = SYSTEM.store.incidents.get(incident_id)
    if incident is None:
        return problem(404, "Incident not found")
    assignee = str(payload.model_dump().get("assignee") or "").strip()
    if not assignee:
        return problem(400, "Invalid assignee", "assignee is required")
    incident.assignee = assignee
    SYSTEM.store.emit("incident.assigned", "事件已分派给 %s" % assignee, {"incident_id": incident_id, "assignee": assignee})
    record_user_audit(
        session,
        "incident.assigned.by_user",
        "用户分派事件",
        {"incident_id": incident_id, "assignee": assignee},
        "incident",
        incident_id,
        request.headers.get("X-Correlation-ID"),
    )
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

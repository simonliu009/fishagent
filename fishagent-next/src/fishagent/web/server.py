import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from fishagent.agent_runtime.crewai_runtime import CrewAIOrchestrator
from fishagent.application.agent_service import FishAgentSystem
from fishagent.core import AppConfig, LLMConfig, RuntimeConfigStore, new_llm_profile_id
from fishagent.domain.models import RiskLevel, ScheduleStatus
from fishagent.infrastructure.auth import auth_from_config
from fishagent.infrastructure.gateways import mqtt_gateway_from_config
from fishagent.infrastructure.mqtt import MqttTelemetryAdapter, MqttTelemetryPublisher
from fishagent.infrastructure.object_store import object_store_from_config
from fishagent.infrastructure.persistence import PersistenceError, repository_from_config
from fishagent.infrastructure.realtime import publisher_from_config

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


def ingest_mqtt_and_persist(**payload):
    defer_persist = bool(payload.pop("defer_persist", False)) and str(payload.get("source_event_id", "")).startswith(
        "demo-seed-"
    )
    result = SYSTEM.ingest_reading(**payload)
    if not defer_persist:
        SYSTEM.snapshot()
    return result


def json_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    payload: dict,
    headers: Optional[dict[str, str]] = None,
) -> None:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(data)


def html_response(handler: BaseHTTPRequestHandler, html: str) -> None:
    data = html.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def svg_response(handler: BaseHTTPRequestHandler, svg: str) -> None:
    data = svg.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "image/svg+xml; charset=utf-8")
    handler.send_header("Cache-Control", "public, max-age=3600")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(length).decode("utf-8") if length else "{}"
    return json.loads(raw or "{}")


def problem(handler: BaseHTTPRequestHandler, status: int, title: str, detail: str = "") -> None:
    json_response(handler, status, {"type": "about:blank", "title": title, "status": status, "detail": detail})


def asset_collection(name: str) -> list:
    return SYSTEM.snapshot().get(name, [])


def create_asset(name: str, payload: dict):
    creators = {
        "farms": SYSTEM.create_farm,
        "ponds": SYSTEM.create_pond,
        "sensors": SYSTEM.create_sensor,
        "devices": SYSTEM.create_device,
        "cameras": SYSTEM.create_camera,
    }
    return creators[name](payload)


def state_item(collection: str, item_id: str) -> dict | None:
    return next((item for item in SYSTEM.snapshot().get(collection, []) if item.get("id") == item_id), None)


def parse_risk(value: object) -> RiskLevel:
    try:
        return RiskLevel(str(value or "L2").upper())
    except ValueError as exc:
        raise ValueError("risk must be L0, L1, L2 or L3") from exc


def auth_roles_for_path(path: str) -> set[str]:
    if "/approve" in path or "/reject" in path or path.startswith("/api/v1/config"):
        return {"admin", "manager"}
    if path.startswith("/api/v1/farms") or path.startswith("/api/v1/ponds") or path.startswith("/api/v1/sensors"):
        return {"admin", "manager"}
    if path.startswith("/api/v1/devices") or path.startswith("/api/v1/cameras"):
        return {"admin", "manager"}
    return {"admin", "manager", "operator", "viewer"}


def authorize(handler: BaseHTTPRequestHandler, path: str, write: bool = False) -> bool:
    if not AUTH.enabled:
        handler.auth_session = AUTH.authenticate("")  # type: ignore[attr-defined]
        return True
    session = AUTH.authenticate(handler.headers.get("Cookie", ""))
    if session is None:
        problem(handler, 401, "Authentication required", "请先登录")
        return False
    if session.role not in auth_roles_for_path(path):
        problem(handler, 403, "Forbidden", "当前角色没有此操作权限")
        return False
    if write and handler.headers.get("X-CSRF-Token") != session.csrf_token:
        problem(handler, 403, "CSRF validation failed", "缺少有效 CSRF Token")
        return False
    handler.auth_session = session  # type: ignore[attr-defined]
    return True


def test_llm_connection(config: Optional[LLMConfig] = None) -> dict:
    llm = config or CONFIG.llm
    if not llm.has_api_key():
        raise ValueError("API Key 未配置")
    url = llm.base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps(
        {
            "model": llm.model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 8,
            "temperature": 0,
        }
    ).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={
            "Authorization": "Bearer %s" % llm.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "HTTP-Referer": "http://localhost:3000",
            "X-Title": "FishAgent",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=8) as response:
            body = json.loads(response.read().decode("utf-8"))
            return {
                "ok": 200 <= response.status < 300 and bool(body.get("choices")),
                "status_code": response.status,
                "endpoint": url,
                "model": body.get("model") or llm.model,
            }
    except HTTPError as exc:
        return {"ok": False, "status_code": exc.code, "endpoint": url, "detail": "模型服务拒绝了连接"}
    except URLError as exc:
        return {"ok": False, "status_code": 0, "endpoint": url, "detail": "无法连接模型服务：%s" % exc.reason}


def _legacy_page() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>智渔 Agent 控制台</title>
  <link rel="icon" href="/static/fish.svg">
  <style>
    :root {
      --bg: #eef3f2;
      --panel: #ffffff;
      --ink: #172024;
      --muted: #66757d;
      --line: #d8e0e3;
      --brand: #0f766e;
      --brand-strong: #0b3b3a;
      --accent: #2563eb;
      --soft: #e7f6f2;
      --warn: #b45309;
      --bad: #b91c1c;
      --ok: #15803d;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Microsoft YaHei", sans-serif; color: var(--ink); background: var(--bg); overflow-x: hidden; }
    header { min-height: 86px; padding: 18px 28px; background: var(--brand-strong); color: white; display: flex; align-items: center; justify-content: space-between; gap: 16px; border-bottom: 4px solid #38bdf8; }
    .brand { display: flex; align-items: center; gap: 12px; min-width: 260px; }
    .brand img { width: 42px; height: 42px; border-radius: 10px; box-shadow: 0 8px 22px rgba(0,0,0,.22); }
    h1 { margin: 0; font-size: 22px; letter-spacing: 0; }
    .topline { color: #c8f7ef; font-size: 13px; margin-top: 4px; }
    .header-metrics { display: grid; grid-template-columns: repeat(4, minmax(110px, 1fr)); gap: 10px; width: min(560px, 100%); }
    .header-metric { border: 1px solid rgba(255,255,255,.22); background: rgba(255,255,255,.08); border-radius: 8px; padding: 10px; }
    .header-metric b { display:block; font-size: 18px; }
    .header-metric span { color:#cde8e5; font-size: 12px; }
    main { padding: 22px; max-width: 1440px; margin: 0 auto; min-width: 0; }
    .grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(360px, .8fr); gap: 16px; align-items: start; }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; box-shadow: 0 10px 28px rgba(15, 23, 42, .06); min-width: 0; }
    .panel-title { display:flex; align-items:center; justify-content:space-between; gap: 10px; margin-bottom: 12px; }
    h2 { font-size: 16px; margin: 0; }
    h3 { font-size: 14px; margin: 16px 0 10px; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    button { border: 0; border-radius: 6px; padding: 9px 12px; background: var(--brand); color: white; cursor: pointer; font-weight: 600; }
    button.secondary { background: #40535c; }
    button.blue { background: var(--accent); }
    button.warn { background: var(--warn); }
    button:focus, input:focus, select:focus { outline: 2px solid #38bdf8; outline-offset: 2px; }
    label { display: block; font-size: 12px; color: var(--muted); margin-top: 8px; }
    input, select { width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 9px; margin-top: 4px; }
    .form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .form-grid .wide { grid-column: 1 / -1; }
    .cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .card { border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #fbfcfc; min-height: 96px; }
    .metric { font-size: 24px; font-weight: 700; margin-top: 8px; }
    .muted { color: var(--muted); font-size: 13px; }
    .status { display: inline-block; padding: 3px 8px; border-radius: 999px; background: #e7f6f2; color: var(--brand); font-size: 12px; font-weight: 700; }
    .status.warn { background: #fef3c7; color: #92400e; }
    .status.badge-blue { background: #dbeafe; color: #1d4ed8; }
    .bad { color: var(--bad); }
    .ok { color: var(--ok); }
    .timeline { display: grid; gap: 8px; max-height: 520px; overflow: auto; }
    .event { border-left: 3px solid var(--brand); padding: 8px 10px; background: #fbfcfc; border-radius: 4px; }
    .asset-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }
    .asset-tile { border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: #fbfcfc; min-height: 92px; }
    .asset-tile b { display:block; font-size: 13px; margin-bottom: 6px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    #schedules, #asset_tables, #work_queue { max-width: 100%; overflow-x: auto; }
    #schedules table, #asset_tables table, #work_queue table { min-width: 560px; }
    th, td { padding: 9px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: 12px; background: #f8faf9; }
    .split { display: grid; grid-template-columns: .95fr 1.05fr; gap: 14px; align-items: start; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; background: #111827; color: #e5e7eb; padding: 12px; border-radius: 8px; max-height: 360px; overflow: auto; }
    @media (max-width: 860px) {
      header { align-items: flex-start; flex-direction: column; }
      main { padding: 12px; }
      .grid, .cards, .asset-grid, .split, .form-grid, .header-metrics { grid-template-columns: 1fr; }
      .panel { padding: 12px; }
      .header-metrics { width: 100%; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <img src="/static/fish.svg" alt="FishAgent">
      <div>
        <h1>智渔 Agent 控制台</h1>
        <div class="topline">实时运营控制台 · Nginx 入口 3001 · 应用服务 3000 · 安全策略门开启</div>
      </div>
    </div>
    <div class="header-metrics" id="top_metrics"></div>
  </header>
  <main>
    <section class="panel" id="login_panel" style="display:none; margin-bottom:16px">
      <div class="panel-title"><h2>登录</h2><span class="status warn">需要身份验证</span></div>
      <div class="form-grid">
        <label>用户名<input id="auth_username" value="admin" autocomplete="username"></label>
        <label>密码<input id="auth_password" type="password" autocomplete="current-password"></label>
      </div>
      <div class="actions" style="margin-top:10px"><button class="blue" onclick="login()">登录</button></div>
      <div id="auth_message" class="muted" style="margin-top:8px"></div>
    </section>
    <div class="grid">
      <section class="panel">
        <div class="panel-title"><h2>运营总览</h2><span class="status badge-blue">实时状态</span></div>
        <div class="cards" id="cards"></div>
        <h3>B-01 闭环演示</h3>
        <div class="actions">
          <button class="secondary" onclick="initDemo()">初始化演示资产</button>
          <button onclick="demo('success')">成功闭环</button>
          <button onclick="demo('failure')">复核失败升级</button>
          <button onclick="demo('dedup')">防重复动作</button>
          <button class="warn" onclick="demo('approval')">L2 审批演示</button>
          <button class="secondary" onclick="refresh()">刷新</button>
        </div>
        <h3>资产管理</h3>
        <div class="split">
          <div>
            <div class="form-grid">
              <label>资产类型<select id="asset_type" onchange="syncAssetForm()"><option value="farms">养殖场</option><option value="ponds">池塘</option><option value="sensors">传感器</option><option value="devices">设备</option><option value="cameras">摄像头</option></select></label>
              <label>名称<input id="asset_name" placeholder="例如：B-02 精养池"></label>
              <label>ID<input id="asset_id" placeholder="留空自动生成"></label>
              <label id="asset_farm_wrap">所属养殖场<input id="asset_farm_id" placeholder="farm-demo"></label>
              <label id="asset_pond_wrap">所属池塘<input id="asset_pond_id" placeholder="B-01"></label>
              <label id="asset_species_wrap">养殖品种<input id="asset_species" placeholder="加州鲈"></label>
              <label id="asset_metric_wrap">指标/能力<input id="asset_metric" placeholder="DO 或 aeration"></label>
              <label id="asset_unit_wrap">单位/来源<input id="asset_unit" placeholder="mg/L 或 HTTP_SNAPSHOT"></label>
              <label id="asset_source_url_wrap" class="wide">摄像头来源地址<input id="asset_source_url" placeholder="http://camera.local/snapshot.jpg 或 rtsp://..."></label>
              <label id="asset_state_wrap">状态<input id="asset_state" placeholder="ONLINE / off / UNAVAILABLE"></label>
            </div>
            <div class="actions" style="margin-top:10px"><button class="blue" onclick="createAsset()">创建资产</button></div>
            <div id="asset_message" class="muted" style="margin-top:8px"></div>
          </div>
          <div class="asset-grid" id="asset_tiles"></div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-title"><h2>事件轨迹</h2><span class="status">Outbox 模拟</span></div>
        <div id="events" class="timeline"></div>
      </section>
    </div>
    <section class="panel" style="margin-top:16px">
      <div class="panel-title"><h2>Agent 控制室</h2><span class="status warn">确定性 Flow + 多 Agent 轨迹</span></div>
      <div class="form-grid">
        <label class="wide">用户目标<input id="agent_goal" value="巡查全场" placeholder="例如：巡查全场或检查 B-01 溶氧"></label>
        <label>指定池塘（可选）<input id="agent_pond_id" placeholder="B-01"></label>
      </div>
      <div class="actions" style="margin:10px 0"><button class="blue" onclick="runGoal()">提交 Agent 目标</button></div>
      <div id="agent_message" class="muted" style="margin-bottom:10px"></div>
      <div id="runs"></div>
    </section>
    <section class="panel" style="margin-top:16px">
      <div class="panel-title"><h2>审批与人工任务</h2><span class="status warn">L2 必须审批 · L3 仅人工</span></div>
      <div id="work_queue"></div>
    </section>
    <section class="panel" style="margin-top:16px">
      <div class="panel-title"><h2>调度中心</h2><span class="status badge-blue">到期复核 / 全场巡查</span></div>
      <div class="form-grid">
        <label>调度名称<input id="schedule_name" value="全场巡查"></label>
        <label>周期（秒）<input id="schedule_interval" type="number" min="5" value="300"></label>
      </div>
      <div class="actions" style="margin-top:10px">
        <button class="blue" onclick="createSchedule()">创建调度</button>
        <button class="secondary" onclick="dispatchJobs()">立即处理到期作业</button>
      </div>
      <div id="schedules" style="margin-top:12px"></div>
    </section>
    <section class="panel" style="margin-top:16px">
      <div class="panel-title"><h2>资产台账</h2><span class="status badge-blue">Farm / Pond / Sensor / Device / Camera</span></div>
      <div id="asset_tables"></div>
    </section>
    <section class="panel" style="margin-top:16px">
      <div class="panel-title"><h2>大模型 API 配置</h2><span class="status">OpenAI-compatible</span></div>
      <div class="muted">启用后由 CrewAI/大模型负责研判和动作建议；设备指令通过 MQTT 发布，模型不可用时转人工。</div>
      <div class="form-grid">
        <label>提供商<select id="llm_provider"><option value="zai">Z.ai</option><option value="openai">OpenAI</option><option value="compatible">OpenAI-compatible</option></select></label>
        <label>模型<input id="llm_model"></label>
        <label class="wide">Base URL<input id="llm_base_url"></label>
        <label>API Key<input id="llm_api_key" type="password" placeholder="留空则保持当前密钥"></label>
        <label><input id="llm_enabled" type="checkbox" style="width:auto"> 启用模型调用</label>
      </div>
      <div class="actions" style="margin-top:10px"><button onclick="saveModelConfig()">保存模型配置</button><button class="secondary" onclick="testModelConfig()">测试连接</button></div>
      <div id="llm_status" class="muted" style="margin-top:8px"></div>
    </section>
    <section class="panel" style="margin-top:16px">
      <div class="panel-title"><h2>状态快照</h2><span class="status">JSON</span></div>
      <pre id="raw"></pre>
    </section>
  </main>
<script>
let csrfToken = '';
async function api(path, options) {
  const request = options || {};
  request.headers = Object.assign({}, request.headers || {});
  if (csrfToken && request.method && request.method !== 'GET') request.headers['X-CSRF-Token'] = csrfToken;
  const res = await fetch(path, request);
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}
async function login() {
  try {
    const result = await api('/api/v1/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({
      username: document.getElementById('auth_username').value,
      password: document.getElementById('auth_password').value
    })});
    csrfToken = result.csrf_token;
    document.getElementById('login_panel').style.display = 'none';
    await refresh();
  } catch (err) {
    document.getElementById('auth_message').textContent = '登录失败：' + err.message;
  }
}
async function demo(mode) {
  await api('/api/v1/demo/' + mode, {method:'POST'});
  await refresh();
}
async function initDemo() {
  await api('/api/v1/demo/init', {method:'POST'});
  await refresh();
}
async function createAsset() {
  const type = document.getElementById('asset_type').value;
  const name = document.getElementById('asset_name').value;
  const id = document.getElementById('asset_id').value;
  const farmId = document.getElementById('asset_farm_id').value;
  const pondId = document.getElementById('asset_pond_id').value;
  const species = document.getElementById('asset_species').value;
  const metric = document.getElementById('asset_metric').value;
  const unit = document.getElementById('asset_unit').value;
  const sourceUrl = document.getElementById('asset_source_url').value;
  const state = document.getElementById('asset_state').value;
  const payload = {id, name};
  if (type === 'farms') payload.location = state;
  if (type === 'ponds') Object.assign(payload, {farm_id: farmId, species, dissolved_oxygen_min: Number(metric || 4)});
  if (type === 'sensors') Object.assign(payload, {pond_id: pondId, metric: metric || 'DO', unit: unit || 'mg/L', status: state || 'ONLINE'});
  if (type === 'devices') Object.assign(payload, {pond_id: pondId, capability: metric || 'aeration', shadow_state: state || 'off'});
  if (type === 'cameras') Object.assign(payload, {pond_id: pondId, source_type: unit || 'HTTP_SNAPSHOT', source_url: sourceUrl, status: state || 'UNAVAILABLE'});
  try {
    await api('/api/v1/' + type, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    document.getElementById('asset_message').textContent = '资产已创建：' + (name || id || type);
    ['asset_name','asset_id','asset_metric','asset_unit','asset_source_url','asset_state'].forEach(k => document.getElementById(k).value = '');
    await refresh();
  } catch (err) {
    document.getElementById('asset_message').textContent = '创建失败：' + err.message;
  }
}
function syncAssetForm() {
  const type = document.getElementById('asset_type').value;
  document.getElementById('asset_farm_wrap').style.display = type === 'ponds' ? 'block' : 'none';
  document.getElementById('asset_pond_wrap').style.display = ['sensors','devices','cameras'].includes(type) ? 'block' : 'none';
  document.getElementById('asset_species_wrap').style.display = type === 'ponds' ? 'block' : 'none';
  document.getElementById('asset_metric_wrap').style.display = ['ponds','sensors','devices'].includes(type) ? 'block' : 'none';
  document.getElementById('asset_unit_wrap').style.display = ['sensors','cameras'].includes(type) ? 'block' : 'none';
  document.getElementById('asset_source_url_wrap').style.display = type === 'cameras' ? 'block' : 'none';
  document.getElementById('asset_state_wrap').style.display = ['farms','sensors','devices','cameras'].includes(type) ? 'block' : 'none';
  const labels = {
    farms: ['地址', '例如：浙江湖州'],
    ponds: ['溶氧安全线', '4.0'],
    sensors: ['指标', 'DO'],
    devices: ['能力', 'aeration'],
    cameras: ['来源类型', 'HTTP_SNAPSHOT']
  };
  document.getElementById('asset_metric').placeholder = labels[type][1];
}
async function saveModelConfig() {
  const payload = {
    provider: document.getElementById('llm_provider').value,
    base_url: document.getElementById('llm_base_url').value,
    model: document.getElementById('llm_model').value,
    api_key: document.getElementById('llm_api_key').value,
    enabled: document.getElementById('llm_enabled').checked
  };
  const data = await api('/api/v1/config/llm', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  renderLLM(data.llm);
}
async function testModelConfig() {
  const status = await api('/api/v1/config/llm/test', {method:'POST'});
  document.getElementById('llm_status').textContent = status.ok ? '连接成功：HTTP ' + status.status_code : '连接失败：' + (status.detail || ('HTTP ' + status.status_code));
}
async function approveProposal(proposalId) {
  await api('/api/v1/action-proposals/' + proposalId + '/approve', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({approver:'现场负责人', reason:'控制台确认执行'})
  });
  await refresh();
}
async function rejectProposal(proposalId) {
  await api('/api/v1/action-proposals/' + proposalId + '/reject', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({approver:'现场负责人', reason:'控制台拒绝'})
  });
  await refresh();
}
async function completeTask(taskId) {
  await api('/api/v1/manual-tasks/' + taskId + '/complete', {method:'POST'});
  await refresh();
}
async function createSchedule() {
  await api('/api/v1/schedules', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      name: document.getElementById('schedule_name').value,
      interval_seconds: Number(document.getElementById('schedule_interval').value),
      job_type: 'patrol'
    })
  });
  await refresh();
}
async function dispatchJobs() {
  await api('/api/v1/scheduled-jobs:dispatch', {method:'POST'});
  await refresh();
}
async function runGoal() {
  const goal = document.getElementById('agent_goal').value;
  const pondId = document.getElementById('agent_pond_id').value;
  try {
    const data = await api('/api/v1/agent-runs', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({goal: goal, pond_id: pondId || null})
    });
    document.getElementById('agent_message').textContent = '目标已完成：' + data.run.stop_reason;
    await refresh();
  } catch (err) {
    document.getElementById('agent_message').textContent = '目标提交失败：' + err.message;
  }
}
async function scheduleAction(scheduleId, action) {
  await api('/api/v1/schedules/' + scheduleId + '/' + action, {method:'POST'});
  await refresh();
}
function renderLLM(llm) {
  document.getElementById('llm_provider').value = llm.provider;
  document.getElementById('llm_base_url').value = llm.base_url;
  document.getElementById('llm_model').value = llm.model;
  document.getElementById('llm_enabled').checked = llm.enabled;
  document.getElementById('llm_status').textContent = '状态：' + (llm.enabled ? '已启用' : '未启用') + '，密钥：' + (llm.api_key_configured ? llm.api_key_preview : '未配置');
}
function render(data) {
  const incidents = data.incidents || [];
  const devices = data.devices || [];
  const ponds = data.ponds || [];
  const sensors = data.sensors || [];
  const cameras = data.cameras || [];
  const approvals = data.approvals || [];
  const tasks = data.manual_tasks || [];
  const latest = incidents[incidents.length - 1];
  document.getElementById('top_metrics').innerHTML = [
    ['池塘', ponds.length],
    ['活动事件', incidents.filter(i => !['RESOLVED','ESCALATED','DISMISSED'].includes(i.status)).length],
    ['待复核', incidents.filter(i => i.status === 'VERIFY_PENDING').length],
    ['待审批', approvals.filter(a => a.status === 'PENDING').length]
  ].map(x => '<div class="header-metric"><b>'+x[1]+'</b><span>'+x[0]+'</span></div>').join('');
  document.getElementById('cards').innerHTML = [
    ['养殖场', (data.farms || []).length, '资产'],
    ['池塘', ponds.length, latest ? latest.status : '无事件'],
    ['传感器', sensors.length, sensors.filter(s => s.status !== 'ONLINE').length ? '有离线' : '在线'],
    ['设备', devices.length, devices.map(d => d.shadow_state).join(' / ') || '待配置']
  ].map(x => '<div class="card"><div class="muted">'+x[0]+'</div><div class="metric">'+x[1]+'</div><span class="status">'+x[2]+'</span></div>').join('');
  document.getElementById('asset_tiles').innerHTML = [
    ['养殖场', data.farms || []],
    ['池塘', ponds],
    ['传感器', sensors],
    ['设备', devices],
    ['摄像头', cameras]
  ].map(x => '<div class="asset-tile"><b>'+x[0]+'</b><div class="metric">'+x[1].length+'</div><div class="muted">'+(x[1][0]?.name || '暂无')+'</div></div>').join('');
  document.getElementById('events').innerHTML = (data.events || []).slice().reverse().map(e => '<div class="event"><b>#'+e.sequence+' '+e.event_type+'</b><div>'+e.summary+'</div><div class="muted">'+e.occurred_at+'</div></div>').join('');
  document.getElementById('runs').innerHTML = (data.agent_runs || []).slice().reverse().map(r => '<div class="card" style="margin-bottom:8px"><b>'+r.goal+'</b> <span class="status">'+r.status+'</span><div class="muted">停止原因：'+(r.stop_reason || '-')+' · 委派：'+r.delegated_agents.join(' → ')+'</div><ol>'+r.steps.map(s => '<li>'+s.agent+' / '+s.action+'：'+s.summary+'</li>').join('')+'</ol></div>').join('');
  renderWorkQueue(data);
  renderSchedules(data);
  renderAssets(data);
  document.getElementById('raw').textContent = JSON.stringify(data, null, 2);
}
function renderWorkQueue(data) {
  const proposals = data.action_proposals || [];
  const approvals = data.approvals || [];
  const tasks = data.manual_tasks || [];
  const pending = approvals.filter(a => a.status === 'PENDING').map(a => {
    const proposal = proposals.find(p => p.id === a.proposal_id) || {};
    return '<div class="card" style="margin-bottom:8px"><b>审批：'+(proposal.rationale || proposal.id)+'</b><span class="status warn">风险 '+(proposal.risk || '?')+'</span><div class="muted">池塘：'+(proposal.pond_id || '-')+' · 设备：'+(proposal.device_id || '-')+'</div><div class="actions" style="margin-top:8px"><button data-id="'+proposal.id+'" onclick="approveProposal(this.dataset.id)">批准执行</button><button class="secondary" data-id="'+proposal.id+'" onclick="rejectProposal(this.dataset.id)">拒绝</button></div></div>';
  }).join('');
  const openTasks = tasks.filter(t => t.status !== 'COMPLETED' && t.status !== 'CANCELLED').map(t => '<div class="card" style="margin-bottom:8px"><b>'+t.title+'</b><span class="status warn">'+t.priority+'</span><div>'+t.description+'</div><div class="muted">负责人：'+t.assignee+' · '+t.status+'</div><div class="actions" style="margin-top:8px"><button class="blue" data-id="'+t.id+'" onclick="completeTask(this.dataset.id)">标记完成</button></div></div>').join('');
  document.getElementById('work_queue').innerHTML = (pending || openTasks) ? (pending + openTasks) : '<div class="muted">当前没有待审批或人工任务</div>';
}
function renderSchedules(data) {
  const schedules = data.schedules || [];
  const jobs = data.scheduled_jobs || [];
  if (!schedules.length && !jobs.length) {
    document.getElementById('schedules').innerHTML = '<div class="muted">暂无调度。创建后将保留周期定义和每次作业状态。</div>';
    return;
  }
  document.getElementById('schedules').innerHTML = renderTable('调度定义', schedules, [['id','ID'],['name','名称'],['job_type','类型'],['interval_seconds','周期秒'],['status','状态'],['next_run_at','下次运行']]) +
    '<h3>最近作业</h3>' + (jobs.length ? '<table><thead><tr><th>ID</th><th>类型</th><th>状态</th><th>到期时间</th><th>尝试</th></tr></thead><tbody>'+jobs.slice().reverse().slice(0,10).map(j => '<tr><td>'+j.id+'</td><td>'+j.job_type+'</td><td>'+j.status+'</td><td>'+j.due_at+'</td><td>'+j.attempts+'</td></tr>').join('')+'</tbody></table>' : '<div class="muted">暂无作业</div>');
}
function renderTable(title, rows, columns) {
  if (!rows.length) return '<h3>'+title+'</h3><div class="muted">暂无数据</div>';
  return '<h3>'+title+'</h3><table><thead><tr>'+columns.map(c => '<th>'+c[1]+'</th>').join('')+'</tr></thead><tbody>' +
    rows.map(row => '<tr>'+columns.map(c => '<td>'+(row[c[0]] ?? '-')+'</td>').join('')+'</tr>').join('') + '</tbody></table>';
}
function renderAssets(data) {
  document.getElementById('asset_tables').innerHTML = [
    renderTable('养殖场', data.farms || [], [['id','ID'],['name','名称'],['location','位置']]),
    renderTable('池塘', data.ponds || [], [['id','ID'],['farm_id','养殖场'],['name','名称'],['species','品种'],['dissolved_oxygen_min','DO 下限']]),
    renderTable('传感器', data.sensors || [], [['id','ID'],['pond_id','池塘'],['name','名称'],['metric','指标'],['unit','单位'],['status','状态']]),
    renderTable('设备', data.devices || [], [['id','ID'],['pond_id','池塘'],['name','名称'],['capability','能力'],['shadow_state','影子状态'],['healthy','健康']]),
    renderTable('摄像头', data.cameras || [], [['id','ID'],['pond_id','池塘'],['name','名称'],['source_type','来源'],['status','状态']])
  ].join('');
}
async function refresh() {
  try {
    const state = await api('/api/v1/state');
    render(state);
    const cfg = await api('/api/v1/config/llm');
    renderLLM(cfg.llm);
  } catch (err) {
    if (String(err.message).includes('401')) document.getElementById('login_panel').style.display = 'block';
  }
}
api('/api/v1/auth/config').then(config => { if (config.enabled) document.getElementById('login_panel').style.display = 'block'; }).finally(refresh);
syncAssetForm();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


def page() -> str:
    return (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/api/v1/auth/config", "/auth/config"}:
            json_response(self, 200, {"enabled": AUTH.enabled})
            return
        if path in {"/me", "/api/v1/me"}:
            if not authorize(self, path):
                return
            session = getattr(self, "auth_session")
            json_response(self, 200, {"username": session.username, "role": session.role})
            return
        if path not in {"/", "/static/fish.svg", "/health/live", "/health/ready"} and not authorize(self, path):
            return
        if path == "/":
            html_response(self, page())
        elif path == "/static/fish.svg":
            svg_response(self, (STATIC_DIR / "fish.svg").read_text(encoding="utf-8"))
        elif path == "/health/live":
            json_response(self, 200, {"status": "ok", "port": CONFIG.port})
        elif path == "/health/ready":
            if AUTH.enabled and not CONFIG.auth_password:
                json_response(
                    self,
                    503,
                    {
                        "status": "not_ready",
                        "port": CONFIG.port,
                        "detail": "FISHAGENT_ADMIN_PASSWORD must be set when authentication is enabled",
                    },
                )
                return
            try:
                persistence = REPOSITORY.health() if REPOSITORY else {"status": "ok", "backend": "memory"}
            except PersistenceError as exc:
                json_response(
                    self,
                    503,
                    {"status": "not_ready", "port": CONFIG.port, "backend": "postgres", "detail": str(exc)},
                )
                return
            realtime = EVENT_PUBLISHER.health() if EVENT_PUBLISHER else {"status": "disabled", "backend": "redis"}
            media = OBJECT_STORE.health() if OBJECT_STORE else {"status": "disabled", "backend": "minio"}
            json_response(
                self,
                200,
                {"status": "ok", "port": CONFIG.port, "persistence": persistence, "realtime": realtime, "media": media},
            )
        elif path == "/api/v1/state":
            json_response(self, 200, SYSTEM.snapshot())
        elif path == "/api/v1/config/llm":
            json_response(
                self,
                200,
                {"llm": CONFIG.llm.public_dict(), "profiles": [profile.public_dict() for profile in LLM_PROFILES]},
            )
        elif path in {"/api/v1/farms", "/api/v1/ponds", "/api/v1/sensors", "/api/v1/devices", "/api/v1/cameras"}:
            json_response(self, 200, {path.rsplit("/", 1)[-1]: asset_collection(path.rsplit("/", 1)[-1])})
        elif path == "/api/v1/incidents":
            json_response(self, 200, {"incidents": SYSTEM.snapshot()["incidents"]})
        elif path == "/api/v1/action-proposals":
            json_response(self, 200, {"action_proposals": SYSTEM.snapshot()["action_proposals"]})
        elif path == "/api/v1/approvals":
            json_response(self, 200, {"approvals": SYSTEM.snapshot()["approvals"]})
        elif path == "/api/v1/manual-tasks":
            json_response(self, 200, {"manual_tasks": SYSTEM.snapshot()["manual_tasks"]})
        elif path == "/api/v1/schedules":
            json_response(self, 200, {"schedules": SYSTEM.snapshot()["schedules"]})
        elif path == "/api/v1/scheduled-jobs":
            json_response(self, 200, {"scheduled_jobs": SYSTEM.snapshot()["scheduled_jobs"]})
        elif path == "/api/v1/telemetry/snapshot":
            readings = SYSTEM.snapshot()["readings"]
            latest = {}
            for reading in readings:
                latest["%s:%s" % (reading["pond_id"], reading["metric"])] = reading
            json_response(self, 200, {"readings": list(latest.values())})
        elif path == "/api/v1/telemetry/series":
            query = parse_qs(urlparse(self.path).query)
            pond_id = query.get("pond_id", [""])[0]
            metric = query.get("metric", ["DO"])[0]
            limit = min(int(query.get("limit", ["100"])[0]), 1000)
            readings = [
                reading
                for reading in SYSTEM.snapshot()["readings"]
                if reading["pond_id"] == pond_id and reading["metric"] == metric
            ]
            json_response(self, 200, {"readings": readings[-limit:]})
        elif path == "/api/v1/events":
            query = parse_qs(urlparse(self.path).query)
            after = int(query.get("after", ["0"])[0])
            json_response(self, 200, {"events": [e for e in SYSTEM.store.events if e["sequence"] > after]})
        elif path.startswith("/api/v1/incidents/") and path.count("/") == 4:
            incident = state_item("incidents", path.rsplit("/", 1)[-1])
            if incident is None:
                problem(self, 404, "Incident not found")
            else:
                json_response(self, 200, {"incident": incident})
        elif path.startswith("/api/v1/agent-runs"):
            json_response(self, 200, {"agent_runs": SYSTEM.snapshot()["agent_runs"]})
        else:
            problem(self, 404, "Not Found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path in {"/auth/login", "/api/v1/auth/login"}:
            payload = read_json_body(self)
            session = AUTH.login(str(payload.get("username") or ""), str(payload.get("password") or ""))
            if session is None:
                problem(self, 401, "Invalid credentials", "用户名或密码错误")
                return
            json_response(
                self,
                200,
                {"user": {"username": session.username, "role": session.role}, "csrf_token": session.csrf_token},
                headers={
                    "Set-Cookie": "fishagent_session=%s; HttpOnly; Path=/; SameSite=Strict" % session.token,
                },
            )
            return
        if path in {"/auth/logout", "/api/v1/auth/logout"}:
            AUTH.logout(self.headers.get("Cookie", ""))
            json_response(
                self,
                200,
                {"logged_out": True},
                headers={"Set-Cookie": "fishagent_session=; Max-Age=0; HttpOnly; Path=/; SameSite=Strict"},
            )
            return
        if not authorize(self, path, write=True):
            return
        if path.startswith("/api/v1/demo/"):
            mode = path.rsplit("/", 1)[-1]
            if mode == "init":
                json_response(self, 200, SYSTEM.initialize_demo())
                return
            if mode not in {"success", "failure", "dedup", "approval"}:
                json_response(self, 400, {"type": "bad_request", "title": "Unknown demo mode", "status": 400})
                return
            json_response(self, 200, SYSTEM.run_demo(mode))
            return
        if path == "/api/v1/config/llm/test":
            try:
                result = test_llm_connection()
            except ValueError as exc:
                problem(self, 400, "LLM connection test unavailable", str(exc))
                return
            json_response(self, 200 if result["ok"] else 502, result)
            return
        if path == "/api/v1/config/llm":
            payload = read_json_body(self)
            keep_existing_key = not payload.get("api_key")
            if keep_existing_key:
                payload.pop("api_key", None)
            requested_profile_id = str(payload.pop("profile_id", "") or "").strip()
            save_as_profile = bool(payload.pop("save_as_profile", False))
            if save_as_profile:
                name = str(payload.get("name", "") or "").strip()
                if not name:
                    problem(self, 400, "Invalid LLM provider", "自定义供应商必须填写名称")
                    return
                payload["profile_id"] = requested_profile_id if requested_profile_id and requested_profile_id != "__new__" else new_llm_profile_id()
            elif requested_profile_id:
                payload["profile_id"] = requested_profile_id
            CONFIG.llm.update_from_payload(payload)
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
            )
            CONFIG_STORE.save_llm(CONFIG.llm, LLM_PROFILES)
            SYSTEM.agent_orchestrator = CrewAIOrchestrator(SYSTEM, CONFIG.llm)
            json_response(
                self,
                200,
                {"llm": CONFIG.llm.public_dict(), "profiles": [profile.public_dict() for profile in LLM_PROFILES]},
            )
            return
        if path == "/api/v1/agent-runs":
            payload = read_json_body(self)
            try:
                run = SYSTEM.run_goal(str(payload.get("goal") or ""), payload.get("pond_id"))
            except ValueError as exc:
                problem(self, 400, "Invalid agent goal", str(exc))
                return
            snapshot_run = next(item for item in SYSTEM.snapshot()["agent_runs"] if item["id"] == run.id)
            json_response(self, 202, {"run": snapshot_run, "state": SYSTEM.snapshot()})
            return
        if path == "/api/v1/action-proposals":
            payload = read_json_body(self)
            try:
                proposal = SYSTEM.propose_action(
                    incident_id=str(payload.get("incident_id") or ""),
                    device_id=str(payload.get("device_id") or ""),
                    target_state=str(payload.get("target_state") or "on"),
                    risk=parse_risk(payload.get("risk")),
                    rationale=str(payload.get("rationale") or ""),
                )
            except (KeyError, ValueError) as exc:
                problem(self, 400, "Invalid action proposal", str(exc))
                return
            json_response(self, 201, {"proposal": state_item("action_proposals", proposal.id), "state": SYSTEM.snapshot()})
            return
        if path.startswith("/api/v1/action-proposals/"):
            parts = path.split("/")
            if len(parts) == 6 and parts[-1] in {"approve", "reject"}:
                proposal_id = parts[-2]
                payload = read_json_body(self)
                try:
                    if parts[-1] == "approve":
                        command = SYSTEM.approve_action(
                            proposal_id,
                            str(payload.get("approver") or "现场负责人"),
                            str(payload.get("reason") or ""),
                        )
                        json_response(self, 200, {"command": command.__dict__, "state": SYSTEM.snapshot()})
                    else:
                        approval = SYSTEM.reject_action(
                            proposal_id,
                            str(payload.get("approver") or "现场负责人"),
                            str(payload.get("reason") or "未提供原因"),
                        )
                        json_response(self, 200, {"approval": approval.__dict__, "state": SYSTEM.snapshot()})
                except (KeyError, ValueError) as exc:
                    problem(self, 409, "Action decision rejected", str(exc))
                return
        if path.startswith("/api/v1/incidents/"):
            parts = path.split("/")
            if len(parts) == 6 and parts[-1] in {"approve", "reject", "verify"}:
                payload = read_json_body(self)
                try:
                    if parts[-1] == "verify":
                        incident = SYSTEM.verify_incident(parts[-2])
                        json_response(self, 200, {"incident": state_item("incidents", incident.id), "state": SYSTEM.snapshot()})
                    else:
                        proposal_id = str(payload.get("proposal_id") or "")
                        if parts[-1] == "approve":
                            command = SYSTEM.approve_action(
                                proposal_id,
                                str(payload.get("approver") or "现场负责人"),
                                str(payload.get("reason") or ""),
                            )
                            json_response(self, 200, {"command": command.__dict__, "state": SYSTEM.snapshot()})
                        else:
                            approval = SYSTEM.reject_action(
                                proposal_id,
                                str(payload.get("approver") or "现场负责人"),
                                str(payload.get("reason") or "未提供原因"),
                            )
                            json_response(self, 200, {"approval": approval.__dict__, "state": SYSTEM.snapshot()})
                except (KeyError, ValueError) as exc:
                    problem(self, 409, "Incident action rejected", str(exc))
                return
        if path.startswith("/api/v1/manual-tasks/") and path.endswith("/complete"):
            task_id = path.split("/")[-2]
            try:
                task = SYSTEM.complete_manual_task(task_id)
            except KeyError as exc:
                problem(self, 404, "Manual task not found", str(exc))
                return
            json_response(self, 200, {"task": task.__dict__, "state": SYSTEM.snapshot()})
            return
        if path == "/api/v1/manual-tasks":
            payload = read_json_body(self)
            task = SYSTEM.create_manual_task(
                title=str(payload.get("title") or "现场处理任务"),
                description=str(payload.get("description") or ""),
                incident_id=payload.get("incident_id"),
                assignee=str(payload.get("assignee") or "现场操作员"),
                priority=str(payload.get("priority") or "HIGH"),
            )
            json_response(self, 201, {"task": task.__dict__, "state": SYSTEM.snapshot()})
            return
        if path == "/api/v1/schedules":
            try:
                schedule = SYSTEM.create_schedule(read_json_body(self))
            except ValueError as exc:
                problem(self, 400, "Invalid schedule", str(exc))
                return
            json_response(self, 201, {"schedule": schedule.__dict__, "state": SYSTEM.snapshot()})
            return
        if path.startswith("/api/v1/schedules/"):
            parts = path.split("/")
            if len(parts) == 6 and parts[-1] in {"pause", "resume", "run-now"}:
                schedule_id = parts[-2]
                try:
                    if parts[-1] == "run-now":
                        job = SYSTEM.run_schedule_now(schedule_id)
                        json_response(self, 202, {"job": job.__dict__, "state": SYSTEM.snapshot()})
                    else:
                        status = ScheduleStatus.PAUSED if parts[-1] == "pause" else ScheduleStatus.ACTIVE
                        schedule = SYSTEM.set_schedule_status(schedule_id, status)
                        json_response(self, 200, {"schedule": schedule.__dict__, "state": SYSTEM.snapshot()})
                except KeyError as exc:
                    problem(self, 404, "Schedule not found", str(exc))
                return
        if path == "/api/v1/scheduled-jobs:dispatch":
            jobs = SYSTEM.run_due_jobs()
            json_response(self, 200, {"completed_job_ids": [job.id for job in jobs], "state": SYSTEM.snapshot()})
            return
        if path in {"/api/v1/farms", "/api/v1/ponds", "/api/v1/sensors", "/api/v1/devices", "/api/v1/cameras"}:
            collection = path.rsplit("/", 1)[-1]
            try:
                asset = create_asset(collection, read_json_body(self))
            except ValueError as exc:
                problem(self, 400, "Invalid asset payload", str(exc))
                return
            json_response(self, 201, {"asset": asset.__dict__, "state": SYSTEM.snapshot()})
            return
        if path == "/api/v1/telemetry/readings:batch":
            payload = read_json_body(self)
            readings = payload.get("readings", [])
            accepted = 0
            incidents = []
            try:
                for item in readings:
                    new_incident = SYSTEM.ingest_reading(
                        pond_id=str(item.get("pond_id", "")),
                        value=float(item.get("value")),
                        metric=str(item.get("metric") or "DO"),
                        unit=str(item["unit"]) if item.get("unit") else None,
                        source_event_id=item.get("source_event_id"),
                        seconds_old=int(item.get("seconds_old", 0)),
                        sensor_id=item.get("sensor_id"),
                        quality=str(item.get("quality") or "GOOD"),
                    )
                    accepted += 1
                    if new_incident:
                        incidents.append(new_incident.id)
            except (TypeError, ValueError) as exc:
                problem(self, 400, "Invalid telemetry payload", str(exc))
                return
            json_response(self, 202, {"accepted": accepted, "incident_ids": incidents, "state": SYSTEM.snapshot()})
            return
        problem(self, 404, "Not Found")


def main() -> None:
    global MQTT_ADAPTER
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=CONFIG.host)
    parser.add_argument("--port", type=int, default=CONFIG.port)
    args = parser.parse_args()
    CONFIG.host = args.host
    CONFIG.port = args.port
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    if CONFIG.mqtt_enabled:
        MQTT_ADAPTER = MqttTelemetryAdapter(
            CONFIG.mqtt_host,
            CONFIG.mqtt_port,
            CONFIG.mqtt_topic,
            ingest_mqtt_and_persist,
        )
        MQTT_ADAPTER.start()
    scheduler_stop = threading.Event()

    def scheduler_loop() -> None:
        while not scheduler_stop.wait(5):
            try:
                SYSTEM.run_due_jobs()
            except Exception as exc:
                SYSTEM.store.emit("scheduler.error", "调度循环异常：%s" % exc)

    scheduler = threading.Thread(target=scheduler_loop, name="fishagent-scheduler", daemon=True)
    scheduler.start()
    print("FishAgent web listening on http://%s:%s" % (args.host, args.port))
    try:
        server.serve_forever()
    finally:
        scheduler_stop.set()
        scheduler.join(timeout=2)
        server.server_close()
        if MQTT_ADAPTER:
            MQTT_ADAPTER.stop()
        if DEVICE_GATEWAY:
            DEVICE_GATEWAY.close()
        if TELEMETRY_PUBLISHER:
            TELEMETRY_PUBLISHER.close()


if __name__ == "__main__":
    main()

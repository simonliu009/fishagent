import argparse
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from fishagent.application.agent_service import FishAgentSystem
from fishagent.core import AppConfig, RuntimeConfigStore


SYSTEM = FishAgentSystem()
CONFIG = AppConfig.from_env()
CONFIG_STORE = RuntimeConfigStore()
CONFIG.llm = CONFIG_STORE.load_llm(CONFIG.llm)
STATIC_DIR = Path(__file__).parent / "static"


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
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


def page() -> str:
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
    body { margin: 0; font-family: Arial, "Microsoft YaHei", sans-serif; color: var(--ink); background: var(--bg); }
    header { min-height: 86px; padding: 18px 28px; background: var(--brand-strong); color: white; display: flex; align-items: center; justify-content: space-between; gap: 16px; border-bottom: 4px solid #38bdf8; }
    .brand { display: flex; align-items: center; gap: 12px; min-width: 260px; }
    .brand img { width: 42px; height: 42px; border-radius: 10px; box-shadow: 0 8px 22px rgba(0,0,0,.22); }
    h1 { margin: 0; font-size: 22px; letter-spacing: 0; }
    .topline { color: #c8f7ef; font-size: 13px; margin-top: 4px; }
    .header-metrics { display: grid; grid-template-columns: repeat(3, minmax(120px, 1fr)); gap: 10px; width: min(560px, 100%); }
    .header-metric { border: 1px solid rgba(255,255,255,.22); background: rgba(255,255,255,.08); border-radius: 8px; padding: 10px; }
    .header-metric b { display:block; font-size: 18px; }
    .header-metric span { color:#cde8e5; font-size: 12px; }
    main { padding: 22px; max-width: 1440px; margin: 0 auto; }
    .grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(360px, .8fr); gap: 16px; align-items: start; }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; box-shadow: 0 10px 28px rgba(15, 23, 42, .06); }
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
    th, td { padding: 9px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: 12px; background: #f8faf9; }
    .split { display: grid; grid-template-columns: .95fr 1.05fr; gap: 14px; align-items: start; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; background: #111827; color: #e5e7eb; padding: 12px; border-radius: 8px; max-height: 360px; overflow: auto; }
    @media (max-width: 860px) {
      header { align-items: flex-start; flex-direction: column; }
      main { padding: 12px; }
      .grid, .cards, .asset-grid, .split, .form-grid, .header-metrics { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <img src="/static/fish.svg" alt="FishAgent">
      <div>
        <h1>智渔 Agent 控制台</h1>
        <div class="topline">模拟器模式 · 前端端口 3008 · 安全策略门开启</div>
      </div>
    </div>
    <div class="header-metrics" id="top_metrics"></div>
  </header>
  <main>
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
      <div id="runs"></div>
    </section>
    <section class="panel" style="margin-top:16px">
      <div class="panel-title"><h2>资产台账</h2><span class="status badge-blue">Farm / Pond / Sensor / Device / Camera</span></div>
      <div id="asset_tables"></div>
    </section>
    <section class="panel" style="margin-top:16px">
      <div class="panel-title"><h2>大模型 API 配置</h2><span class="status">OpenAI-compatible</span></div>
      <div class="muted">用于后续 CrewAI/OpenAI-compatible 接入；当前垂直切片仍由确定性 Agent 运行。</div>
      <div class="form-grid">
        <label>提供商<select id="llm_provider"><option value="zai">Z.ai</option><option value="openai">OpenAI</option><option value="compatible">OpenAI-compatible</option></select></label>
        <label>模型<input id="llm_model"></label>
        <label class="wide">Base URL<input id="llm_base_url"></label>
        <label>API Key<input id="llm_api_key" type="password" placeholder="留空则保持当前密钥"></label>
        <label><input id="llm_enabled" type="checkbox" style="width:auto"> 启用模型调用</label>
      </div>
      <div class="actions" style="margin-top:10px"><button onclick="saveModelConfig()">保存模型配置</button></div>
      <div id="llm_status" class="muted" style="margin-top:8px"></div>
    </section>
    <section class="panel" style="margin-top:16px">
      <div class="panel-title"><h2>状态快照</h2><span class="status">JSON</span></div>
      <pre id="raw"></pre>
    </section>
  </main>
<script>
async function api(path, options) {
  const res = await fetch(path, options || {});
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
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
  const state = document.getElementById('asset_state').value;
  const payload = {id, name};
  if (type === 'farms') payload.location = state;
  if (type === 'ponds') Object.assign(payload, {farm_id: farmId, species, dissolved_oxygen_min: Number(metric || 4)});
  if (type === 'sensors') Object.assign(payload, {pond_id: pondId, metric: metric || 'DO', unit: unit || 'mg/L', status: state || 'ONLINE'});
  if (type === 'devices') Object.assign(payload, {pond_id: pondId, capability: metric || 'aeration', shadow_state: state || 'off'});
  if (type === 'cameras') Object.assign(payload, {pond_id: pondId, source_type: unit || 'HTTP_SNAPSHOT', status: state || 'UNAVAILABLE'});
  try {
    await api('/api/v1/' + type, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    document.getElementById('asset_message').textContent = '资产已创建：' + (name || id || type);
    ['asset_name','asset_id','asset_metric','asset_unit','asset_state'].forEach(k => document.getElementById(k).value = '');
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
  const latest = incidents[incidents.length - 1];
  document.getElementById('top_metrics').innerHTML = [
    ['池塘', ponds.length],
    ['活动事件', incidents.filter(i => !['RESOLVED','ESCALATED','DISMISSED'].includes(i.status)).length],
    ['待复核', incidents.filter(i => i.status === 'VERIFY_PENDING').length]
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
  renderAssets(data);
  document.getElementById('raw').textContent = JSON.stringify(data, null, 2);
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
  const state = await api('/api/v1/state');
  render(state);
  const cfg = await api('/api/v1/config/llm');
  renderLLM(cfg.llm);
}
refresh();
syncAssetForm();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            html_response(self, page())
        elif path == "/static/fish.svg":
            svg_response(self, (STATIC_DIR / "fish.svg").read_text(encoding="utf-8"))
        elif path in {"/health/live", "/health/ready"}:
            json_response(self, 200, {"status": "ok", "port": CONFIG.port})
        elif path == "/api/v1/state":
            json_response(self, 200, SYSTEM.snapshot())
        elif path == "/api/v1/config/llm":
            json_response(self, 200, {"llm": CONFIG.llm.public_dict()})
        elif path in {"/api/v1/farms", "/api/v1/ponds", "/api/v1/sensors", "/api/v1/devices", "/api/v1/cameras"}:
            json_response(self, 200, {path.rsplit("/", 1)[-1]: asset_collection(path.rsplit("/", 1)[-1])})
        elif path == "/api/v1/events":
            query = parse_qs(urlparse(self.path).query)
            after = int(query.get("after", ["0"])[0])
            json_response(self, 200, {"events": [e for e in SYSTEM.store.events if e["sequence"] > after]})
        else:
            problem(self, 404, "Not Found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/v1/demo/"):
            mode = path.rsplit("/", 1)[-1]
            if mode == "init":
                json_response(self, 200, SYSTEM.initialize_demo())
                return
            if mode not in {"success", "failure", "dedup"}:
                json_response(self, 400, {"type": "bad_request", "title": "Unknown demo mode", "status": 400})
                return
            json_response(self, 200, SYSTEM.run_demo(mode))
            return
        if path == "/api/v1/config/llm":
            payload = read_json_body(self)
            keep_existing_key = not payload.get("api_key")
            if keep_existing_key:
                payload.pop("api_key", None)
            CONFIG.llm.update_from_payload(payload)
            SYSTEM.store.emit(
                "config.llm.updated",
                "大模型 API 配置已更新：%s / %s" % (CONFIG.llm.provider, CONFIG.llm.model),
            )
            CONFIG_STORE.save_llm(CONFIG.llm)
            json_response(self, 200, {"llm": CONFIG.llm.public_dict()})
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
            for item in readings:
                if item.get("metric") != "DO":
                    continue
                incident = SYSTEM.ingest_do(
                    pond_id=str(item.get("pond_id", "")),
                    value=float(item.get("value")),
                    source_event_id=item.get("source_event_id"),
                    seconds_old=int(item.get("seconds_old", 0)),
                )
                accepted += 1
                if incident:
                    incidents.append(incident.id)
            json_response(self, 202, {"accepted": accepted, "incident_ids": incidents, "state": SYSTEM.snapshot()})
            return
        problem(self, 404, "Not Found")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=CONFIG.host)
    parser.add_argument("--port", type=int, default=CONFIG.port)
    args = parser.parse_args()
    CONFIG.host = args.host
    CONFIG.port = args.port
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("FishAgent web listening on http://%s:%s" % (args.host, args.port))
    server.serve_forever()


if __name__ == "__main__":
    main()

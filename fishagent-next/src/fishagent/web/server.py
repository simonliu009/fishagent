import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from fishagent.application.agent_service import FishAgentSystem
from fishagent.core import AppConfig, RuntimeConfigStore


SYSTEM = FishAgentSystem()
CONFIG = AppConfig.from_env()
CONFIG_STORE = RuntimeConfigStore()
CONFIG.llm = CONFIG_STORE.load_llm(CONFIG.llm)


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


def page() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>智渔 Agent 控制台</title>
  <style>
    :root {
      --bg: #f5f7f8;
      --panel: #ffffff;
      --ink: #172024;
      --muted: #66757d;
      --line: #d8e0e3;
      --brand: #0f766e;
      --warn: #b45309;
      --bad: #b91c1c;
      --ok: #15803d;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: Arial, "Microsoft YaHei", sans-serif; color: var(--ink); background: var(--bg); }
    header { padding: 18px 28px; background: #0b3b3a; color: white; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    h1 { margin: 0; font-size: 22px; letter-spacing: 0; }
    main { padding: 22px; max-width: 1440px; margin: 0 auto; }
    .grid { display: grid; grid-template-columns: 1.2fr 1fr; gap: 16px; align-items: start; }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }
    h2 { font-size: 16px; margin: 0 0 12px; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    button { border: 0; border-radius: 6px; padding: 9px 12px; background: var(--brand); color: white; cursor: pointer; font-weight: 600; }
    button.secondary { background: #40535c; }
    label { display: block; font-size: 12px; color: var(--muted); margin-top: 8px; }
    input, select { width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 9px; margin-top: 4px; }
    .cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
    .card { border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #fbfcfc; min-height: 96px; }
    .metric { font-size: 24px; font-weight: 700; margin-top: 8px; }
    .muted { color: var(--muted); font-size: 13px; }
    .status { display: inline-block; padding: 3px 8px; border-radius: 999px; background: #e7f6f2; color: var(--brand); font-size: 12px; font-weight: 700; }
    .bad { color: var(--bad); }
    .ok { color: var(--ok); }
    .timeline { display: grid; gap: 8px; max-height: 520px; overflow: auto; }
    .event { border-left: 3px solid var(--brand); padding: 8px 10px; background: #fbfcfc; border-radius: 4px; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; background: #111827; color: #e5e7eb; padding: 12px; border-radius: 8px; max-height: 360px; overflow: auto; }
    @media (max-width: 860px) {
      header { align-items: flex-start; flex-direction: column; }
      main { padding: 12px; }
      .grid, .cards { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>智渔 Agent 控制台</h1>
    <div>模拟器模式 · 前端端口 3008 · 安全策略门开启</div>
  </header>
  <main>
    <div class="grid">
      <section class="panel">
        <h2>运营总览</h2>
        <div class="cards" id="cards"></div>
        <h2 style="margin-top:16px">B-01 闭环演示</h2>
        <div class="actions">
          <button class="secondary" onclick="initDemo()">初始化演示资产</button>
          <button onclick="demo('success')">成功闭环</button>
          <button onclick="demo('failure')">复核失败升级</button>
          <button onclick="demo('dedup')">防重复动作</button>
          <button class="secondary" onclick="refresh()">刷新</button>
        </div>
        <h2 style="margin-top:16px">大模型 API 配置</h2>
        <div class="muted">用于后续 CrewAI/OpenAI-compatible 接入；当前垂直切片仍由确定性 Agent 运行。</div>
        <label>提供商</label>
        <select id="llm_provider"><option value="zai">Z.ai</option><option value="openai">OpenAI</option><option value="compatible">OpenAI-compatible</option></select>
        <label>Base URL</label><input id="llm_base_url">
        <label>模型</label><input id="llm_model">
        <label>API Key</label><input id="llm_api_key" type="password" placeholder="留空则保持当前密钥">
        <label><input id="llm_enabled" type="checkbox" style="width:auto"> 启用模型调用</label>
        <div class="actions" style="margin-top:10px"><button onclick="saveModelConfig()">保存模型配置</button></div>
        <div id="llm_status" class="muted" style="margin-top:8px"></div>
      </section>
      <section class="panel">
        <h2>事件轨迹</h2>
        <div id="events" class="timeline"></div>
      </section>
    </div>
    <section class="panel" style="margin-top:16px">
      <h2>Agent 控制室</h2>
      <div id="runs"></div>
    </section>
    <section class="panel" style="margin-top:16px">
      <h2>状态快照</h2>
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
  const latest = incidents[incidents.length - 1];
  document.getElementById('cards').innerHTML = [
    ['活动事件', incidents.length, latest ? latest.status : '无'],
    ['设备状态', devices.map(d => d.name + ':' + d.shadow_state).join(' / ') || '无', '模拟网关'],
    ['Agent Run', (data.agent_runs || []).length, (data.agent_runs || []).slice(-1)[0]?.stop_reason || '待命']
  ].map(x => '<div class="card"><div class="muted">'+x[0]+'</div><div class="metric">'+x[1]+'</div><span class="status">'+x[2]+'</span></div>').join('');
  document.getElementById('events').innerHTML = (data.events || []).slice().reverse().map(e => '<div class="event"><b>#'+e.sequence+' '+e.event_type+'</b><div>'+e.summary+'</div><div class="muted">'+e.occurred_at+'</div></div>').join('');
  document.getElementById('runs').innerHTML = (data.agent_runs || []).slice().reverse().map(r => '<div class="card" style="margin-bottom:8px"><b>'+r.goal+'</b> <span class="status">'+r.status+'</span><div class="muted">停止原因：'+(r.stop_reason || '-')+' · 委派：'+r.delegated_agents.join(' → ')+'</div><ol>'+r.steps.map(s => '<li>'+s.agent+' / '+s.action+'：'+s.summary+'</li>').join('')+'</ol></div>').join('');
  document.getElementById('raw').textContent = JSON.stringify(data, null, 2);
}
async function refresh() {
  const state = await api('/api/v1/state');
  render(state);
  const cfg = await api('/api/v1/config/llm');
  renderLLM(cfg.llm);
}
refresh();
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
        elif path in {"/health/live", "/health/ready"}:
            json_response(self, 200, {"status": "ok", "port": CONFIG.port})
        elif path == "/api/v1/state":
            json_response(self, 200, SYSTEM.snapshot())
        elif path == "/api/v1/config/llm":
            json_response(self, 200, {"llm": CONFIG.llm.public_dict()})
        elif path == "/api/v1/events":
            query = parse_qs(urlparse(self.path).query)
            after = int(query.get("after", ["0"])[0])
            json_response(self, 200, {"events": [e for e in SYSTEM.store.events if e["sequence"] > after]})
        else:
            json_response(self, 404, {"type": "not_found", "title": "Not Found", "status": 404})

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
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(raw or "{}")
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
        if path == "/api/v1/telemetry/readings:batch":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(raw or "{}")
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
        json_response(self, 404, {"type": "not_found", "title": "Not Found", "status": 404})


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

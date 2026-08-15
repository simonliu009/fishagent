"""Build a standalone daily report from the application snapshot."""

import os
from collections import defaultdict
from datetime import date, datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

METRIC_LABELS = {
    "AMMONIA": "氨氮",
    "NITRITE": "亚硝酸根离子",
    "TURBIDITY": "浊度",
    "CHLOROPHYLL": "叶绿素",
    "DO": "溶解氧",
    "PH": "pH",
    "TEMPERATURE": "水温",
}


def report_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(os.environ.get("FISHAGENT_TIMEZONE", "Asia/Shanghai"))
    except Exception:
        return ZoneInfo("UTC")


def _local_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(report_timezone()).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(value or "-")


def _svg(values: list[float]) -> str:
    if not values:
        return '<svg viewBox="0 0 360 72" role="img"><text x="12" y="40">暂无趋势数据</text></svg>'
    low = min(values)
    high = max(values)
    spread = max(high - low, 0.0001)
    points = []
    for index, value in enumerate(values):
        x = 8 + (index / max(len(values) - 1, 1)) * 344
        y = 62 - ((value - low) / spread) * 48
        points.append("%.1f,%.1f" % (x, y))
    x, y = points[-1].split(",")
    return (
        '<svg viewBox="0 0 360 72" role="img" aria-label="传感器趋势">'
        '<path d="M8 62H352" stroke="#d8dee8" fill="none"/>'
        '<polyline points="%s" fill="none" stroke="#1677ff" stroke-width="2.5"/>'
        '<circle cx="%s" cy="%s" r="3.5" fill="#1677ff"/></svg>'
        % (" ".join(points), x, y)
    )


def build_daily_report(snapshot: dict[str, Any], report_date: date, generated_at: datetime) -> tuple[str, dict[str, Any], str]:
    ponds = snapshot.get("ponds", [])
    readings = snapshot.get("readings", [])
    readings_by_metric: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for reading in readings:
        readings_by_metric[str(reading.get("metric", ""))].append(reading)
    trends = []
    for metric, label in METRIC_LABELS.items():
        metric_readings = sorted(readings_by_metric.get(metric, []), key=lambda item: item.get("sampled_at", ""))[-24:]
        trends.append(
            {
                "metric": metric,
                "label": label,
                "unit": metric_readings[-1].get("unit", "") if metric_readings else "",
                "readings": metric_readings,
                "chart_svg": _svg([float(item["value"]) for item in metric_readings if item.get("value") is not None]),
            }
        )

    incidents = snapshot.get("incidents", [])
    active_incidents = [item for item in incidents if item.get("status") not in {"RESOLVED", "DISMISSED"}]
    operation_logs = []
    for command in snapshot.get("commands", [])[-100:]:
        operation_logs.append(
            {
                "time": _local_time(command.get("created_at", "")),
                "type": "设备操作",
                "summary": "%s：%s -> %s（%s）"
                % (
                    command.get("pond_id", ""),
                    command.get("device_id", ""),
                    "开启" if command.get("target_state") == "on" else "关闭",
                    command.get("status", ""),
                ),
            }
        )
    for run in snapshot.get("agent_runs", [])[-100:]:
        for step in run.get("steps", [])[-20:]:
            operation_logs.append(
                {
                    "time": _local_time(step.get("created_at", "")),
                    "type": "Agent轨迹",
                    "summary": "%s：%s" % (step.get("agent", ""), step.get("summary", "")),
                }
            )
    operation_logs.sort(key=lambda item: item["time"], reverse=True)
    low_stock = [
        item for item in snapshot.get("inventory", [])
        if float(item.get("stock_quantity", 0)) <= float(item.get("minimum_quantity", 0))
    ]
    knowledge = sorted(
        snapshot.get("knowledge_documents", []),
        key=lambda item: (item.get("metric") != "DO", item.get("title", "")),
    )[:4]
    data = {
        "report_date": report_date.isoformat(),
        "generated_at": generated_at.isoformat(),
        "pond_count": len(ponds),
        "active_incident_count": len(active_incidents),
        "summary": "今日共检查 %d 个养殖池，当前有 %d 个未关闭事件，库存中有 %d 项低于补货线。"
        % (len(ponds), len(active_incidents), len(low_stock)),
        "ponds": ponds,
        "trends": trends,
        "incidents": incidents[-50:],
        "active_incidents": active_incidents,
        "knowledge_recommendations": knowledge,
        "medication_prescriptions": knowledge,
        "inventory": snapshot.get("inventory", []),
        "low_stock": low_stock,
        "restock_orders": snapshot.get("restock_orders", []),
        "operation_logs": operation_logs[:100],
    }
    trend_sections = "".join(
        '<section class="trend"><h3>%s</h3>%s<p class="muted">最近 %d 条读数，单位：%s</p></section>'
        % (escape(item["label"]), item["chart_svg"], len(item["readings"]), escape(item["unit"] or "-"))
        for item in trends
    )
    pond_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (escape(item.get("id", "")), escape(item.get("name", "")), escape(item.get("species", "")))
        for item in ponds
    ) or '<tr><td colspan="3">暂无池塘数据</td></tr>'
    incident_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (
            escape(item.get("pond_id", "")),
            escape(item.get("title", "")),
            escape(item.get("status", "")),
            escape("；".join(evidence.get("summary", "") for evidence in item.get("evidence", []))),
        )
        for item in incidents[-50:]
    ) or '<tr><td colspan="4">过去报告周期没有告警事件</td></tr>'
    knowledge_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (
            escape(item.get("title", "")),
            escape(item.get("source", "")),
            escape(item.get("reference_dose", "")),
            escape(item.get("risk_notes", "")),
        )
        for item in knowledge
    ) or '<tr><td colspan="4">暂无知识库命中</td></tr>'
    inventory_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s %s</td><td>%s</td></tr>"
        % (
            escape(item.get("name", "")),
            escape(item.get("category", "")),
            item.get("stock_quantity", 0),
            escape(item.get("unit", "")),
            "待补货" if item in low_stock else "正常",
        )
        for item in snapshot.get("inventory", [])
    ) or '<tr><td colspan="4">暂无库存数据</td></tr>'
    log_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (escape(item["time"]), escape(item["type"]), escape(item["summary"]))
        for item in operation_logs[:100]
    ) or '<tr><td colspan="3">暂无操作日志</td></tr>'
    report_title = "今日渔场巡检与操作建议报告 · %s" % report_date.isoformat()
    html = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>%s</title>
<style>
body{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;color:#172033;background:#f5f7fb;margin:0;padding:32px;line-height:1.55}
main{max-width:1180px;margin:auto;background:#fff;padding:32px 40px;box-shadow:0 8px 30px #17203312}
h1{margin:0 0 4px;font-size:28px}h2{margin:32px 0 12px;border-bottom:2px solid #e8edf5;padding-bottom:8px}h3{margin:0 0 6px;font-size:16px}.muted{color:#68748a;font-size:12px}
.summary{padding:16px 20px;background:#edf6ff;border-left:4px solid #1677ff;margin:20px 0}.trends{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}.trend{border:1px solid #e1e7f0;padding:14px}.trend svg{width:100%%;height:72px;background:#fbfcfe}
table{border-collapse:collapse;width:100%%;font-size:13px}th,td{text-align:left;border-bottom:1px solid #e8edf5;padding:9px 8px;vertical-align:top}th{background:#f6f8fb}
@media print{body{background:#fff;padding:0}main{box-shadow:none;padding:0}}@media(max-width:700px){body{padding:12px}main{padding:20px}.trends{grid-template-columns:1fr}}
</style></head><body><main>
<h1>%s</h1><div class="muted">生成时间：%s · 报告数据均为应用内模拟数据</div>
<div class="summary">%s</div>
<h2>一、养殖池概览</h2><table><thead><tr><th>池塘</th><th>名称</th><th>品种</th></tr></thead><tbody>%s</tbody></table>
<h2>二、水质趋势图</h2><div class="trends">%s</div>
<h2>三、告警与事件</h2><table><thead><tr><th>池塘</th><th>事件</th><th>状态</th><th>证据</th></tr></thead><tbody>%s</tbody></table>
<h2>四、用药处方参考（需人工确认）</h2><p class="muted">以下是知识库检索到的参考边界，不构成直接用药处方。任何用药必须由专业人员确认产品、剂量和休药期。</p><table><thead><tr><th>文档</th><th>来源</th><th>参考用量/边界</th><th>风险提示</th></tr></thead><tbody>%s</tbody></table>
<h2>五、库存与补货</h2><table><thead><tr><th>物资</th><th>类别</th><th>库存</th><th>状态</th></tr></thead><tbody>%s</tbody></table>
<h2>六、设备操作日志与 Agent 轨迹</h2><table><thead><tr><th>时间</th><th>类型</th><th>内容</th></tr></thead><tbody>%s</tbody></table>
</main></body></html>""" % (
        escape(report_title),
        escape(report_title),
        escape(_local_time(generated_at.isoformat())),
        escape(data["summary"]),
        pond_rows,
        trend_sections,
        incident_rows,
        knowledge_rows,
        inventory_rows,
        log_rows,
    )
    return data["summary"], data, html

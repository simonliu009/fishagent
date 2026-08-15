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


CHART_COLORS = ("#1677ff", "#07966f", "#e45a55", "#c99324", "#5746d9", "#3971b8")


def _reading_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.min


def _svg(series: list[dict[str, Any]]) -> str:
    readings = [reading for item in series for reading in item["readings"] if reading.get("value") is not None]
    if not readings:
        return '<svg viewBox="0 0 520 170" role="img"><text x="16" y="86">暂无真实采样数据</text></svg>'
    values = [float(reading["value"]) for reading in readings]
    low = min(values)
    high = max(values)
    spread = max(high - low, max(abs(high), 1.0) * 0.04, 0.0001)
    low -= spread * 0.08
    high += spread * 0.08
    start = min((_reading_time(reading.get("sampled_at", "")) for reading in readings), default=datetime.min)
    end = max((_reading_time(reading.get("sampled_at", "")) for reading in readings), default=datetime.max)
    time_span = max((end - start).total_seconds(), 1.0)
    left, top, right, bottom = 48, 12, 510, 132

    def point(reading: dict[str, Any]) -> tuple[float, float]:
        x = left + ((_reading_time(reading.get("sampled_at", "")) - start).total_seconds() / time_span) * (right - left)
        y = bottom - ((float(reading["value"]) - low) / max(high - low, 0.0001)) * (bottom - top)
        return x, y

    grid = []
    for index in range(3):
        value = low + (high - low) * index / 2
        y = bottom - (bottom - top) * index / 2
        grid.append('<path d="M%.1f %.1fH%.1f" stroke="#d8dee8"/><text x="4" y="%.1f">%.2f</text>' % (left, y, right, y + 4, value))
    paths = []
    for index, item in enumerate(series):
        ordered = sorted(item["readings"], key=lambda reading: reading.get("sampled_at", ""))
        points = [point(reading) for reading in ordered if reading.get("value") is not None]
        if not points:
            continue
        color = CHART_COLORS[index % len(CHART_COLORS)]
        paths.append(
            '<polyline points="%s" fill="none" stroke="%s" stroke-width="2.2" stroke-linejoin="round"/>'
            % (" ".join("%.1f,%.1f" % item for item in points), color)
        )
        paths.extend('<circle cx="%.1f" cy="%.1f" r="2.2" fill="%s"/>' % (x, y, color) for x, y in points)
    start_label = _local_time(start.isoformat()).replace(":00", "") if start != datetime.min else "--"
    end_label = _local_time(end.isoformat()).replace(":00", "") if end != datetime.max else "--"
    return (
        '<svg viewBox="0 0 520 170" role="img" aria-label="按池塘展示的真实传感器趋势">'
        '<g font-family="system-ui, sans-serif" font-size="10" fill="#68748a">%s</g>'
        '<path d="M%.1f %.1fH%.1f" stroke="#9da9bb"/><text x="%.1f" y="151">%s</text><text x="%.1f" y="151" text-anchor="end">%s</text>'
        '%s</svg>'
        % ("".join(grid), left, bottom, right, left, escape(start_label), right, escape(end_label), "".join(paths))
    )


def build_daily_report(snapshot: dict[str, Any], report_date: date, generated_at: datetime) -> tuple[str, dict[str, Any], str]:
    ponds = snapshot.get("ponds", [])
    readings = snapshot.get("readings", [])
    pond_names = {item.get("id"): item.get("name", item.get("id", "")) for item in ponds}
    readings_by_metric: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for reading in readings:
        readings_by_metric[str(reading.get("metric", ""))][str(reading.get("pond_id", ""))].append(reading)
    trends = []
    for metric, label in METRIC_LABELS.items():
        metric_series = [
            {
                "pond_id": pond_id,
                "pond_name": pond_names.get(pond_id, pond_id),
                "readings": sorted(items, key=lambda item: item.get("sampled_at", ""))[-24:],
            }
            for pond_id, items in sorted(readings_by_metric.get(metric, {}).items())
        ]
        metric_readings = [reading for item in metric_series for reading in item["readings"]]
        trends.append(
            {
                "metric": metric,
                "label": label,
                "unit": metric_readings[-1].get("unit", "") if metric_readings else "",
                "readings": metric_readings,
                "series": metric_series,
                "chart_svg": _svg(metric_series),
            }
        )

    incidents = snapshot.get("incidents", [])
    active_incidents = [item for item in incidents if item.get("status") not in {"RESOLVED", "DISMISSED"}]
    operation_logs = []
    for command in snapshot.get("commands", []):
        operation_logs.append(
            {
                "time": _local_time(command.get("created_at", "")),
                "type": "自动设备操作",
                "summary": "%s：%s -> %s（%s）"
                % (
                    command.get("pond_id", ""),
                    command.get("device_id", ""),
                    "开启" if command.get("target_state") == "on" else "关闭",
                    command.get("status", ""),
                ),
            }
        )
    for task in snapshot.get("manual_tasks", []):
        operation_logs.append(
            {
                "time": _local_time(task.get("created_at", "")),
                "type": "人工任务",
                "summary": "%s（%s，负责人：%s）：%s"
                % (task.get("title", ""), task.get("status", ""), task.get("assignee", "现场操作员"), task.get("description", "")),
            }
        )
    for run in snapshot.get("agent_runs", []):
        for step in run.get("steps", []):
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
        "inventory": snapshot.get("inventory", []),
        "low_stock": low_stock,
        "restock_orders": snapshot.get("restock_orders", []),
        "operation_logs": operation_logs,
        "automatic_operations": [item for item in operation_logs if item["type"] == "自动设备操作"],
        "manual_tasks": snapshot.get("manual_tasks", []),
    }
    trend_sections = "".join(
        '<section class="trend"><h3>%s</h3>%s<div class="trend-legend">%s</div><p class="muted">基于最近 %d 条真实采样读数，单位：%s</p></section>'
        % (
            escape(item["label"]),
            item["chart_svg"],
            "".join(
                '<span><i style="background:%s"></i>%s</span>' % (CHART_COLORS[index % len(CHART_COLORS)], escape(series["pond_name"]))
                for index, series in enumerate(item["series"])
            ),
            len(item["readings"]),
            escape(item["unit"] or "-"),
        )
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
        for item in operation_logs
    ) or '<tr><td colspan="3">暂无操作日志</td></tr>'
    report_title = "今日渔场巡检与操作建议报告 · %s" % report_date.isoformat()
    html = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>%s</title>
<style>
body{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;color:#172033;background:#f5f7fb;margin:0;padding:32px;line-height:1.55}
main{max-width:1180px;margin:auto;background:#fff;padding:32px 40px;box-shadow:0 8px 30px #17203312}
h1{margin:0 0 4px;font-size:28px}h2{margin:32px 0 12px;border-bottom:2px solid #e8edf5;padding-bottom:8px}h3{margin:0 0 6px;font-size:16px}.muted{color:#68748a;font-size:12px}
.summary{padding:16px 20px;background:#edf6ff;border-left:4px solid #1677ff;margin:20px 0}.trends{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}.trend{border:1px solid #e1e7f0;padding:14px}.trend svg{width:100%%;height:170px;background:#fbfcfe}.trend-legend{display:flex;gap:10px;flex-wrap:wrap;margin:3px 0 2px;color:#68748a;font-size:11px}.trend-legend span{display:inline-flex;align-items:center;gap:4px}.trend-legend i{display:inline-block;width:8px;height:8px;border-radius:50%%}
table{border-collapse:collapse;width:100%%;font-size:13px}th,td{text-align:left;border-bottom:1px solid #e8edf5;padding:9px 8px;vertical-align:top}th{background:#f6f8fb}
@media print{body{background:#fff;padding:0}main{box-shadow:none;padding:0}}@media(max-width:700px){body{padding:12px}main{padding:20px}.trends{grid-template-columns:1fr}}
</style></head><body><main>
<h1>%s</h1><div class="muted">生成时间：%s · 报告数据均为应用内模拟数据</div>
<div class="summary">%s</div>
<h2>一、养殖池概览</h2><table><thead><tr><th>池塘</th><th>名称</th><th>品种</th></tr></thead><tbody>%s</tbody></table>
<h2>二、水质趋势图</h2><div class="trends">%s</div>
<h2>三、告警与事件</h2><table><thead><tr><th>池塘</th><th>事件</th><th>状态</th><th>证据</th></tr></thead><tbody>%s</tbody></table>
<h2>四、库存与补货</h2><table><thead><tr><th>物资</th><th>类别</th><th>库存</th><th>状态</th></tr></thead><tbody>%s</tbody></table>
<h2>五、设备操作日志与 Agent 轨迹</h2><table><thead><tr><th>时间</th><th>类型</th><th>内容</th></tr></thead><tbody>%s</tbody></table>
</main></body></html>""" % (
        escape(report_title),
        escape(report_title),
        escape(_local_time(generated_at.isoformat())),
        escape(data["summary"]),
        pond_rows,
        trend_sections,
        incident_rows,
        inventory_rows,
        log_rows,
    )
    return data["summary"], data, html

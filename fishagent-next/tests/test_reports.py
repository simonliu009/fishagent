from datetime import datetime
from zoneinfo import ZoneInfo

from fishagent.application.agent_service import FishAgentSystem


def test_knowledge_inventory_and_report_are_persistent_in_snapshot() -> None:
    system = FishAgentSystem()
    system.initialize_demo()

    matches = system.search_knowledge("氨氮 pH")
    assert matches[0]["id"] == "kb-ammonia-control"
    assert matches[0]["reference_dose"]
    assert matches[0]["risk_notes"]

    order = system.draft_restock_order("inventory-shrimp-feed", 300, "库存低于补货线")
    task = system.create_manual_task("现场复核 B-04", "携带便携式试剂盒复核氨氮并记录结果")
    report = system.generate_daily_report("2026-08-15")
    state = system.snapshot()

    assert next(item for item in state["inventory"] if item["id"] == "inventory-shrimp-feed")["low_stock"] is True
    assert state["restock_orders"][0]["status"] == "PENDING_CONFIRMATION"
    assert state["daily_reports"][0]["id"] == report.id
    assert report.html_content.startswith("<!doctype html>")
    assert "水质趋势图" in report.html_content
    assert "知识库" not in report.html_content
    assert "用药处方" not in report.html_content
    assert "人工任务" in report.html_content
    assert task.title in report.html_content
    assert report.data["manual_tasks"][0]["id"] == task.id
    assert report.data["automatic_operations"] == []
    assert "<polyline" in report.html_content
    assert "B-01" in report.html_content
    assert len(report.data["trends"]) == 7
    assert order.items[0]["inventory_id"] == "inventory-shrimp-feed"


def test_report_history_keeps_multiple_generated_versions() -> None:
    system = FishAgentSystem()
    system.initialize_demo()
    first = system.generate_daily_report("2026-08-15")
    second = system.generate_daily_report("2026-08-15")

    assert first.id != second.id
    assert [item["id"] for item in system.snapshot()["daily_reports"]] == [first.id, second.id]


def test_daily_report_is_generated_once_at_local_2359() -> None:
    system = FishAgentSystem()
    system.initialize_demo()
    timezone = ZoneInfo("Asia/Shanghai")

    assert system.generate_daily_report_if_due(datetime(2026, 8, 15, 23, 58, tzinfo=timezone)) is None
    report = system.generate_daily_report_if_due(datetime(2026, 8, 15, 23, 59, tzinfo=timezone))
    repeated = system.generate_daily_report_if_due(datetime(2026, 8, 15, 23, 59, 55, tzinfo=timezone))

    assert report is not None
    assert repeated is report
    assert report.report_date == "2026-08-15"
    assert report.data["generation_mode"] == "automatic"
    assert len(system.snapshot()["daily_reports"]) == 1


def test_daily_report_can_be_deleted() -> None:
    system = FishAgentSystem()
    system.initialize_demo()
    report = system.generate_daily_report("2026-08-15")

    deleted = system.delete_daily_report(report.id)

    assert deleted.id == report.id
    assert system.snapshot()["daily_reports"] == []


def test_knowledge_documents_can_be_added_and_deleted() -> None:
    system = FishAgentSystem()
    system.initialize_demo()
    document = system.create_knowledge_document(
        {
            "title": "夜间巡塘复核",
            "content": "启动设备后等待现场复核。",
            "keywords": "夜间,复核",
        }
    )
    assert document.id in {item["id"] for item in system.snapshot()["knowledge_documents"]}
    system.delete_knowledge_document(document.id)
    assert document.id not in {item["id"] for item in system.snapshot()["knowledge_documents"]}

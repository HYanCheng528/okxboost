from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.models import Task
from app.routers import tasks as tasks_router
from app.services.task_progress import clear_task_runtime_state
from app.services.task_runner import run_task


def _wait_task_done(client: TestClient, task_id: str) -> dict:
    detail = None
    for _ in range(40):
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        detail = response.json()
        if detail["status"] != "running":
            break
        time.sleep(0.1)
    assert detail is not None
    return detail


def _create_running_task() -> str:
    task_id = f"tsk_{uuid4().hex[:12]}"
    db = SessionLocal()
    try:
        start_time = datetime(2026, 2, 9, 0, 0, tzinfo=timezone.utc)
        end_time = datetime(2026, 2, 9, 1, 0, tzinfo=timezone.utc)
        task = Task(
            id=task_id,
            task_name="manual-task",
            chain="base",
            wallets_json=json.dumps(["0x1111111111111111111111111111111111111111"]),
            token="0xtoken000000000000000000000000000000000001",
            base_token="USDT",
            time_ranges_json=json.dumps(
                [{"startTime": start_time.isoformat(), "endTime": end_time.isoformat()}]
            ),
            start_time=start_time,
            end_time=end_time,
            boost_multiplier=Decimal("0.85"),
            epsilon=Decimal("0.0001"),
            pair_timeout_minutes=30,
            status="running",
        )
        db.add(task)
        db.commit()
        return task_id
    finally:
        db.close()


def test_task_api_end_to_end(client: TestClient) -> None:
    payload = {
        "taskName": "multi-range-task",
        "chain": "base",
        "wallets": ["0x1111111111111111111111111111111111111111"],
        "token": "0xtoken000000000000000000000000000000000001",
        "baseToken": "USDT",
        "timeRanges": [
            {"startTime": "2026-02-09T00:00:00Z", "endTime": "2026-02-09T01:00:00Z"},
            {"startTime": "2026-02-09T01:00:00Z", "endTime": "2026-02-09T02:00:00Z"},
        ],
        "boostMultiplier": 0.85,
        "epsilon": 0.0001,
        "pairTimeoutMinutes": 30,
        "actualBoostVolume": None,
    }

    created = client.post("/api/tasks", json=payload)
    assert created.status_code == 200
    task_id = created.json()["taskId"]

    detail = _wait_task_done(client, task_id)
    assert detail["status"] == "completed"
    assert detail["taskName"] == "multi-range-task"
    assert len(detail["timeRanges"]) == 2
    assert len(detail["rangeSummaries"]) == 2
    assert detail["rangeSummaries"][0]["rangeIndex"] == 1
    assert detail["rangeSummaries"][1]["rangeIndex"] == 2
    assert detail["rangeSummaries"][0]["cycleCount"] == 1
    assert detail["rangeSummaries"][1]["cycleCount"] == 1
    assert (
        detail["rangeSummaries"][0]["cycleCount"] + detail["rangeSummaries"][1]["cycleCount"]
        == detail["summary"]["cycleCount"]
    )
    assert Decimal(str(detail["summary"]["sumTotalVolume"])) == Decimal("436")
    assert detail["summary"]["cycleCount"] == 2

    cycles = client.get(f"/api/tasks/{task_id}/cycles?page=1&pageSize=50")
    assert cycles.status_code == 200
    cycles_json = cycles.json()
    assert cycles_json["total"] == 2
    assert len(cycles_json["items"]) == 2

    patched = client.patch(f"/api/tasks/{task_id}", json={"actualBoostVolume": 400})
    assert patched.status_code == 200
    patched_json = patched.json()
    assert Decimal(str(patched_json["summary"]["actualBoostVolume"])) == Decimal("400")

    export_res = client.get(f"/api/tasks/{task_id}/export.csv")
    assert export_res.status_code == 200
    assert "summary,sum_total_volume" in export_res.text


def test_append_ranges_rescan_same_task(client: TestClient) -> None:
    create_payload = {
        "taskName": "append-range-task",
        "chain": "base",
        "wallets": ["0x1111111111111111111111111111111111111111"],
        "token": "0xtoken000000000000000000000000000000000001",
        "baseToken": "USDT",
        "startTime": "2026-02-09T00:00:00Z",
        "endTime": "2026-02-09T01:00:00Z",
        "boostMultiplier": 0.85,
        "epsilon": 0.0001,
        "pairTimeoutMinutes": 30,
    }
    created = client.post("/api/tasks", json=create_payload)
    assert created.status_code == 200
    task_id = created.json()["taskId"]

    first_detail = _wait_task_done(client, task_id)
    assert first_detail["status"] == "completed"
    assert len(first_detail["timeRanges"]) == 1

    append_payload = {
        "timeRanges": [
            {"startTime": "2026-02-09T01:00:00Z", "endTime": "2026-02-09T02:00:00Z"},
            {"startTime": "2026-02-09T02:00:00Z", "endTime": "2026-02-09T03:00:00Z"},
        ]
    }
    append_res = client.post(f"/api/tasks/{task_id}/append-ranges", json=append_payload)
    assert append_res.status_code == 200
    assert append_res.json()["taskId"] == task_id

    merged_detail = _wait_task_done(client, task_id)
    assert merged_detail["status"] == "completed"
    assert len(merged_detail["timeRanges"]) == 3
    assert len(merged_detail["rangeSummaries"]) == 3
    assert merged_detail["startTime"] == "2026-02-09T00:00:00Z"
    assert merged_detail["endTime"] == "2026-02-09T03:00:00Z"
    assert Decimal(str(merged_detail["summary"]["sumTotalVolume"])) == Decimal("436")
    assert merged_detail["summary"]["cycleCount"] == 2
    assert merged_detail["rangeSummaries"][2]["cycleCount"] == 0


def test_task_folder_classification(client: TestClient) -> None:
    folder_res = client.post("/api/tasks/folders", json={"name": "project-A"})
    assert folder_res.status_code == 200
    folder_id = folder_res.json()["folderId"]

    created = client.post(
        "/api/tasks",
        json={
            "taskName": "folder-task",
            "folderId": folder_id,
            "chain": "base",
            "wallets": ["0x1111111111111111111111111111111111111111"],
            "token": "0xtoken000000000000000000000000000000000001",
            "baseToken": "USDT",
            "startTime": "2026-02-09T00:00:00Z",
            "endTime": "2026-02-09T01:00:00Z",
            "boostMultiplier": 0.85,
            "epsilon": 0.0001,
            "pairTimeoutMinutes": 30,
        },
    )
    assert created.status_code == 200
    task_id = created.json()["taskId"]

    detail = _wait_task_done(client, task_id)
    assert detail["folderId"] == folder_id
    assert detail["folderName"] == "project-A"

    listed = client.get(f"/api/tasks?folderId={folder_id}")
    assert listed.status_code == 200
    listed_items = listed.json()
    assert len(listed_items) == 1
    assert listed_items[0]["taskId"] == task_id

    unassign = client.patch(f"/api/tasks/{task_id}/folder", json={"folderId": None})
    assert unassign.status_code == 200
    assert unassign.json()["folderId"] is None


def test_sync_feishu_append_only(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret_test")
    monkeypatch.setenv("FEISHU_APP_TOKEN", "app_token_test")
    get_settings.cache_clear()

    captured: dict[str, object] = {}

    def _fake_append_cycles(self, *, table_id, cycles, field_mapping):  # type: ignore[no-untyped-def]
        captured["table_id"] = table_id
        captured["cycles"] = len(cycles)
        captured["wallets"] = [item.wallet for item in cycles]
        captured["date_field"] = field_mapping.date_field
        captured["before_field"] = field_mapping.trade_before_field
        captured["after_field"] = field_mapping.trade_after_field
        captured["gas_field"] = field_mapping.gas_usd_field
        return len(cycles)

    monkeypatch.setattr(tasks_router.FeishuBitableService, "append_cycles", _fake_append_cycles)

    create_payload = {
        "taskName": "sync-feishu-task",
        "chain": "base",
        "wallets": ["0x1111111111111111111111111111111111111111"],
        "token": "0xtoken000000000000000000000000000000000001",
        "baseToken": "USDT",
        "startTime": "2026-02-09T00:00:00Z",
        "endTime": "2026-02-09T01:00:00Z",
        "boostMultiplier": 0.85,
        "epsilon": 0.0001,
        "pairTimeoutMinutes": 30,
    }
    created = client.post("/api/tasks", json=create_payload)
    assert created.status_code == 200
    task_id = created.json()["taskId"]

    detail = _wait_task_done(client, task_id)
    assert detail["status"] == "completed"

    sync_payload = {
        "tableId": "tbl_sync_target",
        "wallet": "0x1111111111111111111111111111111111111111",
        "dateField": "日期",
        "tradeBeforeField": "交易前",
        "tradeAfterField": "交易后",
        "gasUsdField": "gas费",
    }
    sync_res = client.post(f"/api/tasks/{task_id}/sync-feishu", json=sync_payload)
    assert sync_res.status_code == 200
    sync_json = sync_res.json()
    assert sync_json["taskId"] == task_id
    assert sync_json["tableId"] == "tbl_sync_target"
    assert sync_json["wallet"] == "0x1111111111111111111111111111111111111111"
    assert sync_json["appendedCount"] == captured["cycles"]
    assert captured["table_id"] == "tbl_sync_target"
    assert captured["cycles"] == detail["summary"]["cycleCount"]
    assert captured["date_field"] == "日期"
    assert captured["before_field"] == "交易前"
    assert captured["after_field"] == "交易后"
    assert captured["gas_field"] == "gas费"

    get_settings.cache_clear()


def test_cancel_running_task(client: TestClient) -> None:
    task_id = _create_running_task()
    try:
        cancel_res = client.post(f"/api/tasks/{task_id}/cancel")
        assert cancel_res.status_code == 200
        cancel_json = cancel_res.json()
        assert cancel_json["taskId"] == task_id
        assert cancel_json["status"] == "running"
        assert cancel_json["progressStage"] == "Canceling"

        run_task(task_id)

        detail_res = client.get(f"/api/tasks/{task_id}")
        assert detail_res.status_code == 200
        detail = detail_res.json()
        assert detail["status"] == "canceled"
        assert detail["summary"]["cycleCount"] == 0
        assert Decimal(str(detail["summary"]["sumTotalVolume"])) == Decimal("0")
        assert detail["errorMessage"] == "Task canceled by user."
    finally:
        clear_task_runtime_state(task_id)


def test_delete_task(client: TestClient) -> None:
    payload = {
        "taskName": "delete-task",
        "chain": "base",
        "wallets": ["0x1111111111111111111111111111111111111111"],
        "token": "0xtoken000000000000000000000000000000000001",
        "baseToken": "USDT",
        "startTime": "2026-02-09T00:00:00Z",
        "endTime": "2026-02-09T01:00:00Z",
        "boostMultiplier": 0.85,
        "epsilon": 0.0001,
        "pairTimeoutMinutes": 30,
    }

    created = client.post("/api/tasks", json=payload)
    assert created.status_code == 200
    task_id = created.json()["taskId"]

    detail = _wait_task_done(client, task_id)
    assert detail["status"] == "completed"

    delete_res = client.delete(f"/api/tasks/{task_id}")
    assert delete_res.status_code == 200
    delete_json = delete_res.json()
    assert delete_json["taskId"] == task_id
    assert delete_json["status"] == "completed"

    missing_detail = client.get(f"/api/tasks/{task_id}")
    assert missing_detail.status_code == 404

    missing_cycles = client.get(f"/api/tasks/{task_id}/cycles?page=1&pageSize=50")
    assert missing_cycles.status_code == 404

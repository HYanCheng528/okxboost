from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.config import ChainConfig, get_settings
from app.database import SessionLocal
from app.models import SavedWallet, Task
from app.routers import rewards as rewards_router
from app.routers import tasks as tasks_router
from app.services.task_progress import clear_task_runtime_state, mark_task_active
from app.services import task_runner
from app.services import reward_scanner
from app.services import solana_reward_scanner
from app.services import wallet_profit as wallet_profit_service
from app.services.reward_scanner import ClaimContractHit, WalletRewardResult
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


def _wait_reward_done(client: TestClient, reward_id: str) -> dict:
    detail = None
    for _ in range(40):
        response = client.get(f"/api/rewards/{reward_id}")
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


def test_boost_average_next_due_uses_low_watermark(client: TestClient) -> None:
    wallet = "0x2222222222222222222222222222222222222222"
    today = datetime.now(timezone.utc).date()
    task_day = today - timedelta(days=8)
    start_time = datetime.combine(task_day, datetime.min.time(), tzinfo=timezone.utc)
    end_time = start_time + timedelta(hours=1)

    db = SessionLocal()
    try:
        db.add(SavedWallet(id="wal_test", label="低保钱包", address=wallet))
        db.add(
            Task(
                id="tsk_low_watermark",
                task_name="low-watermark-task",
                chain="bsc",
                wallets_json=json.dumps([wallet]),
                token="0xtoken000000000000000000000000000000000001",
                base_token="USDT",
                time_ranges_json=json.dumps(
                    [{"startTime": start_time.isoformat(), "endTime": end_time.isoformat()}]
                ),
                start_time=start_time,
                end_time=end_time,
                boost_multiplier=Decimal("1"),
                epsilon=Decimal("0.0001"),
                pair_timeout_minutes=30,
                status="completed",
                sum_total_volume=Decimal("1000"),
                computed_boost_volume=Decimal("1000"),
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.get("/api/stats/boost-average", params={"minDailyAverage": "90"})
    assert response.status_code == 200
    data = response.json()
    result = data["wallets"][0]

    assert data["minDailyAverage"] == 90.0
    assert result["totalBoostVolume"] == 1000.0
    assert result["dailyAverage"] == 100.0
    assert result["nextDueDate"] == (today + timedelta(days=2)).isoformat()
    assert result["daysRemaining"] == 2

    saved = client.put("/api/stats/settings/dashboard", json={"minDailyAverage": 90})
    assert saved.status_code == 200
    assert saved.json()["minDailyAverage"] == "90"

    saved_settings = client.get("/api/stats/settings/dashboard")
    assert saved_settings.status_code == 200
    assert saved_settings.json()["minDailyAverage"] == "90"

    default_response = client.get("/api/stats/boost-average")
    assert default_response.status_code == 200
    default_result = default_response.json()["wallets"][0]
    assert default_response.json()["minDailyAverage"] == 90.0
    assert default_result["nextDueDate"] == (today + timedelta(days=2)).isoformat()
    assert default_result["daysRemaining"] == 2


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
    wallet_res = client.post(
        "/api/address-book/wallets",
        json={"label": "sync-wallet", "address": "0x1111111111111111111111111111111111111111"},
    )
    assert wallet_res.status_code == 201

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


def test_append_ranges_fetches_only_new_chain_segments(client: TestClient, monkeypatch) -> None:
    original_fetch = task_runner._mock_provider.fetch_transactions
    calls: list[tuple[str, str]] = []

    def _tracked_fetch(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((kwargs["start_time"].isoformat(), kwargs["end_time"].isoformat()))
        return original_fetch(*args, **kwargs)

    monkeypatch.setattr(task_runner._mock_provider, "fetch_transactions", _tracked_fetch)

    create_payload = {
        "taskName": "incremental-task",
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
    assert _wait_task_done(client, task_id)["status"] == "completed"

    calls.clear()
    append_res = client.post(
        f"/api/tasks/{task_id}/append-ranges",
        json={"startTime": "2026-02-09T00:30:00Z", "endTime": "2026-02-09T01:30:00Z"},
    )
    assert append_res.status_code == 200
    assert _wait_task_done(client, task_id)["status"] == "completed"

    assert calls == [("2026-02-09T01:00:00+00:00", "2026-02-09T01:30:00+00:00")]


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
    wallet_res = client.post(
        "/api/address-book/wallets",
        json={"label": "sync-wallet", "address": "0x1111111111111111111111111111111111111111"},
    )
    assert wallet_res.status_code == 201

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

    wallet_id = wallet_res.json()["walletId"]
    patch_res = client.patch(
        f"/api/address-book/wallets/{wallet_id}",
        json={"label": "sync-wallet", "feishuTradeTableId": "tbl_wallet_default"},
    )
    assert patch_res.status_code == 200
    sync_by_mapping = client.post(
        f"/api/tasks/{task_id}/sync-feishu",
        json={
            "wallet": "0x1111111111111111111111111111111111111111",
            "dateField": "日期",
            "tradeBeforeField": "交易前",
            "tradeAfterField": "交易后",
            "gasUsdField": "gas费",
        },
    )
    assert sync_by_mapping.status_code == 200
    assert sync_by_mapping.json()["tableId"] == "tbl_wallet_default"
    assert captured["table_id"] == "tbl_wallet_default"

    get_settings.cache_clear()


def test_cancel_running_task(client: TestClient) -> None:
    task_id = _create_running_task()
    try:
        mark_task_active(task_id)
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


def test_reward_scan_all_wallets_and_period_increment(client: TestClient, monkeypatch) -> None:
    wallet_a = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    wallet_b = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    token = "0xcccccccccccccccccccccccccccccccccccccccc"

    assert client.post("/api/address-book/wallets", json={"label": "wallet-a", "address": wallet_a}).status_code == 201
    assert client.post("/api/address-book/wallets", json={"label": "wallet-b", "address": wallet_b}).status_code == 201

    def _fake_scan_reward(*, token_address, chain, wallets, scan_date, known_contract_statuses=None):  # type: ignore[no-untyped-def]
        assert token_address == token
        assert chain == "bsc"
        assert scan_date == "2026-05-12"
        assert wallets == [(wallet_a, "wallet-a"), (wallet_b, "wallet-b")]
        return (
            [
                WalletRewardResult(wallet=wallet_a, label="wallet-a", claimed=Decimal("100"), sold_usdt=Decimal("80")),
                WalletRewardResult(wallet=wallet_b, label="wallet-b", claimed=Decimal("0"), sold_usdt=Decimal("0")),
            ],
            "ABC",
        )

    monkeypatch.setattr(rewards_router, "scan_reward", _fake_scan_reward)

    payload = {"tokenAddress": token, "chain": "bsc", "scanDate": "2026-05-12"}
    first = client.post("/api/rewards/scan", json={**payload, "period": 5})
    assert first.status_code == 200
    assert first.json()["status"] == "running"
    first_json = _wait_reward_done(client, first.json()["rewardId"])
    assert first_json["period"] == 5
    assert first_json["status"] == "completed"
    assert first_json["projectName"] == "ABC"
    assert Decimal(str(first_json["totalClaimed"])) == Decimal("100")
    assert Decimal(str(first_json["totalSoldUsdt"])) == Decimal("80")
    assert first_json["results"][1]["claimed"] == "0"

    second = client.post("/api/rewards/scan", json=payload)
    assert second.status_code == 200
    second_json = _wait_reward_done(client, second.json()["rewardId"])
    assert second_json["period"] == 6

    updated = client.patch(f"/api/rewards/{first_json['rewardId']}", json={"period": 4})
    assert updated.status_code == 200
    assert updated.json()["period"] == 4

    listed = client.get("/api/rewards")
    assert listed.status_code == 200
    items = listed.json()
    assert [item["period"] for item in items] == [6, 4]
    assert items[0]["wallets"][0]["walletLabel"] == "wallet-a"


def test_reward_scan_learns_claim_contract(client: TestClient, monkeypatch) -> None:
    wallet = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    token = "0xcccccccccccccccccccccccccccccccccccccccc"
    contract = "0x4444444444444444444444444444444444444444"

    assert client.post("/api/address-book/wallets", json={"label": "wallet-a", "address": wallet}).status_code == 201

    def _fake_scan_reward(*, token_address, chain, wallets, scan_date, known_contract_statuses=None):  # type: ignore[no-untyped-def]
        assert known_contract_statuses == {}
        return (
            [WalletRewardResult(wallet=wallet, label="wallet-a", claimed=Decimal("10"), sold_usdt=Decimal("8"))],
            "ABC",
            [
                ClaimContractHit(
                    chain=chain,
                    token_address=token_address,
                    contract_address=contract,
                    function_selector="0x12345678",
                    code_hash="hash_test",
                    first_seen_tx="0x" + "12" * 32,
                    hit_count=2,
                )
            ],
        )

    monkeypatch.setattr(rewards_router, "scan_reward", _fake_scan_reward)

    created = client.post("/api/rewards/scan", json={"tokenAddress": token, "chain": "bsc", "scanDate": "2026-05-12"})
    assert created.status_code == 200
    detail = _wait_reward_done(client, created.json()["rewardId"])
    assert detail["status"] == "completed"

    listed = client.get(f"/api/rewards/contracts?chain=bsc&tokenAddress={token}")
    assert listed.status_code == 200
    items = listed.json()
    assert len(items) == 1
    assert items[0]["status"] == "candidate"
    assert items[0]["contractAddress"] == contract
    assert items[0]["functionSelector"] == "0x12345678"
    assert items[0]["hitCount"] == 2

    updated = client.patch(f"/api/rewards/contracts/{items[0]['contractId']}", json={"status": "confirmed"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "confirmed"


def test_solana_reward_scan_route_filters_solana_wallets(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SOLANA_RPC_URL", "https://solana.invalid")
    get_settings.cache_clear()
    sol_wallet = "11111111111111111111111111111111"
    evm_wallet = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    unmapped_evm_wallet = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    token = "So11111111111111111111111111111111111111112"

    saved = client.post(
        "/api/address-book/wallets",
        json={"label": "evm-sol", "address": evm_wallet, "solanaAddress": sol_wallet},
    )
    assert saved.status_code == 201
    assert saved.json()["solanaAddress"] == sol_wallet
    assert client.post("/api/address-book/wallets", json={"label": "evm", "address": unmapped_evm_wallet}).status_code == 201

    seen_wallets: list[list[tuple[str, str | None]]] = []

    def _fake_scan_solana_reward(*, token_address, wallets, scan_date):  # type: ignore[no-untyped-def]
        assert token_address == token
        assert scan_date == "2026-05-12"
        seen_wallets.append(wallets)
        return ([WalletRewardResult(wallet=sol_wallet, label="evm-sol", claimed=Decimal("5"), sold_usdt=Decimal("4"))], "SOLT", [])

    monkeypatch.setattr(rewards_router, "scan_solana_reward", _fake_scan_solana_reward)

    created = client.post("/api/rewards/scan", json={"tokenAddress": token, "chain": "solana", "scanDate": "2026-05-12"})
    assert created.status_code == 200
    detail = _wait_reward_done(client, created.json()["rewardId"])
    assert detail["status"] == "completed"
    assert detail["chain"] == "solana"
    assert detail["projectName"] == "SOLT"
    assert detail["results"][0]["wallet"] == sol_wallet
    assert detail["totalClaimed"] == "5.0000000000"

    selected = client.post(
        "/api/rewards/scan",
        json={"tokenAddress": token, "chain": "solana", "scanDate": "2026-05-12", "walletAddress": evm_wallet},
    )
    assert selected.status_code == 200
    assert _wait_reward_done(client, selected.json()["rewardId"])["status"] == "completed"
    assert seen_wallets == [[(sol_wallet, "evm-sol")], [(sol_wallet, "evm-sol")]]


def test_solana_reward_scanner_counts_claims_and_stable_sells(monkeypatch) -> None:
    monkeypatch.setenv("SOLANA_RPC_URL", "https://solana.invalid")
    get_settings.cache_clear()
    wallet = "11111111111111111111111111111111"
    token = "So11111111111111111111111111111111111111112"
    stable = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
    ts = 1_778_544_000

    transactions = {
        "sig_claim": {
            "transaction": {"message": {"accountKeys": [{"pubkey": wallet, "signer": True}]}},
            "meta": {
                "err": None,
                "preTokenBalances": [],
                "postTokenBalances": [
                    {"owner": wallet, "mint": token, "uiTokenAmount": {"amount": "5000000", "decimals": 6}},
                ],
            },
        },
        "sig_buy": {
            "transaction": {"message": {"accountKeys": [{"pubkey": wallet, "signer": True}]}},
            "meta": {
                "err": None,
                "preTokenBalances": [
                    {"owner": wallet, "mint": stable, "uiTokenAmount": {"amount": "10000000", "decimals": 6}},
                ],
                "postTokenBalances": [
                    {"owner": wallet, "mint": stable, "uiTokenAmount": {"amount": "8000000", "decimals": 6}},
                    {"owner": wallet, "mint": token, "uiTokenAmount": {"amount": "2000000", "decimals": 6}},
                ],
            },
        },
        "sig_sell": {
            "transaction": {"message": {"accountKeys": [{"pubkey": wallet, "signer": True}]}},
            "meta": {
                "err": None,
                "preTokenBalances": [
                    {"owner": wallet, "mint": token, "uiTokenAmount": {"amount": "5000000", "decimals": 6}},
                    {"owner": wallet, "mint": stable, "uiTokenAmount": {"amount": "0", "decimals": 6}},
                ],
                "postTokenBalances": [
                    {"owner": wallet, "mint": token, "uiTokenAmount": {"amount": "3000000", "decimals": 6}},
                    {"owner": wallet, "mint": stable, "uiTokenAmount": {"amount": "3500000", "decimals": 6}},
                ],
            },
        },
    }

    def _fake_rpc(settings, method, params):  # type: ignore[no-untyped-def]
        if method == "getAccountInfo":
            return {"value": {"data": {"parsed": {"type": "mint", "info": {"decimals": 6}}}}}
        if method == "getSignaturesForAddress":
            return [
                {"signature": "sig_claim", "blockTime": ts, "err": None},
                {"signature": "sig_buy", "blockTime": ts, "err": None},
                {"signature": "sig_sell", "blockTime": ts, "err": None},
            ]
        if method == "getTransaction":
            return transactions[params[0]]
        raise AssertionError(method)

    monkeypatch.setattr(solana_reward_scanner, "_rpc_call", _fake_rpc)

    results, symbol, hits = solana_reward_scanner.scan_solana_reward(
        token_address=token,
        wallets=[(wallet, "sol")],
        scan_date="2026-05-12",
    )

    assert symbol is None
    assert hits == []
    assert results[0].claimed == Decimal("5")
    assert results[0].sold_usdt == Decimal("3.5")


def test_solana_reward_scanner_rejects_missing_mint(monkeypatch) -> None:
    monkeypatch.setenv("SOLANA_RPC_URL", "https://solana.invalid")
    get_settings.cache_clear()

    def _fake_rpc(settings, method, params):  # type: ignore[no-untyped-def]
        assert method == "getAccountInfo"
        return {"value": None}

    monkeypatch.setattr(solana_reward_scanner, "_rpc_call", _fake_rpc)

    with pytest.raises(ValueError, match="Solana mint not found"):
        solana_reward_scanner.scan_solana_reward(
            token_address="So11111111111111111111111111111111111111112",
            wallets=[("11111111111111111111111111111111", "sol")],
            scan_date="2026-05-12",
        )


def test_stable_airdrop_candidate_contract_counts(monkeypatch) -> None:
    wallet = "0x1111111111111111111111111111111111111111"
    token = "0x2222222222222222222222222222222222222222"
    contract = "0x3333333333333333333333333333333333333333"
    tx_hash = "0x" + "44" * 32
    wallet_topic = reward_scanner._address_to_topic(wallet)
    contract_topic = reward_scanner._address_to_topic(contract)
    transfer_log = {
        "address": token,
        "transactionHash": tx_hash,
        "topics": [reward_scanner.TRANSFER_TOPIC, contract_topic, wallet_topic],
        "data": hex(100 * 10**18),
    }
    receipt = {"status": "0x1", "logs": [transfer_log]}
    tx = {"from": wallet, "to": contract, "input": "0x12345678abcdef"}
    config = ChainConfig(
        name="bsc",
        native_symbol="BNB",
        chain_id=56,
        rpc_url="http://rpc.invalid",
        rpc_urls=["http://rpc.invalid"],
        explorer_api_url=None,
        explorer_api_key=None,
        base_tokens={},
    )

    monkeypatch.setattr(reward_scanner, "_fetch_logs", lambda *args, **kwargs: [transfer_log])
    monkeypatch.setattr(reward_scanner, "_fetch_receipt", lambda *args, **kwargs: receipt)
    monkeypatch.setattr(reward_scanner, "_fetch_transaction", lambda *args, **kwargs: tx)
    monkeypatch.setattr(reward_scanner, "_get_code_hash", lambda *args, **kwargs: "code_hash")

    result, hits = reward_scanner._scan_wallet_reward(
        config=config,
        wallet_addr=wallet,
        wallet_label="wallet-a",
        token_address=token,
        token_decimals=18,
        is_stable=True,
        stable_addresses=set(),
        stable_decimals={},
        known_contract_statuses={},
        start_block=1,
        end_block=2,
    )

    assert result.claimed == Decimal("100")
    assert result.sold_usdt == Decimal("100")
    assert len(hits) == 1
    assert hits[0].contract_address == contract


def test_airdrop_sell_to_bnb_then_bnb_to_stable_counts_realized_usdt(client: TestClient, monkeypatch) -> None:
    wallet = "0x1111111111111111111111111111111111111111"
    token = "0x2222222222222222222222222222222222222222"
    claim_contract = "0x3333333333333333333333333333333333333333"
    pool = "0x4444444444444444444444444444444444444444"
    router = "0x5555555555555555555555555555555555555555"
    wbnb = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
    usdt = "0x55d398326f99059ff775485246999027b3197955"
    claim_tx = "0x" + "aa" * 32
    token_sell_tx = "0x" + "bb" * 32
    bnb_sell_tx = "0x" + "cc" * 32
    wallet_topic = reward_scanner._address_to_topic(wallet)
    claim_topic = reward_scanner._address_to_topic(claim_contract)
    pool_topic = reward_scanner._address_to_topic(pool)

    def _transfer(contract: str, from_addr: str, to_addr: str, amount: int, tx_hash: str, block: int, index: int) -> dict:
        return {
            "address": contract,
            "transactionHash": tx_hash,
            "blockNumber": hex(block),
            "transactionIndex": hex(index),
            "topics": [
                reward_scanner.TRANSFER_TOPIC,
                reward_scanner._address_to_topic(from_addr),
                reward_scanner._address_to_topic(to_addr),
            ],
            "data": hex(amount),
        }

    claim_log = _transfer(token, claim_contract, wallet, 100 * 10**18, claim_tx, 10, 0)
    token_sell_log = _transfer(token, wallet, pool, 100 * 10**18, token_sell_tx, 11, 0)
    stable_in_log = _transfer(usdt, pool, wallet, 250 * 10**18, bnb_sell_tx, 12, 0)

    receipts = {
        claim_tx: {"status": "0x1", "blockNumber": "0xa", "transactionIndex": "0x0", "logs": [claim_log]},
        token_sell_tx: {
            "status": "0x1",
            "blockNumber": "0xb",
            "transactionIndex": "0x0",
            "logs": [
                token_sell_log,
                {
                    "address": wbnb,
                    "transactionHash": token_sell_tx,
                    "blockNumber": "0xb",
                    "transactionIndex": "0x0",
                    "topics": [reward_scanner.WITHDRAWAL_TOPIC, pool_topic],
                    "data": hex(5 * 10**17),
                },
            ],
        },
        bnb_sell_tx: {
            "status": "0x1",
            "blockNumber": "0xc",
            "transactionIndex": "0x0",
            "logs": [stable_in_log],
        },
    }
    transactions = {
        claim_tx: {"from": wallet, "to": claim_contract, "input": "0x12345678"},
        token_sell_tx: {"from": wallet, "to": router, "input": "0xabcdef01", "value": "0x0"},
        bnb_sell_tx: {"from": wallet, "to": router, "input": "0xabcdef02", "value": hex(10**18)},
    }

    def _fake_fetch_logs(config, address, topics, start_block, end_block):  # type: ignore[no-untyped-def]
        topic_from = topics[1] if len(topics) > 1 else None
        topic_to = topics[2] if len(topics) > 2 else None
        address = address.lower()
        if address == token and topic_from is None and topic_to == wallet_topic:
            return [claim_log]
        if address == token and topic_from == wallet_topic:
            return [token_sell_log]
        if address == usdt and topic_from is None and topic_to == wallet_topic:
            return [stable_in_log]
        return []

    monkeypatch.setattr(reward_scanner, "_fetch_logs", _fake_fetch_logs)
    monkeypatch.setattr(reward_scanner, "_fetch_receipt", lambda _config, tx_hash: receipts.get(tx_hash))
    monkeypatch.setattr(reward_scanner, "_fetch_transaction", lambda _config, tx_hash: transactions.get(tx_hash))
    monkeypatch.setattr(reward_scanner, "_get_code_hash", lambda *args, **kwargs: "code_hash")
    monkeypatch.setattr(reward_scanner, "_get_native_balance_at_block", lambda *args, **kwargs: Decimal("0"))

    config = ChainConfig(
        name="bsc",
        native_symbol="BNB",
        chain_id=56,
        rpc_url="http://rpc.invalid",
        rpc_urls=["http://rpc.invalid"],
        explorer_api_url=None,
        explorer_api_key=None,
        base_tokens={
            "BNB": wbnb,
            "USDT": usdt,
        },
    )

    result, hits = reward_scanner._scan_wallet_reward(
        config=config,
        wallet_addr=wallet,
        wallet_label="wallet-a",
        token_address=token,
        token_decimals=18,
        is_stable=False,
        stable_addresses={usdt},
        stable_decimals={usdt: 18},
        known_contract_statuses={},
        start_block=1,
        end_block=20,
    )

    assert result.claimed == Decimal("100")
    assert result.sold_usdt == Decimal("125")
    assert len(hits) == 1


def test_reward_sync_feishu_append_selected_wallet(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret_test")
    monkeypatch.setenv("FEISHU_APP_TOKEN", "app_token_test")
    get_settings.cache_clear()

    wallet_a = "0x1111111111111111111111111111111111111111"
    wallet_b = "0x2222222222222222222222222222222222222222"
    token = "0x3333333333333333333333333333333333333333"

    assert client.post("/api/address-book/wallets", json={"label": "1号", "address": wallet_a}).status_code == 201
    assert client.post("/api/address-book/wallets", json={"label": "2号", "address": wallet_b}).status_code == 201

    def _fake_scan_reward(*, token_address, chain, wallets, scan_date, known_contract_statuses=None):  # type: ignore[no-untyped-def]
        return (
            [
                WalletRewardResult(wallet=wallet_a, label="1号", claimed=Decimal("100"), sold_usdt=Decimal("80")),
                WalletRewardResult(wallet=wallet_b, label="2号", claimed=Decimal("0"), sold_usdt=Decimal("0")),
            ],
            "ABC",
        )

    captured: dict[str, object] = {}

    def _fake_append_raw_records(self, *, table_id, records):  # type: ignore[no-untyped-def]
        captured["table_id"] = table_id
        captured["records"] = records
        return len(records)

    monkeypatch.setattr(rewards_router, "scan_reward", _fake_scan_reward)
    monkeypatch.setattr(rewards_router.FeishuBitableService, "append_raw_records", _fake_append_raw_records)

    created = client.post("/api/rewards/scan", json={"tokenAddress": token, "chain": "bsc", "scanDate": "2026-05-12"})
    assert created.status_code == 200
    reward_id = created.json()["rewardId"]
    assert _wait_reward_done(client, reward_id)["status"] == "completed"

    sync_payload = {
        "tableId": "tbl_summary_1",
        "walletAddress": wallet_a,
        "dateField": "日期",
        "periodField": "期数",
        "periodOverride": 99,
        "projectField": "项目",
        "quantityField": "数量",
        "avgSellPriceField": "",
        "boostClaimField": "Boost领取奖励",
    }
    synced = client.post(f"/api/rewards/{reward_id}/sync-feishu", json=sync_payload)
    assert synced.status_code == 200
    assert synced.json()["appendedCount"] == 1
    assert captured["table_id"] == "tbl_summary_1"
    records = captured["records"]
    assert isinstance(records, list)
    assert records[0]["期数"] == 99
    assert records[0]["项目"] == "ABC"
    assert records[0]["数量"] == 100.0
    assert records[0]["Boost领取奖励"] == 80.0
    assert "卖出单价（平均）" not in records[0]

    wallet_rows = client.get("/api/address-book/wallets").json()
    wallet_id = next(row["walletId"] for row in wallet_rows if row["address"] == wallet_b)
    patched = client.patch(
        f"/api/address-book/wallets/{wallet_id}",
        json={"label": "2号", "feishuAirdropTableId": "tbl_airdrop_default"},
    )
    assert patched.status_code == 200
    synced_by_mapping = client.post(
        f"/api/rewards/{reward_id}/sync-feishu",
        json={
            "walletAddress": wallet_b,
            "dateField": "日期",
            "periodField": "期数",
            "projectField": "项目",
            "quantityField": "数量",
            "avgSellPriceField": "",
            "boostClaimField": "Boost领取奖励",
            "includeZeroWallets": True,
        },
    )
    assert synced_by_mapping.status_code == 200
    assert synced_by_mapping.json()["tableId"] == "tbl_airdrop_default"
    assert captured["table_id"] == "tbl_airdrop_default"

    get_settings.cache_clear()


def test_wallet_profit_reads_linked_tables_and_rebate(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret_test")
    monkeypatch.setenv("FEISHU_APP_TOKEN", "app_token_test")
    get_settings.cache_clear()

    wallet_a = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    wallet_h = "0x8888888888888888888888888888888888888888"
    assert client.post(
        "/api/address-book/wallets",
        json={
            "label": "1",
            "address": wallet_a,
            "feishuTradeTableId": "tbl_trade_1",
            "feishuAirdropTableId": "tbl_air_1",
        },
    ).status_code == 201
    assert client.post(
        "/api/address-book/wallets",
        json={
            "label": "8号",
            "address": wallet_h,
            "feishuTradeTableId": "tbl_trade_8",
            "feishuAirdropTableId": "tbl_air_8",
        },
    ).status_code == 201

    feishu_calls = {"list_tables": 0, "list_records": 0}

    class _FakeFeishu:
        def __init__(self, settings):  # type: ignore[no-untyped-def]
            self.settings = settings

        def list_tables(self):  # type: ignore[no-untyped-def]
            feishu_calls["list_tables"] += 1
            return [{"tableId": "tbl_rebate", "name": "返佣"}]

        def list_records(self, *, table_id):  # type: ignore[no-untyped-def]
            feishu_calls["list_records"] += 1
            return {
                "tbl_trade_1": [{"日期": "2026/05/01", "磨损": "$10.00"}],
                "tbl_air_1": [{"日期": "2026/05/02", "Boost领取奖励": "$25.00"}],
                "tbl_trade_8": [{"日期": "2026/05/03", "磨损": "$6.00"}],
                "tbl_air_8": [{"日期": "2026/05/04", "Boost领取奖励": "$3.00"}],
                "tbl_rebate": [
                    {"日期": "2026/05/05", "金额": "$4.00", "多选": ["1号"]},
                    {"日期": "2026/05/06", "金额": "$2.00", "多选": ["8号"]},
                    {"日期": "2026/05/14", "金额": "$10.00", "多选": ["1号"]},
                    {"日期": "2026/05/15", "金额": "$5.00", "多选": ["8号"]},
                    {"日期": "2026/05/17", "金额": "$6.00", "多选": ["手返"]},
                ],
            }.get(table_id, [])

        def list_fields(self, *, table_id):  # type: ignore[no-untyped-def]
            return []

    monkeypatch.setattr(wallet_profit_service, "FeishuBitableService", _FakeFeishu)

    adjustment = client.post(
        "/api/stats/wallet-profit/adjustments",
        json={
            "walletKey": "1号",
            "month": "2026-05",
            "lossAdjustment": 1.5,
            "note": "test",
        },
    )
    assert adjustment.status_code == 200

    missing_cache = client.get(
        "/api/stats/wallet-profit",
        params={"startDate": "2026-05-01", "endDate": "2026-05-31"},
    )
    assert missing_cache.status_code == 404
    assert feishu_calls == {"list_tables": 0, "list_records": 0}

    response = client.get(
        "/api/stats/wallet-profit",
        params={"startDate": "2026-05-01", "endDate": "2026-05-31", "refresh": "true"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["fromCache"] is False
    assert data["rebateTableId"] == "tbl_rebate"
    assert data["months"] == ["2026-05"]
    assert data["totals"]["loss"] == 17.5
    assert data["totals"]["rebate"] == 27.0
    assert data["totals"]["actualLoss"] == -9.5
    assert data["totals"]["income"] == 28.0
    assert data["totals"]["netProfit"] == 37.5
    assert data["sourceStats"]["rebateMatchedCount"] == 4
    assert data["sourceStats"]["manualRebateRecordCount"] == 1
    assert data["sourceStats"]["manualRebateAllocatedAmount"] == 6.0
    assert data["sourceStats"]["appliedAdjustmentCount"] == 1

    by_key = {item["walletKey"]: item for item in data["wallets"]}
    assert by_key["1号"]["totals"]["rebate"] == 18.0
    assert by_key["8号"]["totals"]["rebate"] == 9.0
    assert by_key["1号"]["totals"]["netProfit"] == 31.5
    assert by_key["8号"]["totals"]["netProfit"] == 6.0
    calls_after_refresh = dict(feishu_calls)

    cached = client.get(
        "/api/stats/wallet-profit",
        params={"startDate": "2026-05-01", "endDate": "2026-05-31"},
    )
    assert cached.status_code == 200
    cached_data = cached.json()
    assert cached_data["fromCache"] is True
    assert cached_data["totals"] == data["totals"]
    assert feishu_calls == calls_after_refresh

    get_settings.cache_clear()

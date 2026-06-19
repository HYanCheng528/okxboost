from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import ChainConfig, get_settings
from app.database import SessionLocal
from app.models import CopySellAttempt, CopySellSeedBuy, CopySellTask, CopySellWalletResult, RobotWallet, SavedWallet
from app.services.copy_sell_dex import DexRoute, DirectPoolDexAdapter, SwapResult, V3_ROUTER_ABI, protected_min_output
from app.services.copy_sell_executor import due_copy_sell_tasks, execute_copy_sell_for_robot
from app.services.copy_sell_keystore import RobotKey, load_encrypted_keystore, write_encrypted_keystore


PRIVATE_KEY = "0x" + "11" * 32
PRIVATE_KEY_ADDRESS = "0x19e7e376e7c213b7e7e7e46cc70a5dd086daff2a"


def test_robot_keystore_round_trip_and_wrong_password(client: TestClient, tmp_path: Path) -> None:
    path = tmp_path / "robots.enc.json"
    write_encrypted_keystore(
        path,
        "secret",
        [{"keyId": "robot-1", "label": "机器人1", "privateKey": PRIVATE_KEY}],
    )

    keys = load_encrypted_keystore(path, "secret")
    assert len(keys) == 1
    assert keys[0].key_id == "robot-1"
    assert keys[0].address == PRIVATE_KEY_ADDRESS
    assert keys[0].private_key == PRIVATE_KEY

    try:
        load_encrypted_keystore(path, "wrong")
    except ValueError as exc:
        assert "password" in str(exc)
    else:
        raise AssertionError("wrong password should fail")


def test_robot_wallet_refresh_api_does_not_leak_private_key(
    client: TestClient,
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "robots.enc.json"
    write_encrypted_keystore(
        path,
        "secret",
        [{"keyId": "robot-1", "label": "机器人1", "privateKey": PRIVATE_KEY}],
    )
    monkeypatch.setenv("ROBOT_KEYSTORE_PATH", str(path))
    monkeypatch.setenv("ROBOT_KEYSTORE_PASSWORD", "secret")
    get_settings.cache_clear()

    response = client.post("/api/copy-sell/robot-wallets/refresh")
    assert response.status_code == 200
    data = response.json()
    assert data["imported"] == 1
    assert data["wallets"][0]["robotWalletId"] == "robot-1"
    assert data["wallets"][0]["address"] == PRIVATE_KEY_ADDRESS
    assert "privateKey" not in str(data)

    listed = client.get("/api/copy-sell/robot-wallets")
    assert listed.status_code == 200
    assert "privateKey" not in listed.text


def test_bind_robot_wallet_to_address_book(client: TestClient) -> None:
    db = SessionLocal()
    try:
        db.add(RobotWallet(id="robot-1", label="机器人1", address=PRIVATE_KEY_ADDRESS))
        db.commit()
    finally:
        db.close()

    wallet = client.post(
        "/api/address-book/wallets",
        json={"label": "1号", "address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    )
    assert wallet.status_code == 201

    bound = client.patch(
        f"/api/copy-sell/address-book/wallets/{wallet.json()['walletId']}/robot",
        json={"robotWalletId": "robot-1"},
    )
    assert bound.status_code == 200
    assert bound.json()["robotWalletId"] == "robot-1"
    assert bound.json()["robotWalletAddress"] == PRIVATE_KEY_ADDRESS


def test_bind_robot_wallet_rejects_second_participant_wallet(client: TestClient) -> None:
    db = SessionLocal()
    try:
        db.add(RobotWallet(id="robot-1", label="robot", address=PRIVATE_KEY_ADDRESS))
        db.commit()
    finally:
        db.close()

    first = client.post(
        "/api/address-book/wallets",
        json={"label": "wallet-1", "address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    )
    second = client.post(
        "/api/address-book/wallets",
        json={"label": "wallet-2", "address": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
    )
    assert first.status_code == 201
    assert second.status_code == 201

    bound = client.patch(
        f"/api/copy-sell/address-book/wallets/{first.json()['walletId']}/robot",
        json={"robotWalletId": "robot-1"},
    )
    assert bound.status_code == 200

    duplicate = client.patch(
        f"/api/copy-sell/address-book/wallets/{second.json()['walletId']}/robot",
        json={"robotWalletId": "robot-1"},
    )
    assert duplicate.status_code == 400
    assert "already bound" in duplicate.text


def test_copy_sell_start_rejects_when_trading_disabled(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("ROBOT_TRADING_ENABLED", "false")
    get_settings.cache_clear()
    created = client.post(
        "/api/copy-sell/tasks",
        json={
            "name": "COLLECT",
            "chain": "bsc",
            "tokenAddress": "0x2222222222222222222222222222222222222222",
            "outputTokenAddress": "0x55d398326f99059ff775485246999027b3197955",
            "pollIntervalSeconds": 30,
            "slippageBps": 300,
            "maxRetries": 3,
        },
    )
    assert created.status_code == 200
    task_id = created.json()["taskId"]

    started = client.post(f"/api/copy-sell/tasks/{task_id}/start")
    assert started.status_code == 400
    assert "ROBOT_TRADING_ENABLED=false" in started.text


def test_create_copy_sell_task_defaults_name_and_converts_trigger_baseline(client: TestClient, monkeypatch) -> None:
    from app.routers import copy_sell as copy_sell_router

    monkeypatch.setattr(
        copy_sell_router,
        "resolve_token_metadata",
        lambda chain, address: {"name": "Collect", "symbol": "COLLECT", "decimals": 6},
    )

    created = client.post(
        "/api/copy-sell/tasks",
        json={
            "chain": "bsc",
            "tokenAddress": "0x2222222222222222222222222222222222222222",
            "outputTokenAddress": "0x55d398326f99059ff775485246999027b3197955",
            "triggerBaseline": "1.5",
            "routePreference": "v3",
            "allowZeroMinOutput": True,
        },
    )

    assert created.status_code == 200
    data = created.json()
    assert data["name"] == "COLLECT"
    assert data["triggerBaselineRaw"] == "1500000"
    assert data["routePreference"] == "v3"
    assert data["allowZeroMinOutput"] is True


def test_due_copy_sell_tasks_handles_sqlite_naive_datetime(client: TestClient) -> None:
    db = SessionLocal()
    try:
        db.add(
            CopySellTask(
                id="cst_due_naive",
                name="COLLECT",
                chain="bsc",
                token_address="0x2222222222222222222222222222222222222222",
                output_token_address="0x55d398326f99059ff775485246999027b3197955",
                poll_interval_seconds=0.5,
                slippage_bps=300,
                max_retries=3,
                status="active",
                last_checked_at=datetime(2026, 5, 26, 11, 0, 0),
            )
        )
        db.commit()

        due = due_copy_sell_tasks(db, now=datetime(2026, 5, 26, 11, 0, 1, tzinfo=timezone.utc))

        assert [task.id for task in due] == ["cst_due_naive"]
    finally:
        db.close()


def test_protected_min_output_defaults_to_non_zero_slippage_floor() -> None:
    assert protected_min_output(10_000, 300) == 9700
    assert protected_min_output(1, 5000, allow_zero_min_output=True) == 0
    try:
        protected_min_output(1, 5000)
    except ValueError as exc:
        assert "too small" in str(exc)
    else:
        raise AssertionError("minimum output must not silently become zero")


def test_copy_sell_task_reports_participant_sell_success_metric(client: TestClient) -> None:
    db = SessionLocal()
    try:
        db.add(RobotWallet(id="robot-1", label="机器人1", address=PRIVATE_KEY_ADDRESS))
        db.add(
            SavedWallet(
                id="wal_1",
                label="1号",
                address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                robot_wallet_id="robot-1",
            )
        )
        db.add(
            CopySellTask(
                id="cst_metric",
                name="COLLECT",
                chain="bsc",
                token_address="0x2222222222222222222222222222222222222222",
                output_token_address="0x55d398326f99059ff775485246999027b3197955",
                poll_interval_seconds=30,
                slippage_bps=300,
                max_retries=3,
                status="active",
            )
        )
        attempt = CopySellAttempt(
            task_id="cst_metric",
            robot_wallet_id="robot-1",
            wallet_address=PRIVATE_KEY_ADDRESS,
            status="sold",
            balance_raw="100",
            input_amount_raw="100",
            quoted_output_raw="90",
            min_output_raw="87",
            output_amount_raw="90",
            target_balance_after_raw="0",
            swap_tx_hash="0x" + "22" * 32,
        )
        db.add(attempt)
        db.commit()
        db.add(
            CopySellWalletResult(
                attempt_id=attempt.id,
                task_id="cst_metric",
                robot_wallet_id="robot-1",
                wallet_id="wal_1",
                wallet_label="1号",
                wallet_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                status="sold",
                target_balance_before_raw="100",
                target_balance_after_raw="0",
                output_balance_before_raw="10",
                output_balance_after_raw="100",
                output_amount_raw="90",
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.get("/api/copy-sell/tasks/cst_metric")

    assert response.status_code == 200
    data = response.json()
    assert data["sellStatus"] == "sold"
    assert data["boundRobotCount"] == 1
    assert data["soldRobotCount"] == 1
    assert data["participantWalletCount"] == 1
    assert data["participantTargetCount"] == 1
    assert data["participantSoldCount"] == 1
    assert data["participantPendingCount"] == 0
    assert data["attempts"][0]["sellSucceeded"] is True
    assert data["attempts"][0]["participantResults"][0]["sellSucceeded"] is True
    assert data["attempts"][0]["participantResults"][0]["targetBalanceAfterRaw"] == "0"
    assert data["attempts"][0]["participantResults"][0]["outputAmountRaw"] == "90"


def test_robot_success_metric_is_not_overridden_by_later_zero_balance_failure(client: TestClient) -> None:
    db = SessionLocal()
    try:
        db.add(RobotWallet(id="robot-1", label="robot", address=PRIVATE_KEY_ADDRESS))
        db.add(
            SavedWallet(
                id="wal_1",
                label="wallet-1",
                address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                robot_wallet_id="robot-1",
            )
        )
        db.add(
            CopySellTask(
                id="cst_robot_metric",
                name="COLLECT",
                chain="bsc",
                token_address="0x2222222222222222222222222222222222222222",
                output_token_address="0x55d398326f99059ff775485246999027b3197955",
                poll_interval_seconds=30,
                slippage_bps=300,
                max_retries=3,
                status="active",
            )
        )
        db.add(
            CopySellAttempt(
                task_id="cst_robot_metric",
                robot_wallet_id="robot-1",
                wallet_address=PRIVATE_KEY_ADDRESS,
                status="sold",
                balance_raw="100",
                input_amount_raw="100",
                quoted_output_raw="90",
                min_output_raw="87",
                output_amount_raw="90",
                target_balance_after_raw="0",
                swap_tx_hash="0x" + "22" * 32,
            )
        )
        db.commit()
        db.add(
            CopySellAttempt(
                task_id="cst_robot_metric",
                robot_wallet_id="robot-1",
                wallet_address=PRIVATE_KEY_ADDRESS,
                status="failed",
                balance_raw="0",
                input_amount_raw="0",
                retry_count=1,
                error_message="participating wallet has target token, but robot wallet target token balance is 0",
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.get("/api/copy-sell/tasks/cst_robot_metric")

    assert response.status_code == 200
    data = response.json()
    assert data["soldRobotCount"] == 1
    assert data["failedRobotCount"] == 0


def test_participant_sell_success_requires_output_balance_increase(client: TestClient) -> None:
    db = SessionLocal()
    try:
        db.add(RobotWallet(id="robot-1", label="robot", address=PRIVATE_KEY_ADDRESS))
        db.add(
            SavedWallet(
                id="wal_1",
                label="wallet-1",
                address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                robot_wallet_id="robot-1",
            )
        )
        db.add(
            CopySellTask(
                id="cst_output_metric",
                name="COLLECT",
                chain="bsc",
                token_address="0x2222222222222222222222222222222222222222",
                output_token_address="0x55d398326f99059ff775485246999027b3197955",
                poll_interval_seconds=30,
                slippage_bps=300,
                max_retries=3,
                status="active",
            )
        )
        attempt = CopySellAttempt(
            task_id="cst_output_metric",
            robot_wallet_id="robot-1",
            wallet_address=PRIVATE_KEY_ADDRESS,
            status="sold",
            balance_raw="100",
            input_amount_raw="100",
            quoted_output_raw="90",
            min_output_raw="87",
            output_amount_raw="90",
            target_balance_after_raw="0",
            swap_tx_hash="0x" + "22" * 32,
        )
        db.add(attempt)
        db.commit()
        db.add(
            CopySellWalletResult(
                attempt_id=attempt.id,
                task_id="cst_output_metric",
                robot_wallet_id="robot-1",
                wallet_id="wal_1",
                wallet_label="wallet-1",
                wallet_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                status="sold",
                target_balance_before_raw="100",
                target_balance_after_raw="0",
                output_balance_before_raw="10",
                output_balance_after_raw="10",
                output_amount_raw="0",
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.get("/api/copy-sell/tasks/cst_output_metric")

    assert response.status_code == 200
    data = response.json()
    assert data["participantTargetCount"] == 1
    assert data["participantSoldCount"] == 0
    assert data["participantPendingCount"] == 1
    assert data["attempts"][0]["participantResults"][0]["sellSucceeded"] is False


def test_participant_sell_success_allows_balance_at_manual_baseline(client: TestClient) -> None:
    db = SessionLocal()
    try:
        db.add(RobotWallet(id="robot-1", label="机器人1", address=PRIVATE_KEY_ADDRESS))
        db.add(
            SavedWallet(
                id="wal_1",
                label="1号",
                address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                robot_wallet_id="robot-1",
            )
        )
        db.add(
            CopySellTask(
                id="cst_baseline_metric",
                name="COLLECT",
                chain="bsc",
                token_address="0x2222222222222222222222222222222222222222",
                output_token_address="0x55d398326f99059ff775485246999027b3197955",
                trigger_baseline_raw="10",
                poll_interval_seconds=30,
                slippage_bps=300,
                max_retries=3,
                status="active",
            )
        )
        attempt = CopySellAttempt(
            task_id="cst_baseline_metric",
            robot_wallet_id="robot-1",
            wallet_address=PRIVATE_KEY_ADDRESS,
            status="sold",
            balance_raw="100",
            input_amount_raw="100",
            quoted_output_raw="90",
            min_output_raw="87",
            output_amount_raw="90",
            target_balance_after_raw="0",
            swap_tx_hash="0x" + "22" * 32,
        )
        db.add(attempt)
        db.commit()
        db.add(
            CopySellWalletResult(
                attempt_id=attempt.id,
                task_id="cst_baseline_metric",
                robot_wallet_id="robot-1",
                wallet_id="wal_1",
                wallet_label="1号",
                wallet_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                status="sold",
                target_balance_before_raw="100",
                target_balance_after_raw="10",
                output_balance_before_raw="10",
                output_balance_after_raw="20",
                output_amount_raw="10",
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.get("/api/copy-sell/tasks/cst_baseline_metric")

    assert response.status_code == 200
    data = response.json()
    assert data["sellStatus"] == "sold"
    assert data["participantTargetCount"] == 1
    assert data["participantSoldCount"] == 1
    assert data["attempts"][0]["participantResults"][0]["sellSucceeded"] is True


def test_copy_sell_quote_api_uses_quote_service(client: TestClient, monkeypatch) -> None:
    created = client.post(
        "/api/copy-sell/tasks",
        json={
            "chain": "bsc",
            "tokenAddress": "0x2222222222222222222222222222222222222222",
            "outputTokenAddress": "0x55d398326f99059ff775485246999027b3197955",
        },
    )
    assert created.status_code == 200

    from app.routers import copy_sell as copy_sell_router
    from app.services.copy_sell_executor import CopySellQuote

    def _fake_quote(db, task):  # type: ignore[no-untyped-def]
        return [
            CopySellQuote(
                robot_wallet_id="robot-1",
                wallet_address=PRIVATE_KEY_ADDRESS,
                balance_raw="100",
                quoted_output_raw="90",
                min_output_raw="87",
                route={"protocol": "v2"},
            )
        ]

    monkeypatch.setattr(copy_sell_router, "quote_copy_sell_task", _fake_quote)

    quoted = client.post(f"/api/copy-sell/tasks/{created.json()['taskId']}/quote")
    assert quoted.status_code == 200
    assert quoted.json()[0]["quotedOutputRaw"] == "90"
    assert quoted.json()[0]["route"]["protocol"] == "v2"


def test_copy_sell_quote_keeps_balance_when_min_output_protection_fails(client: TestClient, monkeypatch) -> None:
    db = SessionLocal()
    try:
        db.add(RobotWallet(id="robot-1", label="robot", address=PRIVATE_KEY_ADDRESS))
        db.add(
            SavedWallet(
                id="wal_1",
                label="wallet-1",
                address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                robot_wallet_id="robot-1",
            )
        )
        db.commit()
    finally:
        db.close()

    created = client.post(
        "/api/copy-sell/tasks",
        json={
            "chain": "bsc",
            "tokenAddress": "0x2222222222222222222222222222222222222222",
            "outputTokenAddress": "0x55d398326f99059ff775485246999027b3197955",
            "slippageBps": 5000,
        },
    )
    assert created.status_code == 200

    class _FakeAdapter:
        def __init__(self, config):  # type: ignore[no-untyped-def]
            self.config = config

        def token_balance(self, token, wallet):  # type: ignore[no-untyped-def]
            return 123

        def quote_best(self, token_in, token_out, amount_in_raw, route_preference="best"):  # type: ignore[no-untyped-def]
            return DexRoute(
                protocol="v2",
                router="0x1111111111111111111111111111111111111111",
                quoter=None,
                path=[token_in, token_out],
                fees=[],
                amount_in_raw=amount_in_raw,
                amount_out_raw=1,
            )

    monkeypatch.setattr("app.services.copy_sell_executor.DirectPoolDexAdapter", _FakeAdapter)

    response = client.post(f"/api/copy-sell/tasks/{created.json()['taskId']}/quote")

    assert response.status_code == 200
    row = response.json()[0]
    assert row["balanceRaw"] == "123"
    assert row["quotedOutputRaw"] is None
    assert "too small" in row["errorMessage"]


def test_route_scan_api_returns_ranked_manual_pool_quotes(client: TestClient, monkeypatch) -> None:
    created = client.post(
        "/api/copy-sell/tasks",
        json={
            "chain": "bsc",
            "tokenAddress": "0x2222222222222222222222222222222222222222",
            "outputTokenAddress": "0x55d398326f99059ff775485246999027b3197955",
        },
    )
    assert created.status_code == 200

    from app.routers import copy_sell as copy_sell_router

    class _FakeAdapter:
        def __init__(self, config):  # type: ignore[no-untyped-def]
            self.config = config

        def scan_quotes(self, token_in, token_out, amount_in_raw, route_preference="best"):  # type: ignore[no-untyped-def]
            assert amount_in_raw == 10**18
            assert route_preference == "best"
            return [
                DexRoute(
                    protocol="v3",
                    router="0x1111111111111111111111111111111111111111",
                    quoter="0x2222222222222222222222222222222222222222",
                    path=[token_in, token_out],
                    fees=[10000],
                    amount_in_raw=amount_in_raw,
                    amount_out_raw=100,
                    dex_name="Uniswap V3",
                    factory="0xdb1d10011ad0ff90774d0c6bb92e5c5c8b4461f7",
                    pools=["0x3333333333333333333333333333333333333333"],
                )
            ]

    monkeypatch.setattr(copy_sell_router, "DirectPoolDexAdapter", _FakeAdapter)

    response = client.post(
        f"/api/copy-sell/tasks/{created.json()['taskId']}/scan-routes",
        json={"side": "sell", "amount": "1", "routePreference": "best"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data[0]["dexName"] == "Uniswap V3"
    assert data[0]["protocol"] == "v3"
    assert data[0]["fees"] == [10000]
    assert data[0]["pools"] == ["0x3333333333333333333333333333333333333333"]
    assert data[0]["minOutputRaw"] == "90"


def test_bsc_whitelist_includes_official_uniswap_v3() -> None:
    v3_items = DirectPoolDexAdapter.DEFAULT_DEXES["bsc"]["v3"]
    uniswap = [item for item in v3_items if item["name"] == "Uniswap V3"][0]
    assert uniswap["factory"] == "0xdb1d10011ad0ff90774d0c6bb92e5c5c8b4461f7"
    assert uniswap["router"] == "0xb971ef87ede563556b2ed4b1c0b0019111dd85d2"
    assert uniswap["quoter"] == "0x78d78e420da98ad378d7799be8f4af69033eb077"


def test_bsc_whitelist_includes_official_pancake_v3() -> None:
    v3_items = DirectPoolDexAdapter.DEFAULT_DEXES["bsc"]["v3"]
    pancake = [item for item in v3_items if item["name"] == "PancakeSwap V3"][0]
    assert pancake["factory"] == "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865"
    assert pancake["router"] == "0x13f4ea83d0bd40e75c8222255bc855a974568dd4"
    assert pancake["quoter"] == "0xb048bbc1ee6b733fffcfb9e9cef7375518e25997"


def test_ethereum_whitelist_includes_executable_v2_v3_routes() -> None:
    eth_dexes = DirectPoolDexAdapter.DEFAULT_DEXES["ethereum"]
    v2_names = {item["name"] for item in eth_dexes["v2"]}
    v3_items = eth_dexes["v3"]
    uniswap_v3 = [item for item in v3_items if item["name"] == "Uniswap V3"][0]

    assert {"Uniswap V2", "SushiSwap V2"}.issubset(v2_names)
    assert uniswap_v3["factory"] == "0x1f98431c8ad98523631ae4a59f267346ea31f984"
    assert uniswap_v3["router"] == "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45"
    assert uniswap_v3["quoter"] == "0x61ffe014ba17989e743c5f6cb21bf9697530b21e"
    assert 10000 in uniswap_v3["fees"]


def test_ethereum_candidate_paths_include_weth_hop() -> None:
    weth = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
    config = ChainConfig(
        name="ethereum",
        native_symbol="ETH",
        chain_id=1,
        rpc_url="http://127.0.0.1:8545",
        rpc_urls=["http://127.0.0.1:8545"],
        explorer_api_url=None,
        explorer_api_key=None,
        base_tokens={
            "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "ETH": weth,
            "WETH": weth,
        },
    )
    adapter = DirectPoolDexAdapter(config)
    token = "0x526526528f35ac738177003b8773b402b8df8143"
    usdc = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

    assert adapter._candidate_paths(token, usdc) == [
        [token.lower(), usdc.lower()],
        [token.lower(), weth.lower(), usdc.lower()],
    ]


def test_v3_router_abi_uses_swaprouter02_exact_input_params() -> None:
    from web3 import Web3

    w3 = Web3()
    contract = w3.eth.contract(address="0x" + "11" * 20, abi=V3_ROUTER_ABI)
    calldata = contract.encode_abi(
        "exactInput",
        args=[(b"\x11" * 43, "0x" + "22" * 20, 1, 1)],
    )
    assert calldata[:10] == Web3.keccak(text="exactInput((bytes,address,uint256,uint256))").hex()[:10]
    assert calldata[:10] != Web3.keccak(text="exactInput((bytes,address,uint256,uint256,uint256))").hex()[:10]


def test_copy_sell_start_rejects_chain_without_whitelisted_routes(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("ROBOT_TRADING_ENABLED", "true")
    get_settings.cache_clear()
    db = SessionLocal()
    try:
        db.add(RobotWallet(id="robot-1", label="robot", address=PRIVATE_KEY_ADDRESS))
        db.add(
            SavedWallet(
                id="wal_1",
                label="wallet-1",
                address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                robot_wallet_id="robot-1",
            )
        )
        db.commit()
    finally:
        db.close()

    created = client.post(
        "/api/copy-sell/tasks",
        json={
            "chain": "base",
            "tokenAddress": "0x2222222222222222222222222222222222222222",
            "outputTokenAddress": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        },
    )
    assert created.status_code == 200

    response = client.post(f"/api/copy-sell/tasks/{created.json()['taskId']}/start")

    assert response.status_code == 400
    assert "No whitelisted DEX routes configured for chain: base" in response.text


def test_seed_buy_rejects_chain_without_whitelisted_routes(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("ROBOT_TRADING_ENABLED", "true")
    get_settings.cache_clear()
    db = SessionLocal()
    try:
        db.add(RobotWallet(id="robot-1", label="robot", address=PRIVATE_KEY_ADDRESS))
        db.add(
            SavedWallet(
                id="wal_1",
                label="wallet-1",
                address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                robot_wallet_id="robot-1",
            )
        )
        db.commit()
    finally:
        db.close()

    created = client.post(
        "/api/copy-sell/tasks",
        json={
            "chain": "base",
            "tokenAddress": "0x2222222222222222222222222222222222222222",
            "outputTokenAddress": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        },
    )
    assert created.status_code == 200

    response = client.post(
        f"/api/copy-sell/tasks/{created.json()['taskId']}/seed-buy",
        json={"spendAmount": "1"},
    )

    assert response.status_code == 400
    assert "No whitelisted DEX routes configured for chain: base" in response.text


def test_seed_buy_rejects_when_trading_disabled(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("ROBOT_TRADING_ENABLED", "false")
    get_settings.cache_clear()
    created = client.post(
        "/api/copy-sell/tasks",
        json={
            "chain": "bsc",
            "tokenAddress": "0x2222222222222222222222222222222222222222",
            "outputTokenAddress": "0x55d398326f99059ff775485246999027b3197955",
        },
    )
    assert created.status_code == 200

    response = client.post(
        f"/api/copy-sell/tasks/{created.json()['taskId']}/seed-buy",
        json={"spendAmount": "1"},
    )

    assert response.status_code == 400
    assert "ROBOT_TRADING_ENABLED=false" in response.text


def test_seed_buy_buys_target_token_for_bound_robot(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("ROBOT_TRADING_ENABLED", "true")
    get_settings.cache_clear()
    target_token = "0x2222222222222222222222222222222222222222"
    spend_token = "0x55d398326f99059ff775485246999027b3197955"
    db = SessionLocal()
    try:
        db.add(RobotWallet(id="robot-1", label="robot", address=PRIVATE_KEY_ADDRESS))
        db.add(
            SavedWallet(
                id="wal_1",
                label="wallet-1",
                address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                robot_wallet_id="robot-1",
            )
        )
        db.add(
            CopySellTask(
                id="cst_seed",
                chain="bsc",
                token_address=target_token,
                output_token_address=spend_token,
                allow_zero_min_output=True,
                poll_interval_seconds=30,
                slippage_bps=300,
                max_retries=1,
                status="paused",
            )
        )
        db.commit()
    finally:
        db.close()

    class _FakeAdapter:
        def __init__(self, config):  # type: ignore[no-untyped-def]
            self.config = config
            self.swapped = False

        def token_decimals(self, token):  # type: ignore[no-untyped-def]
            return 18

        def token_balance(self, token, wallet):  # type: ignore[no-untyped-def]
            if token.lower() == spend_token:
                return 10**20
            if token.lower() == target_token:
                return 25 if self.swapped else 0
            return 0

        def quote_best(self, token_in, token_out, amount_in_raw, route_preference="best"):  # type: ignore[no-untyped-def]
            assert token_in == spend_token
            assert token_out == target_token
            assert amount_in_raw == 10**18
            assert route_preference == "best"
            return DexRoute(
                protocol="v2",
                router="0x1111111111111111111111111111111111111111",
                quoter=None,
                path=[token_in, token_out],
                fees=[],
                amount_in_raw=amount_in_raw,
                amount_out_raw=25,
            )

        def swap_exact_tokens(self, **kwargs):  # type: ignore[no-untyped-def]
            assert kwargs["min_output_raw"] == 0
            self.swapped = True
            return SwapResult(
                approval_tx_hash="0x" + "11" * 32,
                swap_tx_hash="0x" + "22" * 32,
                output_amount_raw=25,
                route=kwargs["route"],
            )

    monkeypatch.setattr("app.services.copy_sell_executor.DirectPoolDexAdapter", _FakeAdapter)
    monkeypatch.setattr(
        "app.services.copy_sell_executor.robot_key_map",
        lambda: {
            "robot-1": RobotKey(
                key_id="robot-1",
                label="robot",
                address=PRIVATE_KEY_ADDRESS,
                private_key=PRIVATE_KEY,
            )
        },
    )

    response = client.post("/api/copy-sell/tasks/cst_seed/seed-buy", json={"spendAmount": "1"})

    assert response.status_code == 200
    data = response.json()
    assert data[0]["status"] == "bought"
    assert data[0]["spendAmountRaw"] == str(10**18)
    assert data[0]["minOutputRaw"] == "0"
    assert data[0]["targetAmountRaw"] == "25"
    assert data[0]["swapTxHash"] == "0x" + "22" * 32

    db = SessionLocal()
    try:
        row = db.query(CopySellSeedBuy).one()
        assert row.status == "bought"
        assert row.robot_wallet_id == "robot-1"
    finally:
        db.close()


def test_executor_detects_balance_but_does_not_trade_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("ROBOT_TRADING_ENABLED", "false")
    get_settings.cache_clear()
    db = SessionLocal()
    try:
        db.add(RobotWallet(id="robot-1", label="机器人1", address=PRIVATE_KEY_ADDRESS))
        db.add(
            SavedWallet(
                id="wal_1",
                label="1号",
                address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                robot_wallet_id="robot-1",
            )
        )
        db.add(
            CopySellTask(
                id="cst_test",
                chain="bsc",
                token_address="0x2222222222222222222222222222222222222222",
                output_token_address="0x55d398326f99059ff775485246999027b3197955",
                poll_interval_seconds=30,
                slippage_bps=300,
                max_retries=1,
                status="active",
            )
        )
        db.commit()
    finally:
        db.close()

    class _FakeAdapter:
        def __init__(self, config):  # type: ignore[no-untyped-def]
            self.config = config

        def token_balance(self, token, wallet):  # type: ignore[no-untyped-def]
            return 100

        def quote_best(self, token_in, token_out, amount_in_raw, route_preference="best"):  # type: ignore[no-untyped-def]
            return DexRoute(
                protocol="v2",
                router="0x1111111111111111111111111111111111111111",
                quoter=None,
                path=[token_in, token_out],
                fees=[],
                amount_in_raw=100,
                amount_out_raw=90,
            )

        def swap_exact_tokens(self, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("swap must not be called while trading is disabled")

    monkeypatch.setattr("app.services.copy_sell_executor.DirectPoolDexAdapter", _FakeAdapter)

    execute_copy_sell_for_robot("cst_test", "robot-1")

    db = SessionLocal()
    try:
        attempt = db.query(CopySellAttempt).one()
        task = db.get(CopySellTask, "cst_test")
        assert attempt.status == "failed"
        assert "ROBOT_TRADING_ENABLED=false" in (attempt.error_message or "")
        assert task is not None
        assert task.status == "active"
    finally:
        db.close()


def test_executor_keeps_participant_pending_after_initial_follow_timeout(monkeypatch) -> None:
    monkeypatch.setenv("ROBOT_TRADING_ENABLED", "true")
    monkeypatch.setattr("app.services.copy_sell_executor.FOLLOW_SETTLE_TIMEOUT_SECONDS", 0)
    get_settings.cache_clear()
    target_token = "0x2222222222222222222222222222222222222222"
    output_token = "0x55d398326f99059ff775485246999027b3197955"
    participant = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    db = SessionLocal()
    try:
        db.add(RobotWallet(id="robot-1", label="robot", address=PRIVATE_KEY_ADDRESS))
        db.add(SavedWallet(id="wal_1", label="wallet-1", address=participant, robot_wallet_id="robot-1"))
        db.add(
            CopySellTask(
                id="cst_initial_timeout",
                chain="bsc",
                token_address=target_token,
                output_token_address=output_token,
                poll_interval_seconds=30,
                slippage_bps=300,
                max_retries=1,
                status="active",
            )
        )
        db.commit()
    finally:
        db.close()

    class _FakeAdapter:
        def __init__(self, config):  # type: ignore[no-untyped-def]
            self.config = config

        def token_balance(self, token, wallet):  # type: ignore[no-untyped-def]
            if wallet.lower() == participant:
                return 100 if token.lower() == target_token else 10
            return 100

        def quote_best(self, token_in, token_out, amount_in_raw, route_preference="best"):  # type: ignore[no-untyped-def]
            return DexRoute(
                protocol="v2",
                router="0x1111111111111111111111111111111111111111",
                quoter=None,
                path=[token_in, token_out],
                fees=[],
                amount_in_raw=amount_in_raw,
                amount_out_raw=90,
            )

        def swap_exact_tokens(self, **kwargs):  # type: ignore[no-untyped-def]
            return SwapResult(
                approval_tx_hash=None,
                swap_tx_hash="0x" + "22" * 32,
                output_amount_raw=90,
                route=kwargs["route"],
            )

    monkeypatch.setattr("app.services.copy_sell_executor.DirectPoolDexAdapter", _FakeAdapter)
    monkeypatch.setattr(
        "app.services.copy_sell_executor.robot_key_map",
        lambda: {
            "robot-1": RobotKey(
                key_id="robot-1",
                label="robot",
                address=PRIVATE_KEY_ADDRESS,
                private_key=PRIVATE_KEY,
            )
        },
    )

    execute_copy_sell_for_robot("cst_initial_timeout", "robot-1")

    db = SessionLocal()
    try:
        attempt = db.query(CopySellAttempt).one()
        result = db.query(CopySellWalletResult).one()
        assert attempt.status == "sold"
        assert result.status == "pending"
        assert "continuing background reconciliation" in (result.error_message or "")
    finally:
        db.close()


def test_executor_reconciles_late_participant_copy_sell_success(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("ROBOT_TRADING_ENABLED", "true")
    get_settings.cache_clear()
    target_token = "0x2222222222222222222222222222222222222222"
    output_token = "0x55d398326f99059ff775485246999027b3197955"
    participant = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    db = SessionLocal()
    try:
        db.add(RobotWallet(id="robot-1", label="robot", address=PRIVATE_KEY_ADDRESS))
        db.add(SavedWallet(id="wal_1", label="wallet-1", address=participant, robot_wallet_id="robot-1"))
        db.add(
            CopySellTask(
                id="cst_late_reconcile",
                chain="bsc",
                token_address=target_token,
                output_token_address=output_token,
                poll_interval_seconds=30,
                slippage_bps=300,
                max_retries=1,
                status="active",
            )
        )
        attempt = CopySellAttempt(
            task_id="cst_late_reconcile",
            robot_wallet_id="robot-1",
            wallet_address=PRIVATE_KEY_ADDRESS,
            status="sold",
            balance_raw="100",
            input_amount_raw="100",
            quoted_output_raw="90",
            min_output_raw="87",
            output_amount_raw="90",
            target_balance_after_raw="0",
            swap_tx_hash="0x" + "22" * 32,
        )
        db.add(attempt)
        db.commit()
        db.add(
            CopySellWalletResult(
                attempt_id=attempt.id,
                task_id="cst_late_reconcile",
                robot_wallet_id="robot-1",
                wallet_id="wal_1",
                wallet_label="wallet-1",
                wallet_address=participant,
                status="failed",
                target_balance_before_raw="100",
                target_balance_after_raw="100",
                output_balance_before_raw="10",
                output_balance_after_raw="10",
                output_amount_raw="0",
                error_message="participating wallet did not clear target token before timeout",
            )
        )
        db.commit()
    finally:
        db.close()

    class _FakeAdapter:
        def __init__(self, config):  # type: ignore[no-untyped-def]
            self.config = config

        def token_balance(self, token, wallet):  # type: ignore[no-untyped-def]
            if wallet.lower() == participant:
                return 0 if token.lower() == target_token else 120
            return 0

        def quote_best(self, token_in, token_out, amount_in_raw, route_preference="best"):  # type: ignore[no-untyped-def]
            raise AssertionError("quote must not run while reconciling a late copy sell")

        def swap_exact_tokens(self, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("swap must not run while reconciling a late copy sell")

    monkeypatch.setattr("app.services.copy_sell_executor.DirectPoolDexAdapter", _FakeAdapter)

    execute_copy_sell_for_robot("cst_late_reconcile", "robot-1")

    db = SessionLocal()
    try:
        assert db.query(CopySellAttempt).count() == 1
        result = db.query(CopySellWalletResult).one()
        assert result.status == "sold"
        assert result.target_balance_after_raw == "0"
        assert result.output_amount_raw == "110"
    finally:
        db.close()

    response = client.get("/api/copy-sell/tasks/cst_late_reconcile")
    assert response.status_code == 200
    data = response.json()
    assert data["sellStatus"] == "sold"
    assert data["participantSoldCount"] == 1


def test_executor_waits_for_corresponding_participant_airdrop_balance(monkeypatch) -> None:
    monkeypatch.setenv("ROBOT_TRADING_ENABLED", "true")
    get_settings.cache_clear()
    db = SessionLocal()
    try:
        db.add(RobotWallet(id="robot-1", label="robot", address=PRIVATE_KEY_ADDRESS))
        db.add(
            SavedWallet(
                id="wal_1",
                label="wallet-1",
                address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                robot_wallet_id="robot-1",
            )
        )
        db.add(
            CopySellTask(
                id="cst_wait",
                chain="bsc",
                token_address="0x2222222222222222222222222222222222222222",
                output_token_address="0x55d398326f99059ff775485246999027b3197955",
                poll_interval_seconds=30,
                slippage_bps=300,
                max_retries=1,
                status="active",
            )
        )
        db.commit()
    finally:
        db.close()


def test_executor_waits_until_participant_balance_exceeds_manual_baseline(monkeypatch) -> None:
    monkeypatch.setenv("ROBOT_TRADING_ENABLED", "true")
    get_settings.cache_clear()
    db = SessionLocal()
    try:
        db.add(RobotWallet(id="robot-1", label="robot", address=PRIVATE_KEY_ADDRESS))
        db.add(
            SavedWallet(
                id="wal_1",
                label="wallet-1",
                address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                robot_wallet_id="robot-1",
            )
        )
        db.add(
            CopySellTask(
                id="cst_baseline",
                chain="bsc",
                token_address="0x2222222222222222222222222222222222222222",
                output_token_address="0x55d398326f99059ff775485246999027b3197955",
                trigger_baseline_raw="100",
                poll_interval_seconds=30,
                slippage_bps=300,
                max_retries=1,
                status="active",
            )
        )
        db.commit()
    finally:
        db.close()

    class _FakeAdapter:
        def __init__(self, config):  # type: ignore[no-untyped-def]
            self.config = config

        def token_balance(self, token, wallet):  # type: ignore[no-untyped-def]
            if wallet.lower() == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":
                return 100 if token.lower() == "0x2222222222222222222222222222222222222222" else 1000
            return 100

        def quote_best(self, token_in, token_out, amount_in_raw, route_preference="best"):  # type: ignore[no-untyped-def]
            raise AssertionError("quote must not run when participant balance equals the manual baseline")

        def swap_exact_tokens(self, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("swap must not run when participant balance equals the manual baseline")

    monkeypatch.setattr("app.services.copy_sell_executor.DirectPoolDexAdapter", _FakeAdapter)

    execute_copy_sell_for_robot("cst_baseline", "robot-1")

    db = SessionLocal()
    try:
        assert db.query(CopySellAttempt).count() == 0
    finally:
        db.close()

    class _FakeAdapter:
        def __init__(self, config):  # type: ignore[no-untyped-def]
            self.config = config

        def token_balance(self, token, wallet):  # type: ignore[no-untyped-def]
            if wallet.lower() == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":
                return 0 if token.lower() == "0x2222222222222222222222222222222222222222" else 1000
            return 100

        def quote_best(self, token_in, token_out, amount_in_raw, route_preference="best"):  # type: ignore[no-untyped-def]
            raise AssertionError("quote must not run before the corresponding participant wallet has airdrop balance")

        def swap_exact_tokens(self, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("swap must not run before the corresponding participant wallet has airdrop balance")

    monkeypatch.setattr("app.services.copy_sell_executor.DirectPoolDexAdapter", _FakeAdapter)

    execute_copy_sell_for_robot("cst_wait", "robot-1")

    db = SessionLocal()
    try:
        assert db.query(CopySellAttempt).count() == 0
    finally:
        db.close()

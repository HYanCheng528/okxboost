from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import SessionLocal
from ..models import CopySellAttempt, CopySellSeedBuy, CopySellTask, CopySellWalletResult, RobotWallet, SavedWallet
from .copy_sell_dex import DirectPoolDexAdapter, has_whitelisted_dex_routes, protected_min_output
from .copy_sell_keystore import RobotKey, load_robot_keys
from .evm_wss_balances import Erc20BalanceRead, read_erc20_balances_wss

logger = logging.getLogger(__name__)
FOLLOW_SETTLE_TIMEOUT_SECONDS = 10
FOLLOW_SETTLE_INTERVAL_SECONDS = 2
FOLLOW_RECONCILE_TIMEOUT_SECONDS = 180
SEED_BUY_STALE_AFTER_SECONDS = 300


@dataclass(frozen=True)
class CopySellQuote:
    robot_wallet_id: str
    wallet_address: str
    balance_raw: str
    quoted_output_raw: str | None
    min_output_raw: str | None
    route: dict[str, Any] | None
    error_message: str | None = None


@dataclass(frozen=True)
class ParticipantSnapshot:
    wallet: SavedWallet
    target_balance_raw: int | None
    output_balance_raw: int | None
    error_message: str | None = None


@dataclass(frozen=True)
class ParticipantBalance:
    target_balance_raw: int | None
    output_balance_raw: int | None
    error_message: str | None = None


def refresh_robot_wallets(db: Session) -> tuple[int, int, int, list[RobotWallet]]:
    keys = load_robot_keys(get_settings())
    imported = 0
    updated = 0
    for key in keys:
        existing = db.get(RobotWallet, key.key_id)
        if existing is None:
            db.add(RobotWallet(id=key.key_id, label=key.label, address=key.address))
            imported += 1
        else:
            if existing.label != key.label or existing.address.lower() != key.address.lower():
                existing.label = key.label
                existing.address = key.address
                updated += 1
    db.commit()
    wallets = db.scalars(select(RobotWallet).order_by(RobotWallet.created_at.asc())).all()
    return imported, updated, 0, wallets


def robot_key_map() -> dict[str, RobotKey]:
    return {item.key_id: item for item in load_robot_keys(get_settings())}


def bound_robot_wallets(db: Session) -> list[RobotWallet]:
    rows = (
        db.scalars(
            select(RobotWallet)
            .join(SavedWallet, SavedWallet.robot_wallet_id == RobotWallet.id)
            .order_by(RobotWallet.created_at.asc())
            .distinct()
        )
        .all()
    )
    return list(rows)


def bound_wallet_count(db: Session, robot_wallet_id: str) -> int:
    return int(db.scalar(select(func.count(SavedWallet.id)).where(SavedWallet.robot_wallet_id == robot_wallet_id)) or 0)


def bound_participant_wallets(db: Session, robot_wallet_id: str) -> list[SavedWallet]:
    return list(
        db.scalars(
            select(SavedWallet)
            .where(SavedWallet.robot_wallet_id == robot_wallet_id)
            .order_by(SavedWallet.created_at.asc())
        ).all()
    )


def _decimal_amount_to_raw(amount: Decimal, decimals: int) -> int:
    raw = (amount * (Decimal(10) ** int(decimals))).to_integral_value(rounding=ROUND_DOWN)
    value = int(raw)
    if value <= 0:
        raise ValueError("spendAmount is too small for token decimals")
    return value


def quote_copy_sell_task(db: Session, task: CopySellTask) -> list[CopySellQuote]:
    settings = get_settings()
    config = settings.chain_configs.get(task.chain.lower())
    if config is None:
        raise ValueError(f"Unsupported chain: {task.chain}")
    if not has_whitelisted_dex_routes(task.chain):
        raise ValueError(f"No whitelisted DEX routes configured for chain: {task.chain}")
    adapter = DirectPoolDexAdapter(config)
    quotes: list[CopySellQuote] = []
    for robot in bound_robot_wallets(db):
        balance_raw = "0"
        try:
            balance = adapter.token_balance(task.token_address, robot.address)
            balance_raw = str(balance)
            if balance <= 0:
                quotes.append(
                    CopySellQuote(
                        robot_wallet_id=robot.id,
                        wallet_address=robot.address,
                        balance_raw="0",
                        quoted_output_raw=None,
                        min_output_raw=None,
                        route=None,
                    )
                )
                continue
            route = adapter.quote_best(
                task.token_address,
                task.output_token_address,
                balance,
                route_preference=task.route_preference,
            )
            min_out = protected_min_output(
                route.amount_out_raw,
                task.slippage_bps,
                allow_zero_min_output=task.allow_zero_min_output,
            )
            quotes.append(
                CopySellQuote(
                    robot_wallet_id=robot.id,
                    wallet_address=robot.address,
                    balance_raw=str(balance),
                    quoted_output_raw=str(route.amount_out_raw),
                    min_output_raw=str(min_out),
                    route=route.to_dict(),
                )
            )
        except Exception as exc:
            quotes.append(
                CopySellQuote(
                    robot_wallet_id=robot.id,
                    wallet_address=robot.address,
                    balance_raw=balance_raw,
                    quoted_output_raw=None,
                    min_output_raw=None,
                    route=None,
                    error_message=str(exc),
                )
            )
    return quotes


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def due_copy_sell_tasks(db: Session, now: datetime | None = None) -> list[CopySellTask]:
    now = now or datetime.now(timezone.utc)
    now = _as_aware_utc(now) or datetime.now(timezone.utc)
    tasks = db.scalars(select(CopySellTask).where(CopySellTask.status == "active")).all()
    due: list[CopySellTask] = []
    for task in tasks:
        last_checked_at = _as_aware_utc(task.last_checked_at)
        if last_checked_at is None:
            due.append(task)
            continue
        elapsed = (now - last_checked_at).total_seconds()
        if elapsed >= max(0.5, float(task.poll_interval_seconds or 0.5)):
            due.append(task)
    return due


def run_due_copy_sell_tasks_once() -> None:
    db = SessionLocal()
    try:
        tasks = due_copy_sell_tasks(db)
        task_ids = [task.id for task in tasks]
    finally:
        db.close()
    for task_id in task_ids:
        execute_copy_sell_task(task_id)


def execute_copy_sell_task(task_id: str) -> None:
    db = SessionLocal()
    participant_snapshot_by_robot_id: dict[str, ParticipantSnapshot] = {}
    try:
        task = db.get(CopySellTask, task_id)
        if task is None or task.status != "active":
            return
        task.last_checked_at = datetime.now(timezone.utc)
        db.commit()
        robots = bound_robot_wallets(db)
        robot_ids = [robot.id for robot in robots]
        settings = get_settings()
        config = settings.chain_configs.get(task.chain.lower())
        if config is not None and has_whitelisted_dex_routes(task.chain):
            wallets_by_robot_id: dict[str, SavedWallet] = {}
            for robot in robots:
                participants = bound_participant_wallets(db, robot.id)
                if len(participants) == 1:
                    wallets_by_robot_id[robot.id] = participants[0]
            if wallets_by_robot_id:
                try:
                    adapter = DirectPoolDexAdapter(config)
                    snapshots = _snapshot_participant_wallets(adapter, task, list(wallets_by_robot_id.values()))
                    participant_snapshot_by_robot_id = {
                        robot_id: snapshots[wallet.id]
                        for robot_id, wallet in wallets_by_robot_id.items()
                        if wallet.id in snapshots
                    }
                except Exception as exc:
                    logger.warning("copy-sell participant precheck failed; workers will retry individually: %s", exc)
    finally:
        db.close()

    if not robot_ids:
        return

    with ThreadPoolExecutor(max_workers=len(robot_ids)) as executor:
        futures = [
            executor.submit(
                execute_copy_sell_for_robot,
                task_id,
                robot_id,
                participant_snapshot_by_robot_id.get(robot_id),
            )
            for robot_id in robot_ids
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                logger.exception("copy-sell worker failed: %s", exc)


def _failed_attempt_count(db: Session, task_id: str, robot_wallet_id: str) -> int:
    return int(
        db.scalar(
            select(func.count(CopySellAttempt.id)).where(
                CopySellAttempt.task_id == task_id,
                CopySellAttempt.robot_wallet_id == robot_wallet_id,
                CopySellAttempt.status == "failed",
            )
        )
        or 0
    )


def _mark_stale_seed_buys(db: Session, task_id: str) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=SEED_BUY_STALE_AFTER_SECONDS)
    rows = db.scalars(
        select(CopySellSeedBuy).where(
            CopySellSeedBuy.task_id == task_id,
            CopySellSeedBuy.status.in_(("pending", "quoted")),
        )
    ).all()
    changed = False
    for row in rows:
        updated_at = _as_aware_utc(row.updated_at) or _as_aware_utc(row.created_at)
        if updated_at is None or updated_at > cutoff:
            continue
        row.status = "failed"
        row.error_message = (
            "seed-buy request was interrupted before completion; check the wallet balance before retrying"
        )
        changed = True
    if changed:
        db.commit()


def _execute_seed_buy_row(
    *,
    row_id: int,
    task_id: str,
    robot_wallet_id: str,
    spend_amount_raw: int,
    buy_slippage_bps: int,
    key: RobotKey | None,
) -> int:
    settings = get_settings()
    db = SessionLocal()
    try:
        row = db.get(CopySellSeedBuy, row_id)
        task = db.get(CopySellTask, task_id)
        robot = db.get(RobotWallet, robot_wallet_id)
        if row is None or task is None or robot is None:
            return row_id

        try:
            config = settings.chain_configs.get(task.chain.lower())
            if config is None:
                raise RuntimeError(f"Unsupported chain: {task.chain}")
            if bound_wallet_count(db, robot.id) != 1:
                raise RuntimeError("robot wallet must be bound to exactly one participating wallet before seed buy")
            if key is None:
                raise RuntimeError(f"robot private key missing from keystore: {robot.id}")

            adapter = DirectPoolDexAdapter(config)
            spend_balance = adapter.token_balance(task.output_token_address, robot.address)
            if spend_balance < spend_amount_raw:
                raise RuntimeError("robot wallet spend token balance is lower than requested seed-buy amount")

            target_before = adapter.token_balance(task.token_address, robot.address)
            route = adapter.quote_best(
                task.output_token_address,
                task.token_address,
                spend_amount_raw,
                route_preference=task.route_preference,
            )
            min_out = protected_min_output(
                route.amount_out_raw,
                buy_slippage_bps,
                allow_zero_min_output=task.allow_zero_min_output,
            )
            row.status = "quoted"
            row.target_balance_before_raw = str(target_before)
            row.quoted_output_raw = str(route.amount_out_raw)
            row.min_output_raw = str(min_out)
            row.route_json = json.dumps(route.to_dict())
            db.commit()

            result = adapter.swap_exact_tokens(
                private_key=key.private_key,
                token_in=task.output_token_address,
                token_out=task.token_address,
                amount_in_raw=spend_amount_raw,
                min_output_raw=min_out,
                route=route,
            )
            target_after = adapter.token_balance(task.token_address, robot.address)
            target_delta = target_after - target_before
            row.status = "bought"
            row.approval_tx_hash = result.approval_tx_hash
            row.swap_tx_hash = result.swap_tx_hash
            row.target_balance_after_raw = str(target_after)
            row.target_amount_raw = str(target_delta) if target_delta >= 0 else None
            row.route_json = json.dumps(result.route.to_dict())
            row.error_message = None
            db.commit()
        except Exception as exc:
            row.status = "failed"
            row.error_message = str(exc)[:1000]
            db.commit()
        return row_id
    finally:
        db.close()


def seed_buy_copy_sell_task(
    db: Session,
    task: CopySellTask,
    *,
    spend_amount: Decimal,
    slippage_bps: int | None = None,
) -> list[CopySellSeedBuy]:
    settings = get_settings()
    if not settings.robot_trading_enabled:
        raise ValueError("ROBOT_TRADING_ENABLED=false")
    config = settings.chain_configs.get(task.chain.lower())
    if config is None:
        raise ValueError(f"Unsupported chain: {task.chain}")
    if not has_whitelisted_dex_routes(task.chain):
        raise ValueError(f"No whitelisted DEX routes configured for chain: {task.chain}")

    robots = bound_robot_wallets(db)
    if not robots:
        raise ValueError("No bound robot wallets")

    _mark_stale_seed_buys(db, task.id)
    running_rows = db.scalars(
        select(CopySellSeedBuy)
        .where(
            CopySellSeedBuy.task_id == task.id,
            CopySellSeedBuy.status.in_(("pending", "quoted")),
        )
        .order_by(CopySellSeedBuy.created_at.desc())
    ).all()
    if running_rows:
        return list(running_rows)

    adapter = DirectPoolDexAdapter(config)
    spend_amount_raw = _decimal_amount_to_raw(spend_amount, adapter.token_decimals(task.output_token_address))
    key_map = robot_key_map()
    buy_slippage_bps = slippage_bps if slippage_bps is not None else task.slippage_bps
    rows: list[CopySellSeedBuy] = []
    row_ids: list[int] = []
    jobs: list[tuple[int, str, str, RobotKey | None]] = []

    for robot in robots:
        row = CopySellSeedBuy(
            task_id=task.id,
            robot_wallet_id=robot.id,
            wallet_address=robot.address,
            status="pending",
            spend_token_address=task.output_token_address,
            target_token_address=task.token_address,
            spend_amount_raw=str(spend_amount_raw),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        rows.append(row)
        row_ids.append(row.id)
        jobs.append((row.id, task.id, robot.id, key_map.get(robot.id)))

    if jobs:
        with ThreadPoolExecutor(max_workers=min(len(jobs), 7)) as executor:
            futures = [
                executor.submit(
                    _execute_seed_buy_row,
                    row_id=row_id,
                    task_id=task_id,
                    robot_wallet_id=robot_id,
                    spend_amount_raw=spend_amount_raw,
                    buy_slippage_bps=buy_slippage_bps,
                    key=key,
                )
                for row_id, task_id, robot_id, key in jobs
            ]
            for future in as_completed(futures):
                future.result()

    db.expire_all()
    rows = list(
        db.scalars(select(CopySellSeedBuy).where(CopySellSeedBuy.id.in_(row_ids)).order_by(CopySellSeedBuy.id.asc())).all()
    )

    return rows


def _wallet_key(address: str) -> str:
    return (address or "").strip().lower()


def _read_participant_balances(
    adapter: DirectPoolDexAdapter,
    task: CopySellTask,
    wallet_addresses: list[str],
) -> dict[str, ParticipantBalance]:
    settings = get_settings()
    unique_addresses = list(dict.fromkeys(_wallet_key(address) for address in wallet_addresses if address))
    if not unique_addresses:
        return {}

    config = settings.chain_configs.get(task.chain.lower())
    if config is not None and config.wss_url:
        reads: list[Erc20BalanceRead] = []
        for address in unique_addresses:
            reads.append(
                Erc20BalanceRead(
                    key=f"{address}:target",
                    token_address=task.token_address,
                    wallet_address=address,
                )
            )
            reads.append(
                Erc20BalanceRead(
                    key=f"{address}:output",
                    token_address=task.output_token_address,
                    wallet_address=address,
                )
            )
        try:
            raw = read_erc20_balances_wss(
                config.wss_url,
                reads,
                timeout_seconds=max(3, settings.request_timeout_seconds),
            )
            return {
                address: ParticipantBalance(
                    target_balance_raw=raw[f"{address}:target"],
                    output_balance_raw=raw[f"{address}:output"],
                )
                for address in unique_addresses
            }
        except Exception as exc:
            logger.warning("copy-sell WSS balance read failed for %s; falling back to HTTP: %s", task.chain, exc)

    balances: dict[str, ParticipantBalance] = {}
    for address in unique_addresses:
        try:
            balances[address] = ParticipantBalance(
                target_balance_raw=adapter.token_balance(task.token_address, address),
                output_balance_raw=adapter.token_balance(task.output_token_address, address),
            )
        except Exception as exc:
            balances[address] = ParticipantBalance(
                target_balance_raw=None,
                output_balance_raw=None,
                error_message=str(exc)[:1000],
            )
    return balances


def _snapshot_participant_wallets(
    adapter: DirectPoolDexAdapter,
    task: CopySellTask,
    wallets: list[SavedWallet],
) -> dict[str, ParticipantSnapshot]:
    balances = _read_participant_balances(adapter, task, [wallet.address for wallet in wallets])
    snapshots: dict[str, ParticipantSnapshot] = {}
    for wallet in wallets:
        balance = balances.get(_wallet_key(wallet.address))
        if balance is None:
            balance = ParticipantBalance(None, None, "participant wallet balance was not returned")
        snapshots[wallet.id] = ParticipantSnapshot(
            wallet=wallet,
            target_balance_raw=balance.target_balance_raw,
            output_balance_raw=balance.output_balance_raw,
            error_message=balance.error_message,
        )
    return snapshots


def _snapshot_participant_wallet(adapter: DirectPoolDexAdapter, task: CopySellTask, wallet: SavedWallet) -> ParticipantSnapshot:
    return _snapshot_participant_wallets(adapter, task, [wallet])[wallet.id]


def _int_or_zero(value: str | None) -> int:
    try:
        return int(value or "0")
    except (TypeError, ValueError):
        return 0


def _participant_result_sold(result: CopySellWalletResult, trigger_baseline_raw: int = 0) -> bool:
    return (
        _int_or_zero(result.target_balance_before_raw) > trigger_baseline_raw
        and _int_or_zero(result.target_balance_after_raw) <= trigger_baseline_raw
        and _int_or_zero(result.output_balance_after_raw) > _int_or_zero(result.output_balance_before_raw)
    )


def _result_age_seconds(result: CopySellWalletResult) -> float:
    created_at = _as_aware_utc(result.created_at) or datetime.now(timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - created_at).total_seconds())


def _update_participant_result_from_balance(
    result: CopySellWalletResult,
    balance: ParticipantBalance | None,
    trigger_baseline_raw: int,
) -> bool:
    if balance is None or balance.error_message:
        result.status = "check_failed"
        result.error_message = (balance.error_message if balance else "participant wallet balance was not returned")[:1000]
        return False

    result.target_balance_after_raw = str(balance.target_balance_raw)
    result.output_balance_after_raw = str(balance.output_balance_raw)
    result.output_amount_raw = str(
        max(0, int(balance.output_balance_raw or 0) - _int_or_zero(result.output_balance_before_raw))
    )
    if _participant_result_sold(result, trigger_baseline_raw):
        result.status = "sold"
        result.error_message = None
        return True

    if _int_or_zero(result.target_balance_after_raw) <= trigger_baseline_raw:
        result.status = "failed"
        result.error_message = (
            "participating wallet cleared target token, but configured output token did not increase; "
            "check whether OKX copied into a different output token"
        )
        return False

    result.status = "pending"
    if not result.error_message or "timeout" in result.error_message or "target token balance is 0" in result.error_message:
        result.error_message = "waiting for OKX copy-sell confirmation"
    return False


def _reconcile_task_participant_results(
    db: Session,
    adapter: DirectPoolDexAdapter,
    task: CopySellTask,
    robot_wallet_id: str | None = None,
) -> int:
    trigger_baseline_raw = _int_or_zero(task.trigger_baseline_raw)
    query = select(CopySellWalletResult).where(
        CopySellWalletResult.task_id == task.id,
        CopySellWalletResult.status != "sold",
    )
    if robot_wallet_id is not None:
        query = query.where(CopySellWalletResult.robot_wallet_id == robot_wallet_id)
    results = list(db.scalars(query).all())
    tracked = [item for item in results if _int_or_zero(item.target_balance_before_raw) > trigger_baseline_raw]
    if not tracked:
        return 0

    sold_count = 0
    balances = _read_participant_balances(adapter, task, [result.wallet_address for result in tracked])
    for result in tracked:
        was_sold = _update_participant_result_from_balance(
            result,
            balances.get(_wallet_key(result.wallet_address)),
            trigger_baseline_raw,
        )
        if was_sold:
            sold_count += 1
            continue
        if result.status == "pending" and _result_age_seconds(result) >= FOLLOW_RECONCILE_TIMEOUT_SECONDS:
            result.status = "failed"
            result.error_message = (
                "participating wallet did not clear target token and increase output token within "
                f"{FOLLOW_RECONCILE_TIMEOUT_SECONDS}s after robot sell"
            )
    if sold_count and task.error_message and ("max retries" in task.error_message or "最大重试" in task.error_message):
        task.error_message = None
    db.commit()
    return sold_count


def _has_open_robot_follow_confirmation(db: Session, task: CopySellTask, robot_wallet_id: str) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=FOLLOW_RECONCILE_TIMEOUT_SECONDS)
    attempts = list(
        db.scalars(
            select(CopySellAttempt).where(
                CopySellAttempt.task_id == task.id,
                CopySellAttempt.robot_wallet_id == robot_wallet_id,
                CopySellAttempt.status == "sold",
            )
        ).all()
    )
    trigger_baseline_raw = _int_or_zero(task.trigger_baseline_raw)
    for attempt in attempts:
        created_at = _as_aware_utc(attempt.created_at)
        if created_at is None or created_at < cutoff:
            continue
        for result in attempt.wallet_results:
            if (
                _int_or_zero(result.target_balance_before_raw) > trigger_baseline_raw
                and result.status in {"waiting_copy_sell", "pending", "check_failed"}
            ):
                return True
    return False


def _refresh_participant_results(
    db: Session,
    adapter: DirectPoolDexAdapter,
    task: CopySellTask,
    results: list[CopySellWalletResult],
    *,
    mark_failed_on_timeout: bool = False,
) -> None:
    trigger_baseline_raw = _int_or_zero(task.trigger_baseline_raw)
    tracked = [item for item in results if _int_or_zero(item.target_balance_before_raw) > trigger_baseline_raw]
    if not tracked:
        return

    deadline = time.monotonic() + FOLLOW_SETTLE_TIMEOUT_SECONDS
    while True:
        pending: list[CopySellWalletResult] = []
        balances = _read_participant_balances(adapter, task, [result.wallet_address for result in tracked])
        for result in tracked:
            if result.status == "sold":
                continue
            sold = _update_participant_result_from_balance(
                result,
                balances.get(_wallet_key(result.wallet_address)),
                trigger_baseline_raw,
            )
            if not sold and result.status == "pending":
                pending.append(result)
        db.commit()

        if not pending:
            return
        if time.monotonic() >= deadline:
            for result in pending:
                if mark_failed_on_timeout:
                    result.status = "failed"
                    result.error_message = (
                        "participating wallet did not clear target token and increase output token before follow-check timeout"
                    )
                else:
                    result.status = "pending"
                    result.error_message = (
                        "OKX copy-sell was not confirmed within the initial follow-check timeout; "
                        "continuing background reconciliation"
                    )
            db.commit()
            return
        time.sleep(FOLLOW_SETTLE_INTERVAL_SECONDS)


def execute_copy_sell_for_robot(
    task_id: str,
    robot_wallet_id: str,
    participant_snapshot: ParticipantSnapshot | None = None,
) -> None:
    settings = get_settings()
    db = SessionLocal()
    attempt: CopySellAttempt | None = None
    try:
        task = db.get(CopySellTask, task_id)
        robot = db.get(RobotWallet, robot_wallet_id)
        if task is None or robot is None or task.status != "active":
            return
        config = settings.chain_configs.get(task.chain.lower())
        if config is None:
            raise ValueError(f"Unsupported chain: {task.chain}")
        if not has_whitelisted_dex_routes(task.chain):
            task.status = "failed"
            task.error_message = f"No whitelisted DEX routes configured for chain: {task.chain}"
            db.commit()
            return
        adapter = DirectPoolDexAdapter(config)
        _reconcile_task_participant_results(db, adapter, task, robot.id)

        participants = bound_participant_wallets(db, robot.id)
        if not participants:
            return
        if len(participants) > 1:
            attempt = CopySellAttempt(
                task_id=task.id,
                robot_wallet_id=robot.id,
                wallet_address=robot.address,
                status="failed",
                error_message=(
                    "robot wallet is bound to multiple participating wallets; "
                    "one robot wallet must correspond to exactly one participating wallet"
                ),
                retry_count=_failed_attempt_count(db, task.id, robot.id) + 1,
            )
            db.add(attempt)
            task.error_message = attempt.error_message
            db.commit()
            return

        if (
            participant_snapshot is not None
            and _wallet_key(participant_snapshot.wallet.address) == _wallet_key(participants[0].address)
        ):
            snapshots = [participant_snapshot]
        else:
            snapshots = [_snapshot_participant_wallet(adapter, task, participants[0])]
        if snapshots[0].error_message:
            attempt = CopySellAttempt(
                task_id=task.id,
                robot_wallet_id=robot.id,
                wallet_address=robot.address,
                status="failed",
                error_message=f"failed to read corresponding participating wallet balance: {snapshots[0].error_message}",
                retry_count=_failed_attempt_count(db, task.id, robot.id) + 1,
            )
            db.add(attempt)
            db.commit()
            return
        trigger_baseline_raw = _int_or_zero(task.trigger_baseline_raw)
        if not snapshots[0].target_balance_raw or snapshots[0].target_balance_raw <= trigger_baseline_raw:
            return
        if _failed_attempt_count(db, task.id, robot.id) >= max(1, task.max_retries):
            task.error_message = f"robot {robot.label} reached max retries; skipped after reconciliation"
            db.commit()
            return

        balance = adapter.token_balance(task.token_address, robot.address)
        if balance <= 0 and _has_open_robot_follow_confirmation(db, task, robot.id):
            task.error_message = f"robot {robot.label} already sold target token; waiting for OKX copy-sell confirmation"
            db.commit()
            return

        attempt = CopySellAttempt(
            task_id=task.id,
            robot_wallet_id=robot.id,
            wallet_address=robot.address,
            status="detected",
            balance_raw=str(balance),
            input_amount_raw=str(balance),
            retry_count=_failed_attempt_count(db, task.id, robot.id),
        )
        db.add(attempt)
        db.commit()
        db.refresh(attempt)

        participant_results: list[CopySellWalletResult] = []
        for snapshot in snapshots:
            wallet = snapshot.wallet
            if snapshot.error_message:
                status = "check_failed"
            elif snapshot.target_balance_raw and snapshot.target_balance_raw > trigger_baseline_raw:
                status = "waiting_copy_sell"
            else:
                status = "no_target_balance"
            result = CopySellWalletResult(
                attempt_id=attempt.id,
                task_id=task.id,
                robot_wallet_id=robot.id,
                wallet_id=wallet.id,
                wallet_label=wallet.label,
                wallet_address=wallet.address,
                status=status,
                target_balance_before_raw=(
                    str(snapshot.target_balance_raw) if snapshot.target_balance_raw is not None else None
                ),
                output_balance_before_raw=(
                    str(snapshot.output_balance_raw) if snapshot.output_balance_raw is not None else None
                ),
                error_message=snapshot.error_message,
            )
            db.add(result)
            participant_results.append(result)
        db.commit()

        if balance <= 0:
            raise RuntimeError("participating wallet has target token, but robot wallet target token balance is 0")

        route = adapter.quote_best(
            task.token_address,
            task.output_token_address,
            balance,
            route_preference=task.route_preference,
        )
        min_out = protected_min_output(
            route.amount_out_raw,
            task.slippage_bps,
            allow_zero_min_output=task.allow_zero_min_output,
        )
        attempt.status = "quoted"
        attempt.quoted_output_raw = str(route.amount_out_raw)
        attempt.min_output_raw = str(min_out)
        attempt.route_json = json.dumps(route.to_dict())
        db.commit()

        if not settings.robot_trading_enabled:
            raise RuntimeError("ROBOT_TRADING_ENABLED=false")

        key = robot_key_map().get(robot.id)
        if key is None:
            raise RuntimeError(f"robot private key missing from keystore: {robot.id}")

        result = adapter.swap_exact_tokens(
            private_key=key.private_key,
            token_in=task.token_address,
            token_out=task.output_token_address,
            amount_in_raw=balance,
            min_output_raw=min_out,
            route=route,
        )
        try:
            target_balance_after = adapter.token_balance(task.token_address, robot.address)
        except Exception as exc:
            target_balance_after = None
            attempt.error_message = f"post-sell target balance check failed: {exc}"[:1000]
        attempt.status = "sold"
        attempt.approval_tx_hash = result.approval_tx_hash
        attempt.swap_tx_hash = result.swap_tx_hash
        attempt.output_amount_raw = str(result.output_amount_raw) if result.output_amount_raw is not None else None
        attempt.target_balance_after_raw = str(target_balance_after) if target_balance_after is not None else None
        attempt.route_json = json.dumps(result.route.to_dict())
        db.commit()
        _refresh_participant_results(db, adapter, task, participant_results)
    except Exception as exc:
        if attempt is not None:
            attempt.status = "failed"
            attempt.error_message = str(exc)[:1000]
            attempt.retry_count = _failed_attempt_count(db, task_id, robot_wallet_id) + 1
            for wallet_result in attempt.wallet_results:
                if wallet_result.status == "waiting_copy_sell":
                    wallet_result.status = "trigger_failed"
                    wallet_result.error_message = str(exc)[:1000]
        task = db.get(CopySellTask, task_id)
        if task is not None and attempt is not None and attempt.retry_count >= max(1, task.max_retries):
            task.error_message = f"机器人 {robot_wallet_id} 已达到最大重试次数，其他机器人继续轮询: {str(exc)[:900]}"
        db.commit()
    finally:
        db.close()


def _route_payload(route_json: str | None) -> dict | None:
    if not route_json:
        return None
    try:
        value = json.loads(route_json)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def attempt_route_payload(attempt: CopySellAttempt) -> dict | None:
    return _route_payload(attempt.route_json)


def seed_buy_route_payload(seed_buy: CopySellSeedBuy) -> dict | None:
    return _route_payload(seed_buy.route_json)

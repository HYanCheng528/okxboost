from __future__ import annotations

import uuid
from decimal import Decimal, ROUND_DOWN

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import CopySellAttempt, CopySellSeedBuy, CopySellTask, CopySellWalletResult, RobotWallet, SavedWallet
from ..schemas import (
    CopySellAttemptResponse,
    CopySellQuoteResponse,
    CopySellRouteScanRequest,
    CopySellRouteScanResponse,
    CopySellSeedBuyRequest,
    CopySellSeedBuyResponse,
    CopySellTaskCreateRequest,
    CopySellTaskResponse,
    CopySellWalletResultResponse,
    RobotWalletRefreshResponse,
    RobotWalletResponse,
    SavedWalletResponse,
    SavedWalletRobotUpdateRequest,
)
from ..services.address_utils import is_evm_address
from ..services.copy_sell_executor import (
    attempt_route_payload,
    bound_wallet_count,
    bound_robot_wallets,
    quote_copy_sell_task,
    refresh_robot_wallets,
    seed_buy_copy_sell_task,
    seed_buy_route_payload,
)
from ..services.copy_sell_dex import DexRoute, DirectPoolDexAdapter, has_whitelisted_dex_routes, protected_min_output
from ..services.token_resolver import resolve_token_metadata

router = APIRouter(prefix="/api/copy-sell", tags=["copy-sell"])


def _validate_evm_address(value: str, field_name: str) -> str:
    address = (value or "").strip().lower()
    if not is_evm_address(address):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name} address")
    return address


def _robot_response(db: Session, robot: RobotWallet) -> RobotWalletResponse:
    return RobotWalletResponse(
        robotWalletId=robot.id,
        label=robot.label,
        address=robot.address,
        boundWalletCount=bound_wallet_count(db, robot.id),
        createdAt=robot.created_at,
        updatedAt=robot.updated_at,
    )


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _default_task_name(chain: str, token_address: str) -> str:
    try:
        metadata = resolve_token_metadata(chain, token_address)
    except Exception:
        metadata = {}
    label = str(metadata.get("symbol") or metadata.get("name") or "").strip()
    return label or token_address[:10]


def _token_decimals(chain: str, token_address: str) -> int:
    try:
        metadata = resolve_token_metadata(chain, token_address)
        decimals = metadata.get("decimals")
        if decimals is not None:
            return int(decimals)
    except Exception:
        pass
    return 18


def _amount_to_raw(amount: Decimal, decimals: int) -> str:
    value = int((amount * (Decimal(10) ** int(decimals))).to_integral_value(rounding=ROUND_DOWN))
    return str(max(0, value))


def _validate_route_preference(value: str) -> str:
    preference = (value or "best").strip().lower()
    if preference not in {"best", "v2", "v3"}:
        raise HTTPException(status_code=400, detail="routePreference must be best, v2, or v3")
    return preference


def _attempt_sell_succeeded(attempt: CopySellAttempt) -> bool:
    output_amount = _int_or_none(attempt.output_amount_raw)
    target_balance_after = _int_or_none(attempt.target_balance_after_raw)
    return attempt.status == "sold" and output_amount is not None and output_amount > 0 and target_balance_after == 0


def _wallet_result_sell_succeeded(result: CopySellWalletResult) -> bool:
    trigger_baseline = _int_or_none(result.attempt.task.trigger_baseline_raw) if result.attempt and result.attempt.task else 0
    trigger_baseline = trigger_baseline or 0
    target_before = _int_or_none(result.target_balance_before_raw)
    target_after = _int_or_none(result.target_balance_after_raw)
    output_before = _int_or_none(result.output_balance_before_raw)
    output_after = _int_or_none(result.output_balance_after_raw)
    return (
        result.status == "sold"
        and target_before is not None
        and target_before > trigger_baseline
        and target_after is not None
        and target_after <= trigger_baseline
        and output_before is not None
        and output_after is not None
        and output_after > output_before
    )


def _copy_sell_metrics(db: Session, task: CopySellTask) -> dict[str, int | str]:
    bound_robot_ids = {robot.id for robot in bound_robot_wallets(db)}
    latest_by_robot: dict[str, CopySellAttempt] = {}
    sold_robot_ids: set[str] = set()
    for attempt in sorted(task.attempts, key=lambda item: item.created_at, reverse=True):
        if attempt.robot_wallet_id in bound_robot_ids and _attempt_sell_succeeded(attempt):
            sold_robot_ids.add(attempt.robot_wallet_id)
        if attempt.robot_wallet_id in bound_robot_ids and attempt.robot_wallet_id not in latest_by_robot:
            latest_by_robot[attempt.robot_wallet_id] = attempt

    bound_count = len(bound_robot_ids)
    sold_count = len(sold_robot_ids)
    failed_count = sum(
        1
        for robot_id, attempt in latest_by_robot.items()
        if robot_id not in sold_robot_ids and attempt.status == "failed"
    )
    pending_count = max(0, bound_count - sold_count - failed_count)

    participant_wallet_count = int(
        db.scalar(select(func.count(SavedWallet.id)).where(SavedWallet.robot_wallet_id.is_not(None))) or 0
    )

    latest_by_wallet: dict[str, CopySellWalletResult] = {}
    for attempt in sorted(task.attempts, key=lambda item: item.created_at, reverse=True):
        for result in sorted(attempt.wallet_results, key=lambda item: item.created_at, reverse=True):
            wallet_key = result.wallet_id or result.wallet_address.lower()
            if wallet_key not in latest_by_wallet:
                latest_by_wallet[wallet_key] = result

    trigger_baseline = _int_or_none(task.trigger_baseline_raw) or 0
    participant_target_count = sum(
        1
        for result in latest_by_wallet.values()
        if (_int_or_none(result.target_balance_before_raw) or 0) > trigger_baseline
    )
    participant_sold_count = sum(1 for result in latest_by_wallet.values() if _wallet_result_sell_succeeded(result))
    participant_failed_count = sum(
        1
        for result in latest_by_wallet.values()
        if (_int_or_none(result.target_balance_before_raw) or 0) > trigger_baseline
        and result.status in {"failed", "partial", "trigger_failed", "check_failed"}
    )
    participant_pending_count = max(0, participant_target_count - participant_sold_count - participant_failed_count)

    if participant_wallet_count == 0 or participant_target_count == 0:
        sell_status = "not_started"
    elif participant_sold_count == participant_target_count:
        sell_status = "sold"
    elif participant_sold_count > 0:
        sell_status = "partial"
    elif participant_failed_count > 0 and participant_pending_count == 0:
        sell_status = "failed"
    else:
        sell_status = "pending"

    return {
        "sell_status": sell_status,
        "bound_robot_count": bound_count,
        "sold_robot_count": sold_count,
        "failed_robot_count": failed_count,
        "pending_robot_count": pending_count,
        "participant_wallet_count": participant_wallet_count,
        "participant_target_count": participant_target_count,
        "participant_sold_count": participant_sold_count,
        "participant_failed_count": participant_failed_count,
        "participant_pending_count": participant_pending_count,
    }


def _wallet_result_response(result: CopySellWalletResult) -> CopySellWalletResultResponse:
    return CopySellWalletResultResponse(
        resultId=result.id,
        attemptId=result.attempt_id,
        taskId=result.task_id,
        robotWalletId=result.robot_wallet_id,
        walletId=result.wallet_id,
        walletLabel=result.wallet_label,
        walletAddress=result.wallet_address,
        status=result.status,
        targetBalanceBeforeRaw=result.target_balance_before_raw,
        targetBalanceAfterRaw=result.target_balance_after_raw,
        outputBalanceBeforeRaw=result.output_balance_before_raw,
        outputBalanceAfterRaw=result.output_balance_after_raw,
        outputAmountRaw=result.output_amount_raw,
        sellSucceeded=_wallet_result_sell_succeeded(result),
        errorMessage=result.error_message,
        createdAt=result.created_at,
        updatedAt=result.updated_at,
    )


def _seed_buy_response(seed_buy: CopySellSeedBuy) -> CopySellSeedBuyResponse:
    return CopySellSeedBuyResponse(
        seedBuyId=seed_buy.id,
        taskId=seed_buy.task_id,
        robotWalletId=seed_buy.robot_wallet_id,
        walletAddress=seed_buy.wallet_address,
        status=seed_buy.status,
        spendTokenAddress=seed_buy.spend_token_address,
        targetTokenAddress=seed_buy.target_token_address,
        spendAmountRaw=seed_buy.spend_amount_raw,
        quotedOutputRaw=seed_buy.quoted_output_raw,
        minOutputRaw=seed_buy.min_output_raw,
        targetBalanceBeforeRaw=seed_buy.target_balance_before_raw,
        targetBalanceAfterRaw=seed_buy.target_balance_after_raw,
        targetAmountRaw=seed_buy.target_amount_raw,
        approvalTxHash=seed_buy.approval_tx_hash,
        swapTxHash=seed_buy.swap_tx_hash,
        route=seed_buy_route_payload(seed_buy),
        errorMessage=seed_buy.error_message,
        createdAt=seed_buy.created_at,
        updatedAt=seed_buy.updated_at,
    )


def _route_scan_response(route: DexRoute, *, slippage_bps: int, allow_zero_min_output: bool) -> CopySellRouteScanResponse:
    try:
        min_output_raw: str | None = str(
            protected_min_output(
                route.amount_out_raw,
                slippage_bps,
                allow_zero_min_output=allow_zero_min_output,
            )
        )
    except ValueError:
        min_output_raw = None
    return CopySellRouteScanResponse(
        dexName=route.dex_name,
        protocol=route.protocol,
        router=route.router,
        quoter=route.quoter,
        factory=route.factory,
        pools=route.pools,
        path=route.path,
        fees=route.fees,
        amountInRaw=str(route.amount_in_raw),
        amountOutRaw=str(route.amount_out_raw),
        minOutputRaw=min_output_raw,
    )


def _attempt_response(attempt: CopySellAttempt) -> CopySellAttemptResponse:
    participant_results = sorted(attempt.wallet_results, key=lambda item: item.created_at, reverse=True)
    return CopySellAttemptResponse(
        attemptId=attempt.id,
        taskId=attempt.task_id,
        robotWalletId=attempt.robot_wallet_id,
        walletAddress=attempt.wallet_address,
        status=attempt.status,
        balanceRaw=attempt.balance_raw,
        inputAmountRaw=attempt.input_amount_raw,
        quotedOutputRaw=attempt.quoted_output_raw,
        minOutputRaw=attempt.min_output_raw,
        outputAmountRaw=attempt.output_amount_raw,
        targetBalanceAfterRaw=attempt.target_balance_after_raw,
        sellSucceeded=_attempt_sell_succeeded(attempt),
        approvalTxHash=attempt.approval_tx_hash,
        swapTxHash=attempt.swap_tx_hash,
        route=attempt_route_payload(attempt),
        retryCount=attempt.retry_count,
        errorMessage=attempt.error_message,
        createdAt=attempt.created_at,
        updatedAt=attempt.updated_at,
        participantResults=[_wallet_result_response(item) for item in participant_results],
    )


def _task_response(db: Session, task: CopySellTask, *, include_attempts: bool = True) -> CopySellTaskResponse:
    attempts = sorted(task.attempts, key=lambda item: item.created_at, reverse=True)[:100] if include_attempts else []
    seed_buys = sorted(task.seed_buys, key=lambda item: item.created_at, reverse=True)[:100] if include_attempts else []
    metrics = _copy_sell_metrics(db, task)
    return CopySellTaskResponse(
        taskId=task.id,
        name=task.name,
        chain=task.chain,
        tokenAddress=task.token_address,
        outputTokenAddress=task.output_token_address,
        triggerBaselineRaw=task.trigger_baseline_raw or "0",
        routePreference=task.route_preference or "best",
        allowZeroMinOutput=bool(task.allow_zero_min_output),
        pollIntervalSeconds=task.poll_interval_seconds,
        slippageBps=task.slippage_bps,
        maxRetries=task.max_retries,
        status=task.status,
        sellStatus=metrics["sell_status"],
        boundRobotCount=metrics["bound_robot_count"],
        soldRobotCount=metrics["sold_robot_count"],
        failedRobotCount=metrics["failed_robot_count"],
        pendingRobotCount=metrics["pending_robot_count"],
        participantWalletCount=metrics["participant_wallet_count"],
        participantTargetCount=metrics["participant_target_count"],
        participantSoldCount=metrics["participant_sold_count"],
        participantFailedCount=metrics["participant_failed_count"],
        participantPendingCount=metrics["participant_pending_count"],
        errorMessage=None if metrics["sell_status"] == "sold" else task.error_message,
        lastCheckedAt=task.last_checked_at,
        createdAt=task.created_at,
        updatedAt=task.updated_at,
        attempts=[_attempt_response(item) for item in attempts],
        seedBuys=[_seed_buy_response(item) for item in seed_buys],
    )


def _wallet_response(wallet: SavedWallet) -> SavedWalletResponse:
    return SavedWalletResponse(
        walletId=wallet.id,
        label=wallet.label,
        address=wallet.address,
        solanaAddress=wallet.solana_address,
        feishuTradeTableId=wallet.feishu_trade_table_id,
        feishuAirdropTableId=wallet.feishu_airdrop_table_id,
        robotWalletId=wallet.robot_wallet_id,
        robotWalletAddress=wallet.robot_wallet.address if wallet.robot_wallet else None,
        robotWalletLabel=wallet.robot_wallet.label if wallet.robot_wallet else None,
        createdAt=wallet.created_at,
    )


@router.get("/robot-wallets", response_model=list[RobotWalletResponse])
def list_robot_wallets(db: Session = Depends(get_db)) -> list[RobotWalletResponse]:
    rows = db.scalars(select(RobotWallet).order_by(RobotWallet.created_at.asc())).all()
    return [_robot_response(db, row) for row in rows]


@router.post("/robot-wallets/refresh", response_model=RobotWalletRefreshResponse)
def refresh_robot_wallet_list(db: Session = Depends(get_db)) -> RobotWalletRefreshResponse:
    try:
        imported, updated, removed, wallets = refresh_robot_wallets(db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RobotWalletRefreshResponse(
        imported=imported,
        updated=updated,
        removed=removed,
        wallets=[_robot_response(db, item) for item in wallets],
    )


@router.patch("/address-book/wallets/{wallet_id}/robot", response_model=SavedWalletResponse)
def bind_robot_wallet(
    wallet_id: str,
    payload: SavedWalletRobotUpdateRequest,
    db: Session = Depends(get_db),
) -> SavedWalletResponse:
    wallet = db.get(SavedWallet, wallet_id)
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    robot_id = (payload.robot_wallet_id or "").strip() or None
    if robot_id is not None:
        robot = db.get(RobotWallet, robot_id)
        if robot is None:
            raise HTTPException(status_code=404, detail="Robot wallet not found")
        existing_wallet = db.scalar(
            select(SavedWallet).where(SavedWallet.robot_wallet_id == robot_id, SavedWallet.id != wallet.id).limit(1)
        )
        if existing_wallet is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Robot wallet already bound to participating wallet "
                    f"{existing_wallet.label} ({existing_wallet.address})"
                ),
            )
    wallet.robot_wallet_id = robot_id
    db.commit()
    db.refresh(wallet)
    return _wallet_response(wallet)


@router.post("/tasks", response_model=CopySellTaskResponse)
def create_copy_sell_task(payload: CopySellTaskCreateRequest, db: Session = Depends(get_db)) -> CopySellTaskResponse:
    settings = get_settings()
    chain = payload.chain.lower()
    if chain not in settings.chain_configs:
        raise HTTPException(status_code=400, detail=f"Unsupported chain: {payload.chain}")
    token_address = _validate_evm_address(payload.token_address, "token")
    output_token_address = _validate_evm_address(payload.output_token_address, "output token")
    route_preference = _validate_route_preference(payload.route_preference)
    trigger_baseline_raw = _amount_to_raw(payload.trigger_baseline, _token_decimals(chain, token_address))
    task = CopySellTask(
        id=f"cst_{uuid.uuid4().hex[:12]}",
        name=(payload.name or "").strip() or _default_task_name(chain, token_address),
        chain=chain,
        token_address=token_address,
        output_token_address=output_token_address,
        trigger_baseline_raw=trigger_baseline_raw,
        route_preference=route_preference,
        allow_zero_min_output=payload.allow_zero_min_output,
        poll_interval_seconds=payload.poll_interval_seconds,
        slippage_bps=payload.slippage_bps,
        max_retries=payload.max_retries,
        status="paused",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return _task_response(db, task)


@router.get("/tasks", response_model=list[CopySellTaskResponse])
def list_copy_sell_tasks(db: Session = Depends(get_db)) -> list[CopySellTaskResponse]:
    rows = db.scalars(select(CopySellTask).order_by(CopySellTask.created_at.desc())).all()
    return [_task_response(db, row, include_attempts=False) for row in rows]


@router.get("/tasks/{task_id}", response_model=CopySellTaskResponse)
def get_copy_sell_task(task_id: str, db: Session = Depends(get_db)) -> CopySellTaskResponse:
    task = db.get(CopySellTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Copy-sell task not found")
    return _task_response(db, task)


@router.post("/tasks/{task_id}/quote", response_model=list[CopySellQuoteResponse])
def quote_copy_sell(task_id: str, db: Session = Depends(get_db)) -> list[CopySellQuoteResponse]:
    task = db.get(CopySellTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Copy-sell task not found")
    try:
        quotes = quote_copy_sell_task(db, task)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [
        CopySellQuoteResponse(
            robotWalletId=item.robot_wallet_id,
            walletAddress=item.wallet_address,
            balanceRaw=item.balance_raw,
            quotedOutputRaw=item.quoted_output_raw,
            minOutputRaw=item.min_output_raw,
            route=item.route,
            errorMessage=item.error_message,
        )
        for item in quotes
    ]


@router.post("/tasks/{task_id}/scan-routes", response_model=list[CopySellRouteScanResponse])
def scan_copy_sell_routes(
    task_id: str,
    payload: CopySellRouteScanRequest,
    db: Session = Depends(get_db),
) -> list[CopySellRouteScanResponse]:
    task = db.get(CopySellTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Copy-sell task not found")
    side = (payload.side or "sell").strip().lower()
    if side not in {"sell", "buy"}:
        raise HTTPException(status_code=400, detail="side must be sell or buy")
    route_preference = _validate_route_preference(payload.route_preference)

    settings = get_settings()
    config = settings.chain_configs.get(task.chain.lower())
    if config is None:
        raise HTTPException(status_code=400, detail=f"Unsupported chain: {task.chain}")
    if not has_whitelisted_dex_routes(task.chain):
        raise HTTPException(status_code=400, detail=f"No whitelisted DEX routes configured for chain: {task.chain}")

    token_in = task.token_address if side == "sell" else task.output_token_address
    token_out = task.output_token_address if side == "sell" else task.token_address
    amount_raw = int(_amount_to_raw(payload.amount, _token_decimals(task.chain, token_in)))
    try:
        routes = DirectPoolDexAdapter(config).scan_quotes(
            token_in,
            token_out,
            amount_raw,
            route_preference=route_preference,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [
        _route_scan_response(
            route,
            slippage_bps=task.slippage_bps,
            allow_zero_min_output=task.allow_zero_min_output,
        )
        for route in routes
    ]


@router.post("/tasks/{task_id}/seed-buy", response_model=list[CopySellSeedBuyResponse])
def seed_buy_copy_sell(
    task_id: str,
    payload: CopySellSeedBuyRequest,
    db: Session = Depends(get_db),
) -> list[CopySellSeedBuyResponse]:
    task = db.get(CopySellTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Copy-sell task not found")
    try:
        rows = seed_buy_copy_sell_task(
            db,
            task,
            spend_amount=payload.spend_amount,
            slippage_bps=payload.slippage_bps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [_seed_buy_response(row) for row in rows]


@router.post("/tasks/{task_id}/start", response_model=CopySellTaskResponse)
def start_copy_sell_task(task_id: str, db: Session = Depends(get_db)) -> CopySellTaskResponse:
    if not get_settings().robot_trading_enabled:
        raise HTTPException(status_code=400, detail="ROBOT_TRADING_ENABLED=false，真实交易接口已关闭")
    task = db.get(CopySellTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Copy-sell task not found")
    if not has_whitelisted_dex_routes(task.chain):
        raise HTTPException(status_code=400, detail=f"No whitelisted DEX routes configured for chain: {task.chain}")
    if not db.scalar(select(RobotWallet.id).join(SavedWallet, SavedWallet.robot_wallet_id == RobotWallet.id).limit(1)):
        raise HTTPException(status_code=400, detail="没有已绑定地址簿的机器人钱包")
    task.status = "active"
    task.error_message = None
    db.commit()
    db.refresh(task)
    return _task_response(db, task)


@router.post("/tasks/{task_id}/pause", response_model=CopySellTaskResponse)
def pause_copy_sell_task(task_id: str, db: Session = Depends(get_db)) -> CopySellTaskResponse:
    task = db.get(CopySellTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Copy-sell task not found")
    task.status = "paused"
    db.commit()
    db.refresh(task)
    return _task_response(db, task)


@router.post("/tasks/{task_id}/delete", response_model=CopySellTaskResponse)
def delete_copy_sell_task(task_id: str, db: Session = Depends(get_db)) -> CopySellTaskResponse:
    task = db.get(CopySellTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Copy-sell task not found")
    response = _task_response(db, task)
    db.delete(task)
    db.commit()
    return response

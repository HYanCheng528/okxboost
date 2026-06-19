from __future__ import annotations

import json
import logging
from threading import Lock, Thread
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import SessionLocal, get_db
from ..models import AirdropClaimContract, BoostReward, SavedWallet
from ..schemas import (
    RewardClaimContractResponse,
    RewardClaimContractUpdateRequest,
    RewardListItem,
    RewardScanRequest,
    RewardScanResponse,
    RewardSyncFeishuRequest,
    RewardSyncFeishuResponse,
    RewardUpdateRequest,
    RewardWalletResult,
)
from ..services.feishu_bitable import FeishuBitableService
from ..services.address_utils import address_matches_chain, is_evm_address, is_solana_address, normalize_chain_address
from ..services.reward_scanner import ClaimContractHit, scan_reward
from ..services.solana_reward_scanner import scan_solana_reward

router = APIRouter(prefix="/api/rewards", tags=["rewards"])
UTC8 = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)
_period_lock = Lock()


def _next_reward_period(db: Session, *, token_address: str, chain: str) -> int:
    max_period = db.scalar(
        select(func.max(BoostReward.period)).where(
            BoostReward.token_address == token_address,
            BoostReward.chain == chain,
        )
    )
    return (max_period or 0) + 1


def _contract_response(item: AirdropClaimContract) -> RewardClaimContractResponse:
    return RewardClaimContractResponse(
        contractId=item.id,
        chain=item.chain,
        tokenAddress=item.token_address,
        contractAddress=item.contract_address,
        functionSelector=item.function_selector,
        codeHash=item.code_hash,
        status=item.status,
        firstSeenTx=item.first_seen_tx,
        hitCount=item.hit_count,
        createdAt=item.created_at,
        updatedAt=item.updated_at,
    )


def _known_claim_contract_statuses(db: Session, *, chain: str, token_address: str) -> dict[str, str]:
    rows = db.scalars(
        select(AirdropClaimContract).where(
            AirdropClaimContract.chain == chain,
            AirdropClaimContract.token_address == token_address,
        )
    ).all()
    priority = {"ignored": 1, "candidate": 2, "confirmed": 3}
    statuses: dict[str, str] = {}
    for row in rows:
        current = statuses.get(row.contract_address.lower())
        if current is None or priority.get(row.status, 0) > priority.get(current, 0):
            statuses[row.contract_address.lower()] = row.status
    return statuses


def _store_claim_contract_hits(db: Session, hits: list[ClaimContractHit]) -> None:
    for hit in hits:
        chain = hit.chain.lower()
        token_address = hit.token_address.lower()
        contract_address = hit.contract_address.lower()
        function_selector = (hit.function_selector or "").lower()
        existing = db.scalar(
            select(AirdropClaimContract).where(
                AirdropClaimContract.chain == chain,
                AirdropClaimContract.token_address == token_address,
                AirdropClaimContract.contract_address == contract_address,
                AirdropClaimContract.function_selector == function_selector,
            )
        )
        if existing is None:
            db.add(
                AirdropClaimContract(
                    chain=chain,
                    token_address=token_address,
                    contract_address=contract_address,
                    function_selector=function_selector,
                    code_hash=hit.code_hash,
                    status="candidate",
                    first_seen_tx=hit.first_seen_tx,
                    hit_count=max(1, hit.hit_count),
                )
            )
        else:
            existing.hit_count += max(1, hit.hit_count)
            if not existing.code_hash and hit.code_hash:
                existing.code_hash = hit.code_hash
            if not existing.first_seen_tx and hit.first_seen_tx:
                existing.first_seen_tx = hit.first_seen_tx


def _validate_feishu_requirements() -> None:
    settings = get_settings()
    missing: list[str] = []
    if not settings.feishu_app_id:
        missing.append("FEISHU_APP_ID")
    if not settings.feishu_app_secret:
        missing.append("FEISHU_APP_SECRET")
    if not settings.feishu_app_token:
        missing.append("FEISHU_APP_TOKEN")
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing Feishu config: {', '.join(missing)}")


def _wallet_results_from_reward(reward: BoostReward) -> list[dict]:
    try:
        data = json.loads(reward.wallets_json) if reward.wallets_json else []
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _wallet_response_from_reward(reward: BoostReward) -> list[RewardWalletResult]:
    return [
        RewardWalletResult(
            wallet=str(w.get("wallet", "")),
            walletLabel=w.get("label"),
            claimed=Decimal(str(w.get("claimed", "0") or "0")),
            soldUsdt=Decimal(str(w.get("soldUsdt", "0") or "0")),
        )
        for w in _wallet_results_from_reward(reward)
    ]


def _reward_project_name(reward: BoostReward) -> str:
    return reward.token_symbol or reward.token_address[:10]


def _reward_response(reward: BoostReward) -> RewardScanResponse:
    return RewardScanResponse(
        rewardId=reward.id,
        period=reward.period,
        projectName=_reward_project_name(reward),
        tokenAddress=reward.token_address,
        tokenSymbol=reward.token_symbol,
        chain=reward.chain,
        scanDate=reward.scan_date,
        status=reward.status,
        errorMessage=reward.error_message,
        results=_wallet_response_from_reward(reward),
        totalClaimed=reward.total_claimed,
        totalSoldUsdt=reward.total_sold_usdt,
    )


def _friendly_scan_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    lower = text.lower()
    if "<!doctype" in lower or "<html" in lower or "cloudflare" in lower or "gateway time" in lower:
        return "网关超时或上游返回 HTML 错误页。空投检测已在后台结束为失败，请缩小范围或降低并行度后重试。"
    if "read timed out" in lower or "timeout" in lower:
        return f"RPC 请求超时：{text}"
    return text[:1000]


def _run_reward_scan(
    *,
    reward_id: str,
    token_address: str,
    chain: str,
    wallets: list[tuple[str, str | None]],
    scan_date: str,
) -> None:
    try:
        db = SessionLocal()
        try:
            known_statuses = (
                {}
                if chain.lower() == "solana"
                else _known_claim_contract_statuses(db, chain=chain.lower(), token_address=token_address.lower())
            )
        finally:
            db.close()
        if chain.lower() == "solana":
            scan_result = scan_solana_reward(
                token_address=token_address,
                wallets=wallets,
                scan_date=scan_date,
            )
        else:
            scan_result = scan_reward(
                token_address=token_address,
                chain=chain,
                wallets=wallets,
                scan_date=scan_date,
                known_contract_statuses=known_statuses,
            )
        if len(scan_result) == 2:
            results, token_symbol = scan_result
            claim_hits = []
        else:
            results, token_symbol, claim_hits = scan_result
    except Exception as exc:
        logger.exception("Airdrop scan failed: %s", reward_id)
        db = SessionLocal()
        try:
            reward = db.get(BoostReward, reward_id)
            if reward is None:
                return
            reward.status = "failed"
            reward.error_message = _friendly_scan_error(exc)
            db.commit()
        finally:
            db.close()
        return

    wallets_data = [
        {"wallet": r.wallet, "label": r.label, "claimed": str(r.claimed), "soldUsdt": str(r.sold_usdt)}
        for r in results
    ]
    db = SessionLocal()
    try:
        _store_claim_contract_hits(db, claim_hits)
        reward = db.get(BoostReward, reward_id)
        if reward is None:
            return
        reward.token_symbol = token_symbol
        reward.wallets_json = json.dumps(wallets_data)
        reward.total_claimed = sum(r.claimed for r in results)
        reward.total_sold_usdt = sum(r.sold_usdt for r in results)
        reward.status = "completed"
        reward.error_message = None
        db.commit()
    finally:
        db.close()


def _feishu_date_value(scan_date: str) -> int:
    day = datetime.strptime(scan_date, "%Y-%m-%d")
    local_midnight = datetime(day.year, day.month, day.day, tzinfo=UTC8)
    return int(local_midnight.timestamp() * 1000)


def _add_field(fields: dict[str, object], field_name: str, value: object) -> None:
    name = field_name.strip()
    if name:
        fields[name] = value


def _saved_wallet_for_chain(wallet: SavedWallet, chain: str) -> tuple[str, str | None] | None:
    if chain == "solana":
        solana_address = (wallet.solana_address or "").strip()
        if solana_address and is_solana_address(solana_address):
            return solana_address, wallet.label
        if is_solana_address(wallet.address):
            return wallet.address, wallet.label
        return None
    if address_matches_chain(chain, wallet.address):
        return wallet.address, wallet.label
    return None


def _resolve_selected_wallet_for_chain(db: Session, *, chain: str, wallet_address: str) -> tuple[str, str | None]:
    raw = wallet_address.strip()
    if chain == "solana":
        saved: SavedWallet | None = None
        if is_evm_address(raw):
            evm_address = raw.lower()
            saved = db.scalar(select(SavedWallet).where(SavedWallet.address == evm_address))
        elif is_solana_address(raw):
            saved = db.scalar(
                select(SavedWallet).where(
                    (SavedWallet.solana_address == raw) | (SavedWallet.address == raw)
                )
            )
        else:
            raise HTTPException(status_code=400, detail="Invalid Solana wallet address")
        if saved:
            resolved = _saved_wallet_for_chain(saved, chain)
            if not resolved:
                raise HTTPException(status_code=400, detail=f"钱包 {saved.label} 未绑定 Solana 地址")
            return resolved
        return normalize_chain_address(chain, raw), None

    address = normalize_chain_address(chain, raw)
    saved = db.scalar(select(SavedWallet).where(SavedWallet.address == address))
    return (saved.address, saved.label) if saved else (address, None)


@router.post("/scan", response_model=RewardScanResponse)
def scan_rewards(payload: RewardScanRequest, db: Session = Depends(get_db)) -> RewardScanResponse:
    chain = payload.chain.lower()
    scan_date = payload.scan_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    settings = get_settings()

    if chain == "solana":
        if not settings.solana_rpc_urls:
            raise HTTPException(status_code=400, detail="Missing SOLANA_RPC_URL")
        try:
            token_address = normalize_chain_address(chain, payload.token_address)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif chain in settings.chain_configs:
        token_address = payload.token_address.strip().lower()
        try:
            token_address = normalize_chain_address(chain, token_address)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported chain: {payload.chain}")
    try:
        datetime.strptime(scan_date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="scanDate must be YYYY-MM-DD in UTC") from exc

    wallets: list[tuple[str, str | None]] = []
    if payload.wallet_address:
        try:
            wallets = [_resolve_selected_wallet_for_chain(db, chain=chain, wallet_address=payload.wallet_address)]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        saved_wallets = db.scalars(select(SavedWallet).order_by(SavedWallet.created_at)).all()
        wallets = [item for row in saved_wallets if (item := _saved_wallet_for_chain(row, chain))]

    if not wallets:
        raise HTTPException(status_code=400, detail="No wallets found")

    initial_wallets_data = [
        {"wallet": wallet, "label": label, "claimed": "0", "soldUsdt": "0"}
        for wallet, label in wallets
    ]
    with _period_lock:
        period = payload.period or _next_reward_period(db, token_address=token_address, chain=chain)
        reward = BoostReward(
            id=f"rwd_{uuid.uuid4().hex[:12]}",
            period=period,
            token_address=token_address,
            token_symbol=None,
            chain=chain,
            scan_date=scan_date,
            wallets_json=json.dumps(initial_wallets_data),
            total_claimed=Decimal("0"),
            total_sold_usdt=Decimal("0"),
            status="running",
            error_message=None,
        )
        db.add(reward)
        db.commit()
        db.refresh(reward)

    Thread(
        target=_run_reward_scan,
        kwargs={
            "reward_id": reward.id,
            "token_address": token_address,
            "chain": chain,
            "wallets": wallets,
            "scan_date": scan_date,
        },
        daemon=True,
    ).start()

    return _reward_response(reward)


@router.get("/contracts", response_model=list[RewardClaimContractResponse])
def list_claim_contracts(
    chain: str | None = None,
    token_address: str | None = Query(default=None, alias="tokenAddress"),
    token_address_snake: str | None = Query(default=None, alias="token_address"),
    db: Session = Depends(get_db),
) -> list[RewardClaimContractResponse]:
    token_filter = token_address or token_address_snake
    stmt = select(AirdropClaimContract).order_by(AirdropClaimContract.updated_at.desc())
    if chain:
        stmt = stmt.where(AirdropClaimContract.chain == chain.lower())
    if token_filter:
        stmt = stmt.where(AirdropClaimContract.token_address == token_filter.lower())
    return [_contract_response(item) for item in db.scalars(stmt).all()]


@router.patch("/contracts/{contract_id}", response_model=RewardClaimContractResponse)
def update_claim_contract(
    contract_id: int,
    payload: RewardClaimContractUpdateRequest,
    db: Session = Depends(get_db),
) -> RewardClaimContractResponse:
    status = payload.status.lower()
    if status not in {"candidate", "confirmed", "ignored"}:
        raise HTTPException(status_code=400, detail="status must be candidate, confirmed, or ignored")
    item = db.get(AirdropClaimContract, contract_id)
    if item is None:
        raise HTTPException(status_code=404, detail="空投领取合约不存在")
    item.status = status
    db.commit()
    db.refresh(item)
    return _contract_response(item)


@router.patch("/{reward_id}", response_model=RewardScanResponse)
def update_reward(
    reward_id: str,
    payload: RewardUpdateRequest,
    db: Session = Depends(get_db),
) -> RewardScanResponse:
    reward = db.get(BoostReward, reward_id)
    if reward is None:
        raise HTTPException(status_code=404, detail="空投记录不存在")
    if payload.period is not None:
        reward.period = payload.period
    db.commit()
    db.refresh(reward)
    return _reward_response(reward)


@router.get("", response_model=list[RewardListItem])
def list_rewards(db: Session = Depends(get_db)) -> list[RewardListItem]:
    rewards = db.scalars(select(BoostReward).order_by(BoostReward.created_at.desc())).all()
    items: list[RewardListItem] = []
    for r in rewards:
        items.append(RewardListItem(
            rewardId=r.id,
            period=r.period,
            projectName=_reward_project_name(r),
            tokenAddress=r.token_address,
            tokenSymbol=r.token_symbol,
            chain=r.chain,
            scanDate=r.scan_date,
            status=r.status,
            errorMessage=r.error_message,
            wallets=_wallet_response_from_reward(r),
            totalClaimed=r.total_claimed,
            totalSoldUsdt=r.total_sold_usdt,
            createdAt=r.created_at,
        ))
    return items


@router.get("/{reward_id}", response_model=RewardScanResponse)
def get_reward(reward_id: str, db: Session = Depends(get_db)) -> RewardScanResponse:
    reward = db.get(BoostReward, reward_id)
    if reward is None:
        raise HTTPException(status_code=404, detail="空投记录不存在")
    return _reward_response(reward)


@router.post("/{reward_id}/sync-feishu", response_model=RewardSyncFeishuResponse)
def sync_reward_to_feishu(
    reward_id: str,
    payload: RewardSyncFeishuRequest,
    db: Session = Depends(get_db),
) -> RewardSyncFeishuResponse:
    reward = db.get(BoostReward, reward_id)
    if reward is None:
        raise HTTPException(status_code=404, detail="空投记录不存在")
    if reward.status != "completed":
        if reward.status == "running":
            raise HTTPException(status_code=400, detail="空投检测仍在后台运行，完成后再同步到飞书")
        raise HTTPException(status_code=400, detail=f"空投检测未完成：{reward.error_message or reward.status}")

    _validate_feishu_requirements()

    table_id = (payload.table_id or "").strip()

    selected_wallet = payload.wallet_address.strip() if payload.wallet_address else None
    if selected_wallet and reward.chain != "solana":
        selected_wallet = selected_wallet.lower()
    wallet_rows = _wallet_results_from_reward(reward)
    if selected_wallet:
        wallet_rows = [
            item
            for item in wallet_rows
            if (str(item.get("wallet", "")) if reward.chain == "solana" else str(item.get("wallet", "")).lower())
            == selected_wallet
        ]
        if not wallet_rows:
            raise HTTPException(status_code=400, detail=f"wallet not in air drop record: {selected_wallet}")

    records_by_wallet: dict[str, list[dict[str, object]]] = {}
    project_name = reward.token_symbol or reward.token_address[:10]
    for item in wallet_rows:
        wallet = str(item.get("wallet", ""))
        if reward.chain != "solana":
            wallet = wallet.lower()
        claimed = Decimal(str(item.get("claimed", "0") or "0"))
        sold_usdt = Decimal(str(item.get("soldUsdt", "0") or "0"))
        if not selected_wallet and not payload.include_zero_wallets and claimed == 0 and sold_usdt == 0:
            continue

        avg_sell_price = Decimal("0")
        if claimed != 0:
            avg_sell_price = sold_usdt / claimed

        fields: dict[str, object] = {}
        _add_field(fields, payload.date_field, _feishu_date_value(reward.scan_date))
        _add_field(fields, payload.period_field, payload.period_override if payload.period_override is not None else reward.period)
        _add_field(fields, payload.project_field, project_name)
        _add_field(fields, payload.quantity_field, float(claimed))
        _add_field(fields, payload.avg_sell_price_field, float(avg_sell_price))
        _add_field(fields, payload.boost_claim_field, float(sold_usdt))
        records_by_wallet.setdefault(wallet, []).append(fields)

    service = FeishuBitableService(get_settings())
    try:
        if table_id:
            records = [record for group in records_by_wallet.values() for record in group]
            appended_count = service.append_raw_records(table_id=table_id, records=records)
            response_table_id = table_id
        else:
            wallets = list(records_by_wallet.keys())
            saved_wallets = db.scalars(
                select(SavedWallet).where(
                    (SavedWallet.address.in_(wallets)) | (SavedWallet.solana_address.in_(wallets))
                )
            ).all()
            wallet_table_map: dict[str, str] = {}
            for item in saved_wallets:
                table_id_value = (item.feishu_airdrop_table_id or "").strip()
                wallet_table_map[item.address.lower()] = table_id_value
                if item.solana_address:
                    wallet_table_map[item.solana_address] = table_id_value
            missing = [wallet for wallet in wallets if not wallet_table_map.get(wallet)]
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail=f"wallets missing Feishu airdrop table mapping: {', '.join(missing)}",
                )
            appended_count = 0
            for wallet, records in records_by_wallet.items():
                appended_count += service.append_raw_records(
                    table_id=wallet_table_map[wallet],
                    records=records,
                )
            response_table_id = wallet_table_map[wallets[0]] if selected_wallet and len(wallets) == 1 else "wallet_mappings"
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return RewardSyncFeishuResponse(
        rewardId=reward.id,
        tableId=response_table_id,
        walletAddress=selected_wallet,
        appendedCount=appended_count,
    )


@router.delete("/{reward_id}")
def delete_reward(reward_id: str, db: Session = Depends(get_db)):
    reward = db.get(BoostReward, reward_id)
    if reward is None:
        raise HTTPException(status_code=404, detail="空投记录不存在")
    db.delete(reward)
    db.commit()
    return {"ok": True}

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from dataclasses import dataclass, field
from decimal import Decimal
from threading import Lock, Thread
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import SessionLocal, get_db
from ..models import AppCache, WalletProfitAdjustment, utc_now
from ..schemas import (
    DashboardSettingsRequest,
    DashboardSettingsResponse,
    WalletProfitAdjustmentCreateRequest,
    WalletProfitAdjustmentResponse,
)
from ..services.feishu_bitable import FeishuBitableService
from ..services.boost_stats import compute_boost_average
from ..services.dashboard_settings import (
    get_dashboard_min_daily_average,
    get_dashboard_settings,
    save_dashboard_settings,
)
from ..services.scheduler import trigger_reminder_now
from ..services.wallet_profit import ProfitFieldConfig, compute_wallet_profit, normalize_wallet_key

router = APIRouter(prefix="/api/stats", tags=["stats"])
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
WALLET_PROFIT_CACHE_PREFIX = "wallet_profit:"


@dataclass
class WalletProfitRefreshJob:
    job_id: str
    cache_key: str
    status: str = "running"
    progress_percent: int = 0
    progress_message: str = "等待刷新。"
    error_message: str | None = None
    warnings: list[str] = field(default_factory=list)
    started_at: object | None = None
    updated_at: object | None = None
    cached_at: str | None = None


_wallet_profit_jobs: dict[str, WalletProfitRefreshJob] = {}
_wallet_profit_jobs_lock = Lock()


def _wallet_profit_job_payload(job: WalletProfitRefreshJob) -> dict[str, object]:
    return {
        "jobId": job.job_id,
        "cacheKey": job.cache_key,
        "status": job.status,
        "progressPercent": job.progress_percent,
        "progressMessage": job.progress_message,
        "errorMessage": job.error_message,
        "warnings": job.warnings[-20:],
        "cachedAt": job.cached_at,
    }


def _store_wallet_profit_job(job: WalletProfitRefreshJob) -> None:
    now = utc_now()
    if job.started_at is None:
        job.started_at = now
    job.updated_at = now
    with _wallet_profit_jobs_lock:
        _wallet_profit_jobs[job.job_id] = job


def _update_wallet_profit_job(job_id: str, **changes: object) -> None:
    with _wallet_profit_jobs_lock:
        job = _wallet_profit_jobs[job_id]
        for key, value in changes.items():
            setattr(job, key, value)
        job.updated_at = utc_now()


def _get_wallet_profit_job(job_id: str) -> WalletProfitRefreshJob:
    with _wallet_profit_jobs_lock:
        job = _wallet_profit_jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return WalletProfitRefreshJob(
            job_id=job.job_id,
            cache_key=job.cache_key,
            status=job.status,
            progress_percent=job.progress_percent,
            progress_message=job.progress_message,
            error_message=job.error_message,
            warnings=list(job.warnings),
            started_at=job.started_at,
            updated_at=job.updated_at,
            cached_at=job.cached_at,
        )


def _find_running_wallet_profit_job(cache_key: str) -> WalletProfitRefreshJob | None:
    with _wallet_profit_jobs_lock:
        for job in _wallet_profit_jobs.values():
            if job.cache_key == cache_key and job.status == "running":
                return WalletProfitRefreshJob(
                    job_id=job.job_id,
                    cache_key=job.cache_key,
                    status=job.status,
                    progress_percent=job.progress_percent,
                    progress_message=job.progress_message,
                    error_message=job.error_message,
                    warnings=list(job.warnings),
                    started_at=job.started_at,
                    updated_at=job.updated_at,
                    cached_at=job.cached_at,
                )
    return None


def _wallet_profit_cache_key(
    *,
    start_date: date | None,
    end_date: date | None,
    rebate_table_id: str | None,
    fields: ProfitFieldConfig,
) -> str:
    payload = {
        "startDate": start_date.isoformat() if start_date else "",
        "endDate": end_date.isoformat() if end_date else "",
        "rebateTableId": (rebate_table_id or "").strip(),
        "fields": {
            "tradeDateField": fields.trade_date_field,
            "wearField": fields.wear_field,
            "rewardDateField": fields.reward_date_field,
            "incomeField": fields.income_field,
            "rebateDateField": fields.rebate_date_field,
            "rebateAmountField": fields.rebate_amount_field,
            "rebateWalletField": fields.rebate_wallet_field,
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{WALLET_PROFIT_CACHE_PREFIX}{hashlib.sha1(raw.encode('utf-8')).hexdigest()}"


def _load_wallet_profit_cache(db: Session, cache_key: str) -> dict | None:
    row = db.get(AppCache, cache_key)
    if not row:
        return None
    try:
        payload = json.loads(row.payload_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    payload["fromCache"] = True
    payload["cacheKey"] = cache_key
    payload["cachedAt"] = row.updated_at.isoformat()
    return payload


def _save_wallet_profit_cache(db: Session, cache_key: str, payload: dict) -> dict:
    now = utc_now()
    cached_payload = dict(payload)
    cached_payload["fromCache"] = False
    cached_payload["cacheKey"] = cache_key
    cached_payload["cachedAt"] = now.isoformat()
    payload_json = json.dumps(cached_payload, ensure_ascii=False, default=str, separators=(",", ":"))

    row = db.get(AppCache, cache_key)
    if row:
        row.payload_json = payload_json
        row.updated_at = now
    else:
        row = AppCache(key=cache_key, payload_json=payload_json, created_at=now, updated_at=now)
        db.add(row)
    db.commit()
    return cached_payload


def _clear_wallet_profit_cache(db: Session) -> None:
    db.query(AppCache).filter(AppCache.key.like(f"{WALLET_PROFIT_CACHE_PREFIX}%")).delete(synchronize_session=False)
    db.commit()


def _profit_fields_from_query(
    *,
    trade_date_field: str,
    wear_field: str,
    reward_date_field: str,
    income_field: str,
    rebate_date_field: str,
    rebate_amount_field: str,
    rebate_wallet_field: str,
) -> ProfitFieldConfig:
    return ProfitFieldConfig(
        trade_date_field=trade_date_field.strip() or "日期",
        wear_field=wear_field.strip() or "磨损",
        reward_date_field=reward_date_field.strip() or "日期",
        income_field=income_field.strip() or "Boost领取奖励",
        rebate_date_field=rebate_date_field.strip() or "日期",
        rebate_amount_field=rebate_amount_field.strip() or "金额",
        rebate_wallet_field=rebate_wallet_field.strip() or "多选",
    )


def _run_wallet_profit_refresh_job(
    *,
    job_id: str,
    cache_key: str,
    start_date: date | None,
    end_date: date | None,
    rebate_table_id: str | None,
    fields: ProfitFieldConfig,
) -> None:
    db = SessionLocal()
    try:
        _update_wallet_profit_job(job_id, progress_percent=10, progress_message="正在读取飞书数据。")
        settings = get_settings()
        result = compute_wallet_profit(
            db=db,
            settings=settings,
            start_date=start_date,
            end_date=end_date,
            rebate_table_id=rebate_table_id,
            fields=fields,
        )
        _update_wallet_profit_job(job_id, progress_percent=85, progress_message="正在写入后端缓存。")
        cached = _save_wallet_profit_cache(db, cache_key, result)
        _update_wallet_profit_job(
            job_id,
            status="completed",
            progress_percent=100,
            progress_message="盈亏数据已刷新并写入后端缓存。",
            warnings=list(cached.get("warnings") or []),
            cached_at=str(cached.get("cachedAt") or ""),
        )
    except Exception as exc:
        _update_wallet_profit_job(
            job_id,
            status="failed",
            progress_percent=100,
            progress_message="盈亏数据刷新失败。",
            error_message=str(exc),
        )
    finally:
        db.close()


def _start_wallet_profit_refresh_job(
    *,
    cache_key: str,
    start_date: date | None,
    end_date: date | None,
    rebate_table_id: str | None,
    fields: ProfitFieldConfig,
) -> dict[str, object]:
    running = _find_running_wallet_profit_job(cache_key)
    if running:
        return _wallet_profit_job_payload(running)

    job = WalletProfitRefreshJob(
        job_id=f"wpf_{uuid4().hex[:12]}",
        cache_key=cache_key,
        progress_percent=2,
        progress_message="刷新任务已创建，后台正在处理。",
    )
    _store_wallet_profit_job(job)
    Thread(
        target=_run_wallet_profit_refresh_job,
        kwargs={
            "job_id": job.job_id,
            "cache_key": cache_key,
            "start_date": start_date,
            "end_date": end_date,
            "rebate_table_id": rebate_table_id,
            "fields": fields,
        },
        daemon=True,
    ).start()
    return _wallet_profit_job_payload(_get_wallet_profit_job(job.job_id))


@router.get("/boost-average")
def get_boost_average(
    min_daily_average: Decimal | None = Query(default=None, alias="minDailyAverage", ge=Decimal("0")),
    db: Session = Depends(get_db),
) -> dict:
    effective_min = get_dashboard_min_daily_average(db) if min_daily_average is None else min_daily_average
    return compute_boost_average(db, min_daily_average=effective_min)


@router.get("/settings/dashboard", response_model=DashboardSettingsResponse)
def get_dashboard_settings_api(db: Session = Depends(get_db)) -> dict:
    return get_dashboard_settings(db)


@router.put("/settings/dashboard", response_model=DashboardSettingsResponse)
def update_dashboard_settings_api(
    payload: DashboardSettingsRequest,
    db: Session = Depends(get_db),
) -> dict:
    return save_dashboard_settings(db, min_daily_average=payload.min_daily_average)


@router.post("/trigger-reminder")
def trigger_reminder(
    min_daily_average: Decimal | None = Query(default=None, alias="minDailyAverage", ge=Decimal("0")),
) -> dict:
    result = trigger_reminder_now(min_daily_average=min_daily_average)
    return {"message": result}


@router.get("/wallet-profit")
def get_wallet_profit(
    start_date: date | None = Query(default=None, alias="startDate"),
    end_date: date | None = Query(default=None, alias="endDate"),
    rebate_table_id: str | None = Query(default=None, alias="rebateTableId"),
    refresh: bool = Query(default=False),
    trade_date_field: str = Query(default="日期", alias="tradeDateField"),
    wear_field: str = Query(default="磨损", alias="wearField"),
    reward_date_field: str = Query(default="日期", alias="rewardDateField"),
    income_field: str = Query(default="Boost领取奖励", alias="incomeField"),
    rebate_date_field: str = Query(default="日期", alias="rebateDateField"),
    rebate_amount_field: str = Query(default="金额", alias="rebateAmountField"),
    rebate_wallet_field: str = Query(default="多选", alias="rebateWalletField"),
    db: Session = Depends(get_db),
) -> dict:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="startDate cannot be later than endDate")
    fields = _profit_fields_from_query(
        trade_date_field=trade_date_field,
        wear_field=wear_field,
        reward_date_field=reward_date_field,
        income_field=income_field,
        rebate_date_field=rebate_date_field,
        rebate_amount_field=rebate_amount_field,
        rebate_wallet_field=rebate_wallet_field,
    )
    cache_key = _wallet_profit_cache_key(
        start_date=start_date,
        end_date=end_date,
        rebate_table_id=rebate_table_id,
        fields=fields,
    )
    if not refresh:
        cached = _load_wallet_profit_cache(db, cache_key)
        if cached:
            return cached
        raise HTTPException(status_code=404, detail="wallet profit cache not found")

    settings = get_settings()
    if not settings.feishu_app_id or not settings.feishu_app_secret or not settings.feishu_app_token:
        raise HTTPException(status_code=400, detail="Missing Feishu config")
    try:
        result = compute_wallet_profit(
            db=db,
            settings=settings,
            start_date=start_date,
            end_date=end_date,
            rebate_table_id=rebate_table_id,
            fields=fields,
        )
        return _save_wallet_profit_cache(db, cache_key, result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/wallet-profit/refresh-jobs")
def create_wallet_profit_refresh_job(
    start_date: date | None = Query(default=None, alias="startDate"),
    end_date: date | None = Query(default=None, alias="endDate"),
    rebate_table_id: str | None = Query(default=None, alias="rebateTableId"),
    trade_date_field: str = Query(default="日期", alias="tradeDateField"),
    wear_field: str = Query(default="磨损", alias="wearField"),
    reward_date_field: str = Query(default="日期", alias="rewardDateField"),
    income_field: str = Query(default="Boost领取奖励", alias="incomeField"),
    rebate_date_field: str = Query(default="日期", alias="rebateDateField"),
    rebate_amount_field: str = Query(default="金额", alias="rebateAmountField"),
    rebate_wallet_field: str = Query(default="多选", alias="rebateWalletField"),
) -> dict[str, object]:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="startDate cannot be later than endDate")
    settings = get_settings()
    if not settings.feishu_app_id or not settings.feishu_app_secret or not settings.feishu_app_token:
        raise HTTPException(status_code=400, detail="Missing Feishu config")
    fields = _profit_fields_from_query(
        trade_date_field=trade_date_field,
        wear_field=wear_field,
        reward_date_field=reward_date_field,
        income_field=income_field,
        rebate_date_field=rebate_date_field,
        rebate_amount_field=rebate_amount_field,
        rebate_wallet_field=rebate_wallet_field,
    )
    cache_key = _wallet_profit_cache_key(
        start_date=start_date,
        end_date=end_date,
        rebate_table_id=rebate_table_id,
        fields=fields,
    )
    return _start_wallet_profit_refresh_job(
        cache_key=cache_key,
        start_date=start_date,
        end_date=end_date,
        rebate_table_id=rebate_table_id,
        fields=fields,
    )


@router.get("/wallet-profit/refresh-jobs/{job_id}")
def get_wallet_profit_refresh_job(job_id: str) -> dict[str, object]:
    try:
        return _wallet_profit_job_payload(_get_wallet_profit_job(job_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="wallet profit refresh job not found") from exc


def _profit_adjustment_response(row: WalletProfitAdjustment) -> WalletProfitAdjustmentResponse:
    return WalletProfitAdjustmentResponse(
        adjustmentId=row.id,
        walletKey=row.wallet_key,
        month=row.month,
        lossAdjustment=Decimal(row.loss_adjustment or 0),
        rebateAdjustment=Decimal(row.rebate_adjustment or 0),
        incomeAdjustment=Decimal(row.income_adjustment or 0),
        note=row.note,
        createdAt=row.created_at,
    )


@router.get("/wallet-profit/adjustments", response_model=list[WalletProfitAdjustmentResponse])
def list_wallet_profit_adjustments(db: Session = Depends(get_db)) -> list[WalletProfitAdjustmentResponse]:
    rows = (
        db.query(WalletProfitAdjustment)
        .order_by(WalletProfitAdjustment.month.desc(), WalletProfitAdjustment.wallet_key.asc())
        .all()
    )
    return [_profit_adjustment_response(row) for row in rows]


@router.post("/wallet-profit/adjustments", response_model=WalletProfitAdjustmentResponse)
def create_wallet_profit_adjustment(
    payload: WalletProfitAdjustmentCreateRequest,
    db: Session = Depends(get_db),
) -> WalletProfitAdjustmentResponse:
    wallet_key = normalize_wallet_key(payload.wallet_key)
    if not wallet_key:
        raise HTTPException(status_code=400, detail="walletKey cannot be empty")
    if not MONTH_RE.match(payload.month):
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")
    row = WalletProfitAdjustment(
        id=f"adj_{uuid4().hex[:12]}",
        wallet_key=wallet_key,
        month=payload.month,
        loss_adjustment=payload.loss_adjustment,
        rebate_adjustment=payload.rebate_adjustment,
        income_adjustment=payload.income_adjustment,
        note=(payload.note or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _clear_wallet_profit_cache(db)
    return _profit_adjustment_response(row)


@router.delete("/wallet-profit/adjustments/{adjustment_id}")
def delete_wallet_profit_adjustment(adjustment_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    row = db.get(WalletProfitAdjustment, adjustment_id)
    if not row:
        raise HTTPException(status_code=404, detail="adjustment not found")
    db.delete(row)
    _clear_wallet_profit_cache(db)
    return {"adjustmentId": adjustment_id, "status": "deleted"}


@router.get("/feishu/tables")
def list_profit_feishu_tables() -> list[dict[str, str]]:
    settings = get_settings()
    if not settings.feishu_app_id or not settings.feishu_app_secret or not settings.feishu_app_token:
        raise HTTPException(status_code=400, detail="Missing Feishu config")
    try:
        return FeishuBitableService(settings).list_tables()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

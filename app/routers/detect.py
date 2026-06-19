from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import SavedWallet, SavedToken
from ..schemas import DetectJobCreateResponse, DetectJobStatusResponse, DetectSessionsRequest, DetectSessionsResponse
from ..services.detect_job_runner import get_detect_job, start_detect_job
from ..services.session_detector import detect_sessions

router = APIRouter(prefix="/api/detect", tags=["detect"])


@router.post("/jobs", response_model=DetectJobCreateResponse)
def create_detect_job(payload: DetectSessionsRequest) -> DetectJobCreateResponse:
    try:
        status = start_detect_job(
            payload,
            boost_multiplier=payload.boost_multiplier or Decimal("0.6"),
            base_token=payload.base_token or "USDT",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return DetectJobCreateResponse(
        jobId=status.job_id,
        status=status.status,
        progressPercent=status.progress_percent,
        progressMessage=status.progress_message,
    )


@router.get("/jobs/{job_id}", response_model=DetectJobStatusResponse)
def get_detect_job_status(job_id: str) -> DetectJobStatusResponse:
    try:
        return get_detect_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="detect job not found") from exc


@router.post("/sessions", response_model=DetectSessionsResponse)
def detect_sessions_endpoint(
    payload: DetectSessionsRequest,
    db: Session = Depends(get_db),
) -> DetectSessionsResponse:
    wallets = [
        (w.address, w.label)
        for w in db.query(SavedWallet).order_by(SavedWallet.created_at).all()
    ]

    # Filter by specific wallet if requested
    if payload.wallet_address:
        wallet_addr_lower = payload.wallet_address.lower()
        wallets = [(addr, label) for addr, label in wallets if addr.lower() == wallet_addr_lower]
        if not wallets:
            return DetectSessionsResponse(
                sessions=[], scannedPairs=0, totalSessions=0,
                errors=[f"地址簿中未找到钱包: {payload.wallet_address}"]
            )

    tokens = [
        (t.chain, t.address, t.symbol, t.name)
        for t in db.query(SavedToken).order_by(SavedToken.created_at).all()
    ]

    # Filter by chain if specified
    if payload.chain:
        chain_lower = payload.chain.lower()
        tokens = [(c, a, s, n) for c, a, s, n in tokens if c.lower() == chain_lower]

    # Filter by specific token if requested (for time range detection)
    tokens_for_time_detection = tokens
    if payload.token_address:
        token_addr_lower = payload.token_address.lower()
        tokens_for_time_detection = [
            (chain, addr, symbol, name)
            for chain, addr, symbol, name in tokens
            if addr.lower() == token_addr_lower
        ]
        if not tokens_for_time_detection:
            return DetectSessionsResponse(
                sessions=[], scannedPairs=0, totalSessions=0,
                errors=[f"地址簿中未找到代币: {payload.token_address}"]
            )

    errors: list[str] = []
    if not wallets:
        errors.append("地址簿中没有钱包，请先添加")
    if not tokens:
        errors.append("地址簿中没有代币，请先添加")
    if errors:
        return DetectSessionsResponse(
            sessions=[], scannedPairs=0, totalSessions=0, errors=errors
        )

    sessions, scanned, errs = detect_sessions(
        wallets=wallets,
        tokens=tokens,
        tokens_for_time_detection=tokens_for_time_detection,
        target_date=payload.target_date,
        scan_after=payload.scan_after,
    )
    return DetectSessionsResponse(
        sessions=sessions,
        scannedPairs=scanned,
        totalSessions=len(sessions),
        errors=errs,
    )

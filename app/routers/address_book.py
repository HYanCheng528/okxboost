from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import SavedWallet, SavedToken
from ..schemas import (
    SavedWalletCreateRequest,
    SavedWalletResponse,
    SavedTokenCreateRequest,
    SavedTokenResponse,
)
from ..config import get_settings
from ..services.token_resolver import resolve_token_metadata

router = APIRouter(prefix="/api/address-book", tags=["address-book"])

_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _validate_address(address: str) -> str:
    if not _ADDRESS_RE.match(address):
        raise HTTPException(status_code=400, detail="Invalid address format")
    return address.lower()


# --- Wallets ---

@router.get("/wallets", response_model=list[SavedWalletResponse])
def list_wallets(db: Session = Depends(get_db)):
    rows = db.query(SavedWallet).order_by(SavedWallet.created_at.desc()).all()
    return [
        SavedWalletResponse(
            walletId=r.id, label=r.label, address=r.address, createdAt=r.created_at
        )
        for r in rows
    ]


@router.post("/wallets", response_model=SavedWalletResponse, status_code=201)
def add_wallet(payload: SavedWalletCreateRequest, db: Session = Depends(get_db)):
    address = _validate_address(payload.address)
    existing = db.query(SavedWallet).filter(SavedWallet.address == address).first()
    if existing:
        raise HTTPException(status_code=409, detail="Wallet address already saved")
    wallet = SavedWallet(
        id=f"wal_{uuid.uuid4().hex[:12]}",
        label=payload.label.strip(),
        address=address,
    )
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    return SavedWalletResponse(
        walletId=wallet.id, label=wallet.label, address=wallet.address, createdAt=wallet.created_at
    )


@router.delete("/wallets/{wallet_id}", response_model=SavedWalletResponse)
def delete_wallet(wallet_id: str, db: Session = Depends(get_db)):
    wallet = db.query(SavedWallet).filter(SavedWallet.id == wallet_id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    resp = SavedWalletResponse(
        walletId=wallet.id, label=wallet.label, address=wallet.address, createdAt=wallet.created_at
    )
    db.delete(wallet)
    db.commit()
    return resp


# --- Tokens ---

@router.get("/tokens", response_model=list[SavedTokenResponse])
def list_tokens(db: Session = Depends(get_db)):
    rows = db.query(SavedToken).order_by(SavedToken.created_at.desc()).all()
    return [
        SavedTokenResponse(
            tokenId=r.id, chain=r.chain, address=r.address,
            name=r.name, symbol=r.symbol, decimals=r.decimals, createdAt=r.created_at
        )
        for r in rows
    ]


@router.post("/tokens", response_model=SavedTokenResponse, status_code=201)
def add_token(payload: SavedTokenCreateRequest, db: Session = Depends(get_db)):
    settings = get_settings()
    chain = payload.chain.lower()
    if chain not in settings.chain_configs:
        raise HTTPException(status_code=400, detail=f"Unsupported chain: {chain}")
    address = _validate_address(payload.address)
    existing = db.query(SavedToken).filter(
        SavedToken.chain == chain, SavedToken.address == address
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Token already saved for this chain")
    metadata = resolve_token_metadata(chain, address)
    token = SavedToken(
        id=f"tkn_{uuid.uuid4().hex[:12]}",
        chain=chain,
        address=address,
        name=metadata["name"],
        symbol=metadata["symbol"],
        decimals=metadata["decimals"],
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    return SavedTokenResponse(
        tokenId=token.id, chain=token.chain, address=token.address,
        name=token.name, symbol=token.symbol, decimals=token.decimals, createdAt=token.created_at
    )


@router.delete("/tokens/{token_id}", response_model=SavedTokenResponse)
def delete_token(token_id: str, db: Session = Depends(get_db)):
    token = db.query(SavedToken).filter(SavedToken.id == token_id).first()
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")
    resp = SavedTokenResponse(
        tokenId=token.id, chain=token.chain, address=token.address,
        name=token.name, symbol=token.symbol, decimals=token.decimals, createdAt=token.created_at
    )
    db.delete(token)
    db.commit()
    return resp

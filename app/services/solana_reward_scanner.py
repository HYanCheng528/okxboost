from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import time
from typing import Any

import requests

from ..config import Settings, get_settings
from .address_utils import is_solana_address
from .reward_scanner import WalletRewardResult


MAX_SIGNATURES_PER_WALLET = 20_000
RPC_PAGE_LIMIT = 1_000


@dataclass(frozen=True)
class SolanaTxScan:
    signature: str
    signed_by_wallet: bool
    deltas: dict[str, Decimal]


def _rpc_call(settings: Settings, method: str, params: list[Any]) -> Any:
    endpoints = settings.solana_rpc_urls[:]
    if not endpoints:
        raise ValueError("Missing SOLANA_RPC_URL")

    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_error: Exception | None = None
    for endpoint in endpoints:
        for attempt in range(1, 4):
            try:
                response = requests.post(endpoint, json=payload, timeout=settings.request_timeout_seconds)
                response.raise_for_status()
                data = response.json()
            except requests.RequestException as exc:
                last_error = RuntimeError(f"Solana RPC failed from {endpoint}: {exc}")
                time.sleep(0.35 * attempt)
                continue
            if data.get("error"):
                last_error = RuntimeError(f"Solana RPC error from {endpoint}: {data['error']}")
                time.sleep(0.45 * attempt)
                continue
            return data.get("result")
    if last_error:
        raise last_error
    raise RuntimeError("Solana RPC call failed")


def _token_amount(value: dict[str, Any] | None) -> Decimal:
    if not isinstance(value, dict):
        return Decimal("0")
    amount = str(value.get("amount") or "0")
    decimals = int(value.get("decimals") or 0)
    return Decimal(amount) / (Decimal(10) ** decimals)


def _get_mint_decimals(settings: Settings, mint: str) -> int:
    result = _rpc_call(settings, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    if not isinstance(result, dict):
        raise ValueError(f"Solana mint not found on configured RPC: {mint}")
    value = result.get("value")
    if not isinstance(value, dict):
        raise ValueError(f"Solana mint not found on configured RPC: {mint}")
    data = value.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"Solana account is not a parsed SPL token mint: {mint}")
    parsed = data.get("parsed")
    if not isinstance(parsed, dict):
        raise ValueError(f"Solana account is not a parsed SPL token mint: {mint}")
    if parsed.get("type") != "mint":
        raise ValueError(f"Solana account is not an SPL token mint: {mint}")
    info = parsed.get("info")
    if not isinstance(info, dict):
        raise ValueError(f"Solana account is missing mint metadata: {mint}")
    try:
        return int(info.get("decimals") or 0)
    except (TypeError, ValueError):
        raise ValueError(f"Solana mint decimals are invalid: {mint}") from None


def _known_symbol(settings: Settings, mint: str) -> str | None:
    for symbol, address in settings.solana_stable_mints.items():
        if address == mint:
            return "USDC" if symbol.startswith("USDC") else symbol
    return None


def _stable_mints(settings: Settings) -> set[str]:
    return {address for address in settings.solana_stable_mints.values() if is_solana_address(address)}


def _fetch_signatures(settings: Settings, wallet: str, start_ts: int, end_ts: int) -> list[str]:
    signatures: list[str] = []
    before: str | None = None
    scanned = 0

    while scanned < MAX_SIGNATURES_PER_WALLET:
        options: dict[str, Any] = {"limit": RPC_PAGE_LIMIT}
        if before:
            options["before"] = before
        result = _rpc_call(settings, "getSignaturesForAddress", [wallet, options])
        if not isinstance(result, list) or not result:
            break

        stop = False
        for item in result:
            scanned += 1
            if not isinstance(item, dict):
                continue
            before = str(item.get("signature") or "") or before
            block_time = item.get("blockTime")
            if item.get("err") is not None:
                continue
            if block_time is None:
                continue
            try:
                ts = int(block_time)
            except (TypeError, ValueError):
                continue
            if ts > end_ts:
                continue
            if ts < start_ts:
                stop = True
                continue
            signature = str(item.get("signature") or "")
            if signature:
                signatures.append(signature)

        if stop or len(result) < RPC_PAGE_LIMIT:
            break

    return signatures


def _account_key_pubkey(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("pubkey") or "")
    return ""


def _tx_signed_by_wallet(tx: dict[str, Any], wallet: str) -> bool:
    message = ((tx.get("transaction") or {}).get("message") or {})
    keys = message.get("accountKeys") or []
    for key in keys:
        if not isinstance(key, dict):
            continue
        if str(key.get("pubkey") or "") == wallet and bool(key.get("signer")):
            return True
    return False


def _owner_token_balances(meta: dict[str, Any], key: str, wallet: str) -> dict[str, Decimal]:
    balances: dict[str, Decimal] = {}
    rows = meta.get(key) or []
    if not isinstance(rows, list):
        return balances
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("owner") or "") != wallet:
            continue
        mint = str(row.get("mint") or "")
        if not mint:
            continue
        balances[mint] = balances.get(mint, Decimal("0")) + _token_amount(row.get("uiTokenAmount"))
    return balances


def _scan_transaction(settings: Settings, signature: str, wallet: str) -> SolanaTxScan | None:
    tx = _rpc_call(
        settings,
        "getTransaction",
        [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
    if not isinstance(tx, dict):
        return None
    meta = tx.get("meta")
    if not isinstance(meta, dict) or meta.get("err") is not None:
        return None

    pre = _owner_token_balances(meta, "preTokenBalances", wallet)
    post = _owner_token_balances(meta, "postTokenBalances", wallet)
    mints = set(pre) | set(post)
    deltas = {mint: post.get(mint, Decimal("0")) - pre.get(mint, Decimal("0")) for mint in mints}
    return SolanaTxScan(signature=signature, signed_by_wallet=_tx_signed_by_wallet(tx, wallet), deltas=deltas)


def _has_other_token_out(deltas: dict[str, Decimal], token_mint: str) -> bool:
    return any(mint != token_mint and amount < 0 for mint, amount in deltas.items())


def _stable_increase(deltas: dict[str, Decimal], stable_mints: set[str]) -> Decimal:
    total = Decimal("0")
    for mint, amount in deltas.items():
        if mint in stable_mints and amount > 0:
            total += amount
    return total


def scan_solana_reward(
    *,
    token_address: str,
    wallets: list[tuple[str, str | None]],
    scan_date: str,
) -> tuple[list[WalletRewardResult], str | None, list]:
    settings = get_settings()
    if not settings.solana_rpc_urls:
        raise ValueError("Missing SOLANA_RPC_URL")
    if not is_solana_address(token_address):
        raise ValueError("Invalid Solana token mint")

    token_mint = token_address.strip()
    token_symbol = _known_symbol(settings, token_mint)
    stable_mints = _stable_mints(settings)
    is_stable = token_mint in stable_mints
    # Force a light mint read early so bad mints fail before scanning every wallet.
    _get_mint_decimals(settings, token_mint)

    day_start = datetime.strptime(scan_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ts = int(day_start.timestamp())
    end_ts = start_ts + 24 * 60 * 60 - 1

    results: list[WalletRewardResult] = []
    for wallet, label in wallets:
        if not is_solana_address(wallet):
            continue
        signatures = _fetch_signatures(settings, wallet, start_ts, end_ts)
        claimed = Decimal("0")
        sold_usdt = Decimal("0")
        seen: set[str] = set()
        for signature in signatures:
            if signature in seen:
                continue
            seen.add(signature)
            scanned = _scan_transaction(settings, signature, wallet)
            if scanned is None or not scanned.signed_by_wallet:
                continue
            target_delta = scanned.deltas.get(token_mint, Decimal("0"))
            if target_delta > 0 and not _has_other_token_out(scanned.deltas, token_mint):
                claimed += target_delta
            if is_stable:
                continue
            if target_delta < 0:
                sold_usdt += _stable_increase(scanned.deltas, stable_mints)

        if is_stable:
            sold_usdt = claimed
        results.append(WalletRewardResult(wallet=wallet, label=label, claimed=claimed, sold_usdt=sold_usdt))

    return results, token_symbol, []

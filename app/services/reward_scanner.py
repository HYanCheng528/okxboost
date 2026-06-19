from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import requests

from ..config import ChainConfig, get_settings

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
STABLE_TOKENS = {"usdt", "usdt0", "usdc", "dai", "busd"}
DECIMALS_SELECTOR = "0x313ce567"
SYMBOL_SELECTOR = "0x95d89b41"
DEPOSIT_TOPIC = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"
WITHDRAWAL_TOPIC = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"
LOG_CHUNK_BLOCKS = 5000
CHAIN_LOG_CHUNK_BLOCKS = {"bsc": 1000, "xlayer": 500}


@dataclass
class WalletRewardResult:
    wallet: str
    label: str | None
    claimed: Decimal
    sold_usdt: Decimal


@dataclass
class ClaimContractHit:
    chain: str
    token_address: str
    contract_address: str
    function_selector: str
    code_hash: str | None
    first_seen_tx: str
    hit_count: int = 1


def _hex_to_int(value: str | None) -> int:
    if not value:
        return 0
    return int(value, 16)


def _address_to_topic(address: str) -> str:
    return f"0x{'0' * 24}{address[2:].lower()}"


def _topic_to_address(topic: str) -> str:
    if not topic:
        return ""
    return f"0x{topic[-40:]}".lower()


def _rpc_call(config: ChainConfig, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    endpoints = config.rpc_urls[:] if config.rpc_urls else ([config.rpc_url] if config.rpc_url else [])
    if not endpoints:
        raise ValueError(f"Missing RPC URL for {config.name}")

    last_error: Exception | None = None
    for endpoint in endpoints:
        for attempt in range(1, 4):
            try:
                response = requests.post(endpoint, json=payload, timeout=settings.request_timeout_seconds)
                response.raise_for_status()
            except requests.RequestException as exc:
                last_error = RuntimeError(f"RPC failed from {endpoint}: {exc}")
                time.sleep(0.4 * attempt)
                continue
            data = response.json()
            if data.get("error"):
                last_error = RuntimeError(f"RPC error from {endpoint}: {data['error']}")
                time.sleep(0.5 * attempt)
                continue
            return data
    if last_error:
        raise last_error
    raise RuntimeError("RPC call failed")


def _is_log_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "limit exceeded",
            "query returned more than",
            "block range",
            "range is too",
            "too many",
            "response size",
            "timeout",
        )
    )


def _fetch_logs_range(config: ChainConfig, address: str, topics: list, from_block: int, to_block: int) -> list[dict]:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
        "params": [{"address": address, "fromBlock": hex(from_block), "toBlock": hex(to_block), "topics": topics}],
    }
    try:
        result = _rpc_call(config, payload).get("result", [])
    except Exception as exc:
        if from_block < to_block and _is_log_limit_error(exc):
            mid = (from_block + to_block) // 2
            return (
                _fetch_logs_range(config, address, topics, from_block, mid)
                + _fetch_logs_range(config, address, topics, mid + 1, to_block)
            )
        raise
    return result if isinstance(result, list) else []


def _fetch_logs(config: ChainConfig, address: str, topics: list, from_block: int, to_block: int) -> list[dict]:
    chunk_blocks = CHAIN_LOG_CHUNK_BLOCKS.get(config.name.lower(), LOG_CHUNK_BLOCKS)
    all_logs: list[dict] = []
    block = from_block
    while block <= to_block:
        chunk_end = min(block + chunk_blocks - 1, to_block)
        all_logs.extend(_fetch_logs_range(config, address, topics, block, chunk_end))
        block = chunk_end + 1
        if config.name.lower() in {"bsc", "xlayer"} and block <= to_block:
            time.sleep(0.15)
    return all_logs


def _get_block_timestamp(config: ChainConfig, block_no: int) -> int:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber", "params": [hex(block_no), False]}
    result = _rpc_call(config, payload).get("result")
    if not isinstance(result, dict):
        return 0
    return _hex_to_int(result.get("timestamp"))


def _find_block_at_timestamp(config: ChainConfig, target_ts: int, find_first: bool = True) -> int:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
    latest = _hex_to_int(_rpc_call(config, payload).get("result"))
    if latest <= 0:
        return -1

    latest_ts = _get_block_timestamp(config, latest)
    if target_ts > latest_ts:
        return latest + 1 if find_first else latest

    first_ts = _get_block_timestamp(config, 0)
    if target_ts <= first_ts:
        return 0

    low, high = 0, latest
    if find_first:
        while low < high:
            mid = (low + high) // 2
            if _get_block_timestamp(config, mid) < target_ts:
                low = mid + 1
            else:
                high = mid
        return low
    else:
        while low < high:
            mid = (low + high + 1) // 2
            if _get_block_timestamp(config, mid) <= target_ts:
                low = mid
            else:
                high = mid - 1
        return low


def _get_token_decimals(config: ChainConfig, token: str) -> int:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": token, "data": DECIMALS_SELECTOR}, "latest"]}
    try:
        result = _rpc_call(config, payload).get("result")
        return _hex_to_int(str(result)) if result else 18
    except Exception:
        return 18


def _get_token_symbol(config: ChainConfig, token: str) -> str | None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": token, "data": SYMBOL_SELECTOR}, "latest"]}
    try:
        result = _rpc_call(config, payload).get("result")
        if not result or result == "0x":
            return None
        hex_str = result[2:]
        if len(hex_str) >= 128:
            offset = int(hex_str[:64], 16) * 2
            length = int(hex_str[offset:offset+64], 16)
            data = hex_str[offset+64:offset+64+length*2]
            return bytes.fromhex(data).decode("utf-8", errors="ignore").strip("\x00")
        return bytes.fromhex(hex_str).decode("utf-8", errors="ignore").strip("\x00")
    except Exception:
        return None


def _is_stable_token(symbol: str | None) -> bool:
    if not symbol:
        return False
    return symbol.lower() in STABLE_TOKENS


def _get_stable_addresses(config: ChainConfig) -> set[str]:
    stables = set()
    for sym, addr in (config.base_tokens or {}).items():
        if sym.lower() in STABLE_TOKENS:
            stables.add(addr.lower())
    return stables


def _get_stable_decimals(config: ChainConfig, stable_addresses: set[str]) -> dict[str, int]:
    decimals: dict[str, int] = {}
    for address in stable_addresses:
        decimals[address] = _get_token_decimals(config, address)
    return decimals


def _fetch_receipt(config: ChainConfig, tx_hash: str) -> dict[str, Any] | None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx_hash]}
    result = _rpc_call(config, payload).get("result")
    return result if isinstance(result, dict) else None


def _fetch_transaction(config: ChainConfig, tx_hash: str) -> dict[str, Any] | None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionByHash", "params": [tx_hash]}
    result = _rpc_call(config, payload).get("result")
    return result if isinstance(result, dict) else None


def _get_native_balance_at_block(config: ChainConfig, wallet: str, block_no: int) -> Decimal:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getBalance",
        "params": [wallet, hex(block_no)],
    }
    value = _rpc_call(config, payload).get("result")
    return Decimal(_hex_to_int(str(value))) / Decimal(10**18)


_code_hash_cache: dict[tuple[str, str], str | None] = {}


def _get_code_hash(config: ChainConfig, address: str) -> str | None:
    key = (config.name.lower(), address.lower())
    if key in _code_hash_cache:
        return _code_hash_cache[key]
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getCode", "params": [address, "latest"]}
    try:
        code = str(_rpc_call(config, payload).get("result") or "")
    except Exception:
        _code_hash_cache[key] = None
        return None
    if not code or code == "0x":
        _code_hash_cache[key] = None
        return None
    value = hashlib.sha256(code.encode("ascii", errors="ignore")).hexdigest()
    _code_hash_cache[key] = value
    return value


def _function_selector(input_data: str | None) -> str:
    value = (input_data or "").lower()
    return value[:10] if value.startswith("0x") and len(value) >= 10 else ""


def _has_wallet_erc20_out(receipt: dict[str, Any], wallet_topic: str) -> bool:
    for rlog in receipt.get("logs", []):
        topics = rlog.get("topics") or []
        if len(topics) < 3:
            continue
        if str(topics[0]).lower() != TRANSFER_TOPIC:
            continue
        if str(topics[1]).lower() == wallet_topic.lower() and str(topics[2]).lower() != wallet_topic.lower():
            return True
    return False


def _get_wrapped_native_address(config: ChainConfig) -> str:
    native_symbol = config.native_symbol.upper()
    for symbol, address in (config.base_tokens or {}).items():
        if symbol.upper() == native_symbol:
            return address.lower()
    return ""


def _tx_position(receipt: dict[str, Any]) -> tuple[int, int]:
    return (
        _hex_to_int(str(receipt.get("blockNumber") or "0x0")),
        _hex_to_int(str(receipt.get("transactionIndex") or "0x0")),
    )


def _is_after_tx(position: tuple[int, int], previous: tuple[int, int]) -> bool:
    return position[0] > previous[0] or (position[0] == previous[0] and position[1] > previous[1])


def _transaction_value_native(tx: dict[str, Any] | None, wallet_lc: str) -> Decimal:
    if not tx or str(tx.get("from", "")).lower() != wallet_lc:
        return Decimal("0")
    return Decimal(_hex_to_int(str(tx.get("value") or "0x0"))) / Decimal(10**18)


def _stable_received_in_receipt(
    receipt: dict[str, Any],
    *,
    wallet_lc: str,
    stable_addresses: set[str],
    stable_decimals: dict[str, int],
) -> Decimal:
    total = Decimal("0")
    for rlog in receipt.get("logs", []):
        topics = rlog.get("topics") or []
        if len(topics) < 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
            continue
        contract = str(rlog.get("address", "")).lower()
        if contract not in stable_addresses:
            continue
        if _topic_to_address(str(topics[2])) != wallet_lc:
            continue
        amount_raw = _hex_to_int(str(rlog.get("data", "0x0")))
        decimals = stable_decimals.get(contract, 18)
        total += Decimal(amount_raw) / (Decimal(10) ** decimals)
    return total


def _wrapped_native_transfer_amount(
    receipt: dict[str, Any],
    *,
    wrapped_native_address: str,
    wallet_lc: str,
    direction: str,
) -> Decimal:
    if not wrapped_native_address:
        return Decimal("0")
    total = Decimal("0")
    for rlog in receipt.get("logs", []):
        topics = rlog.get("topics") or []
        if len(topics) < 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
            continue
        if str(rlog.get("address", "")).lower() != wrapped_native_address:
            continue
        from_addr = _topic_to_address(str(topics[1]))
        to_addr = _topic_to_address(str(topics[2]))
        if direction == "in" and to_addr != wallet_lc:
            continue
        if direction == "out" and from_addr != wallet_lc:
            continue
        total += Decimal(_hex_to_int(str(rlog.get("data", "0x0")))) / Decimal(10**18)
    return total


def _wrapped_native_event_amount(receipt: dict[str, Any], *, wrapped_native_address: str, topic: str) -> Decimal:
    if not wrapped_native_address:
        return Decimal("0")
    total = Decimal("0")
    for rlog in receipt.get("logs", []):
        topics = rlog.get("topics") or []
        if not topics or str(topics[0]).lower() != topic:
            continue
        if str(rlog.get("address", "")).lower() != wrapped_native_address:
            continue
        total += Decimal(_hex_to_int(str(rlog.get("data", "0x0")))) / Decimal(10**18)
    return total


def _actual_native_received_from_balance_diff(
    config: ChainConfig,
    *,
    wallet_lc: str,
    receipt: dict[str, Any],
    tx: dict[str, Any] | None,
) -> Decimal:
    if not tx or str(tx.get("from", "")).lower() != wallet_lc:
        return Decimal("0")
    block_number = _hex_to_int(str(receipt.get("blockNumber") or "0x0"))
    if block_number <= 0:
        return Decimal("0")
    try:
        bal_before = _get_native_balance_at_block(config, wallet_lc, block_number - 1)
        bal_after = _get_native_balance_at_block(config, wallet_lc, block_number)
        gas_used_raw = _hex_to_int(str(receipt.get("gasUsed") or "0x0"))
        gas_price_raw = _hex_to_int(str(receipt.get("effectiveGasPrice") or receipt.get("gasPrice") or "0x0"))
    except Exception:
        return Decimal("0")
    gas_native = Decimal(gas_used_raw * gas_price_raw) / Decimal(10**18)
    received = bal_after - bal_before + gas_native
    return received if received > 0 else Decimal("0")


def _intermediate_native_received(
    config: ChainConfig,
    *,
    wallet_lc: str,
    receipt: dict[str, Any],
    tx: dict[str, Any] | None,
    wrapped_native_address: str,
) -> Decimal:
    actual_native = _actual_native_received_from_balance_diff(config, wallet_lc=wallet_lc, receipt=receipt, tx=tx)
    if actual_native > 0:
        return actual_native

    wrapped_in = _wrapped_native_transfer_amount(
        receipt,
        wrapped_native_address=wrapped_native_address,
        wallet_lc=wallet_lc,
        direction="in",
    )
    if wrapped_in > 0:
        return wrapped_in

    return _wrapped_native_event_amount(
        receipt,
        wrapped_native_address=wrapped_native_address,
        topic=WITHDRAWAL_TOPIC,
    )


def _native_spent_for_stable(
    *,
    receipt: dict[str, Any],
    tx: dict[str, Any] | None,
    wallet_lc: str,
    wrapped_native_address: str,
) -> Decimal:
    native_value = _transaction_value_native(tx, wallet_lc)
    wrapped_out = _wrapped_native_transfer_amount(
        receipt,
        wrapped_native_address=wrapped_native_address,
        wallet_lc=wallet_lc,
        direction="out",
    )
    wrapped_deposit = _wrapped_native_event_amount(
        receipt,
        wrapped_native_address=wrapped_native_address,
        topic=DEPOSIT_TOPIC,
    )
    return max(native_value, wrapped_out, wrapped_deposit)


def _match_followup_native_sells_to_stable(
    *,
    config: ChainConfig,
    wallet_lc: str,
    wallet_topic: str,
    stable_addresses: set[str],
    stable_decimals: dict[str, int],
    wrapped_native_address: str,
    lots: list[tuple[tuple[int, int], Decimal]],
    end_block: int,
) -> Decimal:
    remaining_lots = [[position, amount] for position, amount in lots if amount > 0]
    if not remaining_lots:
        return Decimal("0")

    from_block = min(position[0] for position, _amount in remaining_lots)
    stable_tx_hashes: dict[str, tuple[int, int]] = {}
    for stable_address in stable_addresses:
        logs = _fetch_logs(config, stable_address, [TRANSFER_TOPIC, None, wallet_topic], from_block, end_block)
        for log in logs:
            tx_hash = str(log.get("transactionHash", "")).lower()
            if not tx_hash:
                continue
            position = (
                _hex_to_int(str(log.get("blockNumber") or "0x0")),
                _hex_to_int(str(log.get("transactionIndex") or "0x0")),
            )
            current = stable_tx_hashes.get(tx_hash)
            if current is None or position < current:
                stable_tx_hashes[tx_hash] = position

    total = Decimal("0")
    for tx_hash, log_position in sorted(stable_tx_hashes.items(), key=lambda item: item[1]):
        eligible_total = sum(amount for position, amount in remaining_lots if _is_after_tx(log_position, position))
        if eligible_total <= 0:
            continue
        receipt = _fetch_receipt(config, tx_hash)
        if not receipt:
            continue
        tx = _fetch_transaction(config, tx_hash)
        stable_received = _stable_received_in_receipt(
            receipt,
            wallet_lc=wallet_lc,
            stable_addresses=stable_addresses,
            stable_decimals=stable_decimals,
        )
        native_spent = _native_spent_for_stable(
            receipt=receipt,
            tx=tx,
            wallet_lc=wallet_lc,
            wrapped_native_address=wrapped_native_address,
        )
        if stable_received <= 0 or native_spent <= 0:
            continue

        matched_native = min(native_spent, eligible_total)
        total += stable_received * (matched_native / native_spent)
        to_consume = matched_native
        for lot in remaining_lots:
            if to_consume <= 0:
                break
            position, amount = lot
            if amount <= 0 or not _is_after_tx(log_position, position):
                continue
            consumed = min(amount, to_consume)
            lot[1] = amount - consumed
            to_consume -= consumed
        time.sleep(0.1)
    return total


def _scan_wallet_reward(
    *,
    config: ChainConfig,
    wallet_addr: str,
    wallet_label: str | None,
    token_address: str,
    token_decimals: int,
    is_stable: bool,
    stable_addresses: set[str],
    stable_decimals: dict[str, int],
    known_contract_statuses: dict[str, str],
    start_block: int,
    end_block: int,
) -> tuple[WalletRewardResult, list[ClaimContractHit]]:
    wallet_lc = wallet_addr.lower()
    wallet_topic = _address_to_topic(wallet_lc)
    wrapped_native_address = _get_wrapped_native_address(config)

    claim_logs = _fetch_logs(config, token_address, [TRANSFER_TOPIC, None, wallet_topic], start_block, end_block)
    claimed = Decimal("0")
    claim_hits: dict[tuple[str, str], ClaimContractHit] = {}
    for log in claim_logs:
        tx_hash = str(log.get("transactionHash", "")).lower()
        if not tx_hash:
            continue
        receipt = _fetch_receipt(config, tx_hash)
        tx = _fetch_transaction(config, tx_hash)
        if not receipt or not tx:
            continue
        if str(receipt.get("status", "")).lower() != "0x1":
            continue
        tx_from = str(tx.get("from", "")).lower()
        tx_to = str(tx.get("to", "")).lower()
        tx_input = str(tx.get("input", "")).lower()
        if tx_from != wallet_lc or not tx_to or tx_to == token_address or tx_input in {"", "0x"}:
            continue
        topics = log.get("topics") or []
        if len(topics) < 3:
            continue
        transfer_from = _topic_to_address(topics[1])
        transfer_to = _topic_to_address(topics[2])
        if transfer_from != tx_to or transfer_to != wallet_lc:
            continue

        selector = _function_selector(tx_input)
        contract_status = known_contract_statuses.get(tx_to, "candidate")
        if contract_status == "ignored":
            continue

        code_hash = _get_code_hash(config, tx_to)
        key = (tx_to, selector)
        if key not in claim_hits:
            claim_hits[key] = ClaimContractHit(
                chain=config.name.lower(),
                token_address=token_address,
                contract_address=tx_to,
                function_selector=selector,
                code_hash=code_hash,
                first_seen_tx=tx_hash,
                hit_count=0,
            )
        claim_hits[key].hit_count += 1

        if is_stable and _has_wallet_erc20_out(receipt, wallet_topic):
            continue
        amount_raw = _hex_to_int(log.get("data", "0x0"))
        claimed += Decimal(amount_raw) / (Decimal(10) ** token_decimals)

    sold_usdt = Decimal("0")
    if is_stable:
        sold_usdt = claimed
    elif claimed > 0:
        sell_logs = _fetch_logs(config, token_address, [TRANSFER_TOPIC, wallet_topic], start_block, end_block)
        sell_tx_hashes = {
            str(log.get("transactionHash", "")).lower()
            for log in sell_logs
            if log.get("transactionHash")
        }
        intermediate_native_lots: list[tuple[tuple[int, int], Decimal]] = []

        for tx_hash in sell_tx_hashes:
            receipt = _fetch_receipt(config, tx_hash)
            if not receipt:
                continue
            tx = _fetch_transaction(config, tx_hash)
            sold_usdt += _stable_received_in_receipt(
                receipt,
                wallet_lc=wallet_lc,
                stable_addresses=stable_addresses,
                stable_decimals=stable_decimals,
            )
            intermediate_native = _intermediate_native_received(
                config,
                wallet_lc=wallet_lc,
                receipt=receipt,
                tx=tx,
                wrapped_native_address=wrapped_native_address,
            )
            if intermediate_native > 0:
                intermediate_native_lots.append((_tx_position(receipt), intermediate_native))
            time.sleep(0.1)

        if intermediate_native_lots:
            sold_usdt += _match_followup_native_sells_to_stable(
                config=config,
                wallet_lc=wallet_lc,
                wallet_topic=wallet_topic,
                stable_addresses=stable_addresses,
                stable_decimals=stable_decimals,
                wrapped_native_address=wrapped_native_address,
                lots=intermediate_native_lots,
                end_block=end_block,
            )

    return (
        WalletRewardResult(wallet=wallet_lc, label=wallet_label, claimed=claimed, sold_usdt=sold_usdt),
        list(claim_hits.values()),
    )


def scan_reward(
    *,
    token_address: str,
    chain: str,
    wallets: list[tuple[str, str | None]],
    scan_date: str,
    known_contract_statuses: dict[str, str] | None = None,
) -> tuple[list[WalletRewardResult], str | None, list[ClaimContractHit]]:
    settings = get_settings()
    config = settings.chain_configs.get(chain.lower())
    if config is None:
        raise ValueError(f"Unsupported chain: {chain}")

    token_address = token_address.lower()
    token_decimals = _get_token_decimals(config, token_address)
    token_symbol = _get_token_symbol(config, token_address)
    is_stable = _is_stable_token(token_symbol)
    stable_addresses = _get_stable_addresses(config)
    stable_decimals = _get_stable_decimals(config, stable_addresses)
    known_contract_statuses = {
        key.lower(): value
        for key, value in (known_contract_statuses or {}).items()
        if value in {"candidate", "confirmed", "ignored"}
    }

    day_start = datetime.strptime(scan_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    day_end = day_start.replace(hour=23, minute=59, second=59)
    start_ts = int(day_start.timestamp())
    end_ts = int(day_end.timestamp())

    start_block = _find_block_at_timestamp(config, start_ts, find_first=True)
    end_block = _find_block_at_timestamp(config, end_ts, find_first=False)
    if start_block < 0 or end_block < 0 or end_block < start_block:
        return (
            [WalletRewardResult(wallet=w, label=lbl, claimed=Decimal("0"), sold_usdt=Decimal("0")) for w, lbl in wallets],
            token_symbol,
            [],
        )

    max_workers = min(max(1, settings.reward_scan_max_workers), len(wallets))
    if max_workers <= 1:
        scans = [
            _scan_wallet_reward(
                config=config,
                wallet_addr=wallet_addr,
                wallet_label=wallet_label,
                token_address=token_address,
                token_decimals=token_decimals,
                is_stable=is_stable,
                stable_addresses=stable_addresses,
                stable_decimals=stable_decimals,
                known_contract_statuses=known_contract_statuses,
                start_block=start_block,
                end_block=end_block,
            )
            for wallet_addr, wallet_label in wallets
        ]
        return _merge_scan_results(scans, token_symbol)

    ordered_results: list[tuple[WalletRewardResult, list[ClaimContractHit]] | None] = [None] * len(wallets)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _scan_wallet_reward,
                config=config,
                wallet_addr=wallet_addr,
                wallet_label=wallet_label,
                token_address=token_address,
                token_decimals=token_decimals,
                is_stable=is_stable,
                stable_addresses=stable_addresses,
                stable_decimals=stable_decimals,
                known_contract_statuses=known_contract_statuses,
                start_block=start_block,
                end_block=end_block,
            ): idx
            for idx, (wallet_addr, wallet_label) in enumerate(wallets)
        }
        for future in as_completed(futures):
            ordered_results[futures[future]] = future.result()

    scans = [item for item in ordered_results if item is not None]
    return _merge_scan_results(scans, token_symbol)


def _merge_scan_results(
    scans: list[tuple[WalletRewardResult, list[ClaimContractHit]]],
    token_symbol: str | None,
) -> tuple[list[WalletRewardResult], str | None, list[ClaimContractHit]]:
    results: list[WalletRewardResult] = []
    candidates: dict[tuple[str, str, str, str], ClaimContractHit] = {}
    for result, hits in scans:
        results.append(result)
        for hit in hits:
            key = (
                hit.chain.lower(),
                hit.token_address.lower(),
                hit.contract_address.lower(),
                hit.function_selector.lower(),
            )
            if key not in candidates:
                candidates[key] = hit
            else:
                candidates[key].hit_count += hit.hit_count
    return results, token_symbol, list(candidates.values())

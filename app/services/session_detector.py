from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from ..config import get_settings
from ..schemas import DetectedSession, DetectedSessionToken

ANKR_CHAIN_MAP = {"bsc": "bsc", "base": "base", "arbitrum": "arbitrum_one", "ethereum": "eth"}

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
RPC_LOG_CHUNK_BLOCKS = {"bsc": 1000, "xlayer": 5000}
DEFAULT_RPC_LOG_CHUNK_BLOCKS = 5000
RPC_SCAN_PAUSE_SECONDS = {"bsc": 0.05, "xlayer": 0.02}
RPC_LOG_WORKERS = {"xlayer": 2}


def detect_sessions(
    *,
    wallets: list[tuple[str, str | None]],
    tokens: list[tuple[str, str, str | None, str | None]],
    tokens_for_time_detection: list[tuple[str, str, str | None, str | None]],
    target_date: str | None = None,
    scan_after: str | None = None,
) -> tuple[list[DetectedSession], int, list[str]]:
    """
    1. Scan target date (UTC 00:00 ~ 23:59), or from scan_after if provided
    2. Find precise time range from target tokens (concurrent per wallet)
    3. Scan all tokens within that precise range (卤1 min)
    """
    settings = get_settings()

    if target_date:
        try:
            target_dt = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return [], 0, [f"Invalid target_date format: {target_date}, expected YYYY-MM-DD"]
    else:
        target_dt = datetime.now(timezone.utc)

    day_start = target_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = target_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    day_start_ts = int(day_start.timestamp())
    day_end_ts = int(day_end.timestamp())

    # If scan_after is provided, only scan after that time
    if scan_after:
        try:
            after_dt = datetime.fromisoformat(scan_after)
            if after_dt.tzinfo is None:
                after_dt = after_dt.replace(tzinfo=timezone.utc)
            after_ts = int(after_dt.timestamp())
            if after_ts > day_start_ts:
                day_start_ts = after_ts
        except (ValueError, TypeError):
            pass

    ankr_key = _extract_ankr_key(settings)
    ankr_url = f"https://rpc.ankr.com/multichain/{ankr_key}" if ankr_key else None

    # Group tokens by chain
    tokens_for_time_by_chain: dict[str, list[tuple[str, str | None, str | None]]] = {}
    for token_chain, token_address, token_symbol, token_name in tokens_for_time_detection:
        tokens_for_time_by_chain.setdefault(token_chain, []).append(
            (token_address.lower(), token_symbol, token_name)
        )

    tokens_by_chain: dict[str, list[tuple[str, str | None, str | None]]] = {}
    for token_chain, token_address, token_symbol, token_name in tokens:
        tokens_by_chain.setdefault(token_chain, []).append(
            (token_address.lower(), token_symbol, token_name)
        )

    # Process wallets concurrently
    sessions: list[DetectedSession] = []
    errors: list[str] = []
    session_gap_minutes = max(1, settings.detect_session_gap_minutes)
    session_padding_seconds = max(0, settings.detect_session_padding_seconds)

    def process_wallet(wallet_address: str, wallet_label: str | None) -> list[DetectedSession]:
        wallet_name = wallet_label or wallet_address[:10]

        # Step 1: Find time range from target tokens
        target_token_transfers: list[dict[str, Any]] = []
        for chain, chain_tokens in tokens_for_time_by_chain.items():
            try:
                transfers = _fetch_transfers_in_range(
                    settings=settings,
                    ankr_url=ankr_url,
                    chain=chain,
                    wallet=wallet_address,
                    token_addresses={addr for addr, _, _ in chain_tokens},
                    start_ts=day_start_ts,
                    end_ts=day_end_ts,
                )
                target_token_transfers.extend(transfers)
            except Exception as exc:
                errors.append(f"{wallet_name} x {chain}: {exc}")

        if not target_token_transfers:
            return []

        timestamped_transfers = sorted(
            (item for item in ((_transfer_timestamp(t), t) for t in target_token_transfers) if item[0] is not None),
            key=lambda item: item[0],
        )
        target_sessions = _group_wallet_sessions(timestamped_transfers, session_gap_minutes)
        detected_sessions: list[DetectedSession] = []

        for target_session in target_sessions:
            timestamps = [_transfer_timestamp(t) for t in target_session]
            timestamps = [ts for ts in timestamps if ts is not None]
            if not timestamps:
                continue
            precise_start_ts = max(day_start_ts, min(timestamps) - session_padding_seconds)
            precise_end_ts = min(day_end_ts, max(timestamps) + session_padding_seconds)

            all_transfers: list[dict[str, Any]] = []
            for chain, chain_tokens in tokens_by_chain.items():
                try:
                    transfers = _fetch_transfers_in_range(
                        settings=settings,
                        ankr_url=ankr_url,
                        chain=chain,
                        wallet=wallet_address,
                        token_addresses={addr for addr, _, _ in chain_tokens},
                        start_ts=precise_start_ts,
                        end_ts=precise_end_ts,
                    )
                    for t in transfers:
                        t["chain"] = chain
                        contract = t["contractAddress"]
                        for addr, symbol, name in chain_tokens:
                            if addr == contract:
                                t["tokenSymbol"] = symbol
                                t["tokenName"] = name
                                break
                    all_transfers.extend(transfers)
                except Exception as exc:
                    errors.append(f"{wallet_name} x {chain} precise: {exc}")

            if not all_transfers:
                continue

            token_map: dict[str, DetectedSessionToken] = {}
            for t in all_transfers:
                addr = t["contractAddress"]
                if addr not in token_map:
                    token_map[addr] = DetectedSessionToken(
                        address=addr,
                        symbol=t.get("tokenSymbol"),
                        name=t.get("tokenName"),
                        chain=t["chain"],
                    )

            start_dt = datetime.fromtimestamp(precise_start_ts, tz=timezone.utc)
            end_dt = datetime.fromtimestamp(precise_end_ts, tz=timezone.utc)
            dur = (end_dt - start_dt).total_seconds() / 60

            detected_sessions.append(
                DetectedSession(
                    wallet=wallet_address,
                    walletLabel=wallet_label,
                    tokens=list(token_map.values()),
                    startTime=start_dt,
                    endTime=end_dt,
                    txCount=len(all_transfers),
                    durationMinutes=round(dur, 1),
                )
            )

        return detected_sessions

    detect_workers = getattr(settings, "detect_scan_max_workers", 2)
    max_workers = max(1, min(max(1, detect_workers), len(wallets)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_wallet, addr, label): (addr, label)
            for addr, label in wallets
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    sessions.extend(result)
            except Exception as exc:
                addr, label = futures[future]
                errors.append(f"{label or addr[:10]}: {exc}")

    sessions.sort(key=lambda s: s.start_time, reverse=True)
    return sessions, len(wallets), errors


def _extract_ankr_key(settings: Any) -> str | None:
    for cfg in settings.chain_configs.values():
        for url in (cfg.rpc_urls or []):
            if "rpc.ankr.com" in url:
                parts = url.rstrip("/").split("/")
                if len(parts) >= 2 and len(parts[-1]) > 10:
                    return parts[-1]
        if cfg.rpc_url and "rpc.ankr.com" in cfg.rpc_url:
            parts = cfg.rpc_url.rstrip("/").split("/")
            if len(parts) >= 2 and len(parts[-1]) > 10:
                return parts[-1]
    return None


def _transfer_timestamp(transfer: dict[str, Any]) -> int | None:
    raw = transfer.get("timestamp")
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        value = raw.strip()
        if value.isdigit():
            return int(value)
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _fetch_transfers_in_range(
    *,
    settings: Any,
    ankr_url: str | None,
    chain: str,
    wallet: str,
    token_addresses: set[str],
    start_ts: int,
    end_ts: int,
) -> list[dict[str, Any]]:
    chain_key = chain.lower()
    errors: list[str] = []
    ankr_chain = ANKR_CHAIN_MAP.get(chain_key)

    if ankr_chain and ankr_url:
        try:
            return _fetch_ankr_transfers_in_range(
                ankr_url=ankr_url,
                blockchain=ankr_chain,
                wallet=wallet,
                token_addresses=token_addresses,
                start_ts=start_ts,
                end_ts=end_ts,
            )
        except Exception as exc:
            errors.append(f"Ankr failed: {exc}")
    elif ankr_chain:
        errors.append("Ankr API key is missing")

    chain_cfg = settings.chain_configs.get(chain_key)
    if chain_cfg and chain_cfg.rpc_urls:
        try:
            return _fetch_rpc_transfers_in_range(
                rpc_urls=chain_cfg.rpc_urls,
                chain=chain_key,
                wallet=wallet,
                token_addresses=token_addresses,
                start_ts=start_ts,
                end_ts=end_ts,
            )
        except Exception as exc:
            errors.append(f"RPC failed: {exc}")
    else:
        errors.append("RPC URL is missing")

    raise RuntimeError("; ".join(errors))


def _fetch_ankr_transfers_all_tokens(
    *, ankr_url: str, blockchain: str, wallet: str,
    token_addresses: set[str], cutoff_ts: int,
) -> list[dict[str, Any]]:
    """Fetch all transfers for a wallet, filtering by token addresses."""
    all_transfers: list[dict] = []
    page_token: str | None = None
    seen_hashes: set[str] = set()

    for _ in range(20):
        params: dict[str, Any] = {
            "blockchain": blockchain,
            "address": [wallet],
            "descOrder": True,
            "pageSize": 100,
        }
        if page_token:
            params["pageToken"] = page_token

        payload = {"jsonrpc": "2.0", "id": 1, "method": "ankr_getTokenTransfers", "params": params}
        data = _ankr_call(ankr_url, payload)
        result = data.get("result", {})
        transfers = result.get("transfers", [])

        if not transfers:
            break

        hit_cutoff = False
        for t in transfers:
            ts = _transfer_timestamp(t)
            if ts is None:
                continue
            if ts < cutoff_ts:
                hit_cutoff = True
                break
            contract = str(t.get("contractAddress", "")).lower()
            if contract not in token_addresses:
                continue
            tx_hash = t.get("transactionHash", "")
            if tx_hash in seen_hashes:
                continue
            seen_hashes.add(tx_hash)
            all_transfers.append({
                "timestamp": ts,
                "txHash": tx_hash,
                "contractAddress": contract,
            })

        if hit_cutoff:
            break
        page_token = result.get("nextPageToken")
        if not page_token:
            break
        time.sleep(0.2)

    return all_transfers


def _fetch_ankr_transfers_in_range(
    *, ankr_url: str, blockchain: str, wallet: str,
    token_addresses: set[str], start_ts: int, end_ts: int,
) -> list[dict[str, Any]]:
    """Fetch transfers within a specific time range."""
    all_transfers: list[dict] = []
    page_token: str | None = None
    seen_hashes: set[str] = set()

    for _ in range(20):
        params: dict[str, Any] = {
            "blockchain": blockchain,
            "address": [wallet],
            "descOrder": True,
            "pageSize": 100,
        }
        if page_token:
            params["pageToken"] = page_token

        payload = {"jsonrpc": "2.0", "id": 1, "method": "ankr_getTokenTransfers", "params": params}
        data = _ankr_call(ankr_url, payload)
        result = data.get("result", {})
        transfers = result.get("transfers", [])

        if not transfers:
            break

        hit_end = False
        for t in transfers:
            ts = _transfer_timestamp(t)
            if ts is None:
                continue

            # Skip if before start time
            if ts < start_ts:
                hit_end = True
                break

            # Skip if after end time
            if ts > end_ts:
                continue

            contract = str(t.get("contractAddress", "")).lower()
            if contract not in token_addresses:
                continue

            tx_hash = t.get("transactionHash", "")
            if tx_hash in seen_hashes:
                continue

            seen_hashes.add(tx_hash)
            all_transfers.append({
                "timestamp": ts,
                "txHash": tx_hash,
                "contractAddress": contract,
            })

        if hit_end:
            break
        page_token = result.get("nextPageToken")
        if not page_token:
            break
        time.sleep(0.2)

    return all_transfers


def _group_wallet_sessions(
    timestamped_transfers: list[tuple[int, dict[str, Any]]],
    gap_minutes: int,
) -> list[list[dict[str, Any]]]:
    """Group transfers into sessions based on time gaps."""
    if not timestamped_transfers:
        return []

    gap_sec = gap_minutes * 60
    sessions: list[list[dict[str, Any]]] = []
    current_session: list[dict[str, Any]] = [timestamped_transfers[0][1]]
    last_ts = timestamped_transfers[0][0]

    for ts, transfer in timestamped_transfers[1:]:
        if ts - last_ts <= gap_sec:
            current_session.append(transfer)
        else:
            sessions.append(current_session)
            current_session = [transfer]
        last_ts = ts

    sessions.append(current_session)
    return sessions


def _ankr_call(url: str, payload: dict) -> dict:
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=30)
            r.raise_for_status()
            data = r.json()
            if data.get("error"):
                if attempt < 2:
                    time.sleep(1)
                    continue
                raise RuntimeError(str(data["error"]))
            return data
        except requests.exceptions.RequestException as exc:
            if attempt < 2:
                time.sleep(1)
                continue
            raise RuntimeError(str(exc)) from exc
    return {}


def _fetch_rpc_transfers_in_range(
    *,
    rpc_urls: list[str],
    chain: str,
    wallet: str,
    token_addresses: set[str],
    start_ts: int,
    end_ts: int,
) -> list[dict[str, Any]]:
    """Fetch only this wallet's Transfer logs instead of scanning token-wide logs."""
    from_block = _find_block_at_timestamp(rpc_urls, start_ts, find_first=True)
    to_block = _find_block_at_timestamp(rpc_urls, end_ts, find_first=False)
    if from_block < 0 or to_block < 0 or to_block < from_block:
        return []

    wallet_topic = _address_to_topic(wallet)
    chunk_size = RPC_LOG_CHUNK_BLOCKS.get(chain.lower(), DEFAULT_RPC_LOG_CHUNK_BLOCKS)
    pause = RPC_SCAN_PAUSE_SECONDS.get(chain.lower(), 0.0)
    all_transfers: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    jobs: list[tuple[str, list[Any], int, int]] = []
    for token_addr in token_addresses:
        token_lc = token_addr.lower()
        # Time detection only needs one side of the transfer pair. Inbound logs
        # catch project-token buys and stablecoin receipts from sells, while
        # avoiding a second full-day RPC scan on strict chains like X Layer.
        for topics in ([TRANSFER_TOPIC, None, wallet_topic],):
            current = from_block
            while current <= to_block:
                chunk_end = min(current + chunk_size - 1, to_block)
                jobs.append((token_lc, topics, current, chunk_end))
                current = chunk_end + 1

    def fetch_job(job: tuple[str, list[Any], int, int]) -> list[dict[str, Any]]:
        token_lc, topics, job_start, job_end = job
        logs = _fetch_logs_range_rpc(
            rpc_urls=rpc_urls,
            address=token_lc,
            topics=topics,
            from_block=job_start,
            to_block=job_end,
        )
        transfers: list[dict[str, Any]] = []
        for log in logs:
            tx_hash = str(log.get("transactionHash", "")).lower()
            if not tx_hash:
                continue
            block_num = int(str(log.get("blockNumber", "0x0")), 16)
            ts = _get_block_timestamp(rpc_urls, block_num)
            if ts is None or ts < start_ts or ts > end_ts:
                continue
            transfers.append(
                {
                    "timestamp": ts,
                    "txHash": tx_hash,
                    "contractAddress": token_lc,
                }
            )
        if pause:
            time.sleep(pause)
        return transfers

    workers = min(max(1, RPC_LOG_WORKERS.get(chain.lower(), 1)), max(1, len(jobs)))
    if workers == 1:
        job_results = (fetch_job(job) for job in jobs)
        for transfers in job_results:
            for item in transfers:
                key = (item["contractAddress"], item["txHash"])
                if key not in seen:
                    seen.add(key)
                    all_transfers.append(item)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fetch_job, job) for job in jobs]
            for future in as_completed(futures):
                for item in future.result():
                    key = (item["contractAddress"], item["txHash"])
                    if key not in seen:
                        seen.add(key)
                        all_transfers.append(item)

    all_transfers.sort(key=lambda item: (item["timestamp"], item["txHash"], item["contractAddress"]))
    return all_transfers


def _address_to_topic(address: str) -> str:
    value = address.lower()
    if not value.startswith("0x") or len(value) != 42:
        raise ValueError(f"Invalid wallet address: {address}")
    return f"0x{'0' * 24}{value[2:]}"


def _fetch_logs_range_rpc(
    *,
    rpc_urls: list[str],
    address: str,
    topics: list[Any],
    from_block: int,
    to_block: int,
) -> list[dict[str, Any]]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getLogs",
        "params": [
            {
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": address,
                "topics": topics,
            }
        ],
    }
    try:
        result = _rpc_call_fallback(rpc_urls, payload).get("result", [])
    except Exception as exc:
        if from_block < to_block and _is_log_limit_error(exc):
            mid = (from_block + to_block) // 2
            return _fetch_logs_range_rpc(
                rpc_urls=rpc_urls,
                address=address,
                topics=topics,
                from_block=from_block,
                to_block=mid,
            ) + _fetch_logs_range_rpc(
                rpc_urls=rpc_urls,
                address=address,
                topics=topics,
                from_block=mid + 1,
                to_block=to_block,
            )
        raise
    return result if isinstance(result, list) else []


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


_latest_block_cache: dict[tuple[str, ...], int] = {}
_block_ts_cache: dict[tuple[tuple[str, ...], int], int] = {}


def _rpc_cache_key(rpc_urls: list[str]) -> tuple[str, ...]:
    return tuple(rpc_urls)


def _get_latest_block(rpc_urls: list[str]) -> int:
    key = _rpc_cache_key(rpc_urls)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
    latest = int(str(_rpc_call_fallback(rpc_urls, payload).get("result", "0x0")), 16)
    _latest_block_cache[key] = latest
    return latest


def _get_block_timestamp(rpc_urls: list[str], block_num: int) -> int | None:
    key = (_rpc_cache_key(rpc_urls), block_num)
    if key in _block_ts_cache:
        return _block_ts_cache[key]
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber", "params": [hex(block_num), False]}
    block_data = _rpc_call_fallback(rpc_urls, payload).get("result")
    if not isinstance(block_data, dict):
        return None
    ts = int(str(block_data.get("timestamp", "0x0")), 16)
    _block_ts_cache[key] = ts
    return ts


def _find_block_at_timestamp(rpc_urls: list[str], target_ts: int, *, find_first: bool) -> int:
    latest = _get_latest_block(rpc_urls)
    if latest <= 0:
        return -1

    latest_ts = _get_block_timestamp(rpc_urls, latest)
    if latest_ts is None:
        return -1
    if target_ts > latest_ts:
        return latest + 1 if find_first else latest

    low, high = 0, latest
    if find_first:
        while low < high:
            mid = (low + high) // 2
            mid_ts = _get_block_timestamp(rpc_urls, mid)
            if mid_ts is None:
                raise RuntimeError(f"Block timestamp unavailable: {mid}")
            if mid_ts < target_ts:
                low = mid + 1
            else:
                high = mid
        return low

    while low < high:
        mid = (low + high + 1) // 2
        mid_ts = _get_block_timestamp(rpc_urls, mid)
        if mid_ts is None:
            raise RuntimeError(f"Block timestamp unavailable: {mid}")
        if mid_ts <= target_ts:
            low = mid
        else:
            high = mid - 1
    return low


def _rpc_call_fallback(rpc_urls: list[str], payload: dict[str, Any]) -> dict[str, Any]:
    last_error: Exception | None = None
    for url in rpc_urls:
        for attempt in range(1, 4):
            try:
                r = requests.post(url, json=payload, timeout=15)
                try:
                    data = r.json()
                except ValueError:
                    r.raise_for_status()
                    data = {}
                if r.status_code >= 400:
                    if isinstance(data, dict) and data.get("error"):
                        last_error = RuntimeError(str(data["error"]))
                    else:
                        r.raise_for_status()
                    sleep_seconds = 2.0 * attempt if _is_rate_limit_text(last_error) else 0.3 * attempt
                    time.sleep(sleep_seconds)
                    continue
            except requests.exceptions.RequestException as exc:
                last_error = exc
                time.sleep(0.3 * attempt)
                continue
            if data.get("error"):
                last_error = RuntimeError(str(data["error"]))
                sleep_seconds = 2.0 * attempt if _is_rate_limit_text(last_error) else 0.3 * attempt
                time.sleep(sleep_seconds)
                continue
            return data
    if last_error is not None:
        raise RuntimeError(str(last_error)) from last_error
    raise RuntimeError("RPC call failed")


def _is_rate_limit_text(error: Exception | None) -> bool:
    if error is None:
        return False
    text = str(error).lower()
    return "rate limit" in text or "over rate" in text or "too many" in text or "limit exceeded" in text

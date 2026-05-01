from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import time
from typing import Any

import requests
from sqlalchemy.orm import Session

from ...config import ChainConfig, get_settings
from ..price_service import PriceService
from .base import ChainProvider, ProgressCallback
from .types import ParsedTx


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DECIMALS_SELECTOR = "0x313ce567"
LOG_CHUNK_BLOCKS = 20000


@dataclass(slots=True)
class TxCandidate:
    tx_hash: str
    timestamp: datetime


def _topic_to_address(topic: str) -> str:
    if not topic:
        return ""
    return f"0x{topic[-40:]}".lower()


def _hex_to_int(value: str | None) -> int:
    if not value:
        return 0
    return int(value, 16)


def _is_rate_limited_error(error_obj: object) -> bool:
    text = str(error_obj).lower()
    tokens = ("limit exceeded", "rate limit", "too many", "-32005", "request limit")
    return any(token in text for token in tokens)


class EvmExplorerProvider(ChainProvider):
    def __init__(self) -> None:
        self.settings = get_settings()
        self.price_service = PriceService()
        self._decimals_cache: dict[tuple[str, str], int] = {}
        self._block_ts_cache: dict[tuple[str, int], int] = {}
        self._latest_block_cache: dict[str, int] = {}

    def fetch_transactions(
        self,
        *,
        chain: str,
        wallets: list[str],
        token: str,
        base_token: str,
        start_time: datetime,
        end_time: datetime,
        db: Session,
        progress_cb: ProgressCallback | None = None,
    ) -> list[ParsedTx]:
        chain_key = chain.lower()
        config = self.settings.chain_configs.get(chain_key)
        if config is None:
            raise ValueError(f"Unsupported chain: {chain}")
        if not config.rpc_url and not config.rpc_urls:
            raise ValueError(f"Chain {chain} requires RPC configuration in environment variables.")
        # Refresh latest block snapshot for each fetch window.
        # A long-lived process can otherwise keep an old latest block value and
        # misclassify later ranges as "future", returning empty results.
        self._latest_block_cache.pop(config.name, None)

        base_address = config.base_tokens.get(base_token.upper())
        if base_address is None:
            raise ValueError(f"Unsupported base token for {chain}: {base_token}")
        token_address = token.lower()

        base_decimals = self._get_token_decimals(config, base_address)
        token_decimals = self._get_token_decimals(config, token_address)

        parsed: list[ParsedTx] = []
        total_wallets = max(1, len(wallets))
        for wallet_index, wallet in enumerate(wallets, start=1):
            wallet_lc = wallet.lower()
            if progress_cb:
                wallet_pct = 15 + int((wallet_index - 1) * 45 / total_wallets)
                progress_cb(wallet_pct, f"正在抓取第 {wallet_index}/{total_wallets} 个钱包交易。")
            candidates = self._fetch_wallet_tx_candidates(
                config=config,
                wallet=wallet_lc,
                token_address=token_address,
                base_address=base_address,
                start_time=start_time,
                end_time=end_time,
            )
            total_candidates = max(1, len(candidates))
            for candidate_index, candidate in enumerate(candidates, start=1):
                receipt = self._fetch_receipt(config, candidate.tx_hash)
                if receipt is None:
                    continue

                parsed_tx = self._parse_receipt(
                    config=config,
                    wallet=wallet_lc,
                    tx_hash=candidate.tx_hash,
                    timestamp=candidate.timestamp,
                    receipt=receipt,
                    token_address=token_address,
                    base_address=base_address,
                    token_decimals=token_decimals,
                    base_decimals=base_decimals,
                    db=db,
                )
                if parsed_tx is not None:
                    parsed.append(parsed_tx)

                if progress_cb and candidate_index % 30 == 0:
                    wallet_base = 15 + int((wallet_index - 1) * 45 / total_wallets)
                    wallet_span = int(45 / total_wallets)
                    candidate_pct = wallet_base + int(candidate_index * wallet_span / total_candidates)
                    progress_cb(
                        candidate_pct,
                        f"钱包 {wallet_index}/{total_wallets} 已解析 {candidate_index}/{total_candidates} 笔候选交易。",
                    )

            if progress_cb:
                wallet_done_pct = 15 + int(wallet_index * 45 / total_wallets)
                progress_cb(wallet_done_pct, f"钱包 {wallet_index}/{total_wallets} 抓取完成。")

        parsed.sort(key=lambda item: (item.wallet, item.timestamp, item.tx_hash))
        if progress_cb:
            progress_cb(65, f"链上抓取完成，共解析 {len(parsed)} 笔相关交易。")
        return parsed

    def _fetch_wallet_tx_candidates(
        self,
        *,
        config: ChainConfig,
        wallet: str,
        token_address: str,
        base_address: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[TxCandidate]:
        if config.explorer_api_url:
            try:
                candidates = self._fetch_wallet_tx_candidates_from_explorer(
                    config=config,
                    wallet=wallet,
                    start_time=start_time,
                    end_time=end_time,
                )
                if candidates:
                    return candidates
            except Exception:
                # Explorer can be unavailable/plan-limited for some chains; fall back to RPC logs.
                pass

        return self._fetch_wallet_tx_candidates_from_rpc_logs(
            config=config,
            wallet=wallet,
            token_address=token_address,
            base_address=base_address,
            start_time=start_time,
            end_time=end_time,
        )

    def _fetch_wallet_tx_candidates_from_explorer(
        self,
        *,
        config: ChainConfig,
        wallet: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[TxCandidate]:
        txs = self._fetch_wallet_txlist(config, wallet)
        deduped: dict[str, TxCandidate] = {}
        for tx in txs:
            tx_hash = str(tx.get("hash", "")).lower()
            if not tx_hash:
                continue
            timestamp = datetime.fromtimestamp(int(tx["timeStamp"]), tz=timezone.utc)
            if timestamp < start_time or timestamp > end_time:
                continue
            deduped[tx_hash] = TxCandidate(tx_hash=tx_hash, timestamp=timestamp)
        values = list(deduped.values())
        values.sort(key=lambda item: (item.timestamp, item.tx_hash))
        return values

    def _fetch_wallet_tx_candidates_from_rpc_logs(
        self,
        *,
        config: ChainConfig,
        wallet: str,
        token_address: str,
        base_address: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[TxCandidate]:
        start_block = self._find_first_block_ge_timestamp(config, int(start_time.timestamp()))
        end_block = self._find_last_block_le_timestamp(config, int(end_time.timestamp()))
        if start_block < 0 or end_block < 0 or end_block < start_block:
            return []

        wallet_topic = self._address_to_topic(wallet)
        tx_to_block: dict[str, int] = {}
        for contract in {base_address.lower(), token_address.lower()}:
            outbound = self._fetch_logs_range(
                config=config,
                address=contract,
                from_block=start_block,
                to_block=end_block,
                topics=[TRANSFER_TOPIC, wallet_topic],
            )
            inbound = self._fetch_logs_range(
                config=config,
                address=contract,
                from_block=start_block,
                to_block=end_block,
                topics=[TRANSFER_TOPIC, None, wallet_topic],
            )

            for log in outbound + inbound:
                tx_hash = str(log.get("transactionHash", "")).lower()
                block_no = _hex_to_int(log.get("blockNumber"))
                if not tx_hash:
                    continue
                existed = tx_to_block.get(tx_hash)
                if existed is None or block_no < existed:
                    tx_to_block[tx_hash] = block_no

        candidates: list[TxCandidate] = []
        for tx_hash, block_no in tx_to_block.items():
            timestamp = datetime.fromtimestamp(self._get_block_timestamp(config, block_no), tz=timezone.utc)
            if timestamp < start_time or timestamp > end_time:
                continue
            candidates.append(TxCandidate(tx_hash=tx_hash, timestamp=timestamp))

        candidates.sort(key=lambda item: (item.timestamp, item.tx_hash))
        return candidates

    def _fetch_wallet_txlist(self, config: ChainConfig, wallet: str) -> list[dict[str, Any]]:
        if not config.explorer_api_url:
            return []

        params: dict[str, Any] = {
            "module": "account",
            "action": "txlist",
            "address": wallet,
            "startblock": 0,
            "endblock": 99999999,
            "sort": "asc",
        }
        if config.chain_id is not None and "/v2/" in config.explorer_api_url:
            params["chainid"] = config.chain_id
        if config.explorer_api_key:
            params["apikey"] = config.explorer_api_key

        try:
            response = requests.get(
                config.explorer_api_url,
                params=params,
                timeout=self.settings.request_timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"Explorer request failed for wallet {wallet}: {exc}") from exc

        payload = response.json()
        result = payload.get("result")
        if isinstance(result, list):
            return result

        message = str(payload.get("message", "")).lower()
        payload_result = str(payload.get("result", "")).lower()
        if "no transactions found" in message or "no transactions found" in payload_result:
            return []

        raise RuntimeError(f"Explorer returned unexpected result for wallet {wallet}: {payload}")

    def _address_to_topic(self, address: str) -> str:
        if not address.startswith("0x") or len(address) != 42:
            raise ValueError(f"Invalid wallet address: {address}")
        return f"0x{'0' * 24}{address[2:].lower()}"

    def _fetch_logs_range(
        self,
        *,
        config: ChainConfig,
        address: str,
        from_block: int,
        to_block: int,
        topics: list[Any],
    ) -> list[dict[str, Any]]:
        if to_block < from_block:
            return []

        logs: list[dict[str, Any]] = []
        block = from_block
        while block <= to_block:
            chunk_end = min(block + LOG_CHUNK_BLOCKS - 1, to_block)
            logs.extend(
                self._fetch_logs_chunk(
                    config=config,
                    address=address,
                    start_block=block,
                    end_block=chunk_end,
                    topics=topics,
                )
            )
            block = chunk_end + 1
        return logs

    def _fetch_logs_chunk(
        self,
        *,
        config: ChainConfig,
        address: str,
        start_block: int,
        end_block: int,
        topics: list[Any],
    ) -> list[dict[str, Any]]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getLogs",
            "params": [
                {
                    "address": address,
                    "fromBlock": hex(start_block),
                    "toBlock": hex(end_block),
                    "topics": topics,
                }
            ],
        }

        try:
            result = self._rpc_call(config, payload).get("result")
            if isinstance(result, list):
                return result
            return []
        except RuntimeError as exc:
            if start_block >= end_block:
                raise
            message = str(exc).lower()
            split_hints = ("more than", "timeout", "limit", "range", "response size")
            if any(token in message for token in split_hints):
                mid = (start_block + end_block) // 2
                left = self._fetch_logs_chunk(
                    config=config,
                    address=address,
                    start_block=start_block,
                    end_block=mid,
                    topics=topics,
                )
                right = self._fetch_logs_chunk(
                    config=config,
                    address=address,
                    start_block=mid + 1,
                    end_block=end_block,
                    topics=topics,
                )
                return left + right
            raise

    def _get_latest_block_number(self, config: ChainConfig) -> int:
        cached = self._latest_block_cache.get(config.name)
        if cached is not None:
            return cached

        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
        latest = _hex_to_int(self._rpc_call(config, payload).get("result"))
        self._latest_block_cache[config.name] = latest
        return latest

    def _get_block_timestamp(self, config: ChainConfig, block_no: int) -> int:
        key = (config.name, block_no)
        cached = self._block_ts_cache.get(key)
        if cached is not None:
            return cached

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getBlockByNumber",
            "params": [hex(block_no), False],
        }
        result = self._rpc_call(config, payload).get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Block not found: {block_no}")
        ts = _hex_to_int(result.get("timestamp"))
        self._block_ts_cache[key] = ts
        return ts

    def _find_first_block_ge_timestamp(self, config: ChainConfig, target_ts: int) -> int:
        latest = self._get_latest_block_number(config)
        if latest < 0:
            return -1

        latest_ts = self._get_block_timestamp(config, latest)
        if target_ts > latest_ts:
            return latest + 1

        first_ts = self._get_block_timestamp(config, 0)
        if target_ts <= first_ts:
            return 0

        low = 0
        high = latest
        while low < high:
            mid = (low + high) // 2
            mid_ts = self._get_block_timestamp(config, mid)
            if mid_ts < target_ts:
                low = mid + 1
            else:
                high = mid
        return low

    def _find_last_block_le_timestamp(self, config: ChainConfig, target_ts: int) -> int:
        latest = self._get_latest_block_number(config)
        if latest < 0:
            return -1

        first_ts = self._get_block_timestamp(config, 0)
        if target_ts < first_ts:
            return -1

        latest_ts = self._get_block_timestamp(config, latest)
        if target_ts >= latest_ts:
            return latest

        low = 0
        high = latest
        while low < high:
            mid = (low + high + 1) // 2
            mid_ts = self._get_block_timestamp(config, mid)
            if mid_ts <= target_ts:
                low = mid
            else:
                high = mid - 1
        return low

    def _fetch_receipt(self, config: ChainConfig, tx_hash: str) -> dict[str, Any] | None:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getTransactionReceipt",
            "params": [tx_hash],
        }
        response = self._rpc_call(config, payload)
        return response.get("result")

    def _parse_receipt(
        self,
        *,
        config: ChainConfig,
        wallet: str,
        tx_hash: str,
        timestamp: datetime,
        receipt: dict[str, Any],
        token_address: str,
        base_address: str,
        token_decimals: int,
        base_decimals: int,
        db: Session,
    ) -> ParsedTx | None:
        usdt_out = Decimal("0")
        usdt_in = Decimal("0")
        token_in = Decimal("0")
        token_out = Decimal("0")

        logs = receipt.get("logs", [])
        for log in logs:
            topics = log.get("topics") or []
            if len(topics) < 3:
                continue
            if str(topics[0]).lower() != TRANSFER_TOPIC:
                continue

            from_addr = _topic_to_address(str(topics[1]))
            to_addr = _topic_to_address(str(topics[2]))
            amount_raw = _hex_to_int(str(log.get("data", "0x0")))
            contract = str(log.get("address", "")).lower()

            if contract == base_address:
                amount = Decimal(amount_raw) / (Decimal(10) ** base_decimals)
                if from_addr == wallet:
                    usdt_out += amount
                if to_addr == wallet:
                    usdt_in += amount
            elif contract == token_address:
                amount = Decimal(amount_raw) / (Decimal(10) ** token_decimals)
                if to_addr == wallet:
                    token_in += amount
                if from_addr == wallet:
                    token_out += amount

        if usdt_out == 0 and usdt_in == 0 and token_in == 0 and token_out == 0:
            return None

        gas_used = _hex_to_int(receipt.get("gasUsed"))
        effective_gas_price = receipt.get("effectiveGasPrice") or receipt.get("gasPrice")
        gas_price = _hex_to_int(effective_gas_price)
        gas_native = Decimal(gas_used * gas_price) / Decimal(10**18)

        native_price = self.price_service.get_price_usd(db, config.native_symbol, timestamp)
        gas_usd = gas_native * native_price if native_price is not None else None

        return ParsedTx(
            chain=config.name,
            wallet=wallet,
            tx_hash=tx_hash,
            timestamp=timestamp,
            usdt_out=usdt_out,
            usdt_in=usdt_in,
            token_in=token_in,
            token_out=token_out,
            gas_native=gas_native,
            gas_usd=gas_usd,
        )

    def _get_token_decimals(self, config: ChainConfig, token: str) -> int:
        cache_key = (config.name, token.lower())
        if cache_key in self._decimals_cache:
            return self._decimals_cache[cache_key]

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": token, "data": DECIMALS_SELECTOR}, "latest"],
        }
        try:
            result = self._rpc_call(config, payload).get("result")
            decimals = _hex_to_int(str(result)) if result else 18
        except Exception:
            decimals = 18

        self._decimals_cache[cache_key] = decimals
        return decimals

    def _rpc_call(self, config: ChainConfig, payload: dict[str, Any]) -> dict[str, Any]:
        endpoints = config.rpc_urls[:] if config.rpc_urls else ([config.rpc_url] if config.rpc_url else [])
        if not endpoints:
            raise ValueError(f"Missing RPC URL for {config.name}")

        last_error: Exception | None = None
        per_endpoint_attempts = 3
        round_attempts = 3
        for round_idx in range(1, round_attempts + 1):
            for endpoint in endpoints:
                for attempt in range(1, per_endpoint_attempts + 1):
                    try:
                        response = requests.post(
                            endpoint,
                            json=payload,
                            timeout=self.settings.request_timeout_seconds,
                        )
                        response.raise_for_status()
                    except requests.RequestException as exc:
                        last_error = RuntimeError(f"RPC call failed for {endpoint}: {exc}")
                        if attempt < per_endpoint_attempts:
                            time.sleep(0.4 * attempt)
                        continue

                    data = response.json()
                    if data.get("error"):
                        if _is_rate_limited_error(data["error"]):
                            last_error = RuntimeError(f"RPC error from {endpoint}: {data['error']}")
                            if attempt < per_endpoint_attempts:
                                time.sleep(0.8 * attempt)
                                continue
                            # move to next endpoint
                            break
                        raise RuntimeError(f"RPC error from {endpoint}: {data['error']}")
                    return data

            # one full round exhausted all endpoints; cool down then retry full round
            if round_idx < round_attempts:
                time.sleep(1.2 * round_idx)

        if last_error is not None:
            raise last_error
        raise RuntimeError("RPC call failed after retries")

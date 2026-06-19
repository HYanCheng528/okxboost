from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from threading import Lock
from typing import Any

from eth_account import Account
from web3 import Web3

from ..config import ChainConfig, get_settings

ERC20_ABI = json.loads(
    """
[
  {"constant":true,"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
  {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
  {"constant":true,"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},
  {"constant":false,"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}
]
"""
)

V2_ROUTER_ABI = json.loads(
    """
[
  {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"}],"name":"getAmountsOut","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactTokensForTokensSupportingFeeOnTransferTokens","outputs":[],"stateMutability":"nonpayable","type":"function"}
]
"""
)

V2_FACTORY_ABI = json.loads(
    """
[
  {"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"}],"name":"getPair","outputs":[{"internalType":"address","name":"pair","type":"address"}],"stateMutability":"view","type":"function"}
]
"""
)

V3_FACTORY_ABI = json.loads(
    """
[
  {"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"}],"name":"getPool","outputs":[{"internalType":"address","name":"pool","type":"address"}],"stateMutability":"view","type":"function"}
]
"""
)

V3_QUOTER_ABI = json.loads(
    """
[
  {"inputs":[{"internalType":"bytes","name":"path","type":"bytes"},{"internalType":"uint256","name":"amountIn","type":"uint256"}],"name":"quoteExactInput","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"},{"internalType":"uint160[]","name":"sqrtPriceX96AfterList","type":"uint160[]"},{"internalType":"uint32[]","name":"initializedTicksCrossedList","type":"uint32[]"},{"internalType":"uint256","name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"}
]
"""
)

V3_ROUTER_ABI = json.loads(
    """
[
  {"inputs":[{"components":[{"internalType":"bytes","name":"path","type":"bytes"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMinimum","type":"uint256"}],"internalType":"struct IV3SwapRouter.ExactInputParams","name":"params","type":"tuple"}],"name":"exactInput","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"}],"stateMutability":"payable","type":"function"}
]
"""
)


@dataclass(frozen=True)
class DexRoute:
    protocol: str
    router: str
    quoter: str | None
    path: list[str]
    fees: list[int]
    amount_in_raw: int
    amount_out_raw: int
    dex_name: str | None = None
    factory: str | None = None
    pools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dexName": self.dex_name,
            "protocol": self.protocol,
            "router": self.router,
            "quoter": self.quoter,
            "factory": self.factory,
            "pools": self.pools,
            "path": self.path,
            "fees": self.fees,
            "amountInRaw": str(self.amount_in_raw),
            "amountOutRaw": str(self.amount_out_raw),
        }


@dataclass(frozen=True)
class SwapResult:
    approval_tx_hash: str | None
    swap_tx_hash: str
    output_amount_raw: int | None
    route: DexRoute


_nonce_locks: dict[tuple[str, str], Lock] = {}
_nonce_locks_guard = Lock()


def _wallet_lock(chain: str, wallet: str) -> Lock:
    key = (chain.lower(), wallet.lower())
    with _nonce_locks_guard:
        if key not in _nonce_locks:
            _nonce_locks[key] = Lock()
        return _nonce_locks[key]


def _checksum(w3: Web3, address: str) -> str:
    return w3.to_checksum_address(address)


def _encode_v3_path(tokens: list[str], fees: list[int]) -> bytes:
    if len(tokens) != len(fees) + 1:
        raise ValueError("v3 path token/fee length mismatch")
    encoded = b""
    for idx, token in enumerate(tokens):
        encoded += bytes.fromhex(token[2:])
        if idx < len(fees):
            encoded += int(fees[idx]).to_bytes(3, byteorder="big")
    return encoded


class DirectPoolDexAdapter:
    """Whitelisted EVM pool/router adapter for copy-sell operations."""

    DEFAULT_DEXES: dict[str, dict[str, Any]] = {
        "bsc": {
            "v2": [
                {
                    "name": "PancakeSwap V2",
                    "router": "0x10ed43c718714eb63d5aa57b78b54704e256024e",
                    "factory": "0xca143ce32fe78f1f7019d7d551a6402fc5350c73",
                }
            ],
            "v3": [
                {
                    "name": "PancakeSwap V3",
                    "router": "0x13f4ea83d0bd40e75c8222255bc855a974568dd4",
                    "factory": "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865",
                    "quoter": "0xb048bbc1ee6b733fffcfb9e9cef7375518e25997",
                    "fees": [100, 500, 2500, 10000],
                },
                {
                    "name": "Uniswap V3",
                    "router": "0xb971ef87ede563556b2ed4b1c0b0019111dd85d2",
                    "factory": "0xdb1d10011ad0ff90774d0c6bb92e5c5c8b4461f7",
                    "quoter": "0x78d78e420da98ad378d7799be8f4af69033eb077",
                    "fees": [100, 500, 3000, 10000],
                }
            ],
        },
        "ethereum": {
            "v2": [
                {
                    "name": "Uniswap V2",
                    "router": "0x7a250d5630b4cf539739df2c5dacb4c659f2488d",
                    "factory": "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f",
                },
                {
                    "name": "SushiSwap V2",
                    "router": "0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f",
                    "factory": "0xc0aee478e3658e2610c5f7a4a2e1777ce9e4f2ac",
                },
            ],
            "v3": [
                {
                    "name": "Uniswap V3",
                    "router": "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",
                    "factory": "0x1f98431c8ad98523631ae4a59f267346ea31f984",
                    "quoter": "0x61ffe014ba17989e743c5f6cb21bf9697530b21e",
                    "fees": [100, 500, 3000, 10000],
                }
            ],
        },
    }

    @classmethod
    def has_whitelisted_routes(cls, chain_name: str) -> bool:
        dexes = cls.DEFAULT_DEXES.get((chain_name or "").lower())
        if not dexes:
            return False
        return bool(dexes.get("v2") or dexes.get("v3"))

    def __init__(self, config: ChainConfig) -> None:
        settings = get_settings()
        endpoints = config.rpc_urls[:] if config.rpc_urls else ([config.rpc_url] if config.rpc_url else [])
        if not endpoints:
            raise ValueError(f"Missing RPC URL for {config.name}")
        self.config = config
        self.w3 = Web3(Web3.HTTPProvider(endpoints[0], request_kwargs={"timeout": settings.request_timeout_seconds}))
        self.dex_config = self.DEFAULT_DEXES.get(config.name.lower(), {"v2": [], "v3": []})

    def token_balance(self, token: str, wallet: str) -> int:
        contract = self.w3.eth.contract(address=_checksum(self.w3, token), abi=ERC20_ABI)
        return int(contract.functions.balanceOf(_checksum(self.w3, wallet)).call())

    def token_decimals(self, token: str) -> int:
        contract = self.w3.eth.contract(address=_checksum(self.w3, token), abi=ERC20_ABI)
        try:
            return int(contract.functions.decimals().call())
        except Exception:
            return 18

    def _candidate_paths(self, token_in: str, token_out: str) -> list[list[str]]:
        token_in = token_in.lower()
        token_out = token_out.lower()
        paths = [[token_in, token_out]]
        wrapped_native = (self.config.base_tokens or {}).get(self.config.native_symbol.upper(), "").lower()
        if wrapped_native and wrapped_native not in {token_in, token_out}:
            paths.append([token_in, wrapped_native, token_out])
        return paths

    def scan_quotes(
        self,
        token_in: str,
        token_out: str,
        amount_in_raw: int,
        route_preference: str = "best",
    ) -> list[DexRoute]:
        if amount_in_raw <= 0:
            raise ValueError("amountIn must be positive")
        preference = (route_preference or "best").lower()
        if preference not in {"best", "v2", "v3"}:
            raise ValueError(f"unsupported route preference: {route_preference}")
        quotes: list[DexRoute] = []
        for path in self._candidate_paths(token_in, token_out):
            if preference in {"best", "v2"}:
                quotes.extend(self._quote_v2(path, amount_in_raw))
            if preference in {"best", "v3"}:
                quotes.extend(self._quote_v3(path, amount_in_raw))
        quotes.sort(key=lambda item: item.amount_out_raw, reverse=True)
        return quotes

    def quote_best(
        self,
        token_in: str,
        token_out: str,
        amount_in_raw: int,
        route_preference: str = "best",
    ) -> DexRoute:
        quotes = self.scan_quotes(token_in, token_out, amount_in_raw, route_preference=route_preference)
        if not quotes:
            raise ValueError("no whitelisted pool route produced a quote")
        return quotes[0]

    def _v2_pools(self, path: list[str], factory_address: str) -> list[str]:
        pools: list[str] = []
        if not factory_address:
            return pools
        factory = self.w3.eth.contract(address=_checksum(self.w3, factory_address), abi=V2_FACTORY_ABI)
        for idx in range(len(path) - 1):
            try:
                pair = str(
                    factory.functions.getPair(_checksum(self.w3, path[idx]), _checksum(self.w3, path[idx + 1])).call()
                ).lower()
            except Exception:
                pair = ""
            pools.append(pair if pair and int(pair, 16) else "")
        return pools

    def _v3_pools(self, path: list[str], fees: list[int], factory_address: str) -> list[str]:
        pools: list[str] = []
        if not factory_address:
            return pools
        factory = self.w3.eth.contract(address=_checksum(self.w3, factory_address), abi=V3_FACTORY_ABI)
        for idx, fee in enumerate(fees):
            try:
                pool = str(
                    factory.functions.getPool(
                        _checksum(self.w3, path[idx]),
                        _checksum(self.w3, path[idx + 1]),
                        int(fee),
                    ).call()
                ).lower()
            except Exception:
                pool = ""
            pools.append(pool if pool and int(pool, 16) else "")
        return pools

    def _quote_v2(self, path: list[str], amount_in_raw: int) -> list[DexRoute]:
        results: list[DexRoute] = []
        checksum_path = [_checksum(self.w3, item) for item in path]
        for item in self.dex_config.get("v2", []):
            dex_name = str(item.get("name") or "V2")
            router_address = str(item.get("router") or "").lower()
            factory_address = str(item.get("factory") or "").lower()
            if not router_address:
                continue
            try:
                router = self.w3.eth.contract(address=_checksum(self.w3, router_address), abi=V2_ROUTER_ABI)
                amounts = router.functions.getAmountsOut(amount_in_raw, checksum_path).call()
                amount_out = int(amounts[-1])
            except Exception:
                continue
            if amount_out > 0:
                pools = self._v2_pools(path, factory_address)
                if factory_address and any(not pool for pool in pools):
                    continue
                results.append(
                    DexRoute(
                        protocol="v2",
                        router=router_address,
                        quoter=None,
                        path=[item.lower() for item in path],
                        fees=[],
                        amount_in_raw=amount_in_raw,
                        amount_out_raw=amount_out,
                        dex_name=dex_name,
                        factory=factory_address or None,
                        pools=pools,
                    )
                )
        return results

    def _quote_v3(self, path: list[str], amount_in_raw: int) -> list[DexRoute]:
        results: list[DexRoute] = []
        for item in self.dex_config.get("v3", []):
            dex_name = str(item.get("name") or "V3")
            router_address = str(item.get("router") or "").lower()
            factory_address = str(item.get("factory") or "").lower()
            quoter_address = str(item.get("quoter") or "").lower()
            fees = [int(fee) for fee in item.get("fees", [])]
            if not router_address or not quoter_address or not fees:
                continue
            fee_paths = [[fee] for fee in fees] if len(path) == 2 else [[fee_a, fee_b] for fee_a in fees for fee_b in fees]
            for fee_path in fee_paths:
                try:
                    encoded_path = _encode_v3_path(path, fee_path)
                    quoter = self.w3.eth.contract(address=_checksum(self.w3, quoter_address), abi=V3_QUOTER_ABI)
                    quote = quoter.functions.quoteExactInput(encoded_path, amount_in_raw).call()
                    amount_out = int(quote[0] if isinstance(quote, (list, tuple)) else quote)
                except Exception:
                    continue
                if amount_out > 0:
                    pools = self._v3_pools(path, fee_path, factory_address)
                    if factory_address and any(not pool for pool in pools):
                        continue
                    results.append(
                        DexRoute(
                            protocol="v3",
                            router=router_address,
                            quoter=quoter_address,
                            path=[item.lower() for item in path],
                            fees=fee_path,
                            amount_in_raw=amount_in_raw,
                            amount_out_raw=amount_out,
                            dex_name=dex_name,
                            factory=factory_address or None,
                            pools=pools,
                        )
                    )
        return results

    def swap_exact_tokens(
        self,
        *,
        private_key: str,
        token_in: str,
        token_out: str,
        amount_in_raw: int,
        min_output_raw: int,
        route: DexRoute,
    ) -> SwapResult:
        account = Account.from_key(private_key)
        wallet = str(account.address).lower()
        lock = _wallet_lock(self.config.name, wallet)
        with lock:
            output_before = self.token_balance(token_out, wallet)
            approval_tx_hash = self._approve_if_needed(
                private_key=private_key,
                owner=wallet,
                token=token_in,
                spender=route.router,
                amount_raw=amount_in_raw,
            )
            swap_tx_hash = self._send_swap(
                private_key=private_key,
                wallet=wallet,
                amount_in_raw=amount_in_raw,
                min_output_raw=min_output_raw,
                route=route,
            )
            output_after = self.token_balance(token_out, wallet)
            output_delta = output_after - output_before
        return SwapResult(
            approval_tx_hash=approval_tx_hash,
            swap_tx_hash=swap_tx_hash,
            output_amount_raw=output_delta if output_delta >= 0 else None,
            route=route,
        )

    def _base_tx(self, wallet: str) -> dict[str, Any]:
        tx: dict[str, Any] = {
            "from": _checksum(self.w3, wallet),
            "nonce": self.w3.eth.get_transaction_count(_checksum(self.w3, wallet), "pending"),
            "chainId": self.config.chain_id or self.w3.eth.chain_id,
        }
        gas_price = self.w3.eth.gas_price
        tx["gasPrice"] = gas_price
        return tx

    def _sign_send_wait(self, private_key: str, tx: dict[str, Any]) -> str:
        if "gas" not in tx:
            tx["gas"] = self.w3.eth.estimate_gas(tx)
        signed = self.w3.eth.account.sign_transaction(tx, private_key=private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if int(receipt.get("status", 0)) != 1:
            raise RuntimeError(f"transaction failed: {tx_hash.hex()}")
        return tx_hash.hex()

    def _approve_if_needed(self, *, private_key: str, owner: str, token: str, spender: str, amount_raw: int) -> str | None:
        token_contract = self.w3.eth.contract(address=_checksum(self.w3, token), abi=ERC20_ABI)
        allowance = int(token_contract.functions.allowance(_checksum(self.w3, owner), _checksum(self.w3, spender)).call())
        if allowance >= amount_raw:
            return None
        tx = token_contract.functions.approve(_checksum(self.w3, spender), amount_raw).build_transaction(
            self._base_tx(owner)
        )
        return self._sign_send_wait(private_key, tx)

    def _send_swap(
        self,
        *,
        private_key: str,
        wallet: str,
        amount_in_raw: int,
        min_output_raw: int,
        route: DexRoute,
    ) -> str:
        deadline = int(time.time()) + 20 * 60
        if route.protocol == "v2":
            router = self.w3.eth.contract(address=_checksum(self.w3, route.router), abi=V2_ROUTER_ABI)
            tx = router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                amount_in_raw,
                min_output_raw,
                [_checksum(self.w3, item) for item in route.path],
                _checksum(self.w3, wallet),
                deadline,
            ).build_transaction(self._base_tx(wallet))
            return self._sign_send_wait(private_key, tx)
        if route.protocol == "v3":
            router = self.w3.eth.contract(address=_checksum(self.w3, route.router), abi=V3_ROUTER_ABI)
            tx = router.functions.exactInput(
                (
                    _encode_v3_path(route.path, route.fees),
                    _checksum(self.w3, wallet),
                    amount_in_raw,
                    min_output_raw,
                )
            ).build_transaction(self._base_tx(wallet))
            return self._sign_send_wait(private_key, tx)
        raise ValueError(f"unsupported route protocol: {route.protocol}")


def apply_slippage(amount_out_raw: int, slippage_bps: int) -> int:
    bps = max(1, min(5000, int(slippage_bps)))
    return int(Decimal(amount_out_raw) * (Decimal(10_000 - bps) / Decimal(10_000)))


def protected_min_output(amount_out_raw: int, slippage_bps: int, *, allow_zero_min_output: bool = False) -> int:
    if amount_out_raw <= 0:
        raise ValueError("quote output must be positive")
    if allow_zero_min_output:
        return 0
    min_output = apply_slippage(amount_out_raw, slippage_bps)
    if min_output <= 0:
        raise ValueError(
            "quoted output is too small for non-zero slippage protection; "
            "increase trade amount or choose another output token"
        )
    return min_output


def has_whitelisted_dex_routes(chain_name: str) -> bool:
    return DirectPoolDexAdapter.has_whitelisted_routes(chain_name)

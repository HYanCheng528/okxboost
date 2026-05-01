from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class ChainConfig:
    name: str
    native_symbol: str
    chain_id: int | None
    rpc_url: str | None
    rpc_urls: list[str]
    explorer_api_url: str | None
    explorer_api_key: str | None
    base_tokens: dict[str, str]


@dataclass(frozen=True)
class Settings:
    database_url: str
    tx_source: str
    mock_tx_file: Path
    request_timeout_seconds: int
    price_bucket_minutes: int
    price_api_url: str
    feishu_app_id: str | None
    feishu_app_secret: str | None
    feishu_app_token: str | None
    chain_configs: dict[str, ChainConfig]


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _chain_config(
    *,
    name: str,
    native_symbol: str,
    chain_id: int | None,
    rpc_env: str,
    explorer_env: str,
    key_env: str,
    default_explorer: str | None,
    base_tokens: dict[str, str],
) -> ChainConfig:
    raw_rpc = os.getenv(rpc_env, "")
    rpc_urls = [item.strip() for item in raw_rpc.split(",") if item.strip()]
    primary_rpc = rpc_urls[0] if rpc_urls else None
    explorer_value = os.getenv(explorer_env)
    if explorer_value is None:
        explorer_api_url = default_explorer
    else:
        explorer_api_url = explorer_value.strip() or None
    return ChainConfig(
        name=name,
        native_symbol=native_symbol,
        chain_id=chain_id,
        rpc_url=primary_rpc,
        rpc_urls=rpc_urls,
        explorer_api_url=explorer_api_url,
        explorer_api_key=os.getenv(key_env),
        base_tokens={symbol.upper(): address.lower() for symbol, address in base_tokens.items()},
    )


def build_chain_configs() -> dict[str, ChainConfig]:
    return {
        "bsc": _chain_config(
            name="bsc",
            native_symbol="BNB",
            chain_id=56,
            rpc_env="BSC_RPC_URL",
            explorer_env="BSC_EXPLORER_API_URL",
            key_env="BSC_EXPLORER_API_KEY",
            default_explorer=None,
            base_tokens={
                "USDT": "0x55d398326f99059fF775485246999027B3197955",
                "USDC": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
            },
        ),
        "base": _chain_config(
            name="base",
            native_symbol="ETH",
            chain_id=8453,
            rpc_env="BASE_RPC_URL",
            explorer_env="BASE_EXPLORER_API_URL",
            key_env="BASE_EXPLORER_API_KEY",
            default_explorer="https://api.basescan.org/api",
            base_tokens={
                "USDT": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
                "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            },
        ),
        "arbitrum": _chain_config(
            name="arbitrum",
            native_symbol="ETH",
            chain_id=42161,
            rpc_env="ARBITRUM_RPC_URL",
            explorer_env="ARBITRUM_EXPLORER_API_URL",
            key_env="ARBITRUM_EXPLORER_API_KEY",
            default_explorer="https://api.arbiscan.io/api",
            base_tokens={
                "USDT": "0xFd086bC7CD5C481DCC9C85EbE478A1C0b69FCbb9",
                "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            },
        ),
        "ethereum": _chain_config(
            name="ethereum",
            native_symbol="ETH",
            chain_id=1,
            rpc_env="ETHEREUM_RPC_URL",
            explorer_env="ETHEREUM_EXPLORER_API_URL",
            key_env="ETHEREUM_EXPLORER_API_KEY",
            default_explorer="https://api.etherscan.io/api",
            base_tokens={
                "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
                "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            },
        ),
    }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./okx_volume_stats.db"),
        tx_source=os.getenv("TX_SOURCE", "auto").lower(),
        mock_tx_file=Path(os.getenv("MOCK_TX_FILE", "data/sample_transactions.json")),
        request_timeout_seconds=_env_int("REQUEST_TIMEOUT_SECONDS", 15),
        price_bucket_minutes=_env_int("PRICE_BUCKET_MINUTES", 5),
        price_api_url=os.getenv(
            "PRICE_API_URL",
            "https://min-api.cryptocompare.com/data/pricehistorical",
        ),
        feishu_app_id=(os.getenv("FEISHU_APP_ID") or "").strip() or None,
        feishu_app_secret=(os.getenv("FEISHU_APP_SECRET") or "").strip() or None,
        feishu_app_token=(os.getenv("FEISHU_APP_TOKEN") or "").strip() or None,
        chain_configs=build_chain_configs(),
    )

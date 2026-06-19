from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
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
    wss_url: str | None = None
    wss_urls: list[str] = field(default_factory=list)


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
    feishu_webhook_url: str | None
    pushplus_token: str | None
    detect_session_gap_minutes: int
    detect_session_padding_seconds: int
    detect_scan_max_workers: int
    reward_scan_max_workers: int
    boost_min_daily_average: Decimal
    robot_trading_enabled: bool
    robot_keystore_path: Path
    robot_keystore_password: str | None
    solana_rpc_url: str | None
    solana_rpc_urls: list[str]
    solana_stable_mints: dict[str, str]
    chain_configs: dict[str, ChainConfig]


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_decimal(name: str, default: Decimal) -> Decimal:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_urls(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def _env_mapping(name: str, defaults: dict[str, str]) -> dict[str, str]:
    values = dict(defaults)
    raw = os.getenv(name, "")
    for item in raw.split(","):
        text = item.strip()
        if not text:
            continue
        if ":" in text:
            symbol, address = text.split(":", 1)
            values[symbol.strip().upper()] = address.strip()
        else:
            values[text[:6].upper()] = text
    return {symbol: address for symbol, address in values.items() if address}


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
    wss_env: str | None = None,
) -> ChainConfig:
    rpc_urls = _env_urls(rpc_env)
    primary_rpc = rpc_urls[0] if rpc_urls else None
    wss_urls = _env_urls(wss_env) if wss_env else []
    primary_wss = wss_urls[0] if wss_urls else None
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
        wss_url=primary_wss,
        wss_urls=wss_urls,
    )


def build_chain_configs() -> dict[str, ChainConfig]:
    return {
        "bsc": _chain_config(
            name="bsc",
            native_symbol="BNB",
            chain_id=56,
            rpc_env="BSC_RPC_URL",
            wss_env="BSC_WSS_RPC_URL",
            explorer_env="BSC_EXPLORER_API_URL",
            key_env="BSC_EXPLORER_API_KEY",
            default_explorer=None,
            base_tokens={
                "USDT": "0x55d398326f99059fF775485246999027B3197955",
                "USDC": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
                "BNB": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
            },
        ),
        "base": _chain_config(
            name="base",
            native_symbol="ETH",
            chain_id=8453,
            rpc_env="BASE_RPC_URL",
            wss_env="BASE_WSS_RPC_URL",
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
            wss_env="ARBITRUM_WSS_RPC_URL",
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
            wss_env="ETHEREUM_WSS_RPC_URL",
            explorer_env="ETHEREUM_EXPLORER_API_URL",
            key_env="ETHEREUM_EXPLORER_API_KEY",
            default_explorer="https://api.etherscan.io/api",
            base_tokens={
                "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
                "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "ETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            },
        ),
        "xlayer": _chain_config(
            name="xlayer",
            native_symbol="OKB",
            chain_id=196,
            rpc_env="XLAYER_RPC_URL",
            wss_env="XLAYER_WSS_RPC_URL",
            explorer_env="XLAYER_EXPLORER_API_URL",
            key_env="XLAYER_EXPLORER_API_KEY",
            default_explorer=None,
            base_tokens={
                "USDT": "0x1E4a5963aBFD975d8c9021ce480b42188849D41d",
                "USDT0": "0x779ded0c9e1022225f8e0630b35a9b54be713736",
                "USDC": "0x74b7F16337b8972027F6196A17a631aC6dE26d22",
                "OKB": "0xe538905cf8410324e03A5A23C1c177a474D59b2b",
            },
        ),
    }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    solana_rpc_urls = _env_urls("SOLANA_RPC_URL")
    solana_stable_mints = _env_mapping(
        "SOLANA_STABLE_MINTS",
        {
            "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "USDC_DEVNET": "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
        },
    )
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
        feishu_webhook_url=(os.getenv("FEISHU_WEBHOOK_URL") or "").strip() or None,
        pushplus_token=(os.getenv("PUSHPLUS_TOKEN") or "").strip() or None,
        detect_session_gap_minutes=_env_int("DETECT_SESSION_GAP_MINUTES", 30),
        detect_session_padding_seconds=_env_int("DETECT_SESSION_PADDING_SECONDS", 60),
        detect_scan_max_workers=max(1, _env_int("DETECT_SCAN_MAX_WORKERS", 2)),
        reward_scan_max_workers=max(1, _env_int("REWARD_SCAN_MAX_WORKERS", 2)),
        boost_min_daily_average=max(Decimal("0"), _env_decimal("BOOST_MIN_DAILY_AVERAGE", Decimal("0"))),
        robot_trading_enabled=_env_bool("ROBOT_TRADING_ENABLED", False),
        robot_keystore_path=Path(os.getenv("ROBOT_KEYSTORE_PATH", "data/robot_wallets.enc.json")),
        robot_keystore_password=(os.getenv("ROBOT_KEYSTORE_PASSWORD") or "").strip() or None,
        solana_rpc_url=solana_rpc_urls[0] if solana_rpc_urls else None,
        solana_rpc_urls=solana_rpc_urls,
        solana_stable_mints=solana_stable_mints,
        chain_configs=build_chain_configs(),
    )

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.config import ChainConfig
from app.services.chain.evm_provider import EvmExplorerProvider, TxCandidate


WALLET = "0x1111111111111111111111111111111111111111"
TOKEN = "0x2222222222222222222222222222222222222222"
BASE = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
ROUTER = "0x3333333333333333333333333333333333333333"
POOL = "0x4444444444444444444444444444444444444444"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _topic(address: str) -> str:
    return f"0x{'0' * 24}{address[2:].lower()}"


def _transfer(contract: str, from_addr: str, to_addr: str, amount: int) -> dict:
    return {
        "address": contract,
        "topics": [TRANSFER_TOPIC, _topic(from_addr), _topic(to_addr)],
        "data": hex(amount),
    }


class _FixedPriceService:
    def get_price_usd(self, db, asset_symbol: str, ts: datetime) -> Decimal:
        return Decimal("100")


def test_explorer_failure_falls_back_to_rpc_logs(monkeypatch) -> None:
    provider = EvmExplorerProvider()
    config = ChainConfig(
        name="bsc",
        native_symbol="BNB",
        chain_id=56,
        rpc_url="https://example-rpc",
        rpc_urls=["https://example-rpc"],
        explorer_api_url="https://example-explorer",
        explorer_api_key="demo",
        base_tokens={"USDT": "0x1"},
    )

    def _raise(*args, **kwargs):
        raise RuntimeError("explorer blocked")

    def _rpc_candidates(*args, **kwargs):
        return [
            TxCandidate(
                tx_hash="0xabc",
                timestamp=datetime(2026, 2, 9, 0, 0, tzinfo=timezone.utc),
            )
        ]

    monkeypatch.setattr(provider, "_fetch_wallet_tx_candidates_from_explorer", _raise)
    monkeypatch.setattr(provider, "_fetch_wallet_tx_candidates_from_rpc_logs", _rpc_candidates)

    candidates = provider._fetch_wallet_tx_candidates(
        config=config,
        wallet="0x1111111111111111111111111111111111111111",
        token_address="0xtoken",
        base_address="0xbase",
        start_time=datetime(2026, 2, 9, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 2, 9, 1, 0, tzinfo=timezone.utc),
    )
    assert len(candidates) == 1
    assert candidates[0].tx_hash == "0xabc"


def test_native_base_buy_uses_transaction_value_when_wallet_has_no_base_log(monkeypatch) -> None:
    provider = EvmExplorerProvider()
    provider.price_service = _FixedPriceService()
    monkeypatch.setattr(provider, "_fetch_transaction_value", lambda *args, **kwargs: Decimal("0.36"))
    config = ChainConfig(
        name="bsc",
        native_symbol="BNB",
        chain_id=56,
        rpc_url="https://example-rpc",
        rpc_urls=["https://example-rpc"],
        explorer_api_url=None,
        explorer_api_key=None,
        base_tokens={"BNB": BASE},
    )

    parsed = provider._parse_receipt(
        config=config,
        wallet=WALLET,
        tx_hash="0xbuy",
        timestamp=datetime(2026, 5, 11, 4, 3, tzinfo=timezone.utc),
        receipt={
            "gasUsed": "0x0",
            "effectiveGasPrice": "0x0",
            "logs": [
                _transfer(TOKEN, POOL, WALLET, 2_000 * 10**18),
            ],
        },
        token_address=TOKEN,
        base_address=BASE,
        base_token="BNB",
        token_decimals=18,
        base_decimals=18,
        db=None,
    )

    assert parsed is not None
    assert parsed.token_in == Decimal("2000")
    assert parsed.usdt_out == Decimal("36.00")
    assert parsed.usdt_in == Decimal("0")


def test_native_base_sell_uses_internal_wrapped_base_transfer_when_wallet_has_no_base_log() -> None:
    provider = EvmExplorerProvider()
    provider.price_service = _FixedPriceService()
    config = ChainConfig(
        name="bsc",
        native_symbol="BNB",
        chain_id=56,
        rpc_url="https://example-rpc",
        rpc_urls=["https://example-rpc"],
        explorer_api_url=None,
        explorer_api_key=None,
        base_tokens={"BNB": BASE},
    )

    parsed = provider._parse_receipt(
        config=config,
        wallet=WALLET,
        tx_hash="0xsell",
        timestamp=datetime(2026, 5, 11, 4, 3, tzinfo=timezone.utc),
        receipt={
            "gasUsed": "0x0",
            "effectiveGasPrice": "0x0",
            "logs": [
                _transfer(TOKEN, WALLET, POOL, 2_000 * 10**18),
                _transfer(BASE, POOL, ROUTER, 35 * 10**16),
                _transfer(BASE, ROUTER, POOL, 20 * 10**16),
            ],
        },
        token_address=TOKEN,
        base_address=BASE,
        base_token="BNB",
        token_decimals=18,
        base_decimals=18,
        db=None,
    )

    assert parsed is not None
    assert parsed.token_out == Decimal("2000")
    assert parsed.usdt_in == Decimal("35.00")
    assert parsed.usdt_out == Decimal("0")

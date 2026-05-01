from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.services.chain.types import ParsedTx
from app.services.cycle_matcher import match_cycles


def _ts(hour: int, minute: int) -> datetime:
    return datetime(2026, 2, 9, hour, minute, tzinfo=timezone.utc)


def test_match_cycles_returns_two_closed_cycles() -> None:
    wallet = "0xabc"
    txs = [
        ParsedTx(
            chain="base",
            wallet=wallet,
            tx_hash="0x1",
            timestamp=_ts(0, 10),
            usdt_out=Decimal("100"),
            usdt_in=Decimal("0"),
            token_in=Decimal("500"),
            token_out=Decimal("0"),
            gas_native=Decimal("0.1"),
            gas_usd=Decimal("0.2"),
        ),
        ParsedTx(
            chain="base",
            wallet=wallet,
            tx_hash="0x2",
            timestamp=_ts(0, 15),
            usdt_out=Decimal("0"),
            usdt_in=Decimal("98"),
            token_in=Decimal("0"),
            token_out=Decimal("500"),
            gas_native=Decimal("0.1"),
            gas_usd=Decimal("0.2"),
        ),
        ParsedTx(
            chain="base",
            wallet=wallet,
            tx_hash="0x3",
            timestamp=_ts(1, 0),
            usdt_out=Decimal("120"),
            usdt_in=Decimal("0"),
            token_in=Decimal("600"),
            token_out=Decimal("0"),
            gas_native=Decimal("0.1"),
            gas_usd=Decimal("0.2"),
        ),
        ParsedTx(
            chain="base",
            wallet=wallet,
            tx_hash="0x4",
            timestamp=_ts(1, 15),
            usdt_out=Decimal("0"),
            usdt_in=Decimal("118"),
            token_in=Decimal("0"),
            token_out=Decimal("600"),
            gas_native=Decimal("0.1"),
            gas_usd=Decimal("0.2"),
        ),
    ]

    cycles = match_cycles(txs, epsilon=Decimal("0.0001"), pair_timeout_minutes=30)
    assert len(cycles) == 2

    first = cycles[0]
    assert first.trade_before_usd == Decimal("100")
    assert first.trade_after_usd == Decimal("98")
    assert first.trade_volume_usd == Decimal("198")
    assert first.wear_usd == Decimal("2")
    assert first.incomplete is False

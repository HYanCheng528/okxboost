from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from .chain.types import ParsedTx


@dataclass(slots=True)
class CycleResult:
    wallet: str
    start_at: datetime
    end_at: datetime
    trade_before_usd: Decimal
    trade_after_usd: Decimal
    trade_volume_usd: Decimal
    wear_usd: Decimal
    fee_rate: Decimal
    gas_native_total: Decimal
    gas_usd_total: Decimal | None
    tx_hashes: list[str]
    incomplete: bool


@dataclass(slots=True)
class _CycleAccumulator:
    wallet: str
    start_at: datetime
    end_at: datetime
    trade_before_usd: Decimal = Decimal("0")
    trade_after_usd: Decimal = Decimal("0")
    gas_native_total: Decimal = Decimal("0")
    gas_usd_total: Decimal = Decimal("0")
    gas_usd_missing: bool = False
    tx_hashes: list[str] = field(default_factory=list)
    incomplete: bool = False

    def apply(self, tx: ParsedTx) -> None:
        if tx.usdt_out > 0 and tx.token_in > 0:
            self.trade_before_usd += tx.usdt_out
        if tx.usdt_in > 0 and tx.token_out > 0:
            self.trade_after_usd += tx.usdt_in

        self.gas_native_total += tx.gas_native
        if tx.gas_usd is None:
            self.gas_usd_missing = True
        else:
            self.gas_usd_total += tx.gas_usd

        self.tx_hashes.append(tx.tx_hash)
        self.end_at = tx.timestamp

    def build(self) -> CycleResult:
        trade_volume = self.trade_before_usd + self.trade_after_usd
        wear = self.trade_before_usd - self.trade_after_usd
        fee_rate = wear / trade_volume if trade_volume != 0 else Decimal("0")

        return CycleResult(
            wallet=self.wallet,
            start_at=self.start_at,
            end_at=self.end_at,
            trade_before_usd=self.trade_before_usd,
            trade_after_usd=self.trade_after_usd,
            trade_volume_usd=trade_volume,
            wear_usd=wear,
            fee_rate=fee_rate,
            gas_native_total=self.gas_native_total,
            gas_usd_total=None if self.gas_usd_missing else self.gas_usd_total,
            tx_hashes=self.tx_hashes.copy(),
            incomplete=self.incomplete,
        )


def match_cycles(
    txs: list[ParsedTx],
    *,
    epsilon: Decimal,
    pair_timeout_minutes: int,
) -> list[CycleResult]:
    if not txs:
        return []

    timeout = timedelta(minutes=max(1, pair_timeout_minutes))
    sorted_txs = sorted(txs, key=lambda item: (item.timestamp, item.tx_hash))
    cycles: list[CycleResult] = []

    running_balance = Decimal("0")
    open_cycle: _CycleAccumulator | None = None
    last_ts = sorted_txs[0].timestamp

    for tx in sorted_txs:
        if open_cycle is not None and tx.timestamp - open_cycle.start_at > timeout and running_balance > epsilon:
            open_cycle.incomplete = True
            open_cycle.end_at = last_ts
            cycles.append(open_cycle.build())
            open_cycle = None
            running_balance = Decimal("0")

        prev_balance = running_balance
        running_balance += tx.token_in - tx.token_out

        if open_cycle is None and prev_balance <= epsilon and running_balance > epsilon:
            open_cycle = _CycleAccumulator(wallet=tx.wallet, start_at=tx.timestamp, end_at=tx.timestamp)

        if open_cycle is not None:
            open_cycle.apply(tx)
            if running_balance <= epsilon:
                cycles.append(open_cycle.build())
                open_cycle = None
                running_balance = Decimal("0")

        last_ts = tx.timestamp

    if open_cycle is not None:
        open_cycle.incomplete = True
        open_cycle.end_at = last_ts
        cycles.append(open_cycle.build())

    return cycles

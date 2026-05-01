from __future__ import annotations

from decimal import Decimal

from .cycle_matcher import CycleResult


def compute_summary(
    cycles: list[CycleResult],
    *,
    boost_multiplier: Decimal,
    actual_boost_volume: Decimal | None,
) -> dict[str, Decimal | int | None]:
    sum_total_volume = sum((cycle.trade_volume_usd for cycle in cycles), Decimal("0"))
    sum_wear = sum((cycle.wear_usd for cycle in cycles), Decimal("0"))
    sum_gas_native = sum((cycle.gas_native_total for cycle in cycles), Decimal("0"))

    gas_missing = any(cycle.gas_usd_total is None for cycle in cycles)
    sum_gas_usd = (
        None
        if gas_missing
        else sum((cycle.gas_usd_total for cycle in cycles if cycle.gas_usd_total is not None), Decimal("0"))
    )

    computed_boost_volume = sum_total_volume * boost_multiplier
    avg_fee_rate = sum_wear / sum_total_volume if sum_total_volume != 0 else Decimal("0")
    boost_diff = None
    if actual_boost_volume is not None:
        boost_diff = actual_boost_volume - computed_boost_volume

    return {
        "sum_total_volume": sum_total_volume,
        "computed_boost_volume": computed_boost_volume,
        "actual_boost_volume": actual_boost_volume,
        "boost_diff": boost_diff,
        "sum_gas_native": sum_gas_native,
        "sum_gas_usd": sum_gas_usd,
        "sum_wear": sum_wear,
        "avg_fee_rate": avg_fee_rate,
        "cycle_count": len(cycles),
    }

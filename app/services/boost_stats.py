from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from ..models import SavedWallet, Task

UTC = timezone.utc
WINDOW_DAYS = 10
LOOKAHEAD_DAYS = 370


def _decimal_value(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _window_total(daily_volume: dict[date, Decimal], day: date) -> Decimal:
    window_start = day - timedelta(days=WINDOW_DAYS - 1)
    total = Decimal("0")
    for item_date, amount in daily_volume.items():
        if window_start <= item_date <= day:
            total += amount
    return total


def _find_next_due_date(
    daily_volume: dict[date, Decimal],
    *,
    today: date,
    min_daily_average: Decimal,
) -> date:
    for offset in range(LOOKAHEAD_DAYS + 1):
        candidate = today + timedelta(days=offset)
        average = _window_total(daily_volume, candidate) / Decimal(WINDOW_DAYS)
        if average < min_daily_average:
            return candidate
    return today + timedelta(days=LOOKAHEAD_DAYS)


def compute_boost_average(
    db: Session,
    *,
    min_daily_average: Decimal | int | float | str = Decimal("0"),
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    today = now.date()
    min_avg = max(Decimal("0"), _decimal_value(min_daily_average))

    wallets_db = db.query(SavedWallet).order_by(SavedWallet.created_at).all()
    tasks = db.query(Task).filter(Task.status == "completed").all()

    wallet_stats: dict[str, dict] = {}
    wallet_daily: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))

    for task in tasks:
        try:
            task_wallets = [w.lower() for w in json.loads(task.wallets_json)]
        except Exception:
            continue
        if not task_wallets or not task.start_time:
            continue

        task_date = task.start_time.date()
        boost_vol = _decimal_value(task.computed_boost_volume)
        per_wallet_vol = boost_vol / Decimal(max(1, len(task_wallets)))

        for wallet_addr in task_wallets:
            if wallet_addr not in wallet_stats:
                wallet_stats[wallet_addr] = {
                    "lastTradeDate": None,
                    "taskCount": 0,
                }
            stats = wallet_stats[wallet_addr]
            stats["taskCount"] += 1
            wallet_daily[wallet_addr][task_date] += per_wallet_vol
            if stats["lastTradeDate"] is None or task_date > stats["lastTradeDate"]:
                stats["lastTradeDate"] = task_date

    result_wallets = []

    for wallet in wallets_db:
        addr_lower = wallet.address.lower()
        daily_volume = dict(wallet_daily.get(addr_lower, {}))
        ten_day_total = _window_total(daily_volume, today)
        daily_average = ten_day_total / Decimal(WINDOW_DAYS)
        stats = wallet_stats.get(addr_lower)
        last_date = stats["lastTradeDate"] if stats else None

        if min_avg > 0:
            next_due = _find_next_due_date(
                daily_volume,
                today=today,
                min_daily_average=min_avg,
            )
        elif last_date:
            # Backward-compatible fallback when no low-watermark is configured.
            next_due = last_date + timedelta(days=WINDOW_DAYS)
        else:
            next_due = None

        days_remaining = (next_due - today).days if next_due else None
        gap = daily_average - min_avg if min_avg > 0 else None

        result_wallets.append(
            {
                "address": wallet.address,
                "label": wallet.label,
                "totalBoostVolume": round(float(ten_day_total), 2),
                "dailyAverage": round(float(daily_average), 2),
                "minDailyAverage": round(float(min_avg), 2),
                "averageGapToWatermark": round(float(gap), 2) if gap is not None else None,
                "lastTradeDate": last_date.isoformat() if last_date else None,
                "nextDueDate": next_due.isoformat() if next_due else None,
                "daysRemaining": days_remaining,
                "taskCount": int(stats["taskCount"]) if stats else 0,
            }
        )

    overall_total = sum(Decimal(str(w["totalBoostVolume"])) for w in result_wallets)
    overall_daily = overall_total / Decimal(WINDOW_DAYS)

    return {
        "wallets": result_wallets,
        "windowDays": WINDOW_DAYS,
        "minDailyAverage": round(float(min_avg), 2),
        "overall": {
            "totalBoostVolume": round(float(overall_total), 2),
            "dailyAverage": round(float(overall_daily), 2),
        },
    }

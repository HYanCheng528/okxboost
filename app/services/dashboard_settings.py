from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AppCache, utc_now

DASHBOARD_SETTINGS_KEY = "settings:dashboard"


def _decimal_value(value: object, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _settings_payload(row: AppCache | None) -> dict:
    if not row:
        return {}
    try:
        payload = json.loads(row.payload_json)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def get_dashboard_min_daily_average(db: Session) -> Decimal:
    row = db.get(AppCache, DASHBOARD_SETTINGS_KEY)
    payload = _settings_payload(row)
    default_value = get_settings().boost_min_daily_average
    value = _decimal_value(payload.get("minDailyAverage"), default_value)
    return max(Decimal("0"), value)


def get_dashboard_settings(db: Session) -> dict:
    row = db.get(AppCache, DASHBOARD_SETTINGS_KEY)
    return {
        "minDailyAverage": get_dashboard_min_daily_average(db),
        "updatedAt": row.updated_at if row else None,
    }


def save_dashboard_settings(db: Session, *, min_daily_average: Decimal) -> dict:
    value = max(Decimal("0"), _decimal_value(min_daily_average))
    now = utc_now()
    payload_json = json.dumps({"minDailyAverage": str(value)}, ensure_ascii=False, separators=(",", ":"))

    row = db.get(AppCache, DASHBOARD_SETTINGS_KEY)
    if row:
        row.payload_json = payload_json
        row.updated_at = now
    else:
        row = AppCache(key=DASHBOARD_SETTINGS_KEY, payload_json=payload_json, created_at=now, updated_at=now)
        db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "minDailyAverage": value,
        "updatedAt": row.updated_at,
    }

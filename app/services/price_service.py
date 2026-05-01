from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Price


class PriceService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _bucket_timestamp(self, ts: datetime) -> datetime:
        bucket_minutes = max(1, self.settings.price_bucket_minutes)
        minute = (ts.minute // bucket_minutes) * bucket_minutes
        return ts.replace(minute=minute, second=0, microsecond=0, tzinfo=timezone.utc)

    def get_price_usd(self, db: Session, asset_symbol: str, ts: datetime) -> Decimal | None:
        asset = asset_symbol.upper()
        bucket_ts = self._bucket_timestamp(ts.astimezone(timezone.utc))

        existing = db.scalar(
            select(Price).where(Price.asset_symbol == asset, Price.bucket_ts == bucket_ts)
        )
        if existing is not None:
            return Decimal(existing.price_usd)

        value = self._fetch_remote_price(asset, ts)
        if value is None:
            return None

        db.add(Price(asset_symbol=asset, bucket_ts=bucket_ts, price_usd=value))
        db.flush()
        return value

    def _fetch_remote_price(self, asset_symbol: str, ts: datetime) -> Decimal | None:
        params = {
            "fsym": asset_symbol.upper(),
            "tsyms": "USD",
            "ts": int(ts.timestamp()),
        }
        try:
            response = requests.get(
                self.settings.price_api_url,
                params=params,
                timeout=self.settings.request_timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException:
            return None

        payload = response.json()
        price = payload.get(asset_symbol.upper(), {}).get("USD")
        if price is None:
            return None
        return Decimal(str(price))

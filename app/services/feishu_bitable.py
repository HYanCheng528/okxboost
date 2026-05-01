from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests

from ..config import Settings
from ..models import Cycle
from ..time_utils import ensure_utc


AUTH_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
API_BASE = "https://open.feishu.cn/open-apis/bitable/v1"
UTC8 = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class FeishuFieldMapping:
    date_field: str
    trade_before_field: str
    trade_after_field: str
    gas_usd_field: str


class FeishuBitableService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._tenant_access_token: str | None = None
        self._token_expires_at: datetime | None = None

    def append_cycles(
        self,
        *,
        table_id: str,
        cycles: list[Cycle],
        field_mapping: FeishuFieldMapping,
    ) -> int:
        if not cycles:
            return 0
        table_key = table_id.strip()
        if not table_key:
            raise ValueError("table_id cannot be empty")

        records = [{"fields": self._build_fields(item, field_mapping)} for item in cycles]
        return self._batch_create_records(table_id=table_key, records=records)

    def list_tables(self) -> list[dict[str, str]]:
        app_token = (self.settings.feishu_app_token or "").strip()
        if not app_token:
            raise ValueError("Missing FEISHU_APP_TOKEN")

        token = self._get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        endpoint = f"{API_BASE}/apps/{app_token}/tables"

        items: list[dict[str, str]] = []
        page_token: str | None = None
        while True:
            params: dict[str, object] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            try:
                response = requests.get(
                    endpoint,
                    headers=headers,
                    params=params,
                    timeout=self.settings.request_timeout_seconds,
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                raise RuntimeError(f"Feishu list tables request failed: {exc}") from exc

            payload = response.json()
            if int(payload.get("code", -1)) != 0:
                msg = str(payload.get("msg", "unknown error"))
                raise RuntimeError(f"Feishu list tables failed: {msg}")

            data = payload.get("data") or {}
            raw_items = data.get("items") or []
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                table_id = str(item.get("table_id", "")).strip()
                name = str(item.get("name", "")).strip() or table_id
                if table_id:
                    items.append({"tableId": table_id, "name": name})

            if not bool(data.get("has_more")):
                break
            next_token = str(data.get("page_token", "")).strip()
            if not next_token:
                break
            page_token = next_token

        return items

    def _build_fields(self, cycle: Cycle, field_mapping: FeishuFieldMapping) -> dict[str, object]:
        start_at = ensure_utc(cycle.start_at).astimezone(UTC8)
        day_start_local = datetime(start_at.year, start_at.month, start_at.day, tzinfo=UTC8)
        day_start_epoch_ms = int(day_start_local.timestamp() * 1000)

        return {
            field_mapping.date_field: day_start_epoch_ms,
            field_mapping.trade_before_field: float(Decimal(cycle.trade_before_usd)),
            field_mapping.trade_after_field: float(Decimal(cycle.trade_after_usd)),
            field_mapping.gas_usd_field: (
                float(Decimal(cycle.gas_usd_total)) if cycle.gas_usd_total is not None else 0.0
            ),
        }

    def _batch_create_records(self, *, table_id: str, records: list[dict[str, object]]) -> int:
        app_token = (self.settings.feishu_app_token or "").strip()
        if not app_token:
            raise ValueError("Missing FEISHU_APP_TOKEN")

        endpoint = f"{API_BASE}/apps/{app_token}/tables/{table_id}/records/batch_create"
        token = self._get_tenant_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        total = 0
        chunk_size = 200
        for index in range(0, len(records), chunk_size):
            chunk = records[index : index + chunk_size]
            payload = {"records": chunk}
            try:
                response = requests.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.settings.request_timeout_seconds,
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                raise RuntimeError(f"Feishu batch_create request failed: {exc}") from exc

            data = response.json()
            if int(data.get("code", -1)) != 0:
                msg = str(data.get("msg", "unknown error"))
                raise RuntimeError(f"Feishu batch_create failed: {msg}")

            total += len(chunk)
        return total

    def _get_tenant_access_token(self) -> str:
        now = datetime.now(timezone.utc)
        if (
            self._tenant_access_token is not None
            and self._token_expires_at is not None
            and now < self._token_expires_at
        ):
            return self._tenant_access_token

        app_id = (self.settings.feishu_app_id or "").strip()
        app_secret = (self.settings.feishu_app_secret or "").strip()
        if not app_id or not app_secret:
            raise ValueError("Missing FEISHU_APP_ID or FEISHU_APP_SECRET")

        payload = {"app_id": app_id, "app_secret": app_secret}
        try:
            response = requests.post(
                AUTH_URL,
                headers={"Content-Type": "application/json; charset=utf-8"},
                json=payload,
                timeout=self.settings.request_timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"Feishu auth request failed: {exc}") from exc

        data = response.json()
        if int(data.get("code", -1)) != 0:
            msg = str(data.get("msg", "unknown error"))
            raise RuntimeError(f"Feishu auth failed: {msg}")

        token = str(data.get("tenant_access_token", "")).strip()
        if not token:
            raise RuntimeError("Feishu auth succeeded but no tenant_access_token returned")

        expire_in = int(data.get("expire", 7200))
        self._tenant_access_token = token
        self._token_expires_at = now + timedelta(seconds=max(60, expire_in - 60))
        return token

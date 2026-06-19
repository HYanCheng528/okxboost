from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from ..config import Settings
from ..models import SavedWallet, WalletProfitAdjustment
from .feishu_bitable import FeishuBitableService

UTC8 = timezone(timedelta(hours=8))
WALLET_KEY_RE = re.compile(r"(\d+)\s*号")
SHORT_NUMBER_RE = re.compile(r"^\s*(\d{1,3})\s*$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
MANUAL_REBATE_TAGS = {"手返"}
FIRST_MANUAL_REBATE_PREVIOUS_DATE = date(2026, 5, 13)


@dataclass(frozen=True)
class ProfitFieldConfig:
    trade_date_field: str = "日期"
    wear_field: str = "磨损"
    reward_date_field: str = "日期"
    income_field: str = "Boost领取奖励"
    rebate_date_field: str = "日期"
    rebate_amount_field: str = "金额"
    rebate_wallet_field: str = "多选"


def normalize_wallet_key(label: str | None) -> str:
    text = (label or "").strip()
    if not text:
        return ""
    match = WALLET_KEY_RE.search(text)
    if match:
        return f"{match.group(1)}号"
    short_number = SHORT_NUMBER_RE.match(text)
    if short_number:
        return f"{short_number.group(1)}号"
    return text


def _wallet_key(label: str) -> str:
    return normalize_wallet_key(label)


def _decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, bool):
        return Decimal("0")
    if isinstance(value, int | float | Decimal):
        return Decimal(str(value))
    if isinstance(value, dict):
        for key in ("value", "text", "name"):
            if key in value:
                return _decimal(value.get(key))
        return Decimal("0")
    if isinstance(value, list):
        if not value:
            return Decimal("0")
        return _decimal(value[0])

    text = str(value).strip()
    if not text:
        return Decimal("0")
    text = text.replace(",", "").replace("$", "").replace("¥", "").replace("￥", "").replace("%", "")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _date_value(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, int | float):
        number = float(value)
        # Feishu date fields are usually epoch milliseconds.
        if number > 10_000_000_000:
            return datetime.fromtimestamp(number / 1000, tz=UTC8).date()
        if number > 100_000_000:
            return datetime.fromtimestamp(number, tz=UTC8).date()
        return None
    if isinstance(value, dict):
        for key in ("value", "text", "date", "timestamp"):
            if key in value:
                parsed = _date_value(value.get(key))
                if parsed:
                    return parsed
        return None
    if isinstance(value, list):
        for item in value:
            parsed = _date_value(item)
            if parsed:
                return parsed
        return None

    text = str(value).strip()
    if not text:
        return None
    text = text.replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC8).date()
    except ValueError:
        return None


def _selection_values(value: Any, option_aliases: dict[str, str] | None = None) -> set[str]:
    if value is None or value == "":
        return set()
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_selection_values(item, option_aliases))
        return result
    if isinstance(value, dict):
        result: set[str] = set()
        for key in ("text", "name", "value"):
            if key in value:
                result.update(_selection_values(value.get(key), option_aliases))
        return result
    text = str(value).strip()
    if not text:
        return set()
    if option_aliases and text in option_aliases:
        text = option_aliases[text]
    parts = re.split(r"[,，/、;；\s]+", text)
    result: set[str] = set()
    for part in parts:
        item = part.strip()
        if not item:
            continue
        result.add(item)
        normalized = normalize_wallet_key(item)
        if normalized:
            result.add(normalized)
    return result


def _is_manual_rebate_selection(selected: set[str]) -> bool:
    return any(item.strip() in MANUAL_REBATE_TAGS for item in selected)


def _option_aliases_for_field(
    service: FeishuBitableService,
    *,
    table_id: str,
    field_name: str,
    warnings: list[str],
) -> dict[str, str]:
    if not hasattr(service, "list_fields"):
        return {}
    try:
        fields = service.list_fields(table_id=table_id)
    except Exception as exc:
        warnings.append(f"返佣字段选项读取失败，已改用记录原值匹配：{exc}")
        return {}

    for field in fields:
        if not isinstance(field, dict):
            continue
        name = str(field.get("field_name") or field.get("fieldName") or field.get("name") or "").strip()
        if name != field_name:
            continue
        aliases: dict[str, str] = {}
        property_data = field.get("property") if isinstance(field.get("property"), dict) else {}
        options = property_data.get("options") if isinstance(property_data, dict) else None
        if not isinstance(options, list):
            options = field.get("options") if isinstance(field.get("options"), list) else []
        for option in options:
            if not isinstance(option, dict):
                continue
            option_name = str(option.get("name") or option.get("text") or option.get("value") or "").strip()
            if not option_name:
                continue
            for key in ("id", "option_id", "optionId"):
                option_id = str(option.get(key) or "").strip()
                if option_id:
                    aliases[option_id] = option_name
        return aliases
    return {}


def _month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _month_in_range(month: str, start_date: date | None, end_date: date | None) -> bool:
    if not MONTH_RE.match(month):
        return False
    year, month_number = (int(part) for part in month.split("-"))
    first_day = date(year, month_number, 1)
    if month_number == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month_number + 1, 1)
    last_day = next_month - timedelta(days=1)
    if start_date and last_day < start_date:
        return False
    if end_date and first_day > end_date:
        return False
    return True


def _empty_month(month: str) -> dict[str, float]:
    return {
        "month": month,
        "loss": 0.0,
        "rebate": 0.0,
        "actualLoss": 0.0,
        "income": 0.0,
        "netProfit": 0.0,
    }


def _in_range(day: date | None, start_date: date | None, end_date: date | None) -> bool:
    if day is None:
        return False
    if start_date and day < start_date:
        return False
    if end_date and day > end_date:
        return False
    return True


def _sum_records_by_month(
    records: list[dict[str, object]],
    *,
    date_field: str,
    amount_field: str,
    start_date: date | None,
    end_date: date | None,
) -> dict[str, Decimal]:
    result: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in records:
        day = _date_value(row.get(date_field))
        if not _in_range(day, start_date, end_date):
            continue
        result[_month_key(day)] += _decimal(row.get(amount_field))
    return dict(result)


def _adjustment_payload(row: WalletProfitAdjustment) -> dict[str, object]:
    return {
        "adjustmentId": row.id,
        "walletKey": row.wallet_key,
        "month": row.month,
        "lossAdjustment": float(Decimal(row.loss_adjustment or 0)),
        "rebateAdjustment": float(Decimal(row.rebate_adjustment or 0)),
        "incomeAdjustment": float(Decimal(row.income_adjustment or 0)),
        "note": row.note,
        "createdAt": row.created_at.isoformat(),
    }


def _find_rebate_table_id(service: FeishuBitableService, rebate_table_id: str | None) -> str:
    if rebate_table_id:
        return rebate_table_id.strip()
    for item in service.list_tables():
        if item.get("name") == "返佣":
            return item["tableId"]
    raise ValueError("未找到名为“返佣”的飞书子表，请在前端选择返佣表")


def compute_wallet_profit(
    *,
    db: Session,
    settings: Settings,
    start_date: date | None,
    end_date: date | None,
    rebate_table_id: str | None = None,
    fields: ProfitFieldConfig | None = None,
) -> dict:
    field_config = fields or ProfitFieldConfig()
    service = FeishuBitableService(settings)
    rebate_table = _find_rebate_table_id(service, rebate_table_id)

    wallets = db.query(SavedWallet).order_by(SavedWallet.created_at.asc()).all()
    warnings: list[str] = []
    wallet_stats: list[dict] = []
    all_months: set[str] = set()

    trade_cache: dict[str, list[dict[str, object]]] = {}
    reward_cache: dict[str, list[dict[str, object]]] = {}
    source_stats = {
        "walletCount": 0,
        "rebateRecordCount": 0,
        "rebateRowsInRange": 0,
        "rebateMatchedCount": 0,
        "rebateUnmatchedCount": 0,
        "walletKeys": [],
        "unmatchedRebateSelections": [],
        "manualRebateRecordCount": 0,
        "manualRebateAllocatedAmount": 0.0,
        "manualRebateUnallocatedAmount": 0.0,
        "manualRebatePeriods": [],
        "adjustmentCount": 0,
        "appliedAdjustmentCount": 0,
    }

    for wallet in wallets:
        key = _wallet_key(wallet.label)
        monthly: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: {
                "loss": Decimal("0"),
                "rebate": Decimal("0"),
                "income": Decimal("0"),
            }
        )

        trade_table_id = (wallet.feishu_trade_table_id or "").strip()
        if trade_table_id:
            try:
                trade_records = trade_cache.setdefault(
                    trade_table_id,
                    service.list_records(table_id=trade_table_id),
                )
                for month, value in _sum_records_by_month(
                    trade_records,
                    date_field=field_config.trade_date_field,
                    amount_field=field_config.wear_field,
                    start_date=start_date,
                    end_date=end_date,
                ).items():
                    monthly[month]["loss"] += value
                    all_months.add(month)
            except Exception as exc:
                warnings.append(f"{wallet.label} 交易表读取失败：{exc}")
        else:
            warnings.append(f"{wallet.label} 未关联交易记录子表")

        reward_table_id = (wallet.feishu_airdrop_table_id or "").strip()
        if reward_table_id:
            try:
                reward_records = reward_cache.setdefault(
                    reward_table_id,
                    service.list_records(table_id=reward_table_id),
                )
                for month, value in _sum_records_by_month(
                    reward_records,
                    date_field=field_config.reward_date_field,
                    amount_field=field_config.income_field,
                    start_date=start_date,
                    end_date=end_date,
                ).items():
                    monthly[month]["income"] += value
                    all_months.add(month)
            except Exception as exc:
                warnings.append(f"{wallet.label} 项目汇总表读取失败：{exc}")
        else:
            warnings.append(f"{wallet.label} 未关联空投汇总子表")

        wallet_stats.append(
            {
                "walletId": wallet.id,
                "label": wallet.label,
                "walletKey": key,
                "address": wallet.address,
                "tradeTableId": trade_table_id or None,
                "airdropTableId": reward_table_id or None,
                "_monthly": monthly,
            }
        )

    rebate_records = service.list_records(table_id=rebate_table)
    wallet_key_map = {item["walletKey"]: item for item in wallet_stats if item["walletKey"]}
    source_stats["walletCount"] = len(wallet_stats)
    source_stats["walletKeys"] = sorted(wallet_key_map)
    source_stats["rebateRecordCount"] = len(rebate_records)
    option_aliases = _option_aliases_for_field(
        service,
        table_id=rebate_table,
        field_name=field_config.rebate_wallet_field,
        warnings=warnings,
    )
    direct_rebate_rows: list[dict[str, object]] = []
    manual_rebate_by_day: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))

    for row in rebate_records:
        day = _date_value(row.get(field_config.rebate_date_field))
        amount = _decimal(row.get(field_config.rebate_amount_field))
        selected = _selection_values(row.get(field_config.rebate_wallet_field), option_aliases)
        in_report_range = _in_range(day, start_date, end_date)
        if in_report_range and (amount or selected):
            source_stats["rebateRowsInRange"] += 1

        if _is_manual_rebate_selection(selected):
            if day and amount:
                manual_rebate_by_day[day] += amount
                if in_report_range:
                    source_stats["manualRebateRecordCount"] += 1
            continue

        matched = False
        matched_wallet_keys: list[str] = []
        for wallet_key, wallet_item in wallet_key_map.items():
            if wallet_key in selected:
                matched = True
                matched_wallet_keys.append(wallet_key)
                if in_report_range and day:
                    month = _month_key(day)
                    wallet_item["_monthly"][month]["rebate"] += amount
                    all_months.add(month)

        if matched and day:
            direct_rebate_rows.append({"day": day, "amount": amount, "walletKeys": matched_wallet_keys})

        if matched and in_report_range:
            source_stats["rebateMatchedCount"] += 1
        elif in_report_range:
            source_stats["rebateUnmatchedCount"] += 1
            if len(source_stats["unmatchedRebateSelections"]) < 8:
                source_stats["unmatchedRebateSelections"].append(
                    {
                        "date": day.isoformat() if day else None,
                        "amount": float(amount),
                        "selected": sorted(selected),
                    }
                )

    previous_manual_day = FIRST_MANUAL_REBATE_PREVIOUS_DATE
    for manual_day in sorted(manual_rebate_by_day):
        manual_amount = manual_rebate_by_day[manual_day]
        if _in_range(manual_day, start_date, end_date):
            basis_by_wallet: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
            for direct_row in direct_rebate_rows:
                direct_day = direct_row["day"]
                if not isinstance(direct_day, date) or not (previous_manual_day < direct_day <= manual_day):
                    continue
                amount = Decimal(direct_row["amount"] or 0)
                for wallet_key in direct_row["walletKeys"]:
                    basis_by_wallet[str(wallet_key)] += amount

            basis_total = sum(basis_by_wallet.values(), Decimal("0"))
            period_payload = {
                "date": manual_day.isoformat(),
                "fromExclusive": previous_manual_day.isoformat(),
                "amount": float(manual_amount),
                "basisAmount": float(basis_total),
                "wallets": [],
            }
            if basis_total <= 0:
                source_stats["manualRebateUnallocatedAmount"] = float(
                    Decimal(str(source_stats["manualRebateUnallocatedAmount"])) + manual_amount
                )
                warnings.append(
                    f"手返 {manual_day.isoformat()} 金额 {manual_amount} 未分摊：上一期手返到本期之间没有可用于计算比例的钱包返佣"
                )
            else:
                month = _month_key(manual_day)
                allocated_total = Decimal("0")
                for wallet_key in sorted(basis_by_wallet):
                    basis_amount = basis_by_wallet[wallet_key]
                    allocated = manual_amount * basis_amount / basis_total
                    wallet_item = wallet_key_map.get(wallet_key)
                    if not wallet_item:
                        continue
                    wallet_item["_monthly"][month]["rebate"] += allocated
                    all_months.add(month)
                    allocated_total += allocated
                    period_payload["wallets"].append(
                        {
                            "walletKey": wallet_key,
                            "basisAmount": float(basis_amount),
                            "allocatedAmount": float(allocated),
                        }
                    )
                source_stats["manualRebateAllocatedAmount"] = float(
                    Decimal(str(source_stats["manualRebateAllocatedAmount"])) + allocated_total
                )
            source_stats["manualRebatePeriods"].append(period_payload)
        previous_manual_day = manual_day

    adjustments_payload: list[dict[str, object]] = []
    adjustments = db.query(WalletProfitAdjustment).order_by(WalletProfitAdjustment.month.asc()).all()
    source_stats["adjustmentCount"] = len(adjustments)
    for adjustment in adjustments:
        wallet_key = normalize_wallet_key(adjustment.wallet_key)
        if not wallet_key or not _month_in_range(adjustment.month, start_date, end_date):
            continue
        wallet_item = wallet_key_map.get(wallet_key)
        if not wallet_item:
            warnings.append(f"盈亏修正未匹配钱包：{adjustment.wallet_key} / {adjustment.month}")
            continue
        wallet_item["_monthly"][adjustment.month]["loss"] += Decimal(adjustment.loss_adjustment or 0)
        wallet_item["_monthly"][adjustment.month]["rebate"] += Decimal(adjustment.rebate_adjustment or 0)
        wallet_item["_monthly"][adjustment.month]["income"] += Decimal(adjustment.income_adjustment or 0)
        all_months.add(adjustment.month)
        source_stats["appliedAdjustmentCount"] += 1
        adjustments_payload.append(_adjustment_payload(adjustment))

    months = sorted(all_months)
    totals = {
        "loss": Decimal("0"),
        "rebate": Decimal("0"),
        "actualLoss": Decimal("0"),
        "income": Decimal("0"),
        "netProfit": Decimal("0"),
    }
    monthly_totals: list[dict[str, float]] = []
    for month in months:
        loss = Decimal("0")
        rebate = Decimal("0")
        income = Decimal("0")
        for wallet_item in wallet_stats:
            data = wallet_item["_monthly"].get(month)
            if not data:
                continue
            loss += data["loss"]
            rebate += data["rebate"]
            income += data["income"]
        actual_loss = loss - rebate
        net_profit = income - actual_loss
        totals["loss"] += loss
        totals["rebate"] += rebate
        totals["actualLoss"] += actual_loss
        totals["income"] += income
        totals["netProfit"] += net_profit
        monthly_totals.append(
            {
                "month": month,
                "loss": float(loss),
                "rebate": float(rebate),
                "actualLoss": float(actual_loss),
                "income": float(income),
                "netProfit": float(net_profit),
            }
        )

    wallets_payload: list[dict] = []
    for wallet_item in wallet_stats:
        monthly_payload: list[dict[str, float]] = []
        wallet_totals = {
            "loss": Decimal("0"),
            "rebate": Decimal("0"),
            "actualLoss": Decimal("0"),
            "income": Decimal("0"),
            "netProfit": Decimal("0"),
        }
        for month in months:
            data = wallet_item["_monthly"].get(
                month,
                {"loss": Decimal("0"), "rebate": Decimal("0"), "income": Decimal("0")},
            )
            loss = data["loss"]
            rebate = data["rebate"]
            income = data["income"]
            actual_loss = loss - rebate
            net_profit = income - actual_loss
            wallet_totals["loss"] += loss
            wallet_totals["rebate"] += rebate
            wallet_totals["actualLoss"] += actual_loss
            wallet_totals["income"] += income
            wallet_totals["netProfit"] += net_profit
            monthly_payload.append(
                {
                    "month": month,
                    "loss": float(loss),
                    "rebate": float(rebate),
                    "actualLoss": float(actual_loss),
                    "income": float(income),
                    "netProfit": float(net_profit),
                }
            )

        payload = {key: value for key, value in wallet_item.items() if key != "_monthly"}
        payload["totals"] = {key: float(value) for key, value in wallet_totals.items()}
        payload["months"] = monthly_payload
        wallets_payload.append(payload)

    return {
        "startDate": start_date.isoformat() if start_date else None,
        "endDate": end_date.isoformat() if end_date else None,
        "rebateTableId": rebate_table,
        "months": months,
        "totals": {key: float(value) for key, value in totals.items()},
        "monthlyTotals": monthly_totals,
        "wallets": wallets_payload,
        "warnings": warnings,
        "sourceStats": source_stats,
        "adjustments": adjustments_payload,
        "fields": {
            "tradeDateField": field_config.trade_date_field,
            "wearField": field_config.wear_field,
            "rewardDateField": field_config.reward_date_field,
            "incomeField": field_config.income_field,
            "rebateDateField": field_config.rebate_date_field,
            "rebateAmountField": field_config.rebate_amount_field,
            "rebateWalletField": field_config.rebate_wallet_field,
        },
    }

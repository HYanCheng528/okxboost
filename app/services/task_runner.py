from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
from decimal import Decimal
from typing import Iterable

from sqlalchemy import delete

from ..config import get_settings
from ..database import SessionLocal
from ..models import Cycle, Task
from ..time_utils import ensure_utc
from .calculator import compute_summary
from .chain.base import ChainProvider
from .chain.evm_provider import EvmExplorerProvider
from .chain.mock_provider import MockChainProvider
from .chain.types import ParsedTx
from .cycle_matcher import CycleResult, match_cycles
from .task_progress import clear_task_cancel, is_task_cancel_requested, set_task_progress


_mock_provider = MockChainProvider()
_evm_provider = EvmExplorerProvider()


class TaskCancelledError(Exception):
    pass


def _load_wallets(task: Task) -> list[str]:
    return [wallet.lower() for wallet in json.loads(task.wallets_json)]


def _parse_iso_datetime(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    return ensure_utc(datetime.fromisoformat(raw))


def _load_time_ranges(task: Task) -> list[tuple[datetime, datetime]]:
    if task.time_ranges_json:
        try:
            raw_ranges = json.loads(task.time_ranges_json)
            parsed: list[tuple[datetime, datetime]] = []
            for item in raw_ranges:
                if not isinstance(item, dict):
                    continue
                start_raw = item.get("startTime") or item.get("start_time")
                end_raw = item.get("endTime") or item.get("end_time")
                if not start_raw or not end_raw:
                    continue
                start_time = _parse_iso_datetime(str(start_raw))
                end_time = _parse_iso_datetime(str(end_raw))
                if start_time < end_time:
                    parsed.append((start_time, end_time))
            if parsed:
                parsed.sort(key=lambda pair: (pair[0], pair[1]))
                return parsed
        except Exception:
            pass
    return [(ensure_utc(task.start_time), ensure_utc(task.end_time))]


def _select_provider(task: Task) -> ChainProvider:
    settings = get_settings()
    mode = settings.tx_source
    if mode == "mock":
        return _mock_provider
    if mode == "explorer":
        return _evm_provider
    if mode == "auto":
        chain_config = settings.chain_configs.get(task.chain.lower())
        if chain_config and (chain_config.rpc_url or chain_config.rpc_urls):
            return _evm_provider
        return _mock_provider
    raise ValueError(f"Unsupported TX_SOURCE: {mode}")


def _dedupe_transactions(txs: Iterable[ParsedTx]) -> list[ParsedTx]:
    deduped: dict[tuple[str, str, str], ParsedTx] = {}
    for tx in txs:
        deduped[tx.dedupe_key] = tx
    values = list(deduped.values())
    values.sort(key=lambda item: (item.wallet, item.timestamp, item.tx_hash))
    return values


def _match_all_cycles(
    *,
    wallets: list[str],
    parsed_txs: list[ParsedTx],
    epsilon: Decimal,
    pair_timeout_minutes: int,
) -> list[CycleResult]:
    grouped: dict[str, list[ParsedTx]] = defaultdict(list)
    for tx in parsed_txs:
        grouped[tx.wallet.lower()].append(tx)

    all_cycles: list[CycleResult] = []
    for wallet in wallets:
        wallet_cycles = match_cycles(
            grouped.get(wallet.lower(), []),
            epsilon=epsilon,
            pair_timeout_minutes=pair_timeout_minutes,
        )
        all_cycles.extend(wallet_cycles)

    all_cycles.sort(key=lambda item: (item.start_at, item.wallet, item.end_at))
    return all_cycles


def _ensure_not_cancelled(task_id: str) -> None:
    if is_task_cancel_requested(task_id):
        raise TaskCancelledError("Task canceled by user.")


def run_task(task_id: str) -> None:
    set_task_progress(task_id, percent=2, stage="Queued", message="Task queued.")
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if task is None:
            clear_task_cancel(task_id)
            return

        _ensure_not_cancelled(task_id)
        set_task_progress(task_id, percent=5, stage="Initializing", message="Preparing task inputs.")
        task.status = "running"
        task.error_message = None
        db.flush()

        wallets = _load_wallets(task)
        provider = _select_provider(task)
        provider_name = provider.__class__.__name__
        _ensure_not_cancelled(task_id)
        set_task_progress(
            task_id,
            percent=10,
            stage="Fetching",
            message=f"Provider selected: {provider_name}.",
        )

        time_ranges = _load_time_ranges(task)
        total_ranges = max(1, len(time_ranges))
        fetch_start_pct = 10
        fetch_end_pct = 68
        all_txs: list[ParsedTx] = []

        for range_index, (start_time, end_time) in enumerate(time_ranges, start=1):
            _ensure_not_cancelled(task_id)
            range_base = fetch_start_pct + int((range_index - 1) * (fetch_end_pct - fetch_start_pct) / total_ranges)
            range_end = fetch_start_pct + int(range_index * (fetch_end_pct - fetch_start_pct) / total_ranges)
            set_task_progress(
                task_id,
                percent=range_base,
                stage="Fetching",
                message=f"Fetching range {range_index}/{total_ranges}.",
            )

            def on_provider_progress(
                percent: int,
                message: str,
                _range_index: int = range_index,
                _range_base: int = range_base,
                _range_end: int = range_end,
                _total_ranges: int = total_ranges,
            ) -> None:
                _ensure_not_cancelled(task_id)
                clamped = max(0, min(100, int(percent)))
                mapped = _range_base + int(clamped * (_range_end - _range_base) / 100)
                set_task_progress(
                    task_id,
                    percent=mapped,
                    stage="Fetching",
                    message=f"[{_range_index}/{_total_ranges}] {message}",
                )

            range_txs = provider.fetch_transactions(
                chain=task.chain,
                wallets=wallets,
                token=task.token,
                base_token=task.base_token,
                start_time=start_time,
                end_time=end_time,
                db=db,
                progress_cb=on_provider_progress,
            )
            all_txs.extend(range_txs)
            set_task_progress(
                task_id,
                percent=range_end,
                stage="Fetching",
                message=(
                    f"Finished range {range_index}/{total_ranges}. "
                    f"Accumulated raw transactions: {len(all_txs)}."
                ),
            )

        txs = all_txs
        parsed_txs = _dedupe_transactions(txs)
        _ensure_not_cancelled(task_id)
        set_task_progress(
            task_id,
            percent=72,
            stage="Matching",
            message=f"Fetched {len(parsed_txs)} related transactions.",
        )

        cycles = _match_all_cycles(
            wallets=wallets,
            parsed_txs=parsed_txs,
            epsilon=Decimal(task.epsilon),
            pair_timeout_minutes=task.pair_timeout_minutes,
        )
        _ensure_not_cancelled(task_id)
        set_task_progress(
            task_id,
            percent=82,
            stage="Persisting",
            message=f"Generated {len(cycles)} cycles.",
        )

        db.execute(delete(Cycle).where(Cycle.task_id == task.id))
        for idx, cycle in enumerate(cycles, start=1):
            _ensure_not_cancelled(task_id)
            db.add(
                Cycle(
                    task_id=task.id,
                    wallet=cycle.wallet,
                    cycle_index=idx,
                    start_at=cycle.start_at,
                    end_at=cycle.end_at,
                    trade_before_usd=cycle.trade_before_usd,
                    trade_after_usd=cycle.trade_after_usd,
                    trade_volume_usd=cycle.trade_volume_usd,
                    wear_usd=cycle.wear_usd,
                    fee_rate=cycle.fee_rate,
                    gas_native_total=cycle.gas_native_total,
                    gas_usd_total=cycle.gas_usd_total,
                    tx_hashes_json=json.dumps(cycle.tx_hashes),
                    incomplete=cycle.incomplete,
                )
            )
            if idx % 200 == 0:
                write_pct = 82 + int(idx * 8 / max(1, len(cycles)))
                set_task_progress(
                    task_id,
                    percent=write_pct,
                    stage="Persisting",
                    message=f"Persisted {idx}/{len(cycles)} cycles.",
                )

        _ensure_not_cancelled(task_id)
        set_task_progress(task_id, percent=92, stage="Summarizing", message="Computing summary.")
        summary = compute_summary(
            cycles,
            boost_multiplier=Decimal(task.boost_multiplier),
            actual_boost_volume=(
                Decimal(task.actual_boost_volume) if task.actual_boost_volume is not None else None
            ),
        )
        task.sum_total_volume = Decimal(summary["sum_total_volume"])
        task.computed_boost_volume = Decimal(summary["computed_boost_volume"])
        task.actual_boost_volume = (
            Decimal(summary["actual_boost_volume"])
            if summary["actual_boost_volume"] is not None
            else None
        )
        task.boost_diff = Decimal(summary["boost_diff"]) if summary["boost_diff"] is not None else None
        task.sum_gas_native = Decimal(summary["sum_gas_native"])
        task.sum_gas_usd = Decimal(summary["sum_gas_usd"]) if summary["sum_gas_usd"] is not None else None
        task.sum_wear = Decimal(summary["sum_wear"])
        task.avg_fee_rate = Decimal(summary["avg_fee_rate"])
        task.cycle_count = int(summary["cycle_count"])
        task.status = "completed"
        task.error_message = None

        db.commit()
        set_task_progress(task_id, percent=100, stage="Completed", message="Task completed.")
        clear_task_cancel(task_id)
    except TaskCancelledError as exc:
        db.rollback()
        task = db.get(Task, task_id)
        if task is not None:
            task.status = "canceled"
            task.error_message = str(exc)
            task.sum_total_volume = Decimal("0")
            task.computed_boost_volume = Decimal("0")
            task.boost_diff = None
            task.sum_gas_native = Decimal("0")
            task.sum_gas_usd = None
            task.sum_wear = Decimal("0")
            task.avg_fee_rate = Decimal("0")
            task.cycle_count = 0
            db.execute(delete(Cycle).where(Cycle.task_id == task.id))
            db.commit()
        set_task_progress(task_id, percent=100, stage="Canceled", message=str(exc))
        clear_task_cancel(task_id)
    except Exception as exc:
        db.rollback()
        task = db.get(Task, task_id)
        if task is not None:
            task.status = "failed"
            task.error_message = str(exc)
            db.commit()
        set_task_progress(task_id, percent=100, stage="Failed", message=str(exc))
        clear_task_cancel(task_id)
    finally:
        db.close()

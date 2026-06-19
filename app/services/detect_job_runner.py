from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from threading import Lock, Thread
from uuid import uuid4

from sqlalchemy import select

from ..database import SessionLocal
from ..models import SavedToken, SavedWallet, Task
from ..schemas import DetectJobStatusResponse, DetectSessionsRequest
from ..time_utils import ensure_utc
from .session_detector import detect_sessions
from .address_utils import is_evm_address
from .task_progress import is_task_active, mark_task_active, set_task_progress
from .task_runner import run_task


TimeRange = tuple[datetime, datetime]


@dataclass
class DetectGroup:
    chain: str
    wallet: str
    token: str
    wallet_label: str | None
    token_symbol: str | None
    ranges: list[TimeRange] = field(default_factory=list)


@dataclass
class DetectJob:
    job_id: str
    target_date: str
    status: str = "running"
    progress_percent: int = 0
    progress_message: str = "Queued."
    scanned_wallets: int = 0
    total_wallets: int = 0
    detected_ranges: int = 0
    created: int = 0
    appended: int = 0
    skipped: int = 0
    failed: int = 0
    task_ids: list[str] = field(default_factory=list)
    rows: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_jobs: dict[str, DetectJob] = {}
_jobs_lock = Lock()


def start_detect_job(payload: DetectSessionsRequest, *, boost_multiplier: Decimal, base_token: str) -> DetectJobStatusResponse:
    target_date = _target_date_key(payload.target_date)
    job = DetectJob(
        job_id=f"det_{uuid4().hex[:12]}",
        target_date=target_date,
        progress_message="Detection job created.",
    )
    _store_job(job)
    Thread(
        target=_run_detect_job,
        kwargs={
            "job_id": job.job_id,
            "payload": payload,
            "boost_multiplier": boost_multiplier,
            "base_token": base_token.upper(),
        },
        daemon=True,
    ).start()
    return get_detect_job(job.job_id)


def get_detect_job(job_id: str) -> DetectJobStatusResponse:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        copy = DetectJob(**{
            "job_id": job.job_id,
            "target_date": job.target_date,
            "status": job.status,
            "progress_percent": job.progress_percent,
            "progress_message": job.progress_message,
            "scanned_wallets": job.scanned_wallets,
            "total_wallets": job.total_wallets,
            "detected_ranges": job.detected_ranges,
            "created": job.created,
            "appended": job.appended,
            "skipped": job.skipped,
            "failed": job.failed,
            "task_ids": list(job.task_ids),
            "rows": list(job.rows),
            "errors": list(job.errors),
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        })

    return DetectJobStatusResponse(
        jobId=copy.job_id,
        status=copy.status,
        progressPercent=copy.progress_percent,
        progressMessage=copy.progress_message,
        targetDate=copy.target_date,
        scannedWallets=copy.scanned_wallets,
        totalWallets=copy.total_wallets,
        detectedRanges=copy.detected_ranges,
        created=copy.created,
        appended=copy.appended,
        skipped=copy.skipped,
        failed=copy.failed,
        taskIds=copy.task_ids,
        rows=copy.rows,
        errors=copy.errors[-100:],
    )


def _store_job(job: DetectJob) -> None:
    job.updated_at = datetime.now(timezone.utc)
    with _jobs_lock:
        _jobs[job.job_id] = job


def _update_job(job_id: str, **changes: object) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        for key, value in changes.items():
            setattr(job, key, value)
        job.updated_at = datetime.now(timezone.utc)


def _target_date_key(target_date: str | None) -> str:
    if target_date:
        datetime.strptime(target_date, "%Y-%m-%d")
        return target_date
    return datetime.now(timezone.utc).date().isoformat()


def _run_detect_job(*, job_id: str, payload: DetectSessionsRequest, boost_multiplier: Decimal, base_token: str) -> None:
    db = SessionLocal()
    try:
        target_date_key = _target_date_key(payload.target_date)
        wallets = [(wallet, label) for wallet, label in _load_wallets(db, payload.wallet_address) if is_evm_address(wallet)]
        tokens = _load_tokens(db, payload.chain)
        tokens_for_time = _filter_time_tokens(tokens, payload.token_address)

        if not wallets:
            raise ValueError("No wallets found in address book for this detection.")
        if not tokens:
            raise ValueError("No saved tokens found for this chain.")
        if not tokens_for_time:
            raise ValueError("Selected token is not found in saved tokens.")

        _update_job(
            job_id,
            total_wallets=len(wallets),
            progress_percent=2,
            progress_message=f"Ready to scan {len(wallets)} wallet(s).",
        )

        existing_tasks = _load_existing_tasks(db)
        for wallet_index, (wallet, label) in enumerate(wallets, start=1):
            wallet_name = label or wallet[:10]
            _update_job(
                job_id,
                progress_percent=_progress_for_wallet(wallet_index - 1, len(wallets)),
                progress_message=f"Scanning wallet {wallet_index}/{len(wallets)}: {wallet_name}.",
            )

            scan_after = _latest_scan_after(existing_tasks, wallet, target_date_key, payload.chain)
            sessions, _, errors = detect_sessions(
                wallets=[(wallet, label)],
                tokens=tokens,
                tokens_for_time_detection=tokens_for_time,
                target_date=target_date_key,
                scan_after=scan_after,
            )
            if errors:
                _append_errors(job_id, errors)
            if not sessions:
                if errors:
                    _increment(job_id, failed=1, scanned_wallets=wallet_index)
                else:
                    _increment(job_id, skipped=1, scanned_wallets=wallet_index)
                continue

            groups = _collect_groups(sessions, payload.token_address, payload.chain)
            if not groups:
                _increment(job_id, skipped=1, scanned_wallets=wallet_index)
                continue

            _increment(job_id, detected_ranges=len(sessions), scanned_wallets=wallet_index)
            _append_row(job_id, f"{wallet_name}: {len(sessions)} time range(s), {len(groups)} token task(s)")

            for group in groups:
                try:
                    task_id, action = _create_or_append_task(
                        db=db,
                        existing_tasks=existing_tasks,
                        target_date_key=target_date_key,
                        group=group,
                        base_token=base_token,
                        boost_multiplier=boost_multiplier,
                    )
                    if action == "created":
                        _increment(job_id, created=1)
                    else:
                        _increment(job_id, appended=1)
                    _append_task_id(job_id, task_id)
                    existing_tasks = _load_existing_tasks(db)
                except Exception as exc:
                    _increment(job_id, failed=1)
                    _append_errors(job_id, [f"{wallet_name} {group.token}: {exc}"])

        final = get_detect_job(job_id)
        status = "completed" if final.failed == 0 else "failed"
        message = (
            f"Done. Created {final.created}, appended {final.appended}, "
            f"skipped {final.skipped}, failed {final.failed}."
        )
        _update_job(job_id, status=status, progress_percent=100, progress_message=message)
    except Exception as exc:
        _update_job(job_id, status="failed", progress_percent=100, progress_message=str(exc))
        _append_errors(job_id, [str(exc)])
    finally:
        db.close()


def _load_wallets(db, wallet_address: str | None) -> list[tuple[str, str | None]]:
    rows = db.scalars(select(SavedWallet).order_by(SavedWallet.created_at.asc())).all()
    wallets = [(row.address.lower(), row.label) for row in rows]
    if wallet_address:
        wallet_lc = wallet_address.lower()
        wallets = [item for item in wallets if item[0] == wallet_lc]
    return wallets


def _load_tokens(db, chain: str | None) -> list[tuple[str, str, str | None, str | None]]:
    rows = db.scalars(select(SavedToken).order_by(SavedToken.created_at.asc())).all()
    tokens = [(row.chain.lower(), row.address.lower(), row.symbol, row.name) for row in rows]
    if chain:
        chain_lc = chain.lower()
        tokens = [item for item in tokens if item[0] == chain_lc]
    return tokens


def _filter_time_tokens(
    tokens: list[tuple[str, str, str | None, str | None]],
    token_address: str | None,
) -> list[tuple[str, str, str | None, str | None]]:
    if not token_address:
        return tokens
    token_lc = token_address.lower()
    return [item for item in tokens if item[1] == token_lc]


def _load_existing_tasks(db) -> list[Task]:
    return list(db.scalars(select(Task).order_by(Task.created_at.desc())).all())


def _latest_scan_after(existing_tasks: list[Task], wallet: str, target_date_key: str, chain: str | None) -> str | None:
    chain_lc = chain.lower() if chain else None
    candidates: list[Task] = []
    for task in existing_tasks:
        if chain_lc and task.chain.lower() != chain_lc:
            continue
        if _utc_date_only(task.start_time) != target_date_key:
            continue
        if wallet.lower() not in _task_wallets(task):
            continue
        candidates.append(task)
    if not candidates:
        return None
    latest = max(candidates, key=lambda item: ensure_utc(item.end_time))
    return (ensure_utc(latest.end_time) - timedelta(seconds=60)).isoformat()


def _collect_groups(sessions: list, selected_token: str | None, selected_chain: str | None) -> list[DetectGroup]:
    selected = selected_token.lower() if selected_token else None
    fallback_chain = (selected_chain or "bsc").lower()
    groups: dict[tuple[str, str, str], DetectGroup] = {}

    for session in sessions:
        wallet = session.wallet.lower()
        token_items = session.tokens or []
        if selected:
            token_items = [item for item in token_items if item.address.lower() == selected]
        for token_info in token_items:
            token = token_info.address.lower()
            chain = (token_info.chain or fallback_chain).lower()
            key = (chain, wallet, token)
            if key not in groups:
                groups[key] = DetectGroup(
                    chain=chain,
                    wallet=wallet,
                    token=token,
                    wallet_label=session.wallet_label,
                    token_symbol=token_info.symbol,
                )
            groups[key].ranges.append((ensure_utc(session.start_time), ensure_utc(session.end_time)))

    result = list(groups.values())
    for group in result:
        group.ranges = _merge_ranges(group.ranges)
    return result


def _create_or_append_task(
    *,
    db,
    existing_tasks: list[Task],
    target_date_key: str,
    group: DetectGroup,
    base_token: str,
    boost_multiplier: Decimal,
) -> tuple[str, str]:
    existing = _find_existing_task(existing_tasks, group, target_date_key)
    if existing is not None:
        if existing.status == "running" and is_task_active(existing.id):
            raise RuntimeError(f"existing task is still running: {existing.id}")
        merged_ranges = _merge_ranges(_task_ranges(existing) + group.ranges)
        existing.time_ranges_json = _ranges_json(merged_ranges)
        existing.start_time = merged_ranges[0][0]
        existing.end_time = merged_ranges[-1][1]
        existing.status = "running"
        existing.error_message = None
        db.commit()
        _start_stats_task(existing.id, group.ranges)
        return existing.id, "appended"

    task = Task(
        id=f"tsk_{uuid4().hex[:12]}",
        task_name=target_date_key,
        folder_id=None,
        chain=group.chain,
        wallets_json=json.dumps([group.wallet]),
        token=group.token,
        base_token=base_token,
        time_ranges_json=_ranges_json(group.ranges),
        start_time=group.ranges[0][0],
        end_time=group.ranges[-1][1],
        boost_multiplier=boost_multiplier,
        epsilon=Decimal("0.0001"),
        pair_timeout_minutes=30,
        actual_boost_volume=None,
        status="running",
    )
    db.add(task)
    db.commit()
    _start_stats_task(task.id, group.ranges)
    return task.id, "created"


def _start_stats_task(task_id: str, ranges: list[TimeRange]) -> None:
    set_task_progress(task_id, percent=2, stage="Queued", message="Created by one-click detection.")
    mark_task_active(task_id)
    Thread(target=run_task, args=(task_id, ranges), daemon=True).start()


def _find_existing_task(existing_tasks: list[Task], group: DetectGroup, target_date_key: str) -> Task | None:
    for task in existing_tasks:
        if task.chain.lower() != group.chain:
            continue
        if group.wallet not in _task_wallets(task):
            continue
        if _utc_date_only(task.start_time) != target_date_key:
            continue
        if group.token in [item.strip().lower() for item in task.token.split(",") if item.strip()]:
            return task
    return None


def _task_wallets(task: Task) -> list[str]:
    try:
        return [str(item).lower() for item in json.loads(task.wallets_json)]
    except Exception:
        return []


def _task_ranges(task: Task) -> list[TimeRange]:
    if task.time_ranges_json:
        try:
            raw = json.loads(task.time_ranges_json)
            ranges = [
                (ensure_utc(datetime.fromisoformat(str(item["startTime"]))), ensure_utc(datetime.fromisoformat(str(item["endTime"]))))
                for item in raw
                if item.get("startTime") and item.get("endTime")
            ]
            if ranges:
                return _merge_ranges(ranges)
        except Exception:
            pass
    return [(ensure_utc(task.start_time), ensure_utc(task.end_time))]


def _merge_ranges(ranges: list[TimeRange]) -> list[TimeRange]:
    ordered = sorted(
        [(ensure_utc(start), ensure_utc(end)) for start, end in ranges if ensure_utc(start) < ensure_utc(end)],
        key=lambda item: (item[0], item[1]),
    )
    merged: list[TimeRange] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        elif end > merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
    return merged


def _ranges_json(ranges: list[TimeRange]) -> str:
    return json.dumps(
        [{"startTime": ensure_utc(start).isoformat(), "endTime": ensure_utc(end).isoformat()} for start, end in ranges]
    )


def _utc_date_only(dt: datetime) -> str:
    return ensure_utc(dt).date().isoformat()


def _progress_for_wallet(done: int, total: int) -> int:
    return min(95, 5 + int(done * 90 / max(1, total)))


def _increment(job_id: str, **changes: int) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        for key, delta in changes.items():
            if key == "scanned_wallets":
                setattr(job, key, max(getattr(job, key), delta))
            else:
                setattr(job, key, getattr(job, key) + delta)
        job.progress_percent = _progress_for_wallet(job.scanned_wallets, max(1, job.total_wallets))
        job.updated_at = datetime.now(timezone.utc)


def _append_errors(job_id: str, errors: list[str]) -> None:
    if not errors:
        return
    with _jobs_lock:
        job = _jobs[job_id]
        job.errors.extend(errors)
        job.updated_at = datetime.now(timezone.utc)


def _append_row(job_id: str, row: str) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        job.rows.append(row)
        job.updated_at = datetime.now(timezone.utc)


def _append_task_id(job_id: str, task_id: str) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        if task_id not in job.task_ids:
            job.task_ids.append(task_id)
        job.updated_at = datetime.now(timezone.utc)

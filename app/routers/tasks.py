from __future__ import annotations

import csv
from datetime import datetime
import io
import json
from decimal import Decimal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import Cycle, Task, TaskFolder
from ..schemas import (
    TaskAppendRangesRequest,
    CycleItem,
    CycleListResponse,
    RangeSummaryResponse,
    SummaryResponse,
    TaskFolderAssignRequest,
    TaskFolderCreateRequest,
    TaskFolderResponse,
    TaskCreateRequest,
    TaskCreateResponse,
    TaskDetailResponse,
    TaskListItem,
    TaskActionResponse,
    TaskPatchRequest,
    TaskSyncFeishuRequest,
    TaskSyncFeishuResponse,
    FeishuTableItem,
    TaskTimeRange,
)
from ..services.feishu_bitable import FeishuBitableService, FeishuFieldMapping
from ..services.task_progress import (
    clear_task_runtime_state,
    get_task_progress,
    is_task_cancel_requested,
    request_task_cancel,
    set_task_progress,
)
from ..services.task_runner import run_task
from ..time_utils import ensure_utc

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _parse_wallets(task: Task) -> list[str]:
    return json.loads(task.wallets_json)


def _folder_name(task: Task) -> str | None:
    return task.folder.name if task.folder is not None else None


def _folder_or_404(db: Session, folder_id: str) -> TaskFolder:
    folder = db.get(TaskFolder, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail=f"folder not found: {folder_id}")
    return folder


def _task_folder_response(folder: TaskFolder) -> TaskFolderResponse:
    return TaskFolderResponse(
        folderId=folder.id,
        name=folder.name,
        createdAt=_to_utc(folder.created_at),
    )


def _validate_runtime_requirements_for_chain(*, chain: str, base_token: str) -> None:
    settings = get_settings()
    chain_key = chain.lower()
    chain_config = settings.chain_configs.get(chain_key)
    if chain_config is None:
        raise HTTPException(status_code=400, detail=f"Unsupported chain: {chain}")

    if base_token.upper() not in chain_config.base_tokens:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported base token for {chain}: {base_token}",
        )

    if settings.tx_source == "explorer":
        has_rpc = bool(chain_config.rpc_url or chain_config.rpc_urls)
        if not has_rpc:
            env_name = f"{chain.upper()}_RPC_URL"
            raise HTTPException(
                status_code=400,
                detail=f"Missing RPC config for chain {chain}. Please set {env_name}.",
            )


def _validate_runtime_requirements(payload: TaskCreateRequest) -> None:
    _validate_runtime_requirements_for_chain(
        chain=payload.chain,
        base_token=payload.base_token,
    )


def _validate_feishu_requirements() -> None:
    settings = get_settings()
    missing: list[str] = []
    if not settings.feishu_app_id:
        missing.append("FEISHU_APP_ID")
    if not settings.feishu_app_secret:
        missing.append("FEISHU_APP_SECRET")
    if not settings.feishu_app_token:
        missing.append("FEISHU_APP_TOKEN")
    if missing:
        joined = ", ".join(missing)
        raise HTTPException(status_code=400, detail=f"Missing Feishu config: {joined}")


def _to_utc(dt: datetime) -> datetime:
    return ensure_utc(dt)


def _parse_iso_datetime(value: str) -> datetime:
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    return _to_utc(datetime.fromisoformat(raw))


def _normalize_time_ranges_input(
    *,
    time_ranges: list[TaskTimeRange] | None,
    start_time: datetime | None,
    end_time: datetime | None,
) -> list[tuple[datetime, datetime]]:
    if time_ranges:
        ranges: list[tuple[datetime, datetime]] = []
        for idx, item in enumerate(time_ranges, start=1):
            start_time = _to_utc(item.start_time)
            end_time = _to_utc(item.end_time)
            if start_time >= end_time:
                raise HTTPException(
                    status_code=400,
                    detail=f"timeRanges[{idx - 1}] startTime must be before endTime",
                )
            ranges.append((start_time, end_time))
        ranges.sort(key=lambda pair: (pair[0], pair[1]))
        return ranges

    if start_time is None or end_time is None:
        raise HTTPException(
            status_code=400,
            detail="Provide either timeRanges or both startTime and endTime",
        )

    start_value = _to_utc(start_time)
    end_value = _to_utc(end_time)
    if start_value >= end_value:
        raise HTTPException(status_code=400, detail="startTime must be before endTime")
    return [(start_value, end_value)]


def _normalize_time_ranges(payload: TaskCreateRequest) -> list[tuple[datetime, datetime]]:
    return _normalize_time_ranges_input(
        time_ranges=payload.time_ranges,
        start_time=payload.start_time,
        end_time=payload.end_time,
    )


def _time_ranges_from_task(task: Task) -> list[tuple[datetime, datetime]]:
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
                parsed.append((_parse_iso_datetime(str(start_raw)), _parse_iso_datetime(str(end_raw))))
            if parsed:
                parsed.sort(key=lambda pair: (pair[0], pair[1]))
                return parsed
        except Exception:
            pass
    return [(_to_utc(task.start_time), _to_utc(task.end_time))]


def _time_ranges_response(task: Task) -> list[TaskTimeRange]:
    return [
        TaskTimeRange(startTime=start_time, endTime=end_time)
        for start_time, end_time in _time_ranges_from_task(task)
    ]


def _merge_time_ranges(ranges: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda pair: (pair[0], pair[1]))
    merged: list[list[datetime]] = []
    for start_time, end_time in ordered:
        if not merged:
            merged.append([start_time, end_time])
            continue
        last = merged[-1]
        if start_time < last[1]:
            if end_time > last[1]:
                last[1] = end_time
            continue
        merged.append([start_time, end_time])
    return [(item[0], item[1]) for item in merged]


def _time_ranges_to_json(ranges: list[tuple[datetime, datetime]]) -> str:
    payload = [
        {"startTime": start_time.isoformat(), "endTime": end_time.isoformat()}
        for start_time, end_time in ranges
    ]
    return json.dumps(payload, ensure_ascii=False)


def _summary_from_task(task: Task) -> SummaryResponse:
    return SummaryResponse(
        sumTotalVolume=Decimal(task.sum_total_volume),
        computedBoostVolume=Decimal(task.computed_boost_volume),
        actualBoostVolume=(
            Decimal(task.actual_boost_volume) if task.actual_boost_volume is not None else None
        ),
        boostDiff=Decimal(task.boost_diff) if task.boost_diff is not None else None,
        sumGasNative=Decimal(task.sum_gas_native),
        sumGasUsd=Decimal(task.sum_gas_usd) if task.sum_gas_usd is not None else None,
        sumWear=Decimal(task.sum_wear),
        avgFeeRate=Decimal(task.avg_fee_rate),
        cycleCount=task.cycle_count,
    )


def _cycle_range_index(
    cycle_start: datetime,
    ranges: list[tuple[datetime, datetime]],
) -> int | None:
    for idx, (start_time, end_time) in enumerate(ranges):
        if cycle_start < start_time:
            return None
        if start_time <= cycle_start < end_time:
            return idx
        if idx == len(ranges) - 1 and cycle_start == end_time:
            return idx
    return None


def _range_summaries_from_task(task: Task, db: Session) -> list[RangeSummaryResponse]:
    ranges = _time_ranges_from_task(task)
    if not ranges:
        return []

    buckets: list[dict[str, object]] = []
    for _ in ranges:
        buckets.append(
            {
                "sum_total_volume": Decimal("0"),
                "sum_wear": Decimal("0"),
                "sum_gas_native": Decimal("0"),
                "sum_gas_usd": Decimal("0"),
                "gas_usd_missing": False,
                "cycle_count": 0,
                "wallets": set(),
            }
        )

    cycles = db.scalars(
        select(Cycle)
        .where(Cycle.task_id == task.id)
        .order_by(Cycle.start_at.asc(), Cycle.cycle_index.asc())
    ).all()

    for cycle in cycles:
        range_idx = _cycle_range_index(_to_utc(cycle.start_at), ranges)
        if range_idx is None:
            continue
        bucket = buckets[range_idx]
        bucket["sum_total_volume"] = Decimal(bucket["sum_total_volume"]) + Decimal(cycle.trade_volume_usd)
        bucket["sum_wear"] = Decimal(bucket["sum_wear"]) + Decimal(cycle.wear_usd)
        bucket["sum_gas_native"] = Decimal(bucket["sum_gas_native"]) + Decimal(cycle.gas_native_total)
        bucket["cycle_count"] = int(bucket["cycle_count"]) + 1
        wallets = bucket["wallets"]
        if isinstance(wallets, set):
            wallets.add(cycle.wallet.lower())

        if cycle.gas_usd_total is None:
            bucket["gas_usd_missing"] = True
        else:
            bucket["sum_gas_usd"] = Decimal(bucket["sum_gas_usd"]) + Decimal(cycle.gas_usd_total)

    boost_multiplier = Decimal(task.boost_multiplier)
    response: list[RangeSummaryResponse] = []
    for idx, (start_time, end_time) in enumerate(ranges, start=1):
        bucket = buckets[idx - 1]
        sum_total_volume = Decimal(bucket["sum_total_volume"])
        sum_wear = Decimal(bucket["sum_wear"])
        avg_fee_rate = sum_wear / sum_total_volume if sum_total_volume != 0 else Decimal("0")
        wallets = bucket["wallets"]
        wallet_count = len(wallets) if isinstance(wallets, set) else 0
        sum_gas_usd = (
            None
            if bool(bucket["gas_usd_missing"])
            else Decimal(bucket["sum_gas_usd"])
        )
        response.append(
            RangeSummaryResponse(
                rangeIndex=idx,
                startTime=start_time,
                endTime=end_time,
                sumTotalVolume=sum_total_volume,
                computedBoostVolume=sum_total_volume * boost_multiplier,
                sumGasNative=Decimal(bucket["sum_gas_native"]),
                sumGasUsd=sum_gas_usd,
                sumWear=sum_wear,
                avgFeeRate=avg_fee_rate,
                cycleCount=int(bucket["cycle_count"]),
                walletCount=wallet_count,
            )
        )
    return response


def _progress_payload(task: Task) -> dict[str, object]:
    progress = get_task_progress(task.id, status=task.status)
    return {
        "progressPercent": progress.percent,
        "progressStage": progress.stage,
        "progressMessage": progress.message,
    }


def _action_response_from_task(task: Task) -> TaskActionResponse:
    progress = _progress_payload(task)
    return TaskActionResponse(
        taskId=task.id,
        status=task.status,
        progressPercent=int(progress["progressPercent"]),
        progressStage=str(progress["progressStage"]),
        progressMessage=str(progress["progressMessage"]) if progress["progressMessage"] else None,
    )


def _detail_from_task(task: Task, db: Session) -> TaskDetailResponse:
    progress = _progress_payload(task)
    return TaskDetailResponse(
        taskId=task.id,
        taskName=task.task_name,
        folderId=task.folder_id,
        folderName=_folder_name(task),
        chain=task.chain,
        wallets=_parse_wallets(task),
        token=task.token,
        baseToken=task.base_token,
        startTime=_to_utc(task.start_time),
        endTime=_to_utc(task.end_time),
        timeRanges=_time_ranges_response(task),
        boostMultiplier=Decimal(task.boost_multiplier),
        epsilon=Decimal(task.epsilon),
        pairTimeoutMinutes=task.pair_timeout_minutes,
        actualBoostVolume=(
            Decimal(task.actual_boost_volume) if task.actual_boost_volume is not None else None
        ),
        status=task.status,
        progressPercent=int(progress["progressPercent"]),
        progressStage=str(progress["progressStage"]),
        progressMessage=str(progress["progressMessage"]) if progress["progressMessage"] else None,
        errorMessage=task.error_message,
        createdAt=_to_utc(task.created_at),
        updatedAt=_to_utc(task.updated_at),
        summary=_summary_from_task(task),
        rangeSummaries=_range_summaries_from_task(task, db),
    )


@router.post("", response_model=TaskCreateResponse)
def create_task(
    payload: TaskCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> TaskCreateResponse:
    if not payload.wallets:
        raise HTTPException(status_code=400, detail="wallets cannot be empty")
    _validate_runtime_requirements(payload)

    ranges = _normalize_time_ranges(payload)

    wallets = [wallet.lower() for wallet in payload.wallets]
    task_name = payload.task_name.strip() if payload.task_name else None
    if task_name == "":
        task_name = None
    folder_id = payload.folder_id.strip() if payload.folder_id else None
    if folder_id == "":
        folder_id = None
    if folder_id is not None:
        _folder_or_404(db, folder_id)

    task = Task(
        id=f"tsk_{uuid4().hex[:12]}",
        task_name=task_name,
        folder_id=folder_id,
        chain=payload.chain.lower(),
        wallets_json=json.dumps(wallets),
        token=payload.token.lower(),
        base_token=payload.base_token.upper(),
        time_ranges_json=_time_ranges_to_json(ranges),
        start_time=ranges[0][0],
        end_time=ranges[-1][1],
        boost_multiplier=payload.boost_multiplier,
        epsilon=payload.epsilon,
        pair_timeout_minutes=payload.pair_timeout_minutes,
        actual_boost_volume=payload.actual_boost_volume,
        status="running",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    set_task_progress(task.id, percent=2, stage="Queued", message="Task created, waiting to run.")

    background_tasks.add_task(run_task, task.id)
    progress = _progress_payload(task)
    return TaskCreateResponse(
        taskId=task.id,
        status=task.status,
        progressPercent=int(progress["progressPercent"]),
        progressStage=str(progress["progressStage"]),
        progressMessage=str(progress["progressMessage"]) if progress["progressMessage"] else None,
    )


@router.post("/{task_id}/append-ranges", response_model=TaskCreateResponse)
def append_task_ranges(
    task_id: str,
    payload: TaskAppendRangesRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> TaskCreateResponse:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status == "running":
        raise HTTPException(status_code=409, detail="task is running, cannot append ranges now")

    _validate_runtime_requirements_for_chain(chain=task.chain, base_token=task.base_token)
    appended_ranges = _normalize_time_ranges_input(
        time_ranges=payload.time_ranges,
        start_time=payload.start_time,
        end_time=payload.end_time,
    )
    merged_ranges = _merge_time_ranges(_time_ranges_from_task(task) + appended_ranges)
    if not merged_ranges:
        raise HTTPException(status_code=400, detail="no valid time ranges")

    task.time_ranges_json = _time_ranges_to_json(merged_ranges)
    task.start_time = merged_ranges[0][0]
    task.end_time = merged_ranges[-1][1]
    task.status = "running"
    task.error_message = None
    db.commit()
    db.refresh(task)

    set_task_progress(
        task.id,
        percent=2,
        stage="Queued",
        message=f"Added {len(appended_ranges)} range(s), re-scanning task.",
    )
    background_tasks.add_task(run_task, task.id)
    progress = _progress_payload(task)
    return TaskCreateResponse(
        taskId=task.id,
        status=task.status,
        progressPercent=int(progress["progressPercent"]),
        progressStage=str(progress["progressStage"]),
        progressMessage=str(progress["progressMessage"]) if progress["progressMessage"] else None,
    )


@router.get("", response_model=list[TaskListItem])
def list_tasks(
    folder_id: str | None = Query(default=None, alias="folderId"),
    db: Session = Depends(get_db),
) -> list[TaskListItem]:
    stmt = select(Task)
    if folder_id is not None:
        key = folder_id.strip()
        if key.lower() == "none":
            stmt = stmt.where(Task.folder_id.is_(None))
        elif key:
            stmt = stmt.where(Task.folder_id == key)
    tasks = db.scalars(stmt.order_by(Task.created_at.desc())).all()
    result: list[TaskListItem] = []
    for task in tasks:
        progress = _progress_payload(task)
        result.append(
            TaskListItem(
                taskId=task.id,
                taskName=task.task_name,
                folderId=task.folder_id,
                folderName=_folder_name(task),
                chain=task.chain,
                wallets=_parse_wallets(task),
                token=task.token,
                baseToken=task.base_token,
                startTime=_to_utc(task.start_time),
                endTime=_to_utc(task.end_time),
                timeRangeCount=len(_time_ranges_from_task(task)),
                boostMultiplier=Decimal(task.boost_multiplier),
                status=task.status,
                progressPercent=int(progress["progressPercent"]),
                progressStage=str(progress["progressStage"]),
                progressMessage=str(progress["progressMessage"]) if progress["progressMessage"] else None,
                createdAt=_to_utc(task.created_at),
                summary=_summary_from_task(task),
            )
        )
    return result


@router.get("/folders", response_model=list[TaskFolderResponse])
def list_task_folders(db: Session = Depends(get_db)) -> list[TaskFolderResponse]:
    folders = db.scalars(select(TaskFolder).order_by(TaskFolder.created_at.desc())).all()
    return [_task_folder_response(item) for item in folders]


@router.get("/feishu/tables", response_model=list[FeishuTableItem])
def list_feishu_tables() -> list[FeishuTableItem]:
    _validate_feishu_requirements()
    service = FeishuBitableService(get_settings())
    try:
        items = service.list_tables()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return [FeishuTableItem(tableId=item["tableId"], name=item["name"]) for item in items]


@router.post("/folders", response_model=TaskFolderResponse)
def create_task_folder(
    payload: TaskFolderCreateRequest,
    db: Session = Depends(get_db),
) -> TaskFolderResponse:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="folder name cannot be empty")

    existing = db.scalar(select(TaskFolder).where(TaskFolder.name == name))
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"folder already exists: {name}")

    folder = TaskFolder(id=f"fld_{uuid4().hex[:12]}", name=name)
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return _task_folder_response(folder)


@router.patch("/{task_id}/folder", response_model=TaskDetailResponse)
def assign_task_folder(
    task_id: str,
    payload: TaskFolderAssignRequest,
    db: Session = Depends(get_db),
) -> TaskDetailResponse:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    folder_id = payload.folder_id.strip() if payload.folder_id else None
    if folder_id == "":
        folder_id = None
    if folder_id is not None:
        _folder_or_404(db, folder_id)

    task.folder_id = folder_id
    db.commit()
    db.refresh(task)
    return _detail_from_task(task, db)


@router.get("/{task_id}", response_model=TaskDetailResponse)
def get_task(task_id: str, db: Session = Depends(get_db)) -> TaskDetailResponse:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return _detail_from_task(task, db)


@router.post("/{task_id}/cancel", response_model=TaskActionResponse)
def cancel_task(task_id: str, db: Session = Depends(get_db)) -> TaskActionResponse:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    if task.status == "running":
        request_task_cancel(task.id)
        set_task_progress(
            task.id,
            percent=100,
            stage="Canceling",
            message="Cancellation requested. Waiting for the task to stop safely.",
        )
        return _action_response_from_task(task)

    if task.status == "canceled":
        return _action_response_from_task(task)

    raise HTTPException(status_code=409, detail=f"task is not running: {task.status}")


@router.delete("/{task_id}", response_model=TaskActionResponse)
def delete_task(task_id: str, db: Session = Depends(get_db)) -> TaskActionResponse:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    if task.status == "running" or is_task_cancel_requested(task.id):
        raise HTTPException(status_code=409, detail="task is running, cancel it before deleting")

    response = _action_response_from_task(task)
    db.execute(delete(Cycle).where(Cycle.task_id == task.id))
    db.delete(task)
    db.commit()
    clear_task_runtime_state(task.id)
    return response


@router.get("/{task_id}/cycles", response_model=CycleListResponse)
def get_cycles(
    task_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500, alias="pageSize"),
    db: Session = Depends(get_db),
) -> CycleListResponse:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    total = db.scalar(select(func.count(Cycle.id)).where(Cycle.task_id == task_id)) or 0
    offset = (page - 1) * page_size
    cycles = db.scalars(
        select(Cycle)
        .where(Cycle.task_id == task_id)
        .order_by(Cycle.cycle_index.asc())
        .offset(offset)
        .limit(page_size)
    ).all()

    items = [
        CycleItem(
            cycleIndex=cycle.cycle_index,
            wallet=cycle.wallet,
            startAt=_to_utc(cycle.start_at),
            endAt=_to_utc(cycle.end_at),
            tradeBeforeUsd=Decimal(cycle.trade_before_usd),
            tradeAfterUsd=Decimal(cycle.trade_after_usd),
            tradeVolumeUsd=Decimal(cycle.trade_volume_usd),
            wearUsd=Decimal(cycle.wear_usd),
            feeRate=Decimal(cycle.fee_rate),
            gasNativeTotal=Decimal(cycle.gas_native_total),
            gasUsdTotal=Decimal(cycle.gas_usd_total) if cycle.gas_usd_total is not None else None,
            txHashes=json.loads(cycle.tx_hashes_json),
            incomplete=cycle.incomplete,
        )
        for cycle in cycles
    ]
    return CycleListResponse(page=page, pageSize=page_size, total=total, items=items)


@router.post("/{task_id}/sync-feishu", response_model=TaskSyncFeishuResponse)
def sync_task_to_feishu(
    task_id: str,
    payload: TaskSyncFeishuRequest,
    db: Session = Depends(get_db),
) -> TaskSyncFeishuResponse:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    _validate_feishu_requirements()

    table_id = payload.table_id.strip()
    if not table_id:
        raise HTTPException(status_code=400, detail="tableId cannot be empty")

    date_field = payload.date_field.strip()
    trade_before_field = payload.trade_before_field.strip()
    trade_after_field = payload.trade_after_field.strip()
    gas_usd_field = payload.gas_usd_field.strip()
    if not all([date_field, trade_before_field, trade_after_field, gas_usd_field]):
        raise HTTPException(status_code=400, detail="field names cannot be empty")

    selected_wallet = payload.wallet.strip().lower() if payload.wallet else None
    if selected_wallet:
        task_wallets = {item.lower() for item in _parse_wallets(task)}
        if selected_wallet not in task_wallets:
            raise HTTPException(status_code=400, detail=f"wallet not in task: {selected_wallet}")

    stmt = select(Cycle).where(Cycle.task_id == task_id)
    if selected_wallet:
        stmt = stmt.where(Cycle.wallet == selected_wallet)
    cycles = db.scalars(stmt.order_by(Cycle.cycle_index.asc())).all()

    service = FeishuBitableService(get_settings())
    field_mapping = FeishuFieldMapping(
        date_field=date_field,
        trade_before_field=trade_before_field,
        trade_after_field=trade_after_field,
        gas_usd_field=gas_usd_field,
    )

    try:
        appended_count = service.append_cycles(
            table_id=table_id,
            cycles=cycles,
            field_mapping=field_mapping,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return TaskSyncFeishuResponse(
        taskId=task.id,
        tableId=table_id,
        wallet=selected_wallet,
        appendedCount=appended_count,
    )


@router.patch("/{task_id}", response_model=TaskDetailResponse)
def patch_task(
    task_id: str,
    payload: TaskPatchRequest,
    db: Session = Depends(get_db),
) -> TaskDetailResponse:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    task.actual_boost_volume = payload.actual_boost_volume
    task.computed_boost_volume = Decimal(task.sum_total_volume) * Decimal(task.boost_multiplier)
    task.boost_diff = (
        Decimal(payload.actual_boost_volume) - Decimal(task.computed_boost_volume)
        if payload.actual_boost_volume is not None
        else None
    )
    db.commit()
    db.refresh(task)
    return _detail_from_task(task, db)


@router.get("/{task_id}/export.csv")
def export_task_csv(task_id: str, db: Session = Depends(get_db)) -> StreamingResponse:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    cycles = db.scalars(
        select(Cycle).where(Cycle.task_id == task_id).order_by(Cycle.cycle_index.asc())
    ).all()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["section", "key", "value"])
    writer.writerow(["task", "task_id", task.id])
    writer.writerow(["task", "task_name", task.task_name or ""])
    writer.writerow(["task", "folder_id", task.folder_id or ""])
    writer.writerow(["task", "folder_name", _folder_name(task) or ""])
    writer.writerow(["task", "time_range_count", str(len(_time_ranges_from_task(task)))])
    writer.writerow(
        [
            "task",
            "time_ranges",
            json.dumps(
                [
                    {"startTime": start_time.isoformat(), "endTime": end_time.isoformat()}
                    for start_time, end_time in _time_ranges_from_task(task)
                ],
                ensure_ascii=False,
            ),
        ]
    )

    summary = _summary_from_task(task)
    writer.writerow(["summary", "sum_total_volume", str(summary.sum_total_volume)])
    writer.writerow(["summary", "computed_boost_volume", str(summary.computed_boost_volume)])
    writer.writerow(
        [
            "summary",
            "actual_boost_volume",
            str(summary.actual_boost_volume) if summary.actual_boost_volume is not None else "",
        ]
    )
    writer.writerow(
        ["summary", "boost_diff", str(summary.boost_diff) if summary.boost_diff is not None else ""]
    )
    writer.writerow(["summary", "sum_gas_native", str(summary.sum_gas_native)])
    writer.writerow(
        ["summary", "sum_gas_usd", str(summary.sum_gas_usd) if summary.sum_gas_usd is not None else ""]
    )
    writer.writerow(["summary", "sum_wear", str(summary.sum_wear)])
    writer.writerow(["summary", "avg_fee_rate", str(summary.avg_fee_rate)])
    writer.writerow(["summary", "cycle_count", str(summary.cycle_count)])
    writer.writerow([])

    writer.writerow(
        [
            "cycle_index",
            "wallet",
            "start_at",
            "end_at",
            "trade_before_usd",
            "trade_after_usd",
            "trade_volume_usd",
            "wear_usd",
            "fee_rate",
            "gas_native_total",
            "gas_usd_total",
            "incomplete",
            "tx_hashes",
        ]
    )

    for cycle in cycles:
        writer.writerow(
            [
                cycle.cycle_index,
                cycle.wallet,
                cycle.start_at.isoformat(),
                cycle.end_at.isoformat(),
                str(cycle.trade_before_usd),
                str(cycle.trade_after_usd),
                str(cycle.trade_volume_usd),
                str(cycle.wear_usd),
                str(cycle.fee_rate),
                str(cycle.gas_native_total),
                str(cycle.gas_usd_total) if cycle.gas_usd_total is not None else "",
                str(cycle.incomplete),
                "|".join(json.loads(cycle.tx_hashes_json)),
            ]
        )

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{task_id}.csv"'},
    )

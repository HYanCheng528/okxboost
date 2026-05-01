from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock


@dataclass(slots=True)
class ProgressSnapshot:
    percent: int
    stage: str
    message: str | None
    updated_at: datetime


_progress_store: dict[str, ProgressSnapshot] = {}
_progress_lock = Lock()
_cancel_requests: set[str] = set()
_cancel_lock = Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def set_task_progress(task_id: str, *, percent: int, stage: str, message: str | None = None) -> None:
    normalized = max(0, min(100, int(percent)))
    snapshot = ProgressSnapshot(
        percent=normalized,
        stage=stage,
        message=message,
        updated_at=_now(),
    )
    with _progress_lock:
        existing = _progress_store.get(task_id)
        if existing is not None and snapshot.percent < existing.percent and stage not in {"失败"}:
            snapshot.percent = existing.percent
        _progress_store[task_id] = snapshot


def clear_task_progress(task_id: str) -> None:
    with _progress_lock:
        _progress_store.pop(task_id, None)


def request_task_cancel(task_id: str) -> None:
    with _cancel_lock:
        _cancel_requests.add(task_id)


def is_task_cancel_requested(task_id: str) -> bool:
    with _cancel_lock:
        return task_id in _cancel_requests


def clear_task_cancel(task_id: str) -> None:
    with _cancel_lock:
        _cancel_requests.discard(task_id)


def clear_task_runtime_state(task_id: str) -> None:
    clear_task_progress(task_id)
    clear_task_cancel(task_id)


def get_task_progress(task_id: str, *, status: str) -> ProgressSnapshot:
    with _progress_lock:
        snapshot = _progress_store.get(task_id)
    if snapshot is not None:
        return snapshot
    if status == "canceled":
        return ProgressSnapshot(
            percent=100,
            stage="Canceled",
            message="Task canceled by user.",
            updated_at=_now(),
        )

    if status == "completed":
        return ProgressSnapshot(percent=100, stage="已完成", message="统计任务完成。", updated_at=_now())
    if status == "failed":
        return ProgressSnapshot(percent=100, stage="失败", message="任务失败，请查看错误信息。", updated_at=_now())
    if status == "running":
        return ProgressSnapshot(percent=5, stage="运行中", message="任务正在执行。", updated_at=_now())
    return ProgressSnapshot(percent=0, stage="排队中", message="等待任务开始。", updated_at=_now())

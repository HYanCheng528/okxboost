from __future__ import annotations

from sqlalchemy import select

from ..database import SessionLocal
from ..models import BoostReward, Task
from .task_progress import clear_task_runtime_state


INTERRUPTED_MESSAGE = "Service restarted before this background job finished. Please rerun it if needed."


def recover_interrupted_jobs() -> None:
    """BackgroundTasks/threads do not survive process restarts; make that explicit in DB."""
    db = SessionLocal()
    try:
        changed = False
        tasks = db.scalars(select(Task).where(Task.status == "running")).all()
        for task in tasks:
            task.status = "failed"
            task.error_message = INTERRUPTED_MESSAGE
            clear_task_runtime_state(task.id)
            changed = True

        rewards = db.scalars(select(BoostReward).where(BoostReward.status == "running")).all()
        for reward in rewards:
            reward.status = "failed"
            reward.error_message = INTERRUPTED_MESSAGE
            changed = True

        if changed:
            db.commit()
    finally:
        db.close()

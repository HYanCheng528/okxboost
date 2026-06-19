from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import init_db
from .routers.tasks import router as tasks_router
from .routers.address_book import router as address_book_router
from .routers.detect import router as detect_router
from .routers.stats import router as stats_router
from .routers.rewards import router as rewards_router
from .routers.copy_sell import router as copy_sell_router
from .services.scheduler import start_scheduler, stop_scheduler
from .services.startup_recovery import recover_interrupted_jobs


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    recover_interrupted_jobs()
    if os.getenv("DISABLE_SCHEDULER", "").strip().lower() not in {"1", "true", "yes"}:
        start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(
    title="On-chain Volume Stats Bot",
    description="PRD MVP: cycle matching, summary metrics, gas stats, task persistence.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(tasks_router)
app.include_router(address_book_router)
app.include_router(detect_router)
app.include_router(stats_router)
app.include_router(rewards_router)
app.include_router(copy_sell_router)

static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")

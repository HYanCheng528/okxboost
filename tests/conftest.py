from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DB = PROJECT_ROOT / "test_okx_volume_stats.db"
if TEST_DB.exists():
    TEST_DB.unlink()

os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
os.environ["TX_SOURCE"] = "mock"
os.environ["MOCK_TX_FILE"] = str(PROJECT_ROOT / "data" / "sample_transactions.json")
os.environ["DISABLE_SCHEDULER"] = "true"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.database import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AirdropClaimContract, AppCache, BoostReward, CopySellAttempt, CopySellSeedBuy, CopySellTask, CopySellWalletResult, Cycle, ParsedTransaction, Price, RobotWallet, SavedToken, SavedWallet, Task, TaskFolder, TaskScanRange, TxCache, WalletProfitAdjustment  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    init_db()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_tables() -> None:
    db = SessionLocal()
    try:
        db.execute(delete(Cycle))
        db.execute(delete(ParsedTransaction))
        db.execute(delete(TaskScanRange))
        db.execute(delete(Task))
        db.execute(delete(TaskFolder))
        db.execute(delete(Price))
        db.execute(delete(TxCache))
        db.execute(delete(AirdropClaimContract))
        db.execute(delete(BoostReward))
        db.execute(delete(CopySellWalletResult))
        db.execute(delete(CopySellAttempt))
        db.execute(delete(CopySellSeedBuy))
        db.execute(delete(CopySellTask))
        db.execute(delete(SavedToken))
        db.execute(delete(SavedWallet))
        db.execute(delete(RobotWallet))
        db.execute(delete(WalletProfitAdjustment))
        db.execute(delete(AppCache))
        db.commit()
    finally:
        db.close()

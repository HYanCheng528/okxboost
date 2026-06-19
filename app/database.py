from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _build_engine() -> Engine:
    settings = get_settings()
    connect_args: dict[str, object] = {}
    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(settings.database_url, future=True, connect_args=connect_args)


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


def _apply_schema_migrations() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    alterations: list[str] = []

    if "tasks" in table_names:
        existing_columns = {column["name"] for column in inspector.get_columns("tasks")}
        if "task_name" not in existing_columns:
            alterations.append("ALTER TABLE tasks ADD COLUMN task_name VARCHAR(128)")
        if "time_ranges_json" not in existing_columns:
            alterations.append("ALTER TABLE tasks ADD COLUMN time_ranges_json TEXT")
        if "folder_id" not in existing_columns:
            alterations.append("ALTER TABLE tasks ADD COLUMN folder_id VARCHAR(32)")

    if "saved_wallets" in table_names:
        existing_columns = {column["name"] for column in inspector.get_columns("saved_wallets")}
        if "solana_address" not in existing_columns:
            alterations.append("ALTER TABLE saved_wallets ADD COLUMN solana_address VARCHAR(64)")
        if "feishu_trade_table_id" not in existing_columns:
            alterations.append("ALTER TABLE saved_wallets ADD COLUMN feishu_trade_table_id VARCHAR(64)")
        if "feishu_airdrop_table_id" not in existing_columns:
            alterations.append("ALTER TABLE saved_wallets ADD COLUMN feishu_airdrop_table_id VARCHAR(64)")
        if "robot_wallet_id" not in existing_columns:
            alterations.append("ALTER TABLE saved_wallets ADD COLUMN robot_wallet_id VARCHAR(64)")

    if "copy_sell_tasks" in table_names:
        existing_columns = {column["name"] for column in inspector.get_columns("copy_sell_tasks")}
        if "last_checked_at" not in existing_columns:
            alterations.append("ALTER TABLE copy_sell_tasks ADD COLUMN last_checked_at DATETIME")
        if "trigger_baseline_raw" not in existing_columns:
            alterations.append("ALTER TABLE copy_sell_tasks ADD COLUMN trigger_baseline_raw TEXT DEFAULT '0'")
        if "route_preference" not in existing_columns:
            alterations.append("ALTER TABLE copy_sell_tasks ADD COLUMN route_preference VARCHAR(16) DEFAULT 'best'")
        if "allow_zero_min_output" not in existing_columns:
            alterations.append("ALTER TABLE copy_sell_tasks ADD COLUMN allow_zero_min_output BOOLEAN DEFAULT 0")

    if "copy_sell_attempts" in table_names:
        existing_columns = {column["name"] for column in inspector.get_columns("copy_sell_attempts")}
        if "target_balance_after_raw" not in existing_columns:
            alterations.append("ALTER TABLE copy_sell_attempts ADD COLUMN target_balance_after_raw TEXT")

    if "copy_sell_seed_buys" in table_names:
        existing_columns = {column["name"] for column in inspector.get_columns("copy_sell_seed_buys")}
        if "target_amount_raw" not in existing_columns:
            alterations.append("ALTER TABLE copy_sell_seed_buys ADD COLUMN target_amount_raw TEXT")

    if "boost_rewards" in table_names:
        existing_columns = {column["name"] for column in inspector.get_columns("boost_rewards")}
        if "status" not in existing_columns:
            alterations.append("ALTER TABLE boost_rewards ADD COLUMN status VARCHAR(16) DEFAULT 'completed'")
        if "error_message" not in existing_columns:
            alterations.append("ALTER TABLE boost_rewards ADD COLUMN error_message TEXT")
        if "updated_at" not in existing_columns:
            alterations.append("ALTER TABLE boost_rewards ADD COLUMN updated_at DATETIME")

    if not alterations:
        return

    with engine.begin() as conn:
        for ddl in alterations:
            conn.execute(text(ddl))


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _apply_schema_migrations()


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_name: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    folder_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("task_folders.id"), nullable=True, index=True)
    chain: Mapped[str] = mapped_column(String(32), index=True)
    wallets_json: Mapped[str] = mapped_column(Text)
    token: Mapped[str] = mapped_column(String(128), index=True)
    base_token: Mapped[str] = mapped_column(String(16))
    time_ranges_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    boost_multiplier: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("1"))
    epsilon: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0.0001"))
    pair_timeout_minutes: Mapped[int] = mapped_column(Integer, default=30)
    actual_boost_volume: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    sum_total_volume: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    computed_boost_volume: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    boost_diff: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)
    sum_gas_native: Mapped[Decimal] = mapped_column(Numeric(30, 18), default=Decimal("0"))
    sum_gas_usd: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)
    sum_wear: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    avg_fee_rate: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    cycle_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, index=True
    )

    folder: Mapped["TaskFolder | None"] = relationship("TaskFolder", back_populates="tasks")
    cycles: Mapped[list["Cycle"]] = relationship("Cycle", back_populates="task", cascade="all, delete-orphan")


class TaskFolder(Base):
    __tablename__ = "task_folders"
    __table_args__ = (UniqueConstraint("name", name="uq_task_folder_name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, index=True
    )

    tasks: Mapped[list[Task]] = relationship("Task", back_populates="folder")


class Cycle(Base):
    __tablename__ = "cycles"
    __table_args__ = (UniqueConstraint("task_id", "cycle_index", name="uq_task_cycle_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("tasks.id"), index=True)
    wallet: Mapped[str] = mapped_column(String(128), index=True)
    cycle_index: Mapped[int] = mapped_column(Integer, index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    trade_before_usd: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    trade_after_usd: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    trade_volume_usd: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    wear_usd: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    fee_rate: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    gas_native_total: Mapped[Decimal] = mapped_column(Numeric(30, 18), default=Decimal("0"))
    gas_usd_total: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)
    tx_hashes_json: Mapped[str] = mapped_column(Text)
    incomplete: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    task: Mapped[Task] = relationship("Task", back_populates="cycles")


class TxCache(Base):
    __tablename__ = "tx_cache"
    __table_args__ = (UniqueConstraint("chain", "wallet", "tx_hash", name="uq_tx_cache"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain: Mapped[str] = mapped_column(String(32), index=True)
    wallet: Mapped[str] = mapped_column(String(128), index=True)
    tx_hash: Mapped[str] = mapped_column(String(80), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    usdt_out: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    usdt_in: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    token_in: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    token_out: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    gas_native: Mapped[Decimal] = mapped_column(Numeric(30, 18), default=Decimal("0"))
    gas_usd: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SavedWallet(Base):
    __tablename__ = "saved_wallets"
    __table_args__ = (UniqueConstraint("address", name="uq_saved_wallet_address"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    label: Mapped[str] = mapped_column(String(128), index=True)
    address: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SavedToken(Base):
    __tablename__ = "saved_tokens"
    __table_args__ = (UniqueConstraint("chain", "address", name="uq_saved_token_chain_address"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), index=True)
    address: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decimals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Price(Base):
    __tablename__ = "prices"
    __table_args__ = (UniqueConstraint("asset_symbol", "bucket_ts", name="uq_price_bucket"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_symbol: Mapped[str] = mapped_column(String(16), index=True)
    bucket_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    price_usd: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

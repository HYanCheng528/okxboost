from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
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
    parsed_transactions: Mapped[list["ParsedTransaction"]] = relationship(
        "ParsedTransaction", back_populates="task", cascade="all, delete-orphan"
    )
    scan_ranges: Mapped[list["TaskScanRange"]] = relationship(
        "TaskScanRange", back_populates="task", cascade="all, delete-orphan"
    )


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


class ParsedTransaction(Base):
    __tablename__ = "parsed_transactions"
    __table_args__ = (
        UniqueConstraint("task_id", "chain", "wallet", "token", "tx_hash", name="uq_parsed_tx_task_token_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("tasks.id"), index=True)
    chain: Mapped[str] = mapped_column(String(32), index=True)
    wallet: Mapped[str] = mapped_column(String(128), index=True)
    token: Mapped[str] = mapped_column(String(128), index=True)
    tx_hash: Mapped[str] = mapped_column(String(80), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    usdt_out: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    usdt_in: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    token_in: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    token_out: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    gas_native: Mapped[Decimal] = mapped_column(Numeric(30, 18), default=Decimal("0"))
    gas_usd: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    task: Mapped[Task] = relationship("Task", back_populates="parsed_transactions")


class TaskScanRange(Base):
    __tablename__ = "task_scan_ranges"
    __table_args__ = (UniqueConstraint("task_id", "start_time", "end_time", name="uq_task_scan_range"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("tasks.id"), index=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    task: Mapped[Task] = relationship("Task", back_populates="scan_ranges")


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
    solana_address: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    feishu_trade_table_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    feishu_airdrop_table_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    robot_wallet_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("robot_wallets.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    robot_wallet: Mapped["RobotWallet | None"] = relationship("RobotWallet")


class RobotWallet(Base):
    __tablename__ = "robot_wallets"
    __table_args__ = (UniqueConstraint("address", name="uq_robot_wallet_address"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(128), index=True)
    address: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class CopySellTask(Base):
    __tablename__ = "copy_sell_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    chain: Mapped[str] = mapped_column(String(32), index=True)
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    output_token_address: Mapped[str] = mapped_column(String(128), index=True)
    trigger_baseline_raw: Mapped[str] = mapped_column(Text, default="0")
    route_preference: Mapped[str] = mapped_column(String(16), default="best")
    allow_zero_min_output: Mapped[bool] = mapped_column(Boolean, default=False)
    poll_interval_seconds: Mapped[float] = mapped_column(Float, default=0.5)
    slippage_bps: Mapped[int] = mapped_column(Integer, default=1000)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(16), default="paused", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    attempts: Mapped[list["CopySellAttempt"]] = relationship(
        "CopySellAttempt", back_populates="task", cascade="all, delete-orphan"
    )
    seed_buys: Mapped[list["CopySellSeedBuy"]] = relationship(
        "CopySellSeedBuy", back_populates="task", cascade="all, delete-orphan"
    )


class CopySellAttempt(Base):
    __tablename__ = "copy_sell_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("copy_sell_tasks.id"), index=True)
    robot_wallet_id: Mapped[str] = mapped_column(String(64), ForeignKey("robot_wallets.id"), index=True)
    wallet_address: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    balance_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_amount_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    quoted_output_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_output_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_amount_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_balance_after_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_tx_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    swap_tx_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    route_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    task: Mapped[CopySellTask] = relationship("CopySellTask", back_populates="attempts")
    robot_wallet: Mapped[RobotWallet] = relationship("RobotWallet")
    wallet_results: Mapped[list["CopySellWalletResult"]] = relationship(
        "CopySellWalletResult", back_populates="attempt", cascade="all, delete-orphan"
    )


class CopySellWalletResult(Base):
    __tablename__ = "copy_sell_wallet_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(Integer, ForeignKey("copy_sell_attempts.id"), index=True)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("copy_sell_tasks.id"), index=True)
    robot_wallet_id: Mapped[str] = mapped_column(String(64), ForeignKey("robot_wallets.id"), index=True)
    wallet_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("saved_wallets.id"), nullable=True, index=True)
    wallet_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    wallet_address: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    target_balance_before_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_balance_after_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_balance_before_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_balance_after_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_amount_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    attempt: Mapped[CopySellAttempt] = relationship("CopySellAttempt", back_populates="wallet_results")
    robot_wallet: Mapped[RobotWallet] = relationship("RobotWallet")
    saved_wallet: Mapped["SavedWallet | None"] = relationship("SavedWallet")


class CopySellSeedBuy(Base):
    __tablename__ = "copy_sell_seed_buys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("copy_sell_tasks.id"), index=True)
    robot_wallet_id: Mapped[str] = mapped_column(String(64), ForeignKey("robot_wallets.id"), index=True)
    wallet_address: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    spend_token_address: Mapped[str] = mapped_column(String(128), index=True)
    target_token_address: Mapped[str] = mapped_column(String(128), index=True)
    spend_amount_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    quoted_output_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_output_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_balance_before_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_balance_after_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_amount_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_tx_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    swap_tx_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    route_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    task: Mapped[CopySellTask] = relationship("CopySellTask", back_populates="seed_buys")
    robot_wallet: Mapped[RobotWallet] = relationship("RobotWallet")


class WalletProfitAdjustment(Base):
    __tablename__ = "wallet_profit_adjustments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    wallet_key: Mapped[str] = mapped_column(String(32), index=True)
    month: Mapped[str] = mapped_column(String(7), index=True)
    loss_adjustment: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    rebate_adjustment: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    income_adjustment: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class AppCache(Base):
    __tablename__ = "app_caches"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, index=True
    )


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


class BoostReward(Base):
    __tablename__ = "boost_rewards"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    period: Mapped[int] = mapped_column(Integer, index=True)
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    token_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    chain: Mapped[str] = mapped_column(String(32), index=True)
    scan_date: Mapped[str] = mapped_column(String(10), index=True)
    wallets_json: Mapped[str] = mapped_column(Text)
    total_claimed: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    total_sold_usdt: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(16), default="completed")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class AirdropClaimContract(Base):
    __tablename__ = "airdrop_claim_contracts"
    __table_args__ = (
        UniqueConstraint(
            "chain",
            "token_address",
            "contract_address",
            "function_selector",
            name="uq_airdrop_claim_contract",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain: Mapped[str] = mapped_column(String(32), index=True)
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    contract_address: Mapped[str] = mapped_column(String(128), index=True)
    function_selector: Mapped[str] = mapped_column(String(16), default="")
    code_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="candidate", index=True)
    first_seen_tx: Mapped[str | None] = mapped_column(String(80), nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

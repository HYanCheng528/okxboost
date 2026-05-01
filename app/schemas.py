from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, from_attributes=True)


class TaskTimeRange(ApiModel):
    start_time: datetime = Field(alias="startTime")
    end_time: datetime = Field(alias="endTime")


class TaskCreateRequest(ApiModel):
    task_name: str | None = Field(default=None, alias="taskName")
    folder_id: str | None = Field(default=None, alias="folderId")
    chain: str
    wallets: list[str]
    token: str
    base_token: str = Field(alias="baseToken")
    start_time: datetime | None = Field(default=None, alias="startTime")
    end_time: datetime | None = Field(default=None, alias="endTime")
    time_ranges: list[TaskTimeRange] | None = Field(default=None, alias="timeRanges")
    boost_multiplier: Decimal = Field(alias="boostMultiplier")
    epsilon: Decimal = Decimal("0.0001")
    pair_timeout_minutes: int = Field(default=30, alias="pairTimeoutMinutes")
    actual_boost_volume: Decimal | None = Field(default=None, alias="actualBoostVolume")


class TaskCreateResponse(ApiModel):
    task_id: str = Field(alias="taskId")
    status: str
    progress_percent: int = Field(alias="progressPercent")
    progress_stage: str = Field(alias="progressStage")
    progress_message: str | None = Field(alias="progressMessage")


class TaskActionResponse(ApiModel):
    task_id: str = Field(alias="taskId")
    status: str
    progress_percent: int = Field(alias="progressPercent")
    progress_stage: str = Field(alias="progressStage")
    progress_message: str | None = Field(alias="progressMessage")


class TaskPatchRequest(ApiModel):
    actual_boost_volume: Decimal | None = Field(default=None, alias="actualBoostVolume")


class TaskFolderAssignRequest(ApiModel):
    folder_id: str | None = Field(default=None, alias="folderId")


class TaskAppendRangesRequest(ApiModel):
    start_time: datetime | None = Field(default=None, alias="startTime")
    end_time: datetime | None = Field(default=None, alias="endTime")
    time_ranges: list[TaskTimeRange] | None = Field(default=None, alias="timeRanges")


class TaskSyncFeishuRequest(ApiModel):
    table_id: str = Field(alias="tableId")
    wallet: str | None = None
    date_field: str = Field(default="日期", alias="dateField")
    trade_before_field: str = Field(default="交易前", alias="tradeBeforeField")
    trade_after_field: str = Field(default="交易后", alias="tradeAfterField")
    gas_usd_field: str = Field(default="gas费", alias="gasUsdField")


class TaskSyncFeishuResponse(ApiModel):
    task_id: str = Field(alias="taskId")
    table_id: str = Field(alias="tableId")
    wallet: str | None = None
    appended_count: int = Field(alias="appendedCount")


class FeishuTableItem(ApiModel):
    table_id: str = Field(alias="tableId")
    name: str


class SummaryResponse(ApiModel):
    sum_total_volume: Decimal = Field(alias="sumTotalVolume")
    computed_boost_volume: Decimal = Field(alias="computedBoostVolume")
    actual_boost_volume: Decimal | None = Field(alias="actualBoostVolume")
    boost_diff: Decimal | None = Field(alias="boostDiff")
    sum_gas_native: Decimal = Field(alias="sumGasNative")
    sum_gas_usd: Decimal | None = Field(alias="sumGasUsd")
    sum_wear: Decimal = Field(alias="sumWear")
    avg_fee_rate: Decimal = Field(alias="avgFeeRate")
    cycle_count: int = Field(alias="cycleCount")


class RangeSummaryResponse(ApiModel):
    range_index: int = Field(alias="rangeIndex")
    start_time: datetime = Field(alias="startTime")
    end_time: datetime = Field(alias="endTime")
    sum_total_volume: Decimal = Field(alias="sumTotalVolume")
    computed_boost_volume: Decimal = Field(alias="computedBoostVolume")
    sum_gas_native: Decimal = Field(alias="sumGasNative")
    sum_gas_usd: Decimal | None = Field(alias="sumGasUsd")
    sum_wear: Decimal = Field(alias="sumWear")
    avg_fee_rate: Decimal = Field(alias="avgFeeRate")
    cycle_count: int = Field(alias="cycleCount")
    wallet_count: int = Field(alias="walletCount")


class TaskListItem(ApiModel):
    task_id: str = Field(alias="taskId")
    task_name: str | None = Field(alias="taskName")
    folder_id: str | None = Field(alias="folderId")
    folder_name: str | None = Field(alias="folderName")
    chain: str
    wallets: list[str]
    token: str
    base_token: str = Field(alias="baseToken")
    start_time: datetime = Field(alias="startTime")
    end_time: datetime = Field(alias="endTime")
    time_range_count: int = Field(alias="timeRangeCount")
    boost_multiplier: Decimal = Field(alias="boostMultiplier")
    status: str
    progress_percent: int = Field(alias="progressPercent")
    progress_stage: str = Field(alias="progressStage")
    progress_message: str | None = Field(alias="progressMessage")
    created_at: datetime = Field(alias="createdAt")
    summary: SummaryResponse


class TaskDetailResponse(ApiModel):
    task_id: str = Field(alias="taskId")
    task_name: str | None = Field(alias="taskName")
    folder_id: str | None = Field(alias="folderId")
    folder_name: str | None = Field(alias="folderName")
    chain: str
    wallets: list[str]
    token: str
    base_token: str = Field(alias="baseToken")
    start_time: datetime = Field(alias="startTime")
    end_time: datetime = Field(alias="endTime")
    time_ranges: list[TaskTimeRange] = Field(alias="timeRanges")
    boost_multiplier: Decimal = Field(alias="boostMultiplier")
    epsilon: Decimal
    pair_timeout_minutes: int = Field(alias="pairTimeoutMinutes")
    actual_boost_volume: Decimal | None = Field(alias="actualBoostVolume")
    status: str
    progress_percent: int = Field(alias="progressPercent")
    progress_stage: str = Field(alias="progressStage")
    progress_message: str | None = Field(alias="progressMessage")
    error_message: str | None = Field(alias="errorMessage")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    summary: SummaryResponse
    range_summaries: list[RangeSummaryResponse] = Field(alias="rangeSummaries")


class CycleItem(ApiModel):
    cycle_index: int = Field(alias="cycleIndex")
    wallet: str
    start_at: datetime = Field(alias="startAt")
    end_at: datetime = Field(alias="endAt")
    trade_before_usd: Decimal = Field(alias="tradeBeforeUsd")
    trade_after_usd: Decimal = Field(alias="tradeAfterUsd")
    trade_volume_usd: Decimal = Field(alias="tradeVolumeUsd")
    wear_usd: Decimal = Field(alias="wearUsd")
    fee_rate: Decimal = Field(alias="feeRate")
    gas_native_total: Decimal = Field(alias="gasNativeTotal")
    gas_usd_total: Decimal | None = Field(alias="gasUsdTotal")
    tx_hashes: list[str] = Field(alias="txHashes")
    incomplete: bool


class CycleListResponse(ApiModel):
    page: int
    page_size: int = Field(alias="pageSize")
    total: int
    items: list[CycleItem]


class TaskFolderCreateRequest(ApiModel):
    name: str


class TaskFolderResponse(ApiModel):
    folder_id: str = Field(alias="folderId")
    name: str
    created_at: datetime = Field(alias="createdAt")

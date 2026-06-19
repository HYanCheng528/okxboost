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
    token: str | None = None
    tokens: list[str] | None = None
    base_token: str = Field(alias="baseToken")
    start_time: datetime | None = Field(default=None, alias="startTime")
    end_time: datetime | None = Field(default=None, alias="endTime")
    time_ranges: list[TaskTimeRange] | None = Field(default=None, alias="timeRanges")
    boost_multiplier: Decimal = Field(alias="boostMultiplier")
    epsilon: Decimal = Decimal("0.0001")
    pair_timeout_minutes: int = Field(default=30, alias="pairTimeoutMinutes")
    actual_boost_volume: Decimal | None = Field(default=None, alias="actualBoostVolume")

    def normalized_tokens(self) -> list[str]:
        raw_tokens: list[str] = []
        if self.token:
            raw_tokens.extend(self.token.split(","))
        if self.tokens:
            raw_tokens.extend(self.tokens)

        normalized: list[str] = []
        seen: set[str] = set()
        for token in raw_tokens:
            value = token.strip().lower()
            if not value or value in seen:
                continue
            normalized.append(value)
            seen.add(value)
        return normalized


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
    table_id: str | None = Field(default=None, alias="tableId")
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


class SavedWalletCreateRequest(ApiModel):
    label: str
    address: str
    solana_address: str | None = Field(default=None, alias="solanaAddress")
    feishu_trade_table_id: str | None = Field(default=None, alias="feishuTradeTableId")
    feishu_airdrop_table_id: str | None = Field(default=None, alias="feishuAirdropTableId")


class SavedWalletUpdateRequest(ApiModel):
    label: str | None = None
    solana_address: str | None = Field(default=None, alias="solanaAddress")
    feishu_trade_table_id: str | None = Field(default=None, alias="feishuTradeTableId")
    feishu_airdrop_table_id: str | None = Field(default=None, alias="feishuAirdropTableId")


class SavedWalletResponse(ApiModel):
    wallet_id: str = Field(alias="walletId")
    label: str
    address: str
    solana_address: str | None = Field(default=None, alias="solanaAddress")
    feishu_trade_table_id: str | None = Field(default=None, alias="feishuTradeTableId")
    feishu_airdrop_table_id: str | None = Field(default=None, alias="feishuAirdropTableId")
    robot_wallet_id: str | None = Field(default=None, alias="robotWalletId")
    robot_wallet_address: str | None = Field(default=None, alias="robotWalletAddress")
    robot_wallet_label: str | None = Field(default=None, alias="robotWalletLabel")
    created_at: datetime = Field(alias="createdAt")


class SavedWalletRobotUpdateRequest(ApiModel):
    robot_wallet_id: str | None = Field(default=None, alias="robotWalletId")


class RobotWalletResponse(ApiModel):
    robot_wallet_id: str = Field(alias="robotWalletId")
    label: str
    address: str
    bound_wallet_count: int = Field(alias="boundWalletCount")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")


class RobotWalletRefreshResponse(ApiModel):
    imported: int
    updated: int
    removed: int
    wallets: list[RobotWalletResponse]


class CopySellTaskCreateRequest(ApiModel):
    name: str | None = None
    chain: str
    token_address: str = Field(alias="tokenAddress")
    output_token_address: str = Field(alias="outputTokenAddress")
    trigger_baseline: Decimal = Field(default=Decimal("0"), alias="triggerBaseline", ge=Decimal("0"))
    route_preference: str = Field(default="best", alias="routePreference")
    allow_zero_min_output: bool = Field(default=False, alias="allowZeroMinOutput")
    poll_interval_seconds: float = Field(default=0.5, alias="pollIntervalSeconds", ge=0.5)
    slippage_bps: int = Field(default=1000, alias="slippageBps", ge=1, le=5000)
    max_retries: int = Field(default=3, alias="maxRetries", ge=0, le=10)


class CopySellSeedBuyRequest(ApiModel):
    spend_amount: Decimal = Field(alias="spendAmount", gt=Decimal("0"))
    slippage_bps: int | None = Field(default=None, alias="slippageBps", ge=1, le=5000)


class CopySellRouteScanRequest(ApiModel):
    side: str = "sell"
    amount: Decimal = Field(gt=Decimal("0"))
    route_preference: str = Field(default="best", alias="routePreference")


class CopySellQuoteResponse(ApiModel):
    robot_wallet_id: str = Field(alias="robotWalletId")
    wallet_address: str = Field(alias="walletAddress")
    balance_raw: str = Field(alias="balanceRaw")
    quoted_output_raw: str | None = Field(default=None, alias="quotedOutputRaw")
    min_output_raw: str | None = Field(default=None, alias="minOutputRaw")
    route: dict | None = None
    error_message: str | None = Field(default=None, alias="errorMessage")


class CopySellRouteScanResponse(ApiModel):
    dex_name: str | None = Field(default=None, alias="dexName")
    protocol: str
    router: str
    quoter: str | None = None
    factory: str | None = None
    pools: list[str] = Field(default_factory=list)
    path: list[str]
    fees: list[int] = Field(default_factory=list)
    amount_in_raw: str = Field(alias="amountInRaw")
    amount_out_raw: str = Field(alias="amountOutRaw")
    min_output_raw: str | None = Field(default=None, alias="minOutputRaw")


class CopySellSeedBuyResponse(ApiModel):
    seed_buy_id: int = Field(alias="seedBuyId")
    task_id: str = Field(alias="taskId")
    robot_wallet_id: str = Field(alias="robotWalletId")
    wallet_address: str = Field(alias="walletAddress")
    status: str
    spend_token_address: str = Field(alias="spendTokenAddress")
    target_token_address: str = Field(alias="targetTokenAddress")
    spend_amount_raw: str | None = Field(default=None, alias="spendAmountRaw")
    quoted_output_raw: str | None = Field(default=None, alias="quotedOutputRaw")
    min_output_raw: str | None = Field(default=None, alias="minOutputRaw")
    target_balance_before_raw: str | None = Field(default=None, alias="targetBalanceBeforeRaw")
    target_balance_after_raw: str | None = Field(default=None, alias="targetBalanceAfterRaw")
    target_amount_raw: str | None = Field(default=None, alias="targetAmountRaw")
    approval_tx_hash: str | None = Field(default=None, alias="approvalTxHash")
    swap_tx_hash: str | None = Field(default=None, alias="swapTxHash")
    route: dict | None = None
    error_message: str | None = Field(default=None, alias="errorMessage")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")


class CopySellWalletResultResponse(ApiModel):
    result_id: int = Field(alias="resultId")
    attempt_id: int = Field(alias="attemptId")
    task_id: str = Field(alias="taskId")
    robot_wallet_id: str = Field(alias="robotWalletId")
    wallet_id: str | None = Field(default=None, alias="walletId")
    wallet_label: str | None = Field(default=None, alias="walletLabel")
    wallet_address: str = Field(alias="walletAddress")
    status: str
    target_balance_before_raw: str | None = Field(default=None, alias="targetBalanceBeforeRaw")
    target_balance_after_raw: str | None = Field(default=None, alias="targetBalanceAfterRaw")
    output_balance_before_raw: str | None = Field(default=None, alias="outputBalanceBeforeRaw")
    output_balance_after_raw: str | None = Field(default=None, alias="outputBalanceAfterRaw")
    output_amount_raw: str | None = Field(default=None, alias="outputAmountRaw")
    sell_succeeded: bool = Field(alias="sellSucceeded")
    error_message: str | None = Field(default=None, alias="errorMessage")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")


class CopySellAttemptResponse(ApiModel):
    attempt_id: int = Field(alias="attemptId")
    task_id: str = Field(alias="taskId")
    robot_wallet_id: str = Field(alias="robotWalletId")
    wallet_address: str = Field(alias="walletAddress")
    status: str
    balance_raw: str | None = Field(default=None, alias="balanceRaw")
    input_amount_raw: str | None = Field(default=None, alias="inputAmountRaw")
    quoted_output_raw: str | None = Field(default=None, alias="quotedOutputRaw")
    min_output_raw: str | None = Field(default=None, alias="minOutputRaw")
    output_amount_raw: str | None = Field(default=None, alias="outputAmountRaw")
    target_balance_after_raw: str | None = Field(default=None, alias="targetBalanceAfterRaw")
    sell_succeeded: bool = Field(alias="sellSucceeded")
    approval_tx_hash: str | None = Field(default=None, alias="approvalTxHash")
    swap_tx_hash: str | None = Field(default=None, alias="swapTxHash")
    route: dict | None = None
    retry_count: int = Field(alias="retryCount")
    error_message: str | None = Field(default=None, alias="errorMessage")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    participant_results: list[CopySellWalletResultResponse] = Field(default_factory=list, alias="participantResults")


class CopySellTaskResponse(ApiModel):
    task_id: str = Field(alias="taskId")
    name: str | None = None
    chain: str
    token_address: str = Field(alias="tokenAddress")
    output_token_address: str = Field(alias="outputTokenAddress")
    trigger_baseline_raw: str = Field(alias="triggerBaselineRaw")
    route_preference: str = Field(alias="routePreference")
    allow_zero_min_output: bool = Field(alias="allowZeroMinOutput")
    poll_interval_seconds: float = Field(alias="pollIntervalSeconds")
    slippage_bps: int = Field(alias="slippageBps")
    max_retries: int = Field(alias="maxRetries")
    status: str
    sell_status: str = Field(alias="sellStatus")
    bound_robot_count: int = Field(alias="boundRobotCount")
    sold_robot_count: int = Field(alias="soldRobotCount")
    failed_robot_count: int = Field(alias="failedRobotCount")
    pending_robot_count: int = Field(alias="pendingRobotCount")
    participant_wallet_count: int = Field(alias="participantWalletCount")
    participant_target_count: int = Field(alias="participantTargetCount")
    participant_sold_count: int = Field(alias="participantSoldCount")
    participant_failed_count: int = Field(alias="participantFailedCount")
    participant_pending_count: int = Field(alias="participantPendingCount")
    error_message: str | None = Field(default=None, alias="errorMessage")
    last_checked_at: datetime | None = Field(default=None, alias="lastCheckedAt")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    attempts: list[CopySellAttemptResponse] = Field(default_factory=list)
    seed_buys: list[CopySellSeedBuyResponse] = Field(default_factory=list, alias="seedBuys")


class WalletProfitAdjustmentCreateRequest(ApiModel):
    wallet_key: str = Field(alias="walletKey")
    month: str
    loss_adjustment: Decimal = Field(default=Decimal("0"), alias="lossAdjustment")
    rebate_adjustment: Decimal = Field(default=Decimal("0"), alias="rebateAdjustment")
    income_adjustment: Decimal = Field(default=Decimal("0"), alias="incomeAdjustment")
    note: str | None = None


class WalletProfitAdjustmentResponse(ApiModel):
    adjustment_id: str = Field(alias="adjustmentId")
    wallet_key: str = Field(alias="walletKey")
    month: str
    loss_adjustment: Decimal = Field(alias="lossAdjustment")
    rebate_adjustment: Decimal = Field(alias="rebateAdjustment")
    income_adjustment: Decimal = Field(alias="incomeAdjustment")
    note: str | None = None
    created_at: datetime = Field(alias="createdAt")


class DashboardSettingsRequest(ApiModel):
    min_daily_average: Decimal = Field(default=Decimal("0"), alias="minDailyAverage", ge=Decimal("0"))


class DashboardSettingsResponse(ApiModel):
    min_daily_average: Decimal = Field(alias="minDailyAverage")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")


class SavedTokenCreateRequest(ApiModel):
    chain: str
    address: str


class SavedTokenResponse(ApiModel):
    token_id: str = Field(alias="tokenId")
    chain: str
    address: str
    name: str | None = None
    symbol: str | None = None
    decimals: int | None = None
    created_at: datetime = Field(alias="createdAt")


class DetectSessionsRequest(ApiModel):
    target_date: str | None = Field(default=None, alias="targetDate")  # Format: "YYYY-MM-DD" (UTC), defaults to today
    wallet_address: str | None = Field(default=None, alias="walletAddress")  # Filter by specific wallet, None = all wallets
    token_address: str | None = Field(default=None, alias="tokenAddress")  # Use specific token for time range detection, None = all tokens
    scan_after: str | None = Field(default=None, alias="scanAfter")  # ISO datetime: only scan transactions after this time
    chain: str | None = Field(default=None)  # Filter tokens by chain, None = all chains
    boost_multiplier: Decimal | None = Field(default=None, alias="boostMultiplier")
    base_token: str | None = Field(default=None, alias="baseToken")


class DetectedSessionToken(ApiModel):
    address: str
    symbol: str | None = None
    name: str | None = None
    chain: str


class DetectedSession(ApiModel):
    wallet: str
    wallet_label: str | None = Field(default=None, alias="walletLabel")
    tokens: list[DetectedSessionToken]
    start_time: datetime = Field(alias="startTime")
    end_time: datetime = Field(alias="endTime")
    tx_count: int = Field(alias="txCount")
    duration_minutes: float = Field(alias="durationMinutes")


class DetectSessionsResponse(ApiModel):
    sessions: list[DetectedSession]
    scanned_pairs: int = Field(alias="scannedPairs")
    total_sessions: int = Field(alias="totalSessions")
    errors: list[str]


class DetectJobCreateResponse(ApiModel):
    job_id: str = Field(alias="jobId")
    status: str
    progress_percent: int = Field(alias="progressPercent")
    progress_message: str = Field(alias="progressMessage")


class DetectJobStatusResponse(ApiModel):
    job_id: str = Field(alias="jobId")
    status: str
    progress_percent: int = Field(alias="progressPercent")
    progress_message: str = Field(alias="progressMessage")
    target_date: str = Field(alias="targetDate")
    scanned_wallets: int = Field(alias="scannedWallets")
    total_wallets: int = Field(alias="totalWallets")
    detected_ranges: int = Field(alias="detectedRanges")
    created: int
    appended: int
    skipped: int
    failed: int
    task_ids: list[str] = Field(alias="taskIds")
    rows: list[str]
    errors: list[str]


# --- Boost Reward Tracking ---

class RewardScanRequest(ApiModel):
    token_address: str = Field(alias="tokenAddress")
    chain: str
    wallet_address: str | None = Field(default=None, alias="walletAddress")
    scan_date: str | None = Field(default=None, alias="scanDate")
    period: int | None = Field(default=None, ge=1)


class RewardUpdateRequest(ApiModel):
    period: int | None = Field(default=None, ge=1)


class RewardClaimContractResponse(ApiModel):
    contract_id: int = Field(alias="contractId")
    chain: str
    token_address: str = Field(alias="tokenAddress")
    contract_address: str = Field(alias="contractAddress")
    function_selector: str = Field(alias="functionSelector")
    code_hash: str | None = Field(default=None, alias="codeHash")
    status: str
    first_seen_tx: str | None = Field(default=None, alias="firstSeenTx")
    hit_count: int = Field(alias="hitCount")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")


class RewardClaimContractUpdateRequest(ApiModel):
    status: str


class RewardWalletResult(ApiModel):
    wallet: str
    wallet_label: str | None = Field(default=None, alias="walletLabel")
    claimed: Decimal
    sold_usdt: Decimal = Field(alias="soldUsdt")


class RewardScanResponse(ApiModel):
    reward_id: str = Field(alias="rewardId")
    period: int
    project_name: str = Field(alias="projectName")
    token_address: str = Field(alias="tokenAddress")
    token_symbol: str | None = Field(default=None, alias="tokenSymbol")
    chain: str
    scan_date: str = Field(alias="scanDate")
    status: str
    error_message: str | None = Field(default=None, alias="errorMessage")
    results: list[RewardWalletResult]
    total_claimed: Decimal = Field(alias="totalClaimed")
    total_sold_usdt: Decimal = Field(alias="totalSoldUsdt")


class RewardListItem(ApiModel):
    reward_id: str = Field(alias="rewardId")
    period: int
    project_name: str = Field(alias="projectName")
    token_address: str = Field(alias="tokenAddress")
    token_symbol: str | None = Field(default=None, alias="tokenSymbol")
    chain: str
    scan_date: str = Field(alias="scanDate")
    status: str
    error_message: str | None = Field(default=None, alias="errorMessage")
    wallets: list[RewardWalletResult]
    total_claimed: Decimal = Field(alias="totalClaimed")
    total_sold_usdt: Decimal = Field(alias="totalSoldUsdt")
    created_at: datetime = Field(alias="createdAt")


class RewardSyncFeishuRequest(ApiModel):
    table_id: str | None = Field(default=None, alias="tableId")
    wallet_address: str | None = Field(default=None, alias="walletAddress")
    period_override: int | None = Field(default=None, alias="periodOverride")
    date_field: str = Field(default="日期", alias="dateField")
    period_field: str = Field(default="期数", alias="periodField")
    project_field: str = Field(default="项目", alias="projectField")
    quantity_field: str = Field(default="数量", alias="quantityField")
    avg_sell_price_field: str = Field(default="", alias="avgSellPriceField")
    boost_claim_field: str = Field(default="Boost领取奖励", alias="boostClaimField")
    include_zero_wallets: bool = Field(default=False, alias="includeZeroWallets")


class RewardSyncFeishuResponse(ApiModel):
    reward_id: str = Field(alias="rewardId")
    table_id: str = Field(alias="tableId")
    wallet_address: str | None = Field(alias="walletAddress")
    appended_count: int = Field(alias="appendedCount")

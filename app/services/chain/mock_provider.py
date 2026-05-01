from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from ...config import get_settings
from .base import ChainProvider, ProgressCallback
from .types import ParsedTx


class MockChainProvider(ChainProvider):
    def fetch_transactions(
        self,
        *,
        chain: str,
        wallets: list[str],
        token: str,
        base_token: str,
        start_time: datetime,
        end_time: datetime,
        db: Session,
        progress_cb: ProgressCallback | None = None,
    ) -> list[ParsedTx]:
        if progress_cb:
            progress_cb(20, "正在加载本地 Mock 数据。")
        settings = get_settings()
        path = settings.mock_tx_file
        if not path.exists():
            if progress_cb:
                progress_cb(65, "未找到 Mock 文件，返回空结果。")
            return []

        wallets_set = {wallet.lower() for wallet in wallets}
        chain_key = chain.lower()
        records = json.loads(path.read_text(encoding="utf-8"))
        parsed: list[ParsedTx] = []
        for row in records:
            row_chain = str(row.get("chain", "")).lower()
            wallet = str(row.get("wallet", "")).lower()
            if row_chain != chain_key or wallet not in wallets_set:
                continue

            timestamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
            if timestamp < start_time or timestamp > end_time:
                continue

            parsed.append(
                ParsedTx(
                    chain=chain_key,
                    wallet=wallet,
                    tx_hash=str(row["txHash"]),
                    timestamp=timestamp,
                    usdt_out=Decimal(str(row.get("usdtOut", "0"))),
                    usdt_in=Decimal(str(row.get("usdtIn", "0"))),
                    token_in=Decimal(str(row.get("tokenIn", "0"))),
                    token_out=Decimal(str(row.get("tokenOut", "0"))),
                    gas_native=Decimal(str(row.get("gasNative", "0"))),
                    gas_usd=(
                        Decimal(str(row["gasUsd"])) if row.get("gasUsd") is not None else None
                    ),
                )
            )
        if progress_cb:
            progress_cb(65, f"Mock 解析完成，共 {len(parsed)} 笔交易。")
        parsed.sort(key=lambda item: (item.wallet, item.timestamp, item.tx_hash))
        return parsed

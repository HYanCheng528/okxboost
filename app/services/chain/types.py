from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(slots=True)
class ParsedTx:
    chain: str
    wallet: str
    tx_hash: str
    timestamp: datetime
    usdt_out: Decimal
    usdt_in: Decimal
    token_in: Decimal
    token_out: Decimal
    gas_native: Decimal
    gas_usd: Decimal | None

    @property
    def dedupe_key(self) -> tuple[str, str, str]:
        return (self.chain.lower(), self.wallet.lower(), self.tx_hash.lower())

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session

from .types import ParsedTx

ProgressCallback = Callable[[int, str], None]


class ChainProvider(ABC):
    @abstractmethod
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
        raise NotImplementedError

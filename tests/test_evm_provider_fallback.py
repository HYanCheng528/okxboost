from __future__ import annotations

from datetime import datetime, timezone

from app.config import ChainConfig
from app.services.chain.evm_provider import EvmExplorerProvider, TxCandidate


def test_explorer_failure_falls_back_to_rpc_logs(monkeypatch) -> None:
    provider = EvmExplorerProvider()
    config = ChainConfig(
        name="bsc",
        native_symbol="BNB",
        chain_id=56,
        rpc_url="https://example-rpc",
        rpc_urls=["https://example-rpc"],
        explorer_api_url="https://example-explorer",
        explorer_api_key="demo",
        base_tokens={"USDT": "0x1"},
    )

    def _raise(*args, **kwargs):
        raise RuntimeError("explorer blocked")

    def _rpc_candidates(*args, **kwargs):
        return [
            TxCandidate(
                tx_hash="0xabc",
                timestamp=datetime(2026, 2, 9, 0, 0, tzinfo=timezone.utc),
            )
        ]

    monkeypatch.setattr(provider, "_fetch_wallet_tx_candidates_from_explorer", _raise)
    monkeypatch.setattr(provider, "_fetch_wallet_tx_candidates_from_rpc_logs", _rpc_candidates)

    candidates = provider._fetch_wallet_tx_candidates(
        config=config,
        wallet="0x1111111111111111111111111111111111111111",
        token_address="0xtoken",
        base_address="0xbase",
        start_time=datetime(2026, 2, 9, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 2, 9, 1, 0, tzinfo=timezone.utc),
    )
    assert len(candidates) == 1
    assert candidates[0].tx_hash == "0xabc"

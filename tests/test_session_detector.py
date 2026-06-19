from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.services import session_detector
from app.services.session_detector import _group_wallet_sessions, _transfer_timestamp


def test_group_wallet_sessions_splits_by_gap() -> None:
    transfers = [
        (100, {"id": "a"}),
        (200, {"id": "b"}),
        (2100, {"id": "c"}),
        (2200, {"id": "d"}),
    ]

    sessions = _group_wallet_sessions(transfers, gap_minutes=30)

    assert [[item["id"] for item in session] for session in sessions] == [
        ["a", "b"],
        ["c", "d"],
    ]


def test_transfer_timestamp_accepts_numeric_and_iso_values() -> None:
    assert _transfer_timestamp({"timestamp": 100}) == 100
    assert _transfer_timestamp({"timestamp": "100"}) == 100
    assert _transfer_timestamp({"timestamp": "1970-01-01T00:01:40Z"}) == 100
    assert _transfer_timestamp({"timestamp": "not-a-time"}) is None


def test_detect_sessions_uses_utc_day_boundaries(monkeypatch) -> None:
    transfer_ts = int(datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc).timestamp())
    calls: list[tuple[int, int]] = []

    fake_settings = SimpleNamespace(
        detect_session_gap_minutes=30,
        detect_session_padding_seconds=60,
        chain_configs={
            "bsc": SimpleNamespace(
                rpc_urls=["https://rpc.ankr.com/bsc/test_ankr_key_123456"],
                rpc_url=None,
            )
        },
    )

    def fake_get_settings():  # type: ignore[no-untyped-def]
        return fake_settings

    def fake_fetch(**kwargs):  # type: ignore[no-untyped-def]
        calls.append((kwargs["start_ts"], kwargs["end_ts"]))
        return [
            {
                "timestamp": transfer_ts,
                "transactionHash": "0xtx",
                "contractAddress": "0xtoken",
            }
        ]

    monkeypatch.setattr(session_detector, "get_settings", fake_get_settings)
    monkeypatch.setattr(session_detector, "_fetch_ankr_transfers_in_range", fake_fetch)

    sessions, _, errors = session_detector.detect_sessions(
        wallets=[("0xwallet", "wallet")],
        tokens=[("bsc", "0xtoken", "TOK", "Token")],
        tokens_for_time_detection=[("bsc", "0xtoken", "TOK", "Token")],
        target_date="2026-05-08",
    )

    assert errors == []
    assert len(sessions) == 1
    assert calls[0] == (
        int(datetime(2026, 5, 8, 0, 0, tzinfo=timezone.utc).timestamp()),
        int(datetime(2026, 5, 8, 23, 59, 59, 999999, tzinfo=timezone.utc).timestamp()),
    )
    assert sessions[0].start_time.tzinfo == timezone.utc
    assert sessions[0].start_time.isoformat() == "2026-05-08T11:59:00+00:00"

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any

from websockets.sync.client import ClientConnection, connect


BALANCE_OF_SELECTOR = "0x70a08231"
_connections: dict[str, ClientConnection] = {}
_connection_locks: dict[str, Lock] = {}
_connections_guard = Lock()


@dataclass(frozen=True)
class Erc20BalanceRead:
    key: str
    token_address: str
    wallet_address: str


def _address_hex(address: str) -> str:
    value = (address or "").strip().lower()
    if value.startswith("0x"):
        value = value[2:]
    if len(value) != 40:
        raise ValueError(f"invalid EVM address: {address}")
    int(value, 16)
    return value


def _balance_of_call_data(wallet_address: str) -> str:
    return BALANCE_OF_SELECTOR + ("0" * 24) + _address_hex(wallet_address)


def _eth_call_payload(read_id: int, read: Erc20BalanceRead) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": read_id,
        "method": "eth_call",
        "params": [
            {
                "to": "0x" + _address_hex(read.token_address),
                "data": _balance_of_call_data(read.wallet_address),
            },
            "latest",
        ],
    }


def _connection_lock(wss_url: str) -> Lock:
    with _connections_guard:
        if wss_url not in _connection_locks:
            _connection_locks[wss_url] = Lock()
        return _connection_locks[wss_url]


def _get_connection(wss_url: str, timeout_seconds: float) -> ClientConnection:
    connection = _connections.get(wss_url)
    if connection is not None:
        return connection
    connection = connect(
        wss_url,
        open_timeout=timeout_seconds,
        close_timeout=3,
        ping_interval=20,
        ping_timeout=10,
    )
    _connections[wss_url] = connection
    return connection


def _drop_connection(wss_url: str) -> None:
    connection = _connections.pop(wss_url, None)
    if connection is None:
        return
    try:
        connection.close()
    except Exception:
        pass


def _read_erc20_balances_wss_once(
    wss_url: str,
    reads: list[Erc20BalanceRead],
    *,
    timeout_seconds: float,
) -> dict[str, int]:
    if not reads:
        return {}

    read_by_id = {idx + 1: read for idx, read in enumerate(reads)}
    payloads = [_eth_call_payload(read_id, read) for read_id, read in read_by_id.items()]
    results: dict[str, int] = {}
    pending = set(read_by_id)

    lock = _connection_lock(wss_url)
    with lock:
        websocket = _get_connection(wss_url, timeout_seconds)
        for payload in payloads:
            websocket.send(json.dumps(payload))

        deadline = time.monotonic() + timeout_seconds
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("WSS balance read timed out")
            raw_message = websocket.recv(timeout=remaining)
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError as exc:
                raise RuntimeError("WSS returned invalid JSON") from exc
            responses = message if isinstance(message, list) else [message]
            for response in responses:
                if not isinstance(response, dict):
                    continue
                response_id = response.get("id")
                if response_id not in pending:
                    continue
                if response.get("error"):
                    error = response.get("error")
                    error_message = error.get("message") if isinstance(error, dict) else str(error)
                    raise RuntimeError(f"WSS balance read failed: {error_message}")
                result = response.get("result")
                if not isinstance(result, str):
                    raise RuntimeError("WSS balance read returned an invalid result")
                read = read_by_id[response_id]
                results[read.key] = int(result or "0x0", 16)
                pending.remove(response_id)

    return results


def read_erc20_balances_wss(
    wss_url: str,
    reads: list[Erc20BalanceRead],
    *,
    timeout_seconds: float = 12,
) -> dict[str, int]:
    try:
        return _read_erc20_balances_wss_once(
            wss_url,
            reads,
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        _drop_connection(wss_url)
        raise

from __future__ import annotations

import requests

from ..config import get_settings, ChainConfig


def _rpc_call(rpc_url: str, method: str, params: list, timeout: int = 10) -> dict | None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        resp = requests.post(rpc_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result")
    except Exception:
        return None


def _decode_abi_string(hex_data: str) -> str | None:
    if not hex_data or hex_data == "0x":
        return None
    raw = hex_data[2:] if hex_data.startswith("0x") else hex_data
    if len(raw) < 64:
        return None
    if len(raw) == 64:
        return bytes.fromhex(raw).rstrip(b"\x00").decode("utf-8", errors="ignore").strip() or None
    try:
        offset = int(raw[:64], 16)
        offset_bytes = offset * 2
        length = int(raw[offset_bytes:offset_bytes + 64], 16)
        data_start = offset_bytes + 64
        data_hex = raw[data_start:data_start + length * 2]
        return bytes.fromhex(data_hex).decode("utf-8", errors="ignore").strip() or None
    except (ValueError, IndexError):
        return bytes.fromhex(raw[:64]).rstrip(b"\x00").decode("utf-8", errors="ignore").strip() or None


def resolve_token_metadata(chain: str, address: str) -> dict:
    settings = get_settings()
    chain_config: ChainConfig | None = settings.chain_configs.get(chain)
    if not chain_config or not chain_config.rpc_urls:
        return {"name": None, "symbol": None, "decimals": None}

    address = address.lower()
    result = {"name": None, "symbol": None, "decimals": None}

    selectors = {
        "name": "0x06fdde03",
        "symbol": "0x95d89b41",
        "decimals": "0x313ce567",
    }

    for rpc_url in chain_config.rpc_urls:
        all_resolved = True
        for field, selector in selectors.items():
            if result[field] is not None:
                continue
            call_result = _rpc_call(rpc_url, "eth_call", [{"to": address, "data": selector}, "latest"])
            if call_result and call_result != "0x":
                if field == "decimals":
                    try:
                        result["decimals"] = int(call_result, 16)
                    except ValueError:
                        all_resolved = False
                else:
                    decoded = _decode_abi_string(call_result)
                    if decoded:
                        result[field] = decoded
                    else:
                        all_resolved = False
            else:
                all_resolved = False
        if all_resolved:
            break

    return result

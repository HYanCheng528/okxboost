from __future__ import annotations

import re


EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def is_evm_address(value: str | None) -> bool:
    return bool(EVM_ADDRESS_RE.match((value or "").strip()))


def is_solana_address(value: str | None) -> bool:
    text = (value or "").strip()
    if not SOLANA_ADDRESS_RE.match(text):
        return False
    try:
        return len(_b58decode(text)) == 32
    except ValueError:
        return False


def normalize_chain_address(chain: str, address: str) -> str:
    value = address.strip()
    if chain.lower() == "solana":
        if not is_solana_address(value):
            raise ValueError("Invalid Solana address")
        return value
    if not is_evm_address(value):
        raise ValueError("Invalid EVM address")
    return value.lower()


def address_matches_chain(chain: str, address: str | None) -> bool:
    return is_solana_address(address) if chain.lower() == "solana" else is_evm_address(address)


def _b58decode(value: str) -> bytes:
    number = 0
    for char in value:
        index = BASE58_ALPHABET.find(char)
        if index < 0:
            raise ValueError("invalid base58 character")
        number = number * 58 + index

    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeroes + raw

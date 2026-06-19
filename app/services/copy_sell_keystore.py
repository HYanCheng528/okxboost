from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from eth_account import Account

from ..config import Settings

KDF_ITERATIONS = 390_000


@dataclass(frozen=True)
class RobotKey:
    key_id: str
    label: str
    address: str
    private_key: str


def _derive_fernet_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _normalize_private_key(private_key: str) -> str:
    value = private_key.strip()
    if not value:
        raise ValueError("private key is empty")
    if not value.startswith("0x"):
        value = f"0x{value}"
    if len(value) != 66:
        raise ValueError("private key must be 32 bytes hex")
    int(value[2:], 16)
    return value


def _address_from_private_key(private_key: str) -> str:
    account = Account.from_key(_normalize_private_key(private_key))
    return str(account.address).lower()


def build_plain_payload(records: list[dict[str, str]]) -> dict[str, Any]:
    keys: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_addresses: set[str] = set()
    for idx, item in enumerate(records, start=1):
        private_key = _normalize_private_key(str(item.get("privateKey") or item.get("private_key") or ""))
        key_id = str(item.get("keyId") or item.get("key_id") or f"robot_{idx}").strip()
        label = str(item.get("label") or key_id).strip()
        if not key_id:
            raise ValueError("keyId cannot be empty")
        if key_id in seen_ids:
            raise ValueError(f"duplicate keyId: {key_id}")
        address = _address_from_private_key(private_key)
        if address in seen_addresses:
            raise ValueError(f"duplicate robot wallet address: {address}")
        seen_ids.add(key_id)
        seen_addresses.add(address)
        keys.append({"keyId": key_id, "label": label or key_id, "address": address, "privateKey": private_key})
    return {"version": 1, "keys": keys}


def write_encrypted_keystore(path: Path, password: str, records: list[dict[str, str]]) -> None:
    if not password:
        raise ValueError("ROBOT_KEYSTORE_PASSWORD is required")
    payload = build_plain_payload(records)
    salt = os.urandom(16)
    fernet = Fernet(_derive_fernet_key(password, salt))
    encrypted = fernet.encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    envelope = {
        "version": 1,
        "cipher": "fernet-aes-cbc-hmac",
        "kdf": "pbkdf2-sha256",
        "iterations": KDF_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "payload": encrypted.decode("ascii"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")


def load_encrypted_keystore(path: Path, password: str) -> list[RobotKey]:
    if not password:
        raise ValueError("ROBOT_KEYSTORE_PASSWORD is required")
    if not path.exists():
        raise ValueError(f"robot keystore not found: {path}")
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        salt = base64.b64decode(str(envelope["salt"]))
        encrypted = str(envelope["payload"]).encode("ascii")
    except Exception as exc:
        raise ValueError("invalid robot keystore format") from exc

    try:
        fernet = Fernet(_derive_fernet_key(password, salt))
        payload = json.loads(fernet.decrypt(encrypted).decode("utf-8"))
    except InvalidToken as exc:
        raise ValueError("invalid robot keystore password") from exc
    except Exception as exc:
        raise ValueError("failed to decrypt robot keystore") from exc

    keys: list[RobotKey] = []
    for item in payload.get("keys", []):
        private_key = _normalize_private_key(str(item.get("privateKey") or ""))
        address = _address_from_private_key(private_key)
        expected_address = str(item.get("address") or "").lower()
        if expected_address and expected_address != address:
            raise ValueError(f"keystore address mismatch for {item.get('keyId')}")
        key_id = str(item.get("keyId") or "").strip()
        if not key_id:
            raise ValueError("keystore entry missing keyId")
        keys.append(
            RobotKey(
                key_id=key_id,
                label=str(item.get("label") or key_id).strip() or key_id,
                address=address,
                private_key=private_key,
            )
        )
    return keys


def load_robot_keys(settings: Settings) -> list[RobotKey]:
    password = settings.robot_keystore_password or ""
    return load_encrypted_keystore(settings.robot_keystore_path, password)

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from app.services.copy_sell_keystore import write_encrypted_keystore


def _load_records(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("keys", [])
        if not isinstance(data, list):
            raise ValueError("JSON input must be a list or an object with keys")
        return [dict(item) for item in data]

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def main() -> None:
    parser = argparse.ArgumentParser(description="Import robot wallet private keys into encrypted keystore.")
    parser.add_argument("--input", required=True, help="CSV or JSON with keyId,label,privateKey columns/fields")
    parser.add_argument("--output", default="data/robot_wallets.enc.json", help="Encrypted keystore output path")
    parser.add_argument("--password", required=True, help="Keystore password; put same value in ROBOT_KEYSTORE_PASSWORD")
    args = parser.parse_args()

    records = _load_records(Path(args.input))
    write_encrypted_keystore(Path(args.output), args.password, records)
    print(f"Imported {len(records)} robot wallet keys into {args.output}")


if __name__ == "__main__":
    main()

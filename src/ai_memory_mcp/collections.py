from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml


COLLECTION_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,79}$")
RECORD_TYPE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


def _collection_base(name: str, record_type: str) -> str:
    payload: dict[str, Any] = {
        "filters": {"and": [f'file.inFolder("Collections/{name}/Records")']},
        "views": [
            {
                "type": "table",
                "name": name,
                "order": [
                    "file.name",
                    "record_type",
                    "status",
                    "updated",
                    "domain",
                ],
                "filters": {"and": [f'record_type == "{record_type}"']},
            }
        ],
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def create_collection(root: Path, *, name: str, record_type: str) -> dict[str, Any]:
    name = name.strip()
    record_type = record_type.strip().casefold()
    if not COLLECTION_NAME.fullmatch(name) or name in {".", ".."}:
        raise ValueError("The collection name contains unsupported characters.")
    if not RECORD_TYPE.fullmatch(record_type):
        raise ValueError("The record type must use lowercase letters, numbers, or hyphens.")

    collection = root / "Collections" / name
    records = collection / "Records"
    base = collection / f"{name}.base"
    records.mkdir(parents=True, exist_ok=True)
    expected = _collection_base(name, record_type)
    created = False
    if base.exists():
        if base.read_text(encoding="utf-8-sig") != expected:
            raise ValueError(f"The existing collection view differs: {base}")
    else:
        base.write_text(expected, encoding="utf-8", newline="\n")
        created = True
    return {
        "name": name,
        "record_type": record_type,
        "directory": str(collection),
        "records_directory": str(records),
        "base": str(base),
        "created": created,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create one optional AI Memory collection."
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--record-type", required=True)
    arguments = parser.parse_args()
    result = create_collection(
        arguments.root,
        name=arguments.name,
        record_type=arguments.record_type,
    )
    print(yaml.safe_dump(result, sort_keys=False), end="")


if __name__ == "__main__":
    main()

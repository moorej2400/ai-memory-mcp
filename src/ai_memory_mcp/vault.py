from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Any

import yaml


HOME = """---
schema_version: 2
memory_id: mem-home
title: Home
type: memory-index
record_type: index
domain: general
status: active
created: {today}
updated: {today}
provenance:
  - source: vault-initializer
---
# Home

This page shows the active memory views.

## Actions

![[Views/Action Dashboard.base]]

## Review

![[Views/Review Queue.base]]

## Health

![[Views/Memory Health.base]]
"""


def _base(name: str, filters: list[str], order: list[str]) -> str:
    payload: dict[str, Any] = {
        "filters": {"and": filters},
        "views": [{"type": "table", "name": name, "order": order}],
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


BASES = {
    "Action Dashboard.base": _base(
        "Action Dashboard",
        ['record_type == "action"', 'status == "active"'],
        ["file.name", "owner", "due", "collection", "updated"],
    ),
    "Review Queue.base": _base(
        "Review Queue",
        ['status == "needs-review"'],
        ["file.name", "record_type", "domain", "updated"],
    ),
    "Memory Health.base": _base(
        "Memory Health",
        ['file.ext == "md"'],
        ["file.name", "schema_version", "status", "updated"],
    ),
}


def _create_if_absent(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def initialize_vault(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    if _create_if_absent(root / "Home.md", HOME.format(today=date.today().isoformat())):
        created.append("Home.md")
    (root / "Notes").mkdir(exist_ok=True)
    for name, content in BASES.items():
        relative = Path("Views") / name
        if _create_if_absent(root / relative, content):
            created.append(relative.as_posix())
    return {"root": str(root), "created": created, "changed": bool(created)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the minimum generic AI Memory vault structure."
    )
    parser.add_argument("--root", type=Path, required=True)
    arguments = parser.parse_args()
    print(
        yaml.safe_dump(
            initialize_vault(arguments.root),
            sort_keys=False,
        ),
        end="",
    )


if __name__ == "__main__":
    main()

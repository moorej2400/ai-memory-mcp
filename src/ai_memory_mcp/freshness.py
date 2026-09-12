"""Publish shared Markdown reconciliation state outside the recall path."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .generation import _publish_json
from .index import MemoryIndex, current_index_path

RECONCILIATION_INTERVAL_SECONDS = 2.0


def publish_markdown_freshness(
    settings: Settings, snapshot: Path, stale: bool
) -> dict[str, Any]:
    state = {
        "snapshot": snapshot.name,
        "stale": stale,
        "reconciled_at": datetime.now(timezone.utc).isoformat(),
        "checked_at": time.time(),
    }
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    _publish_json(settings.state_dir / "markdown-freshness.json", state)
    return state


def reconcile_markdown(settings: Settings) -> dict[str, Any] | None:
    snapshot = current_index_path(settings)
    if snapshot is None:
        return None
    try:
        stale = MemoryIndex(settings, path=snapshot).canonical_stale()
    except (OSError, ValueError, sqlite3.DatabaseError):
        stale = True
    return publish_markdown_freshness(settings, snapshot, stale)


def markdown_freshness(settings: Settings, snapshot: Path) -> dict[str, Any] | None:
    try:
        state = json.loads(
            (settings.state_dir / "markdown-freshness.json").read_text(encoding="utf-8")
        )
        # A marker for another generation, or one left by a stopped reconciler,
        # cannot certify the freshness of the worker's pinned snapshot.
        if (
            not isinstance(state, dict)
            or state.get("snapshot") != snapshot.name
            or not isinstance(state.get("stale"), bool)
            or not 0 <= time.time() - float(state["checked_at"]) <= 30.0
        ):
            return None
        return state
    except (OSError, ValueError, TypeError, KeyError):
        return None

"""Derived source order shared by indexing and canonical context reads."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def position_signals(payload_json, *, entity, source_sequence, import_ordinal, occurred_at):
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        payload = {}
    source = payload.get("source_payload", {}) if isinstance(payload, dict) else {}
    if not isinstance(source, dict):
        source = {}
    def integer(*names):
        for name in names:
            value = source.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 2**63:
                return int(value)
        return None
    position = integer("position", "ordinal", "cue_index", "message_index")
    if position is None and entity == "transcript-cue":
        position = source_sequence
    start = integer("relative_start_ms", "start_ms", "offset_ms", "startTimeMs")
    end = integer("relative_end_ms", "end_ms", "endTimeMs")
    duration = integer("duration_ms", "durationMs")
    if end is None and start is not None and duration is not None:
        end = start + duration
    if position is None and occurred_at is None:
        position = import_ordinal
    return position, start, end


def source_order_key(position, relative_start, occurred_at, artifact_id):
    if position is not None:
        return 0, position, artifact_id
    if relative_start is not None:
        return 1, relative_start, artifact_id
    if occurred_at is not None:
        when = occurred_at if occurred_at.tzinfo else occurred_at.replace(tzinfo=timezone.utc)
        return 2, when.timestamp(), artifact_id
    return 3, 0, artifact_id


def refresh_source_order(connection: sqlite3.Connection, revision: int | None = None) -> None:
    """Update only journaled records, in the same transaction as their source rows."""
    where = ("WHERE a.artifact_id IN (SELECT artifact_id FROM artifact_change_journal WHERE revision = ?)"
             if revision is not None else "")
    cursor = connection.execute(
        "SELECT a.*, (SELECT ordinal FROM artifact_batch_events e WHERE e.event_id = a.last_event_id "
        "ORDER BY e.batch_id, e.ordinal LIMIT 1) AS import_ordinal FROM artifacts a " + where,
        (revision,) if revision is not None else (),
    )
    while rows := cursor.fetchmany(512):
        values = []
        for row in rows:
            position, start, _ = position_signals(
                row["payload_json"], entity=row["entity"], source_sequence=row["source_sequence"],
                import_ordinal=row["import_ordinal"], occurred_at=row["occurred_at"],
            )
            when = datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00")) if row["occurred_at"] else None
            group, value, identity = source_order_key(position, start, when, row["artifact_id"])
            values.append((identity, row["parent_artifact_id"], row["entity"],
                           int(row["deleted_at"] is None and row["redacted_at"] is None), group, value))
        connection.executemany("INSERT OR REPLACE INTO artifact_source_order VALUES (?, ?, ?, ?, ?, ?)", values)


def create_source_order(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE artifact_source_order(artifact_id TEXT PRIMARY KEY REFERENCES artifacts(artifact_id), "
        "parent_artifact_id TEXT, entity TEXT NOT NULL, active INTEGER NOT NULL, "
        "order_group INTEGER NOT NULL, order_value NUMERIC NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX artifact_source_order_neighbors ON artifact_source_order("
        "parent_artifact_id, entity, active, order_group, order_value, artifact_id)"
    )
    refresh_source_order(connection)

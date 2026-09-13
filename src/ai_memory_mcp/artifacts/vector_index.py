from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from ai_memory_mcp.audit import file_lock
from ai_memory_mcp.ann import (
    ANN_BACKEND,
    _numpy,
    available as ann_available,
    quantized_shortlist,
    quantized_vector,
)
from ai_memory_mcp.config import Settings
from ai_memory_mcp.embedding import fingerprint, resolve_provider
from ai_memory_mcp.index import decode_vector, encode_vector
from ai_memory_mcp.models import ArtifactIndexResult
from ai_memory_mcp.query_log import query_stage
from ai_memory_mcp.text import cosine_sparse

from .bursts import REPRESENTATION_VERSION, build_representations
from .ordering import position_signals as _position_signals
from .context import active_ancestor_predicate
from .identity import artifact_uri, parse_artifact_uri
from .models import (
    ArtifactBurst,
    ArtifactBurstRecord,
    ArtifactScope,
    ArtifactSearchHit,
    ArtifactVectorSearchResult,
)
from .schema import (
    ClosingSQLiteConnection,
    connect_artifact_db,
    require_local_database_path,
)
from .search import _bounded_search_span

ARTIFACT_VECTOR_SCHEMA_VERSION = 7
MAX_SEGMENT_FANOUT = 8
COMPACTION_THRESHOLD = 4
COMPACTION_BLOCK_ROWS = 512
_COMPACTION_THREADS: dict[str, threading.Thread] = {}
_COMPACTION_LOCK = threading.Lock()
POINTER_REPLACE_RETRY_SECONDS = 1.0
POINTER_REPLACE_RETRY_INTERVAL_SECONDS = 0.05
_NUMPY_VECTOR_DTYPE = [("index", "<u2"), ("value", "<f2")]
_ANN_SCAN_COLUMNS = (
    "source", "source_instance", "entity", "parent_artifact_id", "started_at", "ended_at",
    "meeting_artifact_uri", "meeting_occurred_at", "anchor_artifact_uri", "burst_id", "ann_vector",
)


@lru_cache(maxsize=16)
def _query_provider(name: str, model: str, dimensions: int) -> Any:
    """Keep the query model warm inside each supervised worker process."""
    return resolve_provider(name, model=model, dimensions=dimensions)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _dense_query_vector(
    vector: dict[int, float], dimensions: int
) -> Any | None:
    numpy = _numpy()
    if numpy is None:
        return None
    dense = numpy.zeros(dimensions, dtype=numpy.float32)
    for index, value in vector.items():
        if 0 <= index < dimensions:
            dense[index] = value
    return dense


def _vector_blob_score(
    query_vector: dict[int, float],
    query_dense: Any | None,
    payload: bytes,
) -> float:
    numpy = _numpy()
    if numpy is None or query_dense is None:
        return cosine_sparse(query_vector, decode_vector(payload))
    values = numpy.frombuffer(payload, dtype=_NUMPY_VECTOR_DTYPE)
    if not len(values):
        return 0.0
    stored = values["value"].astype(numpy.float32, copy=False)
    norm = float(numpy.linalg.norm(stored))
    if norm == 0:
        return 0.0
    return float(numpy.dot(query_dense[values["index"]], stored) / norm)


def _vector_block_scores(
    query_vector: dict[int, float],
    query_dense: Any | None,
    rows: list[sqlite3.Row],
    dimensions: int,
) -> list[float]:
    """Score a dense provider block in NumPy and preserve sparse fallback behavior."""
    numpy = _numpy()
    payloads = [bytes(row["vector_blob"]) for row in rows]
    expected_bytes = dimensions * numpy.dtype(_NUMPY_VECTOR_DTYPE).itemsize if numpy else 0
    if (
        numpy is None
        or query_dense is None
        or dimensions <= 0
        or not payloads
        or any(len(payload) != expected_bytes for payload in payloads)
    ):
        return [
            _vector_blob_score(query_vector, query_dense, payload)
            for payload in payloads
        ]
    encoded = numpy.frombuffer(b"".join(payloads), dtype=_NUMPY_VECTOR_DTYPE).reshape(
        len(rows), dimensions
    )
    values = encoded["value"].astype(numpy.float32, copy=False)
    norms = numpy.linalg.norm(values, axis=1)
    query_norm = float(numpy.linalg.norm(query_dense))
    denominators = norms * query_norm
    scores = numpy.divide(
        values @ query_dense,
        denominators,
        out=numpy.zeros(len(rows), dtype=numpy.float32),
        where=denominators != 0,
    )
    return [float(value) for value in scores]


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    path = require_local_database_path(path)
    if read_only:
        connection = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro",
            uri=True,
            factory=ClosingSQLiteConnection,
        )
    else:
        connection = sqlite3.connect(path, factory=ClosingSQLiteConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'vector_segments'"
    ).fetchone():
        names = [str(row[0]) for row in connection.execute(
            "SELECT path FROM vector_segments ORDER BY ordinal"
        )]
        for name, byte_count, digest in connection.execute("SELECT path, bytes, sha256 FROM vector_segments"):
            segment_path = path.parent / str(name)
            if Path(name).name != name:
                connection.close()
                raise ValueError("The vector segment path is not valid.")
            if _segment_binding(segment_path) != (int(byte_count), str(digest)):
                connection.close()
                raise ValueError("The vector segment integrity check failed.")
        if not 1 <= len(names) <= MAX_SEGMENT_FANOUT:
            connection.close()
            raise ValueError("The vector segment count is not valid.")
        for number, name in enumerate(names):
            if Path(name).name != name or not name.startswith("artifact-segment-"):
                connection.close()
                raise ValueError("The vector segment path is not valid.")
            uri = (path.parent / name).resolve().as_uri() + "?mode=ro"
            connection.execute(f"ATTACH DATABASE ? AS s{number}", (uri,))
        # Later segments replace an anchor completely, including deletion tombstones.
        # Readers pin only the small manifest; its immutable segments never change.
        for table, anchor in (
            ("bursts", "r.anchor_artifact_uri"),
            ("vector_candidates", "r.anchor_artifact_uri"),
            ("representation_coverage", "r.artifact_uri"),
            ("representation_dependencies", "b.anchor_artifact_uri"),
        ):
            parts = []
            for number in range(len(names)):
                join = (f" JOIN s{number}.bursts b USING(burst_id)"
                        if table == "representation_dependencies" else "")
                conditions = [
                    f"NOT EXISTS (SELECT 1 FROM s{later}.updated_anchors u "
                    f"WHERE u.artifact_uri = {anchor})"
                    for later in range(number + 1, len(names))
                ]
                compact = table == "vector_candidates"
                if compact:
                    # An outer count over the full UNION view reads vector_blob
                    # from every arm. Project only covered columns after checking
                    # vector eligibility inside each arm; preserve all tombstones.
                    conditions.insert(0, "r.vector_blob IS NOT NULL")
                where = " WHERE " + " AND ".join(conditions) if conditions else ""
                projection = ", ".join(f"r.{column}" for column in _ANN_SCAN_COLUMNS) if compact else "r.*"
                source_table = "bursts" if compact else table
                parts.append(f"SELECT {projection} FROM s{number}.{source_table} r{join}{where}")
            connection.execute(f"CREATE TEMP VIEW {table} AS " + " UNION ALL ".join(parts))
    elif connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'bursts'").fetchone():
        connection.execute(
            "CREATE TEMP VIEW vector_candidates AS SELECT " + ", ".join(_ANN_SCAN_COLUMNS)
            + " FROM bursts WHERE vector_blob IS NOT NULL"
        )
    return connection


def _schema_matches(path: Path) -> bool:
    try:
        with _connect(path, read_only=True) as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            return bool(row and int(row[0]) == ARTIFACT_VECTOR_SCHEMA_VERSION)
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return False


def current_artifact_index_path(settings: Settings) -> Path | None:
    from ai_memory_mcp.generation import generation_component_path

    # Validate the index root even when it has no snapshot to inspect.
    require_local_database_path(settings.state_dir / "artifact-index.sqlite")
    generated = generation_component_path(settings, "artifact_snapshot")
    if generated is not None and _schema_matches(generated):
        return generated
    pointer = settings.artifact_pointer_path
    if pointer.is_file():
        try:
            payload = json.loads(pointer.read_text(encoding="utf-8"))
            candidate = settings.state_dir / payload["snapshot"]
            if candidate.is_file() and _schema_matches(candidate):
                return candidate
        except (json.JSONDecodeError, KeyError, OSError, TypeError):
            pass
    snapshots = sorted(
        settings.state_dir.glob("artifact-index-*.sqlite"),
        reverse=True,
    )
    return next((path for path in snapshots if _schema_matches(path)), None)


def _metadata(path: Path) -> dict[str, str]:
    with _connect(path, read_only=True) as connection:
        return {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM metadata")
        }


def _canonical_change_counter(settings: Settings) -> int:
    with connect_artifact_db(
        settings.artifact_db,
        read_only=True,
    ) as connection:
        row = connection.execute(
            "SELECT value FROM artifact_metadata WHERE key = 'change_counter'"
        ).fetchone()
    if row is None:
        raise RuntimeError("The artifact database has no change counter.")
    return int(row[0])


def _observed_bounds(settings: Settings) -> tuple[str, str]:
    with connect_artifact_db(settings.artifact_db, read_only=True) as connection:
        row = connection.execute(
            "SELECT min(occurred_at), max(occurred_at) FROM artifacts "
            "WHERE entity IN ('message', 'transcript', 'transcript-cue') "
            "AND deleted_at IS NULL AND redacted_at IS NULL"
        ).fetchone()
    return str(row[0] or ""), str(row[1] or "")


def _participant_names(payload_json: str) -> tuple[str, ...]:
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return ()
    participants = payload.get("participants", []) if isinstance(payload, dict) else []
    if not isinstance(participants, list):
        return ()
    return tuple(
        str(participant.get("name") or "")
        for participant in participants
        if isinstance(participant, dict) and participant.get("name")
    )


def _payload_signals(payload_json: str) -> tuple[str, tuple[str, ...], bool]:
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return "", (), False
    if not isinstance(payload, dict):
        return "", (), False
    reactions = payload.get("reactions", [])
    links = payload.get("links", [])
    has_attachment_link = bool(
        isinstance(links, list)
        and any(
            isinstance(link, dict)
            and isinstance(link.get("target"), dict)
            and link["target"].get("entity") == "attachment"
            for link in links
        )
    )
    return (
        str(payload.get("classification") or ""),
        tuple(str(value) for value in reactions) if isinstance(reactions, list) else (),
        bool(payload.get("object")) or has_attachment_link,
    )


def _context_neighbor_ids(
    connection: sqlite3.Connection, anchor_ids: set[str]
) -> set[str]:
    neighbors: set[str] = set()
    for anchor_id in anchor_ids:
        anchor = connection.execute(
            "SELECT * FROM artifact_source_order WHERE artifact_id = ?",
            (anchor_id,),
        ).fetchone()
        if anchor is None or anchor["parent_artifact_id"] is None:
            continue
        if anchor["entity"] not in {"message", "transcript-cue", "transcript"}:
            continue
        for comparison, direction in (("<", "DESC"), (">", "ASC")):
            rows = connection.execute(
                "SELECT artifact_id FROM artifact_source_order "
                "WHERE parent_artifact_id = ? AND entity = ? AND active = 1 "
                f"AND (order_group, order_value, artifact_id) {comparison} (?, ?, ?) "
                f"ORDER BY order_group {direction}, order_value {direction}, artifact_id {direction} LIMIT 2",
                (anchor["parent_artifact_id"], anchor["entity"], anchor["order_group"], anchor["order_value"], anchor_id),
            )
            neighbors.update(str(row[0]) for row in rows)
    return neighbors


def _load_records(
    settings: Settings,
    previous_counter: int | None = None,
    current_index: Path | None = None,
) -> tuple[int, set[str] | None, list[ArtifactBurstRecord]]:
    records: list[ArtifactBurstRecord] = []
    with connect_artifact_db(
        settings.artifact_db,
        read_only=True,
    ) as connection:
        connection.execute("BEGIN")
        counter_row = connection.execute(
            "SELECT value FROM artifact_metadata WHERE key = 'change_counter'"
        ).fetchone()
        if counter_row is None:
            raise RuntimeError("The artifact database has no change counter.")
        change_counter = int(counter_row[0])
        selected_anchors: set[str] | None = None
        record_ids: set[str] | None = None
        if previous_counter is not None and previous_counter != change_counter:
            journal_rows = connection.execute(
                "SELECT artifact_id, affected_parent_id "
                "FROM artifact_change_journal "
                "WHERE revision > ? AND revision <= ? "
                "ORDER BY revision, ordinal",
                (previous_counter, change_counter),
            ).fetchall()
            if journal_rows:
                changed_ids = {str(row["artifact_id"]) for row in journal_rows}
                affected_ids = {
                    str(row["affected_parent_id"]) for row in journal_rows
                }
                selected_ids = set(changed_ids)
                selected_ids.update(affected_ids)
                placeholders = ", ".join("?" for _ in selected_ids)
                searchable = {
                    str(row["artifact_id"])
                    for row in connection.execute(
                        "SELECT artifact_id FROM artifacts WHERE artifact_id IN ("
                        + placeholders
                        + ") AND entity IN ('message', 'transcript-cue', 'transcript')",
                        sorted(selected_ids),
                    )
                }
                # A changed container can alter titles, dates, or visibility for
                # all contained passages. Expand only this uncommon container case.
                changed_placeholders = ", ".join("?" for _ in changed_ids)
                containers = {
                    str(row["artifact_id"])
                    for row in connection.execute(
                        "SELECT artifact_id FROM artifacts WHERE artifact_id IN ("
                        + changed_placeholders
                        + ") AND entity IN ('conversation', 'meeting')",
                        sorted(changed_ids),
                    )
                }
                for container in containers:
                    searchable.update(
                        str(row[0])
                        for row in connection.execute(
                            "WITH RECURSIVE descendants(artifact_id) AS ("
                            "SELECT artifact_id FROM artifacts WHERE parent_artifact_id = ? "
                            "UNION SELECT child.artifact_id FROM artifacts AS child "
                            "JOIN descendants ON child.parent_artifact_id = descendants.artifact_id) "
                            "SELECT artifact_id FROM artifacts WHERE artifact_id IN descendants "
                            "AND entity IN ('message', 'transcript-cue', 'transcript')",
                            (container,),
                        )
                    )
                # A reply target can change the searchable context of its replies.
                if changed_ids:
                    searchable.update(
                        str(row[0])
                        for row in connection.execute(
                            "SELECT source_artifact_id FROM artifact_links "
                            "WHERE target_artifact_id IN ("
                            + changed_placeholders
                            + ") AND relation IN ('reply', 'reply-to', 'in-reply-to')",
                            sorted(changed_ids),
                        )
                    )
                if current_index is not None and current_index.is_file():
                    changed_uris = []
                    for artifact_id in changed_ids:
                        entity_row = connection.execute(
                            "SELECT entity FROM artifacts WHERE artifact_id = ?",
                            (artifact_id,),
                        ).fetchone()
                        if entity_row is not None:
                            changed_uris.append(
                                artifact_uri(str(entity_row[0]), artifact_id)
                            )
                    if changed_uris:
                        with _connect(current_index, read_only=True) as derived:
                            uri_placeholders = ", ".join("?" for _ in changed_uris)
                            searchable.update(
                                parse_artifact_uri(str(row[0]))[1]
                                for row in derived.execute(
                                    "SELECT DISTINCT b.anchor_artifact_uri "
                                    "FROM representation_dependencies AS dependency "
                                    "JOIN bursts AS b USING(burst_id) "
                                    "WHERE dependency.artifact_uri IN ("
                                    + uri_placeholders
                                    + ")",
                                    changed_uris,
                                )
                            )
                # New records have no stored dependencies yet. Their direct
                # neighbors still need new context after an append or insertion.
                searchable.update(_context_neighbor_ids(connection, changed_ids))
                record_ids = set(searchable)
                neighbor_ids = _context_neighbor_ids(connection, searchable)
                # These rows supply context for affected anchors. Rebuilding
                # them would require another ring and would erase outer context.
                record_ids.update(neighbor_ids)
                selected_anchors = {
                    artifact_uri(str(row["entity"]), str(row["artifact_id"]))
                    for row in connection.execute(
                        "SELECT artifact_id, entity FROM artifacts WHERE artifact_id IN ("
                        + ", ".join("?" for _ in searchable)
                        + ")",
                        sorted(searchable),
                    )
                } if searchable else set()
                if record_ids:
                    record_placeholders = ", ".join("?" for _ in record_ids)
                    record_ids.update(
                        str(row[0])
                        for row in connection.execute(
                            "SELECT target_artifact_id FROM artifact_links "
                            "WHERE source_artifact_id IN ("
                            + record_placeholders
                            + ") AND relation IN ('reply', 'reply-to', 'in-reply-to')",
                            sorted(record_ids),
                        )
                    )
            else:
                # A missing journal range indicates legacy or damaged metadata.
                # Rebuild fully instead of advancing incomplete coverage.
                selected_anchors = None
                record_ids = None
        if record_ids is not None:
            connection.execute(
                "CREATE TEMP TABLE selected_vector_records("
                "artifact_id TEXT PRIMARY KEY)"
            )
            connection.executemany(
                "INSERT INTO selected_vector_records VALUES (?)",
                ((value,) for value in sorted(record_ids)),
            )
        parent_filter = (
            "AND child.artifact_id IN (SELECT artifact_id FROM selected_vector_records)"
            if record_ids is not None
            else ""
        )
        rows = connection.execute(
            f"""
            SELECT child.*, parent.title AS parent_title,
                   parent.payload_json AS parent_payload_json,
                   (
                       SELECT batch_event.ordinal
                       FROM artifact_batch_events AS batch_event
                       WHERE batch_event.event_id = child.last_event_id
                       ORDER BY batch_event.batch_id, batch_event.ordinal
                       LIMIT 1
                   ) AS import_ordinal,
                   (
                       WITH RECURSIVE lineage(
                           artifact_id, entity, parent_artifact_id, depth
                       ) AS (
                           SELECT child.artifact_id, child.entity,
                                  child.parent_artifact_id, 0
                           UNION ALL
                           SELECT ancestor.artifact_id, ancestor.entity,
                                  ancestor.parent_artifact_id, lineage.depth + 1
                           FROM artifacts AS ancestor
                           JOIN lineage
                             ON ancestor.artifact_id = lineage.parent_artifact_id
                           WHERE lineage.depth < 8
                             AND ancestor.deleted_at IS NULL
                             AND ancestor.redacted_at IS NULL
                       )
                       SELECT artifact_id FROM lineage
                       WHERE entity = 'meeting' ORDER BY depth LIMIT 1
                   ) AS meeting_artifact_id,
                   (
                       WITH RECURSIVE lineage(
                           artifact_id, entity, parent_artifact_id,
                           occurred_at, depth
                       ) AS (
                           SELECT child.artifact_id, child.entity,
                                  child.parent_artifact_id, child.occurred_at, 0
                           UNION ALL
                           SELECT ancestor.artifact_id, ancestor.entity,
                                  ancestor.parent_artifact_id,
                                  ancestor.occurred_at, lineage.depth + 1
                           FROM artifacts AS ancestor
                           JOIN lineage
                             ON ancestor.artifact_id = lineage.parent_artifact_id
                           WHERE lineage.depth < 8
                             AND ancestor.deleted_at IS NULL
                             AND ancestor.redacted_at IS NULL
                       )
                       SELECT occurred_at FROM lineage
                       WHERE entity = 'meeting' ORDER BY depth LIMIT 1
                   ) AS meeting_occurred_at,
                   (
                       SELECT link.target_artifact_id
                       FROM artifact_links AS link
                       WHERE link.source_artifact_id = child.artifact_id
                         AND link.relation IN ('reply', 'reply-to', 'in-reply-to')
                       ORDER BY CASE link.relation
                           WHEN 'reply-to' THEN 0
                           WHEN 'in-reply-to' THEN 1
                           ELSE 2
                       END, link.target_artifact_id
                       LIMIT 1
                   ) AS reply_target_artifact_id,
                   EXISTS(
                       SELECT 1 FROM artifact_object_links AS object_link
                       WHERE object_link.artifact_id = child.artifact_id
                   ) AS has_object,
                   EXISTS(
                       SELECT 1
                       FROM artifacts AS attachment
                       WHERE attachment.parent_artifact_id = child.artifact_id
                         AND attachment.entity = 'attachment'
                         AND attachment.deleted_at IS NULL
                         AND attachment.redacted_at IS NULL
                   ) AS has_attachment_child
            FROM artifacts AS child
            JOIN artifacts AS parent
              ON parent.artifact_id = child.parent_artifact_id
            WHERE (
                    child.entity IN ('message', 'transcript-cue')
                    OR (
                        child.entity = 'transcript'
                        AND NOT EXISTS(
                            SELECT 1 FROM artifacts AS cue
                            WHERE cue.parent_artifact_id = child.artifact_id
                              AND cue.entity = 'transcript-cue'
                              AND cue.deleted_at IS NULL
                              AND cue.redacted_at IS NULL
                        )
                    )
                  )
              AND child.deleted_at IS NULL AND child.redacted_at IS NULL
              AND parent.deleted_at IS NULL AND parent.redacted_at IS NULL
              AND {active_ancestor_predicate("child")}
              {parent_filter}
            ORDER BY child.parent_artifact_id, child.artifact_id
            """
        ).fetchall()
        for row in rows:
            classification, reactions, attachment = _payload_signals(
                str(row["payload_json"])
            )
            position, relative_start, relative_end = _position_signals(
                str(row["payload_json"]),
                entity=str(row["entity"]),
                source_sequence=(
                    int(row["source_sequence"])
                    if row["source_sequence"] is not None
                    else None
                ),
                import_ordinal=(
                    int(row["import_ordinal"])
                    if row["import_ordinal"] is not None
                    else None
                ),
                occurred_at=row["occurred_at"],
            )
            records.append(
                ArtifactBurstRecord(
                    artifact_id=str(row["artifact_id"]),
                    artifact_uri=artifact_uri(
                        str(row["entity"]),
                        str(row["artifact_id"]),
                    ),
                    parent_artifact_id=str(row["parent_artifact_id"]),
                    parent_title=str(row["parent_title"] or ""),
                    source=str(row["source"]),
                    source_instance=str(row["source_instance"]),
                    entity=str(row["entity"]),
                    author_id=str(row["author_id"]),
                    author_name=str(row["author_name"]),
                    participant_names=_participant_names(
                        str(row["parent_payload_json"])
                    ),
                    occurred_at=row["occurred_at"],
                    source_position=position,
                    relative_start_ms=relative_start,
                    relative_end_ms=relative_end,
                    reply_target_artifact_id=(
                        str(row["reply_target_artifact_id"])
                        if row["reply_target_artifact_id"] is not None
                        else None
                    ),
                    source_revision=str(row["last_event_id"]),
                    meeting_artifact_id=(
                        str(row["meeting_artifact_id"])
                        if row["meeting_artifact_id"] is not None
                        else None
                    ),
                    meeting_occurred_at=row["meeting_occurred_at"],
                    text=str(row["text_content"]),
                    classification=classification,
                    reactions=reactions,
                    attachment_link=(
                        bool(row["has_object"])
                        or bool(row["has_attachment_child"])
                        or attachment
                    ),
                )
            )
        connection.rollback()
    return change_counter, selected_anchors, records


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE updated_anchors (artifact_uri TEXT PRIMARY KEY);
        CREATE TABLE bursts (
            burst_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            source_instance TEXT NOT NULL,
            entity TEXT NOT NULL,
            parent_artifact_id TEXT NOT NULL,
            parent_title TEXT NOT NULL,
            author_id TEXT NOT NULL,
            author_name TEXT NOT NULL,
            first_artifact_uri TEXT NOT NULL,
            last_artifact_uri TEXT NOT NULL,
            started_at TEXT,
            ended_at TEXT,
            record_count INTEGER NOT NULL,
            representation_kind TEXT NOT NULL,
            representation_version INTEGER NOT NULL,
            anchor_artifact_uri TEXT NOT NULL,
            dependencies_json TEXT NOT NULL,
            dependency_digest TEXT NOT NULL,
            segment_start INTEGER NOT NULL,
            segment_end INTEGER NOT NULL,
            source_position INTEGER,
            relative_start_ms INTEGER,
            relative_end_ms INTEGER,
            exclusion_reason TEXT NOT NULL,
            meeting_artifact_uri TEXT,
            meeting_occurred_at TEXT,
            text_content TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            embed INTEGER NOT NULL CHECK(embed IN (0, 1)),
            vector_blob BLOB,
            ann_vector BLOB
        );
        CREATE INDEX bursts_scope_idx
            ON bursts(source, source_instance, entity, started_at, burst_id);
        CREATE INDEX bursts_anchor_idx
            ON bursts(anchor_artifact_uri, representation_kind, segment_start);
        CREATE TABLE representation_dependencies (
            burst_id TEXT NOT NULL REFERENCES bursts(burst_id) ON DELETE CASCADE,
            artifact_uri TEXT NOT NULL,
            PRIMARY KEY(burst_id, artifact_uri)
        );
        CREATE INDEX representation_dependency_idx
            ON representation_dependencies(artifact_uri, burst_id);
        CREATE TABLE representation_coverage (
            artifact_uri TEXT PRIMARY KEY,
            parent_artifact_id TEXT NOT NULL,
            state TEXT NOT NULL,
            reason TEXT NOT NULL,
            segment_count INTEGER NOT NULL,
            missing_absolute_time INTEGER NOT NULL CHECK(missing_absolute_time IN (0, 1))
        );
        CREATE INDEX representation_coverage_parent_idx
            ON representation_coverage(parent_artifact_id, state, artifact_uri);
        """
    )


def _create_ann_scan_index(connection: sqlite3.Connection) -> None:
    # Candidate counting and int8 scanning must not read source text or full
    # vectors. Include every scope and tombstone key used by the segment view.
    connection.execute(
        "CREATE INDEX bursts_ann_scan_idx ON bursts(" + ", ".join(_ANN_SCAN_COLUMNS) + ") "
        "WHERE vector_blob IS NOT NULL"
    )


def _burst_digest(burst: ArtifactBurst) -> str:
    payload = {
        "burst_id": burst.burst_id,
        "source": burst.source,
        "source_instance": burst.source_instance,
        "entity": burst.entity,
        "parent_artifact_id": burst.parent_artifact_id,
        "parent_title": burst.parent_title,
        "author_id": burst.author_id,
        "author_name": burst.author_name,
        "first_artifact_uri": burst.first_artifact_uri,
        "last_artifact_uri": burst.last_artifact_uri,
        "started_at": _utc_iso(burst.started_at) if burst.started_at else None,
        "ended_at": _utc_iso(burst.ended_at) if burst.ended_at else None,
        "record_count": burst.record_count,
        "representation_kind": burst.representation_kind,
        "representation_version": burst.representation_version,
        "anchor_artifact_uri": burst.anchor_artifact_uri,
        "dependencies": burst.dependency_artifact_uris,
        "dependency_digest": burst.dependency_digest,
        "segment_start": burst.segment_start,
        "segment_end": burst.segment_end,
        "source_position": burst.source_position,
        "relative_start_ms": burst.relative_start_ms,
        "relative_end_ms": burst.relative_end_ms,
        "exclusion_reason": burst.exclusion_reason,
        "meeting_artifact_uri": burst.meeting_artifact_uri,
        "meeting_occurred_at": (
            _utc_iso(burst.meeting_occurred_at)
            if burst.meeting_occurred_at else None
        ),
        "text": burst.text,
        "embed": burst.embed,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _insert_burst(
    connection: sqlite3.Connection,
    burst: ArtifactBurst,
    content_sha256: str,
    vector_blob: bytes | None,
    vector: dict[int, float] | None,
    semantic_dimensions: int,
) -> None:
    connection.execute(
        """
        INSERT INTO bursts(
            burst_id, source, source_instance, entity, parent_artifact_id,
            parent_title, author_id, author_name, first_artifact_uri,
            last_artifact_uri, started_at, ended_at, record_count,
            representation_kind, representation_version, anchor_artifact_uri,
            dependencies_json, dependency_digest, segment_start, segment_end,
            source_position, relative_start_ms, relative_end_ms,
            exclusion_reason, meeting_artifact_uri, meeting_occurred_at, text_content,
            content_sha256, embed, vector_blob, ann_vector
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            burst.burst_id,
            burst.source,
            burst.source_instance,
            burst.entity,
            burst.parent_artifact_id,
            burst.parent_title,
            burst.author_id,
            burst.author_name,
            burst.first_artifact_uri,
            burst.last_artifact_uri,
            _utc_iso(burst.started_at) if burst.started_at else None,
            _utc_iso(burst.ended_at) if burst.ended_at else None,
            burst.record_count,
            burst.representation_kind,
            burst.representation_version,
            burst.anchor_artifact_uri or burst.first_artifact_uri,
            json.dumps(burst.dependency_artifact_uris, ensure_ascii=False),
            burst.dependency_digest,
            burst.segment_start,
            burst.segment_end,
            burst.source_position,
            burst.relative_start_ms,
            burst.relative_end_ms,
            burst.exclusion_reason,
            burst.meeting_artifact_uri,
            _utc_iso(burst.meeting_occurred_at) if burst.meeting_occurred_at else None,
            burst.text,
            content_sha256,
            int(burst.embed),
            vector_blob,
            (
                quantized_vector(vector, semantic_dimensions)
                if vector is not None
                else None
            ),
        ),
    )
    connection.executemany(
        "INSERT INTO representation_dependencies(burst_id, artifact_uri) "
        "VALUES (?, ?)",
        (
            (burst.burst_id, dependency)
            for dependency in burst.dependency_artifact_uris
        ),
    )


def _publish_pointer(settings: Settings, snapshot: Path) -> None:
    pointer = settings.artifact_pointer_path
    temporary = pointer.with_name(
        f".{pointer.name}.partial-{os.getpid()}-{time.time_ns()}"
    )
    payload = {
        "snapshot": snapshot.name,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": ARTIFACT_VECTOR_SCHEMA_VERSION,
    }
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    deadline = time.monotonic() + POINTER_REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(temporary, pointer)
            break
        except PermissionError:
            # Windows scanners can briefly hold the prior pointer after reads.
            # Retry only the atomic replace; the validated snapshot is unchanged.
            if time.monotonic() >= deadline:
                raise
            time.sleep(POINTER_REPLACE_RETRY_INTERVAL_SECONDS)


def _publish_snapshot_no_overwrite(temporary: Path, snapshot: Path) -> None:
    os.link(temporary, snapshot)
    temporary.unlink()


def artifact_segment_paths(snapshot: Path) -> list[Path]:
    """Read manifest references without scanning vector data."""
    with sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True,
                         factory=ClosingSQLiteConnection) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'vector_segments'"
        ).fetchone():
            return []
        names = [str(row[0]) for row in connection.execute(
            "SELECT path FROM vector_segments ORDER BY ordinal"
        )]
        bindings = list(connection.execute("SELECT path, bytes, sha256 FROM vector_segments"))
    if len(names) > MAX_SEGMENT_FANOUT or any(
        Path(name).name != name or not name.startswith("artifact-segment-")
        for name in names
    ):
        raise ValueError("The vector segment references are not valid.")
    for name, byte_count, digest in bindings:
        if _segment_binding(snapshot.parent / name) != (int(byte_count), str(digest)):
            raise ValueError("The vector segment integrity check failed.")
    return [snapshot.parent / name for name in names]


@lru_cache(maxsize=64)
def _checked_segment_binding(path: Path, size: int, modified_ns: int) -> tuple[int, str]:
    # Immutable segment hashes are checked once per file identity in each process.
    # A changed or replaced file invalidates the cache before retention trusts it.
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, factory=ClosingSQLiteConnection) as connection:
        for table in ("bursts", "representation_dependencies", "representation_coverage", "updated_anchors"):
            connection.execute(f"SELECT * FROM {table} LIMIT 0")
    return size, digest.hexdigest()


def _segment_binding(path: Path) -> tuple[int, str]:
    status = path.stat()
    return _checked_segment_binding(path.resolve(), status.st_size, status.st_mtime_ns)


def _compacted_prefix(settings: Settings, segments: list[Path]) -> list[Path]:
    try:
        cached = json.loads((settings.state_dir / "artifact-compaction.json").read_text())
        source = cached["source_segments"]
        name = cached["compacted_segment"]
        if (source and source == [path.name for path in segments[:len(source)]]
                and Path(name).name == name and name.startswith("artifact-segment-")
                and (settings.state_dir / name).is_file()):
            return [settings.state_dir / name, *segments[len(source):]]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return segments


def _compact_segments(settings: Settings, snapshot: Path) -> list[Path]:
    from ai_memory_mcp.generation import _publish_json

    with file_lock(settings.state_dir / "artifact-compaction.lock", 130.0):
        segments = artifact_segment_paths(snapshot)
        reduced = _compacted_prefix(settings, segments)
        if len(reduced) < len(segments):
            return reduced
        started = time.monotonic()
        deadline = started + 120.0
        target = settings.state_dir / f"artifact-segment-compact-{time.time_ns()}.sqlite"
        temporary = target.with_name("." + target.name + ".partial")
        rows_copied = 0
        with _connect(snapshot, read_only=True) as source, _connect(temporary) as dest:
            _create_schema(dest)
            source.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
            dest.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
            for table in ("bursts", "representation_dependencies", "representation_coverage"):
                cursor = source.execute(f"SELECT * FROM {table}")
                while rows := cursor.fetchmany(COMPACTION_BLOCK_ROWS):
                    if time.monotonic() >= deadline or rows_copied > 20_000_000:
                        raise RuntimeError("Vector compaction exceeded its resource budget.")
                    placeholders = ",".join("?" for _ in rows[0])
                    dest.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
                    rows_copied += len(rows)
            dest.execute("INSERT INTO updated_anchors SELECT artifact_uri FROM representation_coverage")
            _create_ann_scan_index(dest)
            dest.commit()
            if dest.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Vector compaction failed its integrity check.")
        with file_lock(settings.state_dir / "generation-retention.lock", settings.index_lock_timeout_seconds):
            _publish_snapshot_no_overwrite(temporary, target)
            _publish_json(settings.state_dir / "artifact-compaction.json", {
                "source_segments": [path.name for path in segments],
                "compacted_segment": target.name,
                "rows_copied": rows_copied,
                "block_rows": COMPACTION_BLOCK_ROWS,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            })
        return [target]


def schedule_artifact_compaction(settings: Settings) -> None:
    """Coalesce background compaction; publication consumes its immutable output."""
    from ai_memory_mcp.generation import lease_current_generation, _publish_json

    key = str(settings.state_dir.resolve())
    def run() -> None:
        try:
            with lease_current_generation(settings) as generation:
                snapshot = (settings.state_dir / generation["artifact_snapshot"]
                            if generation else current_artifact_index_path(settings))
                if snapshot and len(artifact_segment_paths(snapshot)) >= COMPACTION_THRESHOLD:
                    _compact_segments(settings, snapshot)
        except (OSError, sqlite3.Error, ValueError, RuntimeError, TimeoutError) as exc:
            # A failed compaction never changes the active manifest. The hard fan-out
            # limit retries synchronously and reports failure instead of dropping data.
            _publish_json(settings.state_dir / "artifact-compaction-health.json", {
                "ok": False, "error_type": type(exc).__name__,
            })
    with _COMPACTION_LOCK:
        active = _COMPACTION_THREADS.get(key)
        if active is not None and active.is_alive():
            return
        thread = threading.Thread(target=run, name="artifact-compaction", daemon=True)
        _COMPACTION_THREADS[key] = thread
        thread.start()


def acknowledge_artifact_vector_changes(
    settings: Settings,
    change_counter: int,
) -> None:
    """Remove only dirty roots included in a published vector generation."""
    with connect_artifact_db(settings.artifact_db) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT value FROM artifact_metadata WHERE key = 'change_counter'"
        ).fetchone()
        if current is None or int(current[0]) < change_counter:
            connection.rollback()
            raise RuntimeError("The artifact change counter is not valid.")
        connection.execute(
            "DELETE FROM artifact_vector_dirty WHERE change_counter <= ?",
            (change_counter,),
        )
        connection.commit()


def build_artifact_vector_index(
    settings: Settings,
    force: bool = False,
    *,
    publish_pointer: bool = True,
) -> ArtifactIndexResult:
    started = time.perf_counter()
    require_local_database_path(settings.state_dir / "artifact-index.sqlite")
    if not settings.artifact_db.is_file():
        raise FileNotFoundError("Artifact database is not available.")
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        settings.state_dir.chmod(0o700)
    with file_lock(
        settings.state_dir / "artifact-index.lock",
        settings.index_lock_timeout_seconds,
    ):
        provider = resolve_provider(
            settings.embedding_provider,
            model=settings.embedding_model,
            dimensions=settings.semantic_dimensions,
        )
        provider_fingerprint = fingerprint(provider)
        target_ann_backend = ANN_BACKEND if ann_available() else "exact"
        change_counter = _canonical_change_counter(settings)
        current = current_artifact_index_path(settings)
        current_metadata: dict[str, str] = {}
        if current is not None and not force:
            current_metadata = _metadata(current)
            if (
                int(current_metadata.get("artifact_change_counter", "-1"))
                == change_counter
                and current_metadata.get("embedding_fingerprint")
                == provider_fingerprint
                and current_metadata.get("ann_backend") == target_ann_backend
            ):
                if publish_pointer:
                    acknowledge_artifact_vector_changes(
                        settings,
                        change_counter,
                    )
                return ArtifactIndexResult(
                    snapshot=str(current),
                    change_counter=change_counter,
                    bursts=int(current_metadata.get("bursts", "0")),
                    embedded_bursts=int(
                        current_metadata.get("embedded_bursts", "0")
                    ),
                    embedding_provider=provider.name,
                    embedding_model=provider.model,
                    embedding_fingerprint=provider_fingerprint,
                    unchanged=True,
                    reused_bursts=int(current_metadata.get("bursts", "0")),
                    eligible_artifacts=int(
                        current_metadata.get("eligible_artifacts", "0")
                    ),
                    indexed_artifacts=int(
                        current_metadata.get("indexed_artifacts", "0")
                    ),
                    excluded_artifacts=int(
                        current_metadata.get("excluded_artifacts", "0")
                    ),
                    empty_artifacts=int(
                        current_metadata.get("empty_artifacts", "0")
                    ),
                    failed_artifacts=int(
                        current_metadata.get("failed_artifacts", "0")
                    ),
                    missing_absolute_time=int(
                        current_metadata.get("missing_absolute_time", "0")
                    ),
                    ann_backend=current_metadata.get("ann_backend", "exact"),
                    elapsed_ms=round(
                        (time.perf_counter() - started) * 1000,
                        3,
                    ),
                )

        reuse_snapshot = bool(
            current is not None
            and not force
            and current_metadata.get("embedding_fingerprint")
            == provider_fingerprint
            and current_metadata.get("ann_backend") == target_ann_backend
        )
        previous_counter = (
            int(current_metadata.get("artifact_change_counter", "-1"))
            if reuse_snapshot
            else None
        )
        change_counter, selected_anchors, records = _load_records(
            settings,
            previous_counter,
            current,
        )
        if selected_anchors is None:
            reuse_snapshot = False
        bursts = build_representations(
            records,
            anchor_artifact_uris=selected_anchors,
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        snapshot = settings.state_dir / (
            f"artifact-index-{stamp}-{os.getpid()}.sqlite"
        )
        temporary = settings.state_dir / (
            f".artifact-index.partial-{os.getpid()}-{time.time_ns()}.sqlite"
        )
        segment = settings.state_dir / f"artifact-segment-{stamp}-{os.getpid()}.sqlite"
        segments = artifact_segment_paths(current) if reuse_snapshot else []
        segments = _compacted_prefix(settings, segments)
        if len(segments) >= MAX_SEGMENT_FANOUT:
            segments = _compact_segments(settings, current)
        publication_mode = "immutable-delta" if reuse_snapshot else "immutable-base"
        existing: dict[str, sqlite3.Row] = {}
        old_coverage: dict[str, int] = {}
        old_missing = 0
        if reuse_snapshot:
            with _connect(current, read_only=True) as previous:
                previous.execute("CREATE TEMP TABLE changed_anchors(artifact_uri TEXT PRIMARY KEY)")
                previous.executemany("INSERT INTO changed_anchors VALUES (?)",
                                     ((value,) for value in selected_anchors or ()))
                existing = {str(row["burst_id"]): row for row in previous.execute(
                    "SELECT * FROM bursts WHERE anchor_artifact_uri IN "
                    "(SELECT artifact_uri FROM changed_anchors)"
                )}
                old_coverage = {str(row[0]): int(row[1]) for row in previous.execute(
                    "SELECT state, count(*) FROM representation_coverage WHERE artifact_uri "
                    "IN (SELECT artifact_uri FROM changed_anchors) GROUP BY state"
                )}
                old_missing = int(previous.execute(
                    "SELECT count(*) FROM representation_coverage WHERE missing_absolute_time = 1 "
                    "AND artifact_uri IN (SELECT artifact_uri FROM changed_anchors)"
                ).fetchone()[0])
        try:
            with _connect(temporary) as connection:
                _create_schema(connection)
                connection.executemany("INSERT INTO updated_anchors VALUES (?)", (
                    (value,) for value in (selected_anchors if selected_anchors is not None
                                          else {record.artifact_uri for record in records})
                ))
                initial_total = int(current_metadata.get("bursts", "0")) if reuse_snapshot else 0
                desired_ids = {burst.burst_id for burst in bursts}
                removed = len(set(existing) - desired_ids)
                embedded_updates = 0
                reused = initial_total - len(existing)
                for burst in bursts:
                    digest = _burst_digest(burst)
                    prior = existing.get(burst.burst_id)
                    if prior is not None and prior["content_sha256"] == digest:
                        connection.execute(
                            "INSERT INTO bursts VALUES (" + ",".join("?" for _ in prior) + ")", tuple(prior)
                        )
                        connection.executemany(
                            "INSERT INTO representation_dependencies VALUES (?, ?)",
                            ((burst.burst_id, uri) for uri in burst.dependency_artifact_uris),
                        )
                        reused += 1
                        continue
                    vector = None
                    vector_blob = None
                    if burst.embed:
                        vector = provider.embed(burst.text)
                        vector_blob = encode_vector(vector)
                        embedded_updates += 1
                    _insert_burst(
                        connection,
                        burst,
                        digest,
                        vector_blob,
                        vector,
                        provider.dimensions,
                    )
                base_counts: dict[str, int] = {}
                for burst in bursts:
                    if burst.representation_kind == "base":
                        anchor = burst.anchor_artifact_uri or burst.first_artifact_uri
                        base_counts[anchor] = base_counts.get(anchor, 0) + 1
                for record in records:
                    if (
                        selected_anchors is not None
                        and record.artifact_uri not in selected_anchors
                    ):
                        continue
                    if not record.text.strip():
                        state, reason = "empty_content", "empty_content"
                    elif record.classification.casefold() == "system":
                        state, reason = "excluded", "system_noise"
                    else:
                        state, reason = "indexed", ""
                    connection.execute(
                        "INSERT OR REPLACE INTO representation_coverage("
                        "artifact_uri, parent_artifact_id, state, reason, "
                        "segment_count, missing_absolute_time) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            record.artifact_uri,
                            record.parent_artifact_id,
                            state,
                            reason,
                            base_counts.get(record.artifact_uri, 0),
                            int(record.occurred_at is None),
                        ),
                    )
                # Build once after the batch to avoid maintaining a wide B-tree
                # for each inserted representation. Published segments are immutable.
                _create_ann_scan_index(connection)
                embedded = int(
                    connection.execute(
                        "SELECT count(*) FROM bursts "
                        "WHERE vector_blob IS NOT NULL"
                    ).fetchone()[0]
                )
                total_bursts = int(
                    connection.execute("SELECT count(*) FROM bursts").fetchone()[0]
                )
                coverage = {
                    str(row["state"]): int(row["count"])
                    for row in connection.execute(
                        "SELECT state, count(*) AS count "
                        "FROM representation_coverage GROUP BY state"
                    )
                }
                missing_absolute_time = int(
                    connection.execute(
                        "SELECT count(*) FROM representation_coverage "
                        "WHERE missing_absolute_time = 1"
                    ).fetchone()[0]
                )
                if reuse_snapshot:
                    total_bursts += initial_total - len(existing)
                    embedded += int(current_metadata["embedded_bursts"]) - sum(
                        row["vector_blob"] is not None for row in existing.values()
                    )
                    for state, key in (("indexed", "indexed_artifacts"), ("excluded", "excluded_artifacts"),
                                       ("empty_content", "empty_artifacts"), ("failed", "failed_artifacts")):
                        coverage[state] = (coverage.get(state, 0) + int(current_metadata.get(key, "0"))
                                           - old_coverage.get(state, 0))
                    missing_absolute_time += int(current_metadata["missing_absolute_time"]) - old_missing
                observed_from, observed_to = _observed_bounds(settings)
                metadata = {
                    "schema_version": str(ARTIFACT_VECTOR_SCHEMA_VERSION),
                    "built_at": datetime.now(timezone.utc).isoformat(),
                    "artifact_change_counter": str(change_counter),
                    "embedding_provider": provider.name,
                    "embedding_model": provider.model,
                    "embedding_dimensions": str(provider.dimensions),
                    "embedding_fingerprint": provider_fingerprint,
                    "ann_backend": target_ann_backend,
                    "bursts": str(total_bursts),
                    "embedded_bursts": str(embedded),
                    "representation_version": str(REPRESENTATION_VERSION),
                    "publication_mode": publication_mode,
                    "segment_fanout": str(len(segments) + 1),
                    "eligible_artifacts": str(
                        coverage.get("indexed", 0)
                        + coverage.get("pending", 0)
                        + coverage.get("failed", 0)
                    ),
                    "indexed_artifacts": str(coverage.get("indexed", 0)),
                    "excluded_artifacts": str(coverage.get("excluded", 0)),
                    "empty_artifacts": str(coverage.get("empty_content", 0)),
                    "failed_artifacts": str(coverage.get("failed", 0)),
                    "missing_absolute_time": str(missing_absolute_time),
                    "observed_from": observed_from,
                    "observed_to": observed_to,
                }
                connection.execute("DELETE FROM metadata")
                connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)",
                    metadata.items(),
                )
                connection.commit()
                integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
                if integrity != "ok":
                    raise RuntimeError(
                        f"Artifact vector index integrity check failed: {integrity}"
                    )
            if _canonical_change_counter(settings) != change_counter:
                raise RuntimeError(
                    "The artifact database changed during semantic index publication."
                )
            if os.name != "nt":
                temporary.chmod(0o600)
            _publish_snapshot_no_overwrite(temporary, segment)
            # Only this tiny manifest is new for unchanged vectors. Never clone the
            # old corpus merely to append or edit a small number of source anchors.
            with _connect(temporary) as manifest:
                manifest.executescript(
                    "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                    "CREATE TABLE vector_segments(ordinal INTEGER PRIMARY KEY, path TEXT NOT NULL, "
                    "bytes INTEGER NOT NULL, sha256 TEXT NOT NULL);"
                )
                manifest.executemany("INSERT INTO metadata VALUES (?, ?)", metadata.items())
                manifest.executemany("INSERT INTO vector_segments VALUES (?, ?, ?, ?)",
                                     ((i, path.name, *_segment_binding(path))
                                      for i, path in enumerate([*segments, segment])))
                manifest.commit()
            _publish_snapshot_no_overwrite(temporary, snapshot)
            if publish_pointer:
                _publish_pointer(settings, snapshot)
                acknowledge_artifact_vector_changes(settings, change_counter)
        except BaseException:
            if temporary.exists():
                temporary.unlink()
            raise
    return ArtifactIndexResult(
        snapshot=str(snapshot),
        change_counter=change_counter,
        bursts=total_bursts,
        embedded_bursts=embedded,
        embedding_provider=provider.name,
        embedding_model=provider.model,
        embedding_fingerprint=provider_fingerprint,
        unchanged=False,
        embedded_updates=embedded_updates,
        reused_bursts=reused,
        removed_bursts=removed,
        eligible_artifacts=int(metadata["eligible_artifacts"]),
        indexed_artifacts=int(metadata["indexed_artifacts"]),
        excluded_artifacts=int(metadata["excluded_artifacts"]),
        empty_artifacts=int(metadata["empty_artifacts"]),
        failed_artifacts=int(metadata["failed_artifacts"]),
        missing_absolute_time=int(metadata["missing_absolute_time"]),
        ann_backend=metadata["ann_backend"],
        elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
    )


@query_stage("candidate_fetch")
def _fetch_ann_candidates(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    # IN pushes the selected IDs into each immutable segment's primary-key
    # lookup. A join can scan or materialize the complete UNION view instead.
    # Approximate order is unnecessary because full-vector scoring sorts next.
    return connection.execute(
        "SELECT b.* FROM bursts b WHERE b.burst_id IN "
        "(SELECT burst_id FROM query_ann_candidates)"
    ).fetchall()


@query_stage("artifact_semantic")
def search_artifact_vectors(
    settings: Settings,
    query: str,
    scope: ArtifactScope,
    limit: int,
    *,
    index_path: Path | None = None,
    expected_change_counter: int | None = None,
    force_exact: bool = False,
) -> ArtifactVectorSearchResult:
    current = index_path or current_artifact_index_path(settings)
    if current is None:
        return ArtifactVectorSearchResult()
    provider = _query_provider(
        settings.embedding_provider,
        settings.embedding_model,
        settings.semantic_dimensions,
    )
    metadata = _metadata(current)
    if not settings.artifact_db.is_file():
        return ArtifactVectorSearchResult(available=True, stale=True)
    canonical_counter = (
        expected_change_counter
        if expected_change_counter is not None
        else _canonical_change_counter(settings)
    )
    if (
        int(metadata.get("artifact_change_counter", "-1"))
        != canonical_counter
        or metadata.get("embedding_fingerprint") != fingerprint(provider)
    ):
        return ArtifactVectorSearchResult(available=True, stale=True)

    conditions = ["1 = 1"]
    parameters: list[Any] = []
    if scope.source is not None:
        conditions.append("b.source = ?")
        parameters.append(scope.source)
    if scope.source_instance is not None:
        conditions.append("b.source_instance = ?")
        parameters.append(scope.source_instance)
    meeting_scope = scope.entities == ("meeting",)
    if scope.entities:
        if meeting_scope:
            conditions.append("b.meeting_artifact_uri IS NOT NULL")
        else:
            placeholders = ", ".join("?" for _ in scope.entities)
            conditions.append(f"b.entity IN ({placeholders})")
            parameters.extend(scope.entities)
    if scope.parent is not None:
        parent = (
            parse_artifact_uri(scope.parent)[1]
            if scope.parent.startswith("artifact://")
            else scope.parent
        )
        conditions.append("b.parent_artifact_id = ?")
        parameters.append(parent)
    if scope.date_from is not None:
        conditions.append(
            "b.meeting_occurred_at >= ?" if meeting_scope else "b.started_at >= ?"
        )
        parameters.append(_utc_iso(scope.date_from))
    if scope.date_to is not None:
        # A burst is indivisible evidence. Require all of its records to fit
        # inside the requested time range before returning its combined text.
        conditions.append(
            "b.meeting_occurred_at <= ?" if meeting_scope else "b.ended_at <= ?"
        )
        parameters.append(_utc_iso(scope.date_to))
    query_vector = provider.embed(query)
    query_dense = _dense_query_vector(query_vector, provider.dimensions)
    backend = "exact"
    blocks_read = 0
    vectors_scored = 0
    budget_exhausted = False
    prescored: list[tuple[float, sqlite3.Row]] | None = None
    with _connect(current, read_only=True) as connection:
        total = int(
            connection.execute(
                "SELECT count(*) FROM vector_candidates b WHERE "
                + " AND ".join(conditions),
                parameters,
            ).fetchone()[0]
        )
        rows: list[sqlite3.Row] = []
        ann_query = quantized_vector(
            query_vector,
            int(metadata.get("embedding_dimensions", "0")),
        )
        if (
            not force_exact
            and metadata.get("ann_backend") == ANN_BACKEND
            and total > 2_000
            and ann_query
        ):
            candidate_limit = min(
                total,
                settings.ann_candidate_limit,
                max(2_000, math.ceil(math.sqrt(total)) * 40),
            )
            ann_cursor = connection.execute(
                "SELECT b.burst_id, b.ann_vector FROM vector_candidates b WHERE "
                + " AND ".join(conditions)
                + " AND b.ann_vector IS NOT NULL",
                parameters,
            )
            shortlist = quantized_shortlist(
                ann_cursor,
                ann_query,
                provider.dimensions,
                candidate_limit,
                block_size=max(8192, settings.vector_block_size),
            )
            connection.execute(
                "CREATE TEMP TABLE query_ann_candidates("
                "burst_id TEXT PRIMARY KEY, distance REAL NOT NULL) WITHOUT ROWID"
            )
            connection.executemany(
                "INSERT INTO query_ann_candidates VALUES (?, ?)",
                ((identity, distance) for distance, identity in shortlist),
            )
            rows = _fetch_ann_candidates(connection)
            if len(rows) >= min(total, 20):
                backend = ANN_BACKEND
        if backend != ANN_BACKEND:
            cursor = connection.execute(
                "SELECT b.* FROM bursts b WHERE b.vector_blob IS NOT NULL AND "
                + " AND ".join(conditions),
                parameters,
            )
            top: list[tuple[float, str, sqlite3.Row]] = []
            started = time.monotonic()
            top_size = max(1, min(limit, 100))
            while True:
                block = cursor.fetchmany(settings.vector_block_size)
                if not block:
                    break
                blocks_read += 1
                scores = _vector_block_scores(
                    query_vector,
                    query_dense,
                    block,
                    provider.dimensions,
                )
                for row, score in zip(block, scores):
                    if (
                        vectors_scored >= settings.vector_max_vectors
                        or time.monotonic() - started >= settings.vector_max_seconds
                    ):
                        budget_exhausted = True
                        break
                    vectors_scored += 1
                    item = (score, str(row["burst_id"]), row)
                    if len(top) < top_size:
                        heapq.heappush(top, item)
                    elif item[:2] > top[0][:2]:
                        heapq.heapreplace(top, item)
                if budget_exhausted:
                    break
            prescored = [
                (score, row)
                for score, _identity, row in sorted(top, reverse=True)
            ]
            backend = (
                "exact-small-corpus"
                if total <= 2_000
                else "exact-budgeted"
            )
    if prescored is None:
        scored = sorted(
            zip(
                _vector_block_scores(
                    query_vector,
                    query_dense,
                    rows,
                    provider.dimensions,
                ),
                rows,
            ),
            key=lambda item: (item[0], str(item[1]["burst_id"])),
            reverse=True,
        )
        vectors_scored = len(rows)
        blocks_read = (len(rows) + settings.vector_block_size - 1) // settings.vector_block_size
    else:
        scored = prescored
    hits: list[ArtifactSearchHit] = []
    with connect_artifact_db(settings.artifact_db, read_only=True) as canonical:
        for score, row in scored:
            if score <= 0 or len(hits) >= max(1, min(limit, 100)):
                continue
            dependency_uris = tuple(json.loads(str(row["dependencies_json"])))
            dependency_ids = [parse_artifact_uri(value)[1] for value in dependency_uris]
            placeholders = ", ".join("?" for _ in dependency_ids)
            current_rows = (
                canonical.execute(
                    "SELECT * FROM artifacts WHERE artifact_id IN ("
                    + placeholders
                    + ") AND deleted_at IS NULL AND redacted_at IS NULL AND "
                    + active_ancestor_predicate("artifacts"),
                    dependency_ids,
                ).fetchall()
                if dependency_ids
                else []
            )
            current_by_id = {
                str(current_row["artifact_id"]): current_row
                for current_row in current_rows
            }
            if len(current_by_id) != len(dependency_ids):
                continue
            current_digest = hashlib.sha256(
                "\n".join(
                    f"{artifact_id}:{current_by_id[artifact_id]['last_event_id']}:"
                    f"{hashlib.sha256(str(current_by_id[artifact_id]['text_content']).encode('utf-8')).hexdigest()}"
                    for artifact_id in dependency_ids
                ).encode("utf-8")
            ).hexdigest()
            if current_digest != str(row["dependency_digest"]):
                continue
            anchor_uri = str(row["anchor_artifact_uri"])
            entity, artifact_id = parse_artifact_uri(anchor_uri)
            anchor = current_by_id.get(artifact_id)
            if anchor is None:
                continue
            source_text = str(anchor["text_content"])
            if str(row["representation_kind"]) in {"base", "context"}:
                segment_start = min(int(row["segment_start"]), len(source_text))
                segment_end = min(int(row["segment_end"]), len(source_text))
                excerpt = source_text[segment_start:segment_end]
            else:
                excerpt, segment_start, segment_end = _bounded_search_span(
                    source_text, query
                )
            hits.append(
                ArtifactSearchHit(
                    artifact_id=artifact_id,
                    artifact_uri=anchor_uri,
                    entity=entity,
                    source=str(row["source"]),
                    source_instance=str(row["source_instance"]),
                    title=str(row["parent_title"]),
                    text=excerpt,
                    author_name=str(anchor["author_name"]),
                    occurred_at=anchor["occurred_at"],
                    score=score,
                    evidence_class="burst",
                    segment_id=str(row["burst_id"]),
                    segment_start=segment_start,
                    segment_end=segment_end,
                    anchor_artifact_uri=anchor_uri,
                    meeting_artifact_uri=(
                        str(row["meeting_artifact_uri"])
                        if row["meeting_artifact_uri"] else None
                    ),
                    continuation=bool(
                        segment_start > 0 or segment_end < len(source_text)
                    ),
                )
            )
    return ArtifactVectorSearchResult(
        hits=hits,
        available=True,
        stale=False,
        backend=backend,
        candidate_count=(len(rows) if prescored is None else vectors_scored),
        vectors_scored=vectors_scored,
        blocks_read=blocks_read,
        budget_exhausted=budget_exhausted,
    )

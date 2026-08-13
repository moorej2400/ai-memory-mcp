from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_memory_mcp.audit import file_lock
from ai_memory_mcp.config import Settings
from ai_memory_mcp.embedding import fingerprint, resolve_provider
from ai_memory_mcp.index import decode_vector, encode_vector
from ai_memory_mcp.models import ArtifactIndexResult
from ai_memory_mcp.text import cosine_sparse

from .bursts import group_bursts
from .identity import artifact_uri, parse_artifact_uri
from .models import (
    ArtifactBurst,
    ArtifactBurstRecord,
    ArtifactScope,
    ArtifactSearchHit,
    ArtifactVectorSearchResult,
)
from .schema import connect_artifact_db

ARTIFACT_VECTOR_SCHEMA_VERSION = 1


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro",
            uri=True,
        )
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
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


def _load_records(settings: Settings) -> tuple[int, list[ArtifactBurstRecord]]:
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
        rows = connection.execute(
            """
            SELECT child.*, parent.title AS parent_title,
                   parent.payload_json AS parent_payload_json,
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
            WHERE child.entity IN ('message', 'transcript-cue')
              AND child.deleted_at IS NULL AND child.redacted_at IS NULL
              AND parent.deleted_at IS NULL AND parent.redacted_at IS NULL
              AND child.occurred_at IS NOT NULL
            ORDER BY child.parent_artifact_id, child.occurred_at,
                     child.artifact_id
            """
        ).fetchall()
        for row in rows:
            classification, reactions, attachment = _payload_signals(
                str(row["payload_json"])
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
    return change_counter, records


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
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
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            record_count INTEGER NOT NULL,
            text_content TEXT NOT NULL,
            embed INTEGER NOT NULL CHECK(embed IN (0, 1)),
            vector_blob BLOB
        );
        CREATE INDEX bursts_scope_idx
            ON bursts(source, source_instance, entity, started_at, burst_id);
        """
    )


def _insert_burst(
    connection: sqlite3.Connection,
    burst: ArtifactBurst,
    vector_blob: bytes | None,
) -> None:
    connection.execute(
        """
        INSERT INTO bursts VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
            _utc_iso(burst.started_at),
            _utc_iso(burst.ended_at),
            burst.record_count,
            burst.text,
            int(burst.embed),
            vector_blob,
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
    os.replace(temporary, pointer)


def build_artifact_vector_index(
    settings: Settings,
    force: bool = False,
) -> ArtifactIndexResult:
    started = time.perf_counter()
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
        change_counter = _canonical_change_counter(settings)
        current = current_artifact_index_path(settings)
        if current is not None and not force:
            metadata = _metadata(current)
            if (
                int(metadata.get("artifact_change_counter", "-1"))
                == change_counter
                and metadata.get("embedding_fingerprint") == provider_fingerprint
            ):
                return ArtifactIndexResult(
                    snapshot=str(current),
                    change_counter=change_counter,
                    bursts=int(metadata.get("bursts", "0")),
                    embedded_bursts=int(metadata.get("embedded_bursts", "0")),
                    embedding_provider=provider.name,
                    embedding_model=provider.model,
                    embedding_fingerprint=provider_fingerprint,
                    unchanged=True,
                    elapsed_ms=round(
                        (time.perf_counter() - started) * 1000,
                        3,
                    ),
                )

        change_counter, records = _load_records(settings)
        bursts = group_bursts(records)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        snapshot = settings.state_dir / (
            f"artifact-index-{stamp}-{os.getpid()}.sqlite"
        )
        with _connect(snapshot) as connection:
            _create_schema(connection)
            embedded = 0
            for burst in bursts:
                vector_blob = None
                if burst.embed:
                    vector_blob = encode_vector(provider.embed(burst.text))
                    embedded += 1
                _insert_burst(connection, burst, vector_blob)
            metadata = {
                "schema_version": str(ARTIFACT_VECTOR_SCHEMA_VERSION),
                "built_at": datetime.now(timezone.utc).isoformat(),
                "artifact_change_counter": str(change_counter),
                "embedding_provider": provider.name,
                "embedding_model": provider.model,
                "embedding_dimensions": str(provider.dimensions),
                "embedding_fingerprint": provider_fingerprint,
                "bursts": str(len(bursts)),
                "embedded_bursts": str(embedded),
            }
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
            snapshot.chmod(0o600)
        _publish_pointer(settings, snapshot)
    return ArtifactIndexResult(
        snapshot=str(snapshot),
        change_counter=change_counter,
        bursts=len(bursts),
        embedded_bursts=embedded,
        embedding_provider=provider.name,
        embedding_model=provider.model,
        embedding_fingerprint=provider_fingerprint,
        unchanged=False,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
    )


def search_artifact_vectors(
    settings: Settings,
    query: str,
    scope: ArtifactScope,
    limit: int,
) -> ArtifactVectorSearchResult:
    current = current_artifact_index_path(settings)
    if current is None:
        return ArtifactVectorSearchResult()
    provider = resolve_provider(
        settings.embedding_provider,
        model=settings.embedding_model,
        dimensions=settings.semantic_dimensions,
    )
    metadata = _metadata(current)
    if not settings.artifact_db.is_file():
        return ArtifactVectorSearchResult(available=True, stale=True)
    if (
        int(metadata.get("artifact_change_counter", "-1"))
        != _canonical_change_counter(settings)
        or metadata.get("embedding_fingerprint") != fingerprint(provider)
    ):
        return ArtifactVectorSearchResult(available=True, stale=True)

    conditions = ["vector_blob IS NOT NULL"]
    parameters: list[Any] = []
    if scope.source is not None:
        conditions.append("source = ?")
        parameters.append(scope.source)
    if scope.source_instance is not None:
        conditions.append("source_instance = ?")
        parameters.append(scope.source_instance)
    if scope.entities:
        placeholders = ", ".join("?" for _ in scope.entities)
        conditions.append(f"entity IN ({placeholders})")
        parameters.extend(scope.entities)
    if scope.parent is not None:
        parent = (
            parse_artifact_uri(scope.parent)[1]
            if scope.parent.startswith("artifact://")
            else scope.parent
        )
        conditions.append("parent_artifact_id = ?")
        parameters.append(parent)
    if scope.date_from is not None:
        conditions.append("started_at >= ?")
        parameters.append(_utc_iso(scope.date_from))
    if scope.date_to is not None:
        conditions.append("started_at <= ?")
        parameters.append(_utc_iso(scope.date_to))
    with _connect(current, read_only=True) as connection:
        rows = connection.execute(
            "SELECT * FROM bursts WHERE " + " AND ".join(conditions),
            parameters,
        ).fetchall()
    query_vector = provider.embed(query)
    scored = sorted(
        (
            (cosine_sparse(query_vector, decode_vector(row["vector_blob"])), row)
            for row in rows
        ),
        key=lambda item: (item[0], str(item[1]["burst_id"])),
        reverse=True,
    )
    hits: list[ArtifactSearchHit] = []
    for score, row in scored[: max(1, min(limit, 100))]:
        if score <= 0:
            continue
        entity, artifact_id = parse_artifact_uri(str(row["first_artifact_uri"]))
        hits.append(
            ArtifactSearchHit(
                artifact_id=artifact_id,
                artifact_uri=str(row["first_artifact_uri"]),
                entity=entity,
                source=str(row["source"]),
                source_instance=str(row["source_instance"]),
                title=str(row["parent_title"]),
                text=str(row["text_content"])[:5000],
                author_name=str(row["author_name"]),
                occurred_at=row["started_at"],
                score=score,
                evidence_class="burst",
            )
        )
    return ArtifactVectorSearchResult(
        hits=hits,
        available=True,
        stale=False,
    )

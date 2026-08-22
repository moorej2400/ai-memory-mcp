from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_memory_mcp.audit import file_lock
from ai_memory_mcp.ann import (
    ANN_BACKEND,
    available as ann_available,
    bucket_clause,
    multiprobe_buckets,
    vector_buckets,
)
from ai_memory_mcp.config import Settings
from ai_memory_mcp.embedding import fingerprint, resolve_provider
from ai_memory_mcp.index import decode_vector, encode_vector
from ai_memory_mcp.models import ArtifactIndexResult
from ai_memory_mcp.text import cosine_sparse

from .bursts import group_bursts
from .context import active_ancestor_predicate
from .identity import artifact_uri, parse_artifact_uri
from .models import (
    ArtifactBurst,
    ArtifactBurstRecord,
    ArtifactScope,
    ArtifactSearchHit,
    ArtifactVectorSearchResult,
)
from .schema import connect_artifact_db, require_local_database_path

ARTIFACT_VECTOR_SCHEMA_VERSION = 2


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    path = require_local_database_path(path)
    if read_only:
        connection = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro",
            uri=True,
        )
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
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


def _load_records(
    settings: Settings,
    previous_counter: int | None = None,
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
        dirty_parents = {
            str(row[0])
            for row in connection.execute(
                "SELECT parent_artifact_id FROM artifact_vector_dirty "
                "WHERE change_counter <= ?",
                (change_counter,),
            )
        }
        selected_parents: set[str] | None = None
        if previous_counter is not None and previous_counter != change_counter:
            # A missing queue entry indicates a legacy or damaged queue. A full
            # rebuild is safer than publishing a partially updated snapshot.
            selected_parents = dirty_parents or None
        if selected_parents is not None:
            connection.execute(
                "CREATE TEMP TABLE selected_vector_parents("
                "parent_artifact_id TEXT PRIMARY KEY)"
            )
            connection.executemany(
                "INSERT INTO selected_vector_parents VALUES (?)",
                ((value,) for value in sorted(selected_parents)),
            )
        parent_filter = (
            "AND EXISTS(SELECT 1 FROM selected_vector_parents AS selected "
            "WHERE selected.parent_artifact_id = child.parent_artifact_id)"
            if selected_parents is not None
            else ""
        )
        rows = connection.execute(
            f"""
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
              AND {active_ancestor_predicate("child")}
              AND child.occurred_at IS NOT NULL
              {parent_filter}
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
    return change_counter, selected_parents, records


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
            content_sha256 TEXT NOT NULL,
            embed INTEGER NOT NULL CHECK(embed IN (0, 1)),
            vector_blob BLOB
        );
        CREATE INDEX bursts_scope_idx
            ON bursts(source, source_instance, entity, started_at, burst_id);
        CREATE TABLE burst_ann_buckets (
            burst_id TEXT NOT NULL REFERENCES bursts(burst_id) ON DELETE CASCADE,
            band INTEGER NOT NULL,
            bucket INTEGER NOT NULL,
            PRIMARY KEY(burst_id, band)
        );
        CREATE INDEX burst_ann_lookup_idx
            ON burst_ann_buckets(band, bucket, burst_id);
        """
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
        "started_at": _utc_iso(burst.started_at),
        "ended_at": _utc_iso(burst.ended_at),
        "record_count": burst.record_count,
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
        INSERT INTO bursts VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
            content_sha256,
            int(burst.embed),
            vector_blob,
        ),
    )
    if vector is not None:
        connection.executemany(
            "INSERT INTO burst_ann_buckets(burst_id, band, bucket) "
            "VALUES (?, ?, ?)",
            (
                (burst.burst_id, band, bucket)
                for band, bucket in vector_buckets(
                    vector,
                    semantic_dimensions,
                )
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


def _publish_snapshot_no_overwrite(temporary: Path, snapshot: Path) -> None:
    os.link(temporary, snapshot)
    temporary.unlink()


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
        )
        previous_counter = (
            int(current_metadata.get("artifact_change_counter", "-1"))
            if reuse_snapshot
            else None
        )
        change_counter, selected_parents, records = _load_records(
            settings,
            previous_counter,
        )
        bursts = group_bursts(records)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        snapshot = settings.state_dir / (
            f"artifact-index-{stamp}-{os.getpid()}.sqlite"
        )
        temporary = settings.state_dir / (
            f".artifact-index.partial-{os.getpid()}-{time.time_ns()}.sqlite"
        )
        try:
            if reuse_snapshot:
                with _connect(current, read_only=True) as source, _connect(
                    temporary
                ) as target:
                    source.backup(target)
            with _connect(temporary) as connection:
                if not reuse_snapshot:
                    _create_schema(connection)
                if selected_parents is not None:
                    connection.execute(
                        "CREATE TEMP TABLE selected_vector_parents("
                        "parent_artifact_id TEXT PRIMARY KEY)"
                    )
                    connection.executemany(
                        "INSERT INTO selected_vector_parents VALUES (?)",
                        ((value,) for value in sorted(selected_parents)),
                    )
                existing_sql = "SELECT burst_id, content_sha256 FROM bursts"
                if selected_parents is not None:
                    existing_sql += (
                        " WHERE parent_artifact_id IN ("
                        "SELECT parent_artifact_id FROM selected_vector_parents)"
                    )
                existing = {
                    str(row["burst_id"]): str(row["content_sha256"])
                    for row in connection.execute(existing_sql)
                }
                initial_total = int(
                    connection.execute("SELECT count(*) FROM bursts").fetchone()[0]
                )
                rebuild_ann = bool(
                    reuse_snapshot
                    and current_metadata.get("ann_backend")
                    != target_ann_backend
                )
                desired_ids = {burst.burst_id for burst in bursts}
                removed = 0
                for burst_id in set(existing) - desired_ids:
                    connection.execute(
                        "DELETE FROM bursts WHERE burst_id = ?",
                        (burst_id,),
                    )
                    removed += 1
                embedded_updates = 0
                reused = (
                    initial_total - len(existing)
                    if selected_parents is not None
                    else 0
                )
                for burst in bursts:
                    digest = _burst_digest(burst)
                    if existing.get(burst.burst_id) == digest:
                        reused += 1
                        continue
                    connection.execute(
                        "DELETE FROM bursts WHERE burst_id = ?",
                        (burst.burst_id,),
                    )
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
                embedded = int(
                    connection.execute(
                        "SELECT count(*) FROM bursts "
                        "WHERE vector_blob IS NOT NULL"
                    ).fetchone()[0]
                )
                total_bursts = int(
                    connection.execute("SELECT count(*) FROM bursts").fetchone()[0]
                )
                if rebuild_ann:
                    connection.execute("DELETE FROM burst_ann_buckets")
                    for row in connection.execute(
                        "SELECT burst_id, vector_blob FROM bursts "
                        "WHERE vector_blob IS NOT NULL"
                    ):
                        vector = decode_vector(bytes(row["vector_blob"]))
                        connection.executemany(
                            "INSERT INTO burst_ann_buckets"
                            "(burst_id, band, bucket) VALUES (?, ?, ?)",
                            (
                                (str(row["burst_id"]), band, bucket)
                                for band, bucket in vector_buckets(
                                    vector,
                                    provider.dimensions,
                                )
                            ),
                        )
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
        ann_backend=metadata["ann_backend"],
        elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
    )


def search_artifact_vectors(
    settings: Settings,
    query: str,
    scope: ArtifactScope,
    limit: int,
    *,
    index_path: Path | None = None,
    expected_change_counter: int | None = None,
) -> ArtifactVectorSearchResult:
    current = index_path or current_artifact_index_path(settings)
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

    conditions = ["b.vector_blob IS NOT NULL"]
    parameters: list[Any] = []
    if scope.source is not None:
        conditions.append("b.source = ?")
        parameters.append(scope.source)
    if scope.source_instance is not None:
        conditions.append("b.source_instance = ?")
        parameters.append(scope.source_instance)
    if scope.entities:
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
        conditions.append("b.started_at >= ?")
        parameters.append(_utc_iso(scope.date_from))
    if scope.date_to is not None:
        # A burst is indivisible evidence. Require all of its records to fit
        # inside the requested time range before returning its combined text.
        conditions.append("b.ended_at <= ?")
        parameters.append(_utc_iso(scope.date_to))
    query_vector = provider.embed(query)
    backend = "exact"
    with _connect(current, read_only=True) as connection:
        total = int(
            connection.execute(
                "SELECT count(*) FROM bursts b WHERE "
                + " AND ".join(conditions),
                parameters,
            ).fetchone()[0]
        )
        rows: list[sqlite3.Row] = []
        buckets = vector_buckets(
            query_vector,
            int(metadata.get("embedding_dimensions", "0")),
        )
        if (
            metadata.get("ann_backend") == ANN_BACKEND
            and total > 2_000
            and buckets
        ):
            match_clause, match_parameters = bucket_clause(
                multiprobe_buckets(buckets),
                table_alias="a",
            )
            candidate_limit = min(2_000, max(1_000, limit * 80))
            rows = connection.execute(
                "SELECT b.*, count(*) AS ann_matches "
                "FROM burst_ann_buckets a "
                "JOIN bursts b USING(burst_id) "
                f"WHERE ({match_clause}) AND "
                + " AND ".join(conditions)
                + " GROUP BY b.burst_id "
                "ORDER BY ann_matches DESC, b.burst_id LIMIT ?",
                [*match_parameters, *parameters, candidate_limit],
            ).fetchall()
            if len(rows) >= min(total, 20):
                backend = ANN_BACKEND
        if backend != ANN_BACKEND:
            rows = connection.execute(
                "SELECT b.* FROM bursts b WHERE "
                + " AND ".join(conditions),
                parameters,
            ).fetchall()
            backend = "exact-small-corpus" if total <= 2_000 else "exact-fallback"
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
        backend=backend,
        candidate_count=len(rows),
    )

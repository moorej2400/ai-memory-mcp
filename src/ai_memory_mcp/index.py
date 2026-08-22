from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .audit import append_event, file_lock
from .ann import (
    ANN_BACKEND,
    available as ann_available,
    bucket_clause,
    multiprobe_buckets,
    vector_buckets,
)
from .config import Settings
from .embedding import fingerprint, resolve_provider
from .models import MemoryChunk, MemoryDocument, ScopeFilter
from .text import chunk_document, parse_document

SCHEMA_VERSION = 5
_VECTOR_ITEM = struct.Struct("<He")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS documents (
            memory_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL,
            root_scope TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            updated TEXT NOT NULL,
            review_after TEXT NOT NULL,
            related_json TEXT NOT NULL,
            identifiers_json TEXT NOT NULL,
            projects_json TEXT NOT NULL,
            repos_json TEXT NOT NULL,
            tools_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            mtime_ns INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL REFERENCES documents(memory_id) ON DELETE CASCADE,
            source_id TEXT NOT NULL,
            path TEXT NOT NULL,
            title TEXT NOT NULL,
            heading TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            text TEXT NOT NULL,
            vector_blob BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_memory ON chunks(memory_id, ordinal);
        CREATE TABLE IF NOT EXISTS chunk_ann_buckets (
            chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
            band INTEGER NOT NULL,
            bucket INTEGER NOT NULL,
            PRIMARY KEY(chunk_id, band)
        );
        CREATE INDEX IF NOT EXISTS idx_chunk_ann_lookup
            ON chunk_ann_buckets(band, bucket, chunk_id);
        CREATE INDEX IF NOT EXISTS idx_documents_scope
            ON documents(source_id, root_scope, status, scope_kind, scope_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            chunk_id UNINDEXED,
            title,
            heading,
            text,
            identifiers,
            tokenize='unicode61 remove_diacritics 2 tokenchars ''_-:/#'''
        );
        """
    )


def _eligible_markdown(root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in root.rglob("*.md"):
        relative = path.relative_to(root)
        parts = {part.casefold() for part in relative.parts}
        if any(part.startswith(".") for part in relative.parts):
            continue
        if "restricted" in parts or ".trash" in parts:
            continue
        paths.append(path)
    return sorted(paths, key=lambda item: item.as_posix().casefold())


def _insert_document(
    connection: sqlite3.Connection,
    document: MemoryDocument,
    chunks: list[MemoryChunk],
    *,
    semantic_dimensions: int,
) -> None:
    values = asdict(document)
    connection.execute(
        """
        INSERT INTO documents VALUES (
            :memory_id, :source_id, :path, :title, :body, :status, :root_scope,
            :scope_kind, :scope_id, :updated, :review_after, :related_json,
            :identifiers_json, :projects_json, :repos_json, :tools_json,
            :content_hash, :mtime_ns
        )
        """,
        {
            **values,
            "related_json": json.dumps(document.related),
            "identifiers_json": json.dumps(document.identifiers),
            "projects_json": json.dumps(document.projects),
            "repos_json": json.dumps(document.repos),
            "tools_json": json.dumps(document.tools),
        },
    )
    identifiers = " ".join(document.identifiers)
    for chunk in chunks:
        connection.execute(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chunk.chunk_id,
                chunk.memory_id,
                chunk.source_id,
                chunk.path,
                chunk.title,
                chunk.heading,
                chunk.ordinal,
                chunk.text,
                encode_vector(chunk.vector),
            ),
        )
        connection.executemany(
            "INSERT INTO chunk_ann_buckets(chunk_id, band, bucket) "
            "VALUES (?, ?, ?)",
            (
                (chunk.chunk_id, band, bucket)
                for band, bucket in vector_buckets(
                    chunk.vector,
                    semantic_dimensions,
                )
            ),
        )
        connection.execute(
            "INSERT INTO chunks_fts VALUES (?, ?, ?, ?, ?)",
            (
                chunk.chunk_id,
                chunk.title,
                chunk.heading,
                chunk.text,
                identifiers,
            ),
        )


def encode_vector(vector: dict[int, float]) -> bytes:
    return b"".join(
        _VECTOR_ITEM.pack(index, value)
        for index, value in sorted(vector.items())
    )


def decode_vector(payload: bytes) -> dict[int, float]:
    if len(payload) % _VECTOR_ITEM.size:
        raise ValueError("Stored semantic vector has an invalid byte length.")
    return {
        index: value
        for index, value in _VECTOR_ITEM.iter_unpack(payload)
    }


def _remove_document(connection: sqlite3.Connection, memory_id: str) -> None:
    chunk_ids = [
        row["chunk_id"]
        for row in connection.execute(
            "SELECT chunk_id FROM chunks WHERE memory_id = ?", (memory_id,)
        )
    ]
    for chunk_id in chunk_ids:
        connection.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,))
    connection.execute("DELETE FROM documents WHERE memory_id = ?", (memory_id,))


def current_index_path(settings: Settings) -> Path | None:
    from .generation import generation_component_path

    generated = generation_component_path(settings, "markdown_snapshot")
    if generated is not None and _schema_matches(generated):
        return generated
    if settings.pointer_path.exists():
        try:
            payload = json.loads(settings.pointer_path.read_text(encoding="utf-8"))
            candidate = settings.state_dir / payload["snapshot"]
            if candidate.exists() and _schema_matches(candidate):
                return candidate
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    snapshots = sorted(settings.state_dir.glob("index-*.sqlite"), reverse=True)
    return next((path for path in snapshots if _schema_matches(path)), None)


def _schema_matches(path: Path) -> bool:
    try:
        with _connect(path, read_only=True) as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            return bool(row and int(row["value"]) == SCHEMA_VERSION)
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return False


def _index_metadata_value(path: Path, key: str) -> str | None:
    try:
        with _connect(path, read_only=True) as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row else None
    except (OSError, sqlite3.DatabaseError):
        return None


def _build_index(
    settings: Settings,
    *,
    force: bool = False,
    publish_pointer: bool = True,
) -> dict[str, object]:
    started = time.perf_counter()
    sources = settings.memory_sources
    missing = [source for source in sources if not source.root.is_dir()]
    if missing:
        unavailable = ", ".join(
            f"{source.source_id}={source.root}" for source in missing
        )
        raise FileNotFoundError(f"Memory source not found: {unavailable}")
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    with file_lock(
        settings.state_dir / "index.lock",
        settings.index_lock_timeout_seconds,
    ) as lock_wait_ms:
        current = current_index_path(settings)
        provider = resolve_provider(
            settings.embedding_provider,
            model=settings.embedding_model,
            dimensions=settings.semantic_dimensions,
        )
        provider_fingerprint = fingerprint(provider)
        target_ann_backend = ANN_BACKEND if ann_available() else "exact"
        if current and _index_metadata_value(
            current, "embedding_fingerprint"
        ) != provider_fingerprint:
            # A provider change invalidates every stored vector. This must run
            # before the mtime fast path below: unchanged files still need new
            # vectors when the embedding provider changes.
            current = None
        eligible_by_source = {
            source.source_id: _eligible_markdown(source.root)
            for source in sources
        }
        if current and not force:
            with _connect(current, read_only=True) as connection:
                existing_mtimes = {
                    row["path"]: int(row["mtime_ns"])
                    for row in connection.execute(
                        "SELECT path, mtime_ns FROM documents"
                    )
                }
                counts = connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM documents) AS documents,
                        (SELECT COUNT(*) FROM chunks) AS chunks
                    """
                ).fetchone()
            current_mtimes = {
                f"{source.source_id}/{path.relative_to(source.root).as_posix()}":
                    path.stat().st_mtime_ns
                for source in sources
                for path in eligible_by_source[source.source_id]
            }
            if (
                current_mtimes == existing_mtimes
                and _index_metadata_value(current, "ann_backend")
                == target_ann_backend
            ):
                source_stats = [
                    {
                        "source_id": source.source_id,
                        "files": len(eligible_by_source[source.source_id]),
                        "added": 0,
                        "changed": 0,
                        "unchanged": len(
                            eligible_by_source[source.source_id]
                        ),
                        "parse_errors": 0,
                        "elapsed_ms": 0.0,
                    }
                    for source in sources
                ]
                return {
                    "snapshot": str(current),
                    "documents": int(counts["documents"]),
                    "chunks": int(counts["chunks"]),
                    "added": 0,
                    "changed": 0,
                    "unchanged": len(current_mtimes),
                    "removed": 0,
                    "parse_errors": [],
                    "_source_stats": source_stats,
                    "_lock_wait_ms": lock_wait_ms,
                    "elapsed_ms": round(
                        (time.perf_counter() - started) * 1000,
                        2,
                    ),
                }
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        snapshot = settings.state_dir / f"index-{stamp}-{os.getpid()}.sqlite"
        if current and not force and _schema_matches(current):
            # SQLite backup produces a consistent new snapshot while preserving
            # every previously published snapshot for recovery and comparison.
            with _connect(current, read_only=True) as source, _connect(snapshot) as target:
                source.backup(target)
        with _connect(snapshot) as connection:
            _create_schema(connection)
            rebuild_ann = bool(
                current
                and _index_metadata_value(current, "ann_backend")
                != target_ann_backend
            )
            existing = {
                row["path"]: (
                    row["memory_id"],
                    row["content_hash"],
                    row["mtime_ns"],
                )
                for row in connection.execute(
                    "SELECT path, memory_id, content_hash, mtime_ns FROM documents"
                )
            }
            seen: set[str] = set()
            added = changed = unchanged = removed = 0
            parse_errors: list[dict[str, str]] = []
            source_stats: list[dict[str, object]] = []
            for source in sources:
                source_started = time.perf_counter()
                source_added = added
                source_changed = changed
                source_unchanged = unchanged
                source_error_count = len(parse_errors)
                eligible_paths = eligible_by_source[source.source_id]
                for path in eligible_paths:
                    relative = path.relative_to(source.root).as_posix()
                    source_path = f"{source.source_id}/{relative}"
                    seen.add(source_path)
                    old = existing.get(source_path)
                    if (
                        old
                        and old[2] == path.stat().st_mtime_ns
                        and not force
                    ):
                        unchanged += 1
                        continue
                    try:
                        document = parse_document(
                            path,
                            source.root,
                            source.source_id,
                        )
                    except (OSError, UnicodeError, ValueError) as exc:
                        parse_errors.append(
                            {
                                "source_id": source.source_id,
                                "path": relative,
                                "error": str(exc),
                            }
                        )
                        continue
                    if old and old[1] == document.content_hash and not force:
                        if old[2] != document.mtime_ns:
                            connection.execute(
                                "UPDATE documents SET mtime_ns = ? WHERE path = ?",
                                (document.mtime_ns, source_path),
                            )
                        unchanged += 1
                        continue
                    # Check the new ID before removing the previous record. This
                    # preserves the last valid indexed version after a collision.
                    row = connection.execute(
                        "SELECT source_id, path FROM documents "
                        "WHERE memory_id = ? AND path <> ?",
                        (document.memory_id, source_path),
                    ).fetchone()
                    if row:
                        parse_errors.append(
                            {
                                "source_id": source.source_id,
                                "path": relative,
                                "error": (
                                    f"memory_id '{document.memory_id}' "
                                    f"already exists in source "
                                    f"'{row['source_id']}' at {row['path']}"
                                ),
                            }
                        )
                        continue
                    if old:
                        _remove_document(connection, old[0])
                        changed += 1
                    else:
                        added += 1
                    _insert_document(
                        connection,
                        document,
                        chunk_document(document, provider),
                        semantic_dimensions=provider.dimensions,
                    )
                source_stats.append(
                    {
                        "source_id": source.source_id,
                        "files": len(eligible_paths),
                        "added": added - source_added,
                        "changed": changed - source_changed,
                        "unchanged": unchanged - source_unchanged,
                        "parse_errors": len(parse_errors) - source_error_count,
                        "elapsed_ms": round(
                            (time.perf_counter() - source_started) * 1000,
                            3,
                        ),
                    }
                )
            for path, (memory_id, _, _) in existing.items():
                if path not in seen:
                    _remove_document(connection, memory_id)
                    removed += 1
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM documents) AS documents,
                    (SELECT COUNT(*) FROM chunks) AS chunks
                """
            ).fetchone()
            if rebuild_ann:
                connection.execute("DELETE FROM chunk_ann_buckets")
                for row in connection.execute(
                    "SELECT chunk_id, vector_blob FROM chunks"
                ):
                    vector = decode_vector(bytes(row["vector_blob"]))
                    connection.executemany(
                        "INSERT INTO chunk_ann_buckets(chunk_id, band, bucket) "
                        "VALUES (?, ?, ?)",
                        (
                            (str(row["chunk_id"]), band, bucket)
                            for band, bucket in vector_buckets(
                                vector,
                                provider.dimensions,
                            )
                        ),
                    )
            metadata = {
                "schema_version": str(SCHEMA_VERSION),
                "built_at": _utc_now(),
                "memory_root": str(settings.memory_root.resolve()),
                "memory_sources": json.dumps(
                    [source.source_id for source in sources],
                    separators=(",", ":"),
                ),
                "semantic_dimensions": str(provider.dimensions),
                "embedding_provider": provider.name,
                "embedding_model": provider.model,
                "embedding_fingerprint": provider_fingerprint,
                "ann_backend": target_ann_backend,
                "documents": str(counts["documents"]),
                "chunks": str(counts["chunks"]),
            }
            connection.executemany(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                metadata.items(),
            )
            connection.commit()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"SQLite integrity check failed: {integrity}")
        if publish_pointer:
            _publish_index_pointer(settings, snapshot)
    return {
        "snapshot": str(snapshot),
        "documents": int(counts["documents"]),
        "chunks": int(counts["chunks"]),
        "added": added,
        "changed": changed,
        "unchanged": unchanged,
        "removed": removed,
        "parse_errors": parse_errors,
        "_source_stats": source_stats,
        "_lock_wait_ms": lock_wait_ms,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _publish_index_pointer(settings: Settings, snapshot: Path) -> None:
    pointer = {
        "snapshot": snapshot.name,
        "published_at": _utc_now(),
        "schema_version": SCHEMA_VERSION,
    }
    temporary = settings.pointer_path.with_name(
        f".{settings.pointer_path.name}.partial-{os.getpid()}-{time.time_ns()}"
    )
    temporary.write_text(json.dumps(pointer, indent=2), encoding="utf-8")
    os.replace(temporary, settings.pointer_path)


def build_index(
    settings: Settings,
    *,
    force: bool = False,
    publish_pointer: bool = True,
) -> dict[str, object]:
    started = time.perf_counter()
    append_event(
        settings,
        "index",
        "index_started",
        {
            "force": force,
            "source_ids": [
                source.source_id for source in settings.memory_sources
            ],
        },
    )
    try:
        result = _build_index(
            settings,
            force=force,
            publish_pointer=publish_pointer,
        )
    except Exception as exc:
        append_event(
            settings,
            "index",
            "index_failed",
            {
                "force": force,
                "elapsed_ms": round(
                    (time.perf_counter() - started) * 1000,
                    3,
                ),
                "error_type": type(exc).__name__,
                "error_sha256": hashlib.sha256(
                    str(exc).encode("utf-8")
                ).hexdigest(),
            },
        )
        raise
    source_stats = result.pop("_source_stats")
    lock_wait_ms = result.pop("_lock_wait_ms")
    telemetry_result = {
        key: value
        for key, value in result.items()
        if key != "parse_errors"
    }
    telemetry_result["parse_error_count"] = len(result.get("parse_errors", []))
    append_event(
        settings,
        "index",
        "index_completed",
        {
            **telemetry_result,
            "source_stats": source_stats,
            "lock_wait_ms": lock_wait_ms,
        },
    )
    return result


class MemoryIndex:
    def __init__(self, settings: Settings, path: Path | None = None):
        self.settings = settings
        self._vector_cache: dict[
            tuple[str | None, ...], list[tuple[sqlite3.Row, dict[int, float]]]
        ] = {}
        self.path = path or current_index_path(settings)
        if self.path is None:
            raise FileNotFoundError(
                "Memory index is not available. Call memory_sync before recall."
            )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with _connect(self.path, read_only=True) as connection:
            yield connection

    def metadata(self) -> dict[str, str]:
        with self.connection() as connection:
            return {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM metadata")
            }

    def canonical_stale(self) -> bool:
        """Return true when canonical Markdown differs from this snapshot."""
        try:
            current = {
                f"{source.source_id}/{path.relative_to(source.root).as_posix()}":
                    path.stat().st_mtime_ns
                for source in self.settings.memory_sources
                for path in _eligible_markdown(source.root)
            }
        except OSError:
            return True
        with self.connection() as connection:
            indexed = {
                str(row["path"]): int(row["mtime_ns"])
                for row in connection.execute(
                    "SELECT path, mtime_ns FROM documents"
                )
            }
        return current != indexed

    def document(
        self,
        identity: str,
        scope: ScopeFilter | None = None,
    ) -> dict[str, object] | None:
        where, parameters = scope_sql(scope or ScopeFilter(status=""))
        scope_clause = where.replace("WHERE", "AND", 1) if where else ""
        with self.connection() as connection:
            row = connection.execute(
                f"""
                SELECT d.* FROM documents d
                WHERE (
                    d.memory_id = ?
                    OR lower(d.path) = lower(?)
                    OR lower(d.title) = lower(?)
                )
                {scope_clause}
                ORDER BY
                    CASE
                        WHEN d.memory_id = ? THEN 0
                        WHEN d.path = ? THEN 1
                        WHEN lower(d.title) = lower(?) THEN 2
                        ELSE 3
                    END,
                    length(d.path)
                LIMIT 1
                """,
                (
                    identity,
                    identity,
                    identity,
                    *parameters,
                    identity,
                    identity,
                    identity,
                ),
            ).fetchone()
        return decode_document(row) if row else None

    def mentioned_documents(
        self,
        query: str,
        scope: ScopeFilter,
        *,
        limit: int = 3,
    ) -> list[dict[str, object]]:
        where, parameters = scope_sql(scope)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT d.* FROM documents d {where}",
                parameters,
            ).fetchall()
        query_text = query.casefold()
        matches: list[tuple[int, int, dict[str, object]]] = []
        for row in rows:
            document = decode_document(row)
            path = str(document["path"])
            identities = {
                str(document["memory_id"]),
                path,
                Path(path).stem,
                str(document["title"]),
            }
            matched = [
                (query_text.index(identity.casefold()), len(identity))
                for identity in identities
                if len(identity) >= 4 and identity.casefold() in query_text
            ]
            if matched:
                position, matched_length = min(
                    matched,
                    key=lambda item: (item[0], -item[1]),
                )
                matches.append((position, matched_length, document))
        matches.sort(
            key=lambda item: (
                item[0],
                -item[1],
                str(item[2]["title"]).casefold(),
            )
        )
        return [
            document
            for _, _, document in matches[: max(1, limit)]
        ]

    def scoped_identities(self, scope: ScopeFilter) -> set[str]:
        """Return normalized document identities that are inside one scope."""
        where, parameters = scope_sql(scope)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT d.* FROM documents d {where}",
                parameters,
            ).fetchall()
        identities: set[str] = set()
        for row in rows:
            document = decode_document(row)
            path = str(document["path"])
            identities.update(
                {
                    str(document["memory_id"]).casefold(),
                    path.casefold(),
                    Path(path).stem.casefold(),
                    str(document["title"]).casefold(),
                }
            )
            identities.update(
                str(value).casefold()
                for value in document["identifiers"]
            )
        return identities

    def all_vectors(
        self, scope: ScopeFilter
    ) -> list[tuple[sqlite3.Row, dict[int, float]]]:
        cache_key = (
            scope.source_id,
            scope.root_scope,
            scope.repository,
            scope.project,
            scope.ticket,
            scope.status,
            scope.path_prefix,
        )
        cached = self._vector_cache.get(cache_key)
        if cached is not None:
            return cached
        where, parameters = scope_sql(scope)
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*, d.status, d.root_scope, d.scope_kind, d.scope_id,
                       d.projects_json, d.repos_json, d.updated, d.review_after
                FROM chunks c JOIN documents d USING(memory_id)
                {where}
                """,
                parameters,
            ).fetchall()
        decoded = [
            (row, decode_vector(row["vector_blob"]))
            for row in rows
        ]
        # A published snapshot is immutable, so decoded vectors remain valid
        # until MemoryService replaces the engine after the next refresh.
        self._vector_cache[cache_key] = decoded
        return decoded

    def vector_candidates(
        self,
        scope: ScopeFilter,
        query_vector: dict[int, float],
        limit: int,
    ) -> tuple[list[tuple[sqlite3.Row, dict[int, float]]], str]:
        metadata = self.metadata()
        if metadata.get("ann_backend") != ANN_BACKEND:
            return self.all_vectors(scope), "exact-fallback"
        dimensions = int(metadata.get("semantic_dimensions", "0"))
        buckets = vector_buckets(query_vector, dimensions)
        if not buckets:
            return self.all_vectors(scope), "exact-fallback"
        where, scope_parameters = scope_sql(scope)
        scope_clause = where.replace("WHERE", "AND", 1) if where else ""
        with self.connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT count(*) FROM chunks c "
                    f"JOIN documents d USING(memory_id) {where}",
                    scope_parameters,
                ).fetchone()[0]
            )
            if total <= 2_000:
                return self.all_vectors(scope), "exact-small-corpus"
            match_clause, match_parameters = bucket_clause(
                multiprobe_buckets(buckets),
                table_alias="a",
            )
            candidate_limit = min(2_000, max(400, limit * 12))
            rows = connection.execute(
                f"""
                SELECT c.*, d.status, d.root_scope, d.scope_kind, d.scope_id,
                       d.projects_json, d.repos_json, d.updated, d.review_after,
                       count(*) AS ann_matches
                FROM chunk_ann_buckets a
                JOIN chunks c USING(chunk_id)
                JOIN documents d USING(memory_id)
                WHERE ({match_clause})
                {scope_clause}
                GROUP BY c.chunk_id
                ORDER BY ann_matches DESC, c.chunk_id
                LIMIT ?
                """,
                [*match_parameters, *scope_parameters, candidate_limit],
            ).fetchall()
        minimum = min(total, 20)
        if len(rows) < minimum:
            return self.all_vectors(scope), "exact-fallback"
        return (
            [(row, decode_vector(row["vector_blob"])) for row in rows],
            ANN_BACKEND,
        )

    def chunks_for_memories(
        self,
        memory_ids: list[str],
    ) -> dict[str, list[sqlite3.Row]]:
        if not memory_ids:
            return {}
        placeholders = ", ".join("?" for _ in memory_ids)
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM chunks
                WHERE memory_id IN ({placeholders})
                ORDER BY memory_id, ordinal
                """,
                memory_ids,
            ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {
            memory_id: [] for memory_id in memory_ids
        }
        for row in rows:
            grouped[str(row["memory_id"])].append(row)
        return grouped


def decode_document(row: sqlite3.Row) -> dict[str, object]:
    result = dict(row)
    for key in (
        "related_json",
        "identifiers_json",
        "projects_json",
        "repos_json",
        "tools_json",
    ):
        result[key.removesuffix("_json")] = json.loads(result.pop(key))
    return result


def scope_sql(scope: ScopeFilter) -> tuple[str, list[str]]:
    clauses: list[str] = []
    parameters: list[str] = []
    if scope.source_id:
        clauses.append("d.source_id = ?")
        parameters.append(scope.source_id)
    if scope.root_scope:
        clauses.append("d.root_scope = ?")
        parameters.append(scope.root_scope)
    if scope.status:
        clauses.append("d.status = ?")
        parameters.append(scope.status)
    if scope.path_prefix:
        clauses.append("d.path LIKE ? ESCAPE '\\'")
        parameters.append(f"{_like_literal(scope.path_prefix.rstrip('/'))}%")
    if scope.repository:
        clauses.append(
            """
            (
                EXISTS (
                    SELECT 1 FROM json_each(d.repos_json)
                    WHERE lower(value) = lower(?)
                )
                OR lower(d.scope_id) = lower(?)
                OR lower(d.path) LIKE lower(?) ESCAPE '\\'
            )
            """
        )
        parameters.extend(
            (
                scope.repository,
                scope.repository,
                f"%/{_like_literal(scope.repository)}/%",
            )
        )
    if scope.project:
        clauses.append(
            """
            (
                EXISTS (
                    SELECT 1 FROM json_each(d.projects_json)
                    WHERE lower(value) = lower(?)
                )
                OR lower(d.scope_id) = lower(?)
                OR lower(d.path) LIKE lower(?) ESCAPE '\\'
            )
            """
        )
        parameters.extend(
            (
                scope.project,
                scope.project,
                f"%/{_like_literal(scope.project)}/%",
            )
        )
    if scope.ticket:
        clauses.append(
            """
            (
                EXISTS (
                    SELECT 1 FROM json_each(d.identifiers_json)
                    WHERE lower(value) = lower(?)
                )
                OR lower(d.scope_id) = lower(?)
                OR lower(d.path) LIKE lower(?) ESCAPE '\\'
            )
            """
        )
        parameters.extend(
            (
                scope.ticket,
                scope.ticket,
                f"%/{_like_literal(scope.ticket)}/%",
            )
        )
    return (f"WHERE {' AND '.join(clauses)}" if clauses else "", parameters)


def _like_literal(value: str) -> str:
    """Escape user scope text before it enters a SQLite LIKE pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
